"""Real API flows, independent revisions and material requirements with local fixtures."""
from decimal import Decimal
from pathlib import Path
import json
import pytest

from tidoc.api import Api
from tidoc.engine import ParsedInvoice,RecognitionContext,PolicyContext,check_invoice
from tidoc.engine.parser import _parse_invoice_text,parse_xml


def setup_api(tmp_path,package='org.tidoc.generic'):
    api=Api(tmp_path)
    scheme=next(s for s in api.adapters.list_schemes() if s['package_id']==package)
    assert api.complete_adapter_setup(scheme['id'])['ok']
    return api


def test_first_choice_gates_import_and_preserves_empty_titles(tmp_path):
    api=Api(tmp_path)
    claimant=api.create_profile('测试人员','')['data']
    assert api.adapter_setup_state()['data']['needs_selection']
    assert api.create_entry(claimant['id'])['ok'] is False
    assert api.title_profiles()['data']['profiles']==[]
    api.complete_adapter_setup(api.adapters.default_binding()[0])
    entry=api.create_entry(claimant['id'])['data']
    assert entry['scheme_revision_id']
    assert entry['title']==''
    assert entry['completeness']['status']=='draft'
    assert Api(tmp_path).adapter_setup_state()['data']['ready']


def test_api_instances_do_not_share_title_settings_or_historical_policy(tmp_path):
    first=setup_api(tmp_path/'first')
    second=setup_api(tmp_path/'second')
    first.set_title_profiles([{'name':'甲单位','tax_id':'AAA'}])
    second.set_title_profiles([{'name':'乙单位','tax_id':'BBB'}])
    claimant=first.create_profile('材料归属人','')['data']
    entry_id=first.entries.create(claimant['id'],parsed=ParsedInvoice(buyer_name='甲单位',buyer_tax_id='AAA',total=Decimal('0'),source='xml'))
    revision=first.entries.get(entry_id)['scheme_revision_id']
    first.set_title_profiles([{'name':'丙单位','tax_id':'CCC'}])
    assert first.entries.get(entry_id)['scheme_revision_id']==revision
    parsed=ParsedInvoice(buyer_name='甲单位',buyer_tax_id='AAA',total=Decimal('0'))
    assert '不在' not in check_invoice(parsed,context=first.entries.policy_context(entry_id)).message
    assert '不在' in check_invoice(parsed,context=second._policy_context()).message
    first.db.close();second.db.close()


def test_explicit_recognition_context_changes_ambiguous_pdf_candidates():
    text='名称:\n甲研究所\n名称:\n乙公司\n¥10.00'
    first=RecognitionContext(({'id':'a','name':'甲研究所','tax_id':''},))
    second=RecognitionContext(({'id':'b','name':'乙公司','tax_id':''},))
    assert _parse_invoice_text(text,context=first).buyer_name=='甲研究所'
    assert _parse_invoice_text(text,context=second).buyer_name=='乙公司'
    assert first.fingerprint!=second.fingerprint


def test_xml_missing_zero_negative_and_precise_amounts(tmp_path):
    api=setup_api(tmp_path/'db')
    claimant=api.create_profile('测试人员','')['data']
    for index,value in enumerate(('', '0.00', '-12.30','12.3456')):
        path=tmp_path/f'{index}.xml'
        path.write_text(f'<root><EIid>{index}</EIid><BuyerName>虚构单位</BuyerName><TotalTax-includedAmount>{value}</TotalTax-includedAmount></root>','utf-8')
        parsed=parse_xml(path)
        eid=api.entries.create(claimant['id'],parsed=parsed)
        entry=api.entries.get(eid)
        assert entry['total']==value
        assert entry['fields']['paid_amount']['current']==value
    api.db.close()


def test_default_switch_does_not_rescan_existing_entries(tmp_path,monkeypatch):
    api=setup_api(tmp_path)
    person=api.create_profile('测试人员','')['data']
    existing=api.entries.create(person['id'])
    old=api.entries.get(existing)
    def forbidden(*args,**kwargs):raise AssertionError('default change scanned existing entries')
    monkeypatch.setattr(api.entries,'recompute_status',forbidden)
    alternative=next(s for s in api.adapters.list_schemes() if not s['is_default'])
    assert api.set_default_scheme(alternative['id'])['ok']
    assert api.entries.get(existing)['scheme_revision_id']==old['scheme_revision_id']


def test_payment_recognition_preview_and_execution_use_each_pinned_revision(tmp_path,monkeypatch):
    from tidoc.services import folder_import
    api=setup_api(tmp_path/'data')
    person=api.create_profile('付款识别人员','')['data']
    # 付款识别默认是手动填写；这里明确开启本地识别，才能和之后改回手动的修订对比。
    scheme=api.adapters.get_scheme()
    api.adapters.update_scheme_settings(scheme['id'],scheme['current_revision_id'],
                                       {'assist.payment_ocr':'local'})
    enabled=api.entries.create(person['id'],parsed=ParsedInvoice(total=Decimal('10.00')))
    scheme=api.adapters.get_scheme()
    api.adapters.update_scheme_settings(scheme['id'],scheme['current_revision_id'],
                                       {'assist.payment_ocr':'manual'})
    disabled=api.entries.create(person['id'],parsed=ParsedInvoice(total=Decimal('10.00')))
    for entry_id in (enabled,disabled):
        source=tmp_path/(entry_id+'.png');source.write_bytes(b'fixture payment')
        api.attachments.add(entry_id,source,'payment_screenshot')
    recognized=[]
    def recognize(path):
        recognized.append(str(path))
        return '10.00'
    monkeypatch.setattr(folder_import,'extract_payment_image_amount',recognize)
    assert api.recognition_preview([])['data']['payment']['enabled'] is False
    assert api.recognition_preview(['missing',disabled,disabled])['data']['payment']['enabled'] is False
    assert api.recognition_preview([enabled,disabled])['data']['payment']['enabled'] is True
    result=api.rerecognize_materials([enabled,disabled],['payment'])
    assert result['ok'],result
    assert result['data']['payment']['processed']==1
    assert len(recognized)==1
    assert enabled in recognized[0]
    assert api.attachments.list(disabled)[0]['recognition_version']==''
    api.db.close()


def test_real_lab_conditional_role_fields_and_reclassification(tmp_path):
    api=setup_api(tmp_path/'data')
    source=Path(__file__).resolve().parents[1]/'examples/adapters/lab'
    preview=api.inspect_adapter(str(source))
    assert preview['ok'],preview
    scheme=api.install_adapter((preview.get('data') or preview)['preview_id'])['data']
    api.set_default_scheme(scheme['id'])
    claimant=api.create_profile('虚构人员','')['data']
    eid=api.entries.create(claimant['id'],parsed=ParsedInvoice(total=Decimal('1200'),source='xml',invoice_no='LAB-1'))
    invoice=tmp_path/'invoice.xml';invoice.write_text('<invoice/>','utf-8')
    api.attachments.add(eid,invoice,'invoice_xml')
    entry=api.entries.get(eid)
    assert any(d['code']=='RULE_CONDITION_PENDING' for d in entry['diagnostics'])
    form=api.get_form_description('entry',eid)['data']
    assert api.save_extension_values('entry',eid,{'purpose':'equipment'},expected_version=form['version'])['ok']
    entry=api.entries.get(eid)
    assert any('审批' in d['message'] for d in entry['diagnostics'])
    approval=next(r for r in entry['material_roles'] if 'approval' in r['id'])
    from pypdf import PdfWriter
    document=tmp_path/'approval.pdf';writer=PdfWriter();writer.add_blank_page(300,300);writer.write(document)
    result=api.add_attachment(eid,str(document),'other',options={'role_id':approval['id']})
    assert result['ok'],result
    att=result['data']
    assert api.entries.get(eid)['completeness']['ready']
    assert api.reclassify_attachment(att['id'],'other')['ok']
    assert not api.entries.get(eid)['completeness']['ready']
    assert api.entries.get(eid)['extension_history'][-1]['kind']=='material'
    assert api.delete_attachment(att['id'])['ok']
    api.db.close()


def test_live_form_preview_uses_real_entry_context_and_never_saves(tmp_path):
    import shutil
    source=Path(__file__).resolve().parents[1]/'examples/adapters/lab'
    package=tmp_path/'lab-preview';shutil.copytree(source,package)
    fields_path=package/'fields.json';data=json.loads(fields_path.read_text('utf-8'))
    data['fields'].append({'id':'trigger','scope':'entry','label':'显示开关','type':'text',
        'required_at':[],'presentation':'visible','sensitive':False,'transfer':'include'})
    purpose=next(field for field in data['fields'] if field['id']=='purpose')
    purpose['visible_when']={'all':[
        {'field':'entry.fields.trigger','op':'eq','value':'yes'},
        {'field':'invoice.paid_amount','op':'gte','value':'4.00'}]}
    fields_path.write_text(json.dumps(data,ensure_ascii=False),'utf-8')
    api=setup_api(tmp_path/'data')
    result=api.inspect_adapter(str(package));assert result['ok'],result
    scheme=api.install_adapter((result.get('data') or result)['preview_id'])['data']
    api.set_default_scheme(scheme['id'])
    claimant=api.create_profile('预览人员','')['data']
    entry_id=api.entries.create(claimant['id'],parsed=ParsedInvoice(total=Decimal('10.00'),source='xml'))
    api.update_field(entry_id,'paid_amount','5.00')
    form=api.adapters.get_form_description('entry',entry_id)
    api.adapters.save_extension_values('entry',entry_id,{'trigger':'yes'},expected_version=form['version'])
    before=api.adapters.get_form_description('entry',entry_id)
    assert next(field for field in before['fields'] if field['id']=='purpose')['visible'] is True
    preview=api.adapters.preview_form_description('entry',entry_id,{'trigger':'no','purpose':'equipment'})
    purpose=next(field for field in preview['fields'] if field['id']=='purpose')
    assert purpose['visible'] is False
    assert purpose['value']=='equipment'
    assert api.adapters.get_form_description('entry',entry_id)['values']==before['values']
    export_preview=api.adapters.preview_form_description('export','export-preview',{},scheme['id'])
    assert export_preview['scope']=='export'
    with pytest.raises(ValueError):
        api.adapters.preview_form_description('entry',entry_id,{'purpose':'not-an-option'})
    assert api.adapters.get_form_description('entry',entry_id)['values']==before['values']
    api.db.close()


def test_historical_batch_form_can_save_against_explicit_revision(tmp_path):
    api=setup_api(tmp_path/'data')
    source=Path(__file__).resolve().parents[1]/'examples/adapters/lab'
    result=api.inspect_adapter(str(source));assert result['ok'],result
    scheme=api.install_adapter((result.get('data') or result)['preview_id'])['data']
    api.set_default_scheme(scheme['id'])
    old_revision=scheme['current_revision_id']
    claimant=api.create_profile('历史修订人员','')['data']
    entry_id=api.entries.create(claimant['id'],parsed=ParsedInvoice(total=Decimal('5.00'),source='xml'))
    batch=api.batches.create('历史批次')
    current=api.adapters.get_scheme(scheme['id'])
    updated_scheme=api.adapters.update_scheme_settings(scheme['id'],current['current_revision_id'],{'profile.reviewer_required':True})
    noncurrent_entry_form=api.adapters.get_form_description('entry',entry_id,scheme['id'],updated_scheme['current_revision_id'])
    with pytest.raises(ValueError):
        api.adapters.save_extension_values('entry',entry_id,{'purpose':'equipment'},scheme['id'],
            noncurrent_entry_form['version'],revision_id=updated_scheme['current_revision_id'])
    historical=api.adapters.get_form_description('batch',batch['id'],scheme['id'],old_revision)
    saved=api.adapters.save_extension_values('batch',batch['id'],{'project_code':'OLD-REV'},
        scheme['id'],historical['version'],revision_id=old_revision)
    assert saved['values']['project_code']=='OLD-REV'
    assert saved['revision_id']==old_revision
    assert api.batches.get(batch['id'])['default_revision_id']==old_revision
    with pytest.raises(ValueError):
        api.adapters.save_extension_values('batch',batch['id'],{'project_code':'BAD'},
            scheme['id'],historical['version']+'stale',revision_id=old_revision)
    api.db.close()


def test_batch_output_settings_validate_persist_and_check_revision_constraints(tmp_path):
    api=setup_api(tmp_path/'data')
    batch=api.batches.create('输出设置')
    current=api.batches.get(batch['id'])
    updated=api.adapters.update_batch_output_settings(batch['id'],{'print.numbering':False},current['updated_at'])
    assert updated['output_settings']=={'print.numbering':False}
    assert api.batches.get(batch['id'])['output_settings']=={'print.numbering':False}
    payee=api.adapters.save_payee(values={'name':'历史收款人','personnel_id':'00021'})
    api.adapters.payees.set_batch(batch['id'],batch['default_scheme_id'],payee['id'])
    assert api.batches.get(batch['id'])['payee_mappings'][batch['default_scheme_id']]['personnel_id']=='00021'
    with pytest.raises(ValueError):
        api.adapters.update_batch_output_settings(batch['id'],{'transfer.include_notes':False})
    with pytest.raises(ValueError):
        api.adapters.update_batch_output_settings(batch['id'],{'print.numbering':True},current['updated_at'])
    import shutil
    fixed_package=tmp_path/'fixed-setting';shutil.copytree(
        Path(__file__).resolve().parents[1]/'examples/adapters/minimal',fixed_package)
    manifest_path=fixed_package/'manifest.json';manifest=json.loads(manifest_path.read_text('utf-8'))
    manifest['package_id']='org.tidoc.test.fixed-batch-output';manifest_path.write_text(json.dumps(manifest),'utf-8')
    scheme_path=fixed_package/'scheme.json';scheme=json.loads(scheme_path.read_text('utf-8'))
    scheme['settings']={'print.numbering':{'fixed':True,'editable':False,'presentation':'visible'}}
    scheme_path.write_text(json.dumps(scheme),'utf-8')
    inspected=api.inspect_adapter(str(fixed_package));assert inspected['ok'],inspected
    fixed=api.install_adapter((inspected.get('data') or inspected)['preview_id'])['data']
    api.batches.set_default_binding(batch['id'],fixed['id'],fixed['current_revision_id'])
    with pytest.raises(ValueError):
        api.adapters.update_batch_output_settings(batch['id'],{'print.numbering':False})
    api.db.close()


def test_payment_recognition_defaults_to_manual(tmp_path):
    api=setup_api(tmp_path/'data')
    for scheme in api.adapters.list_schemes():
        assert scheme['definition']['effective_settings']['assist.payment_ocr']=='manual'
    assert api.app_preference('tidoc.paymentScreenshotOcr')['data']=='0'
    api.db.close()


def test_update_scheme_saves_titles_requirements_and_settings_as_one_revision(tmp_path):
    api=setup_api(tmp_path/'data')
    scheme=api.scheme_details()['data']
    before=len(scheme['revision_history'])
    result=api.update_scheme(scheme['id'],scheme['current_revision_id'],{
        'titles':[{'name':'甲单位','tax_id':'111'},{'name':'乙单位','tax_id':'222'}],
        'material_requirements':{'payment_screenshot':True,'paid_amount':True},
        'settings':{'print.numbering':False},
        'clear':[],
    })
    assert result['ok'],result
    saved=api.scheme_details(scheme['id'])['data']
    assert len(saved['revision_history'])==before+1
    assert [t['name'] for t in saved['definition']['scheme']['titles']]==['甲单位','乙单位']
    assert saved['requirements']['payment_screenshot'] and saved['requirements']['paid_amount']
    assert not saved['requirements']['inspection_pdf']
    assert saved['definition']['effective_settings']['print.numbering'] is False
    # 恢复默认所需的包基线不受本地修改影响。
    assert saved['titles_baseline']==scheme['definition']['scheme']['titles']
    assert saved['requirements_baseline']==scheme['requirements']
    api.db.close()


def test_update_scheme_clears_default_title_when_that_title_is_removed(tmp_path):
    api=setup_api(tmp_path/'data')
    scheme=api.scheme_details()['data']
    first=api.update_scheme(scheme['id'],scheme['current_revision_id'],{'titles':[{'name':'甲单位','tax_id':'111'},{'name':'乙单位','tax_id':'222'}]})['data']
    jia=next(t['id'] for t in first['definition']['scheme']['titles'] if t['name']=='甲单位')
    second=api.update_scheme(first['id'],first['current_revision_id'],{'settings':{'entry.default_title_id':jia}})['data']
    assert second['definition']['effective_settings']['entry.default_title_id']==jia
    # 同一次保存里移除甲单位：默认抬头一并清空，而不是留下无效引用。
    third=api.update_scheme(second['id'],second['current_revision_id'],{'titles':[{'name':'乙单位','tax_id':'222'}]})
    assert third['ok'],third
    assert third['data']['definition']['effective_settings']['entry.default_title_id'] is None
    assert api.update_scheme(second['id'],second['current_revision_id'],{'bogus':1})['ok'] is False
    # 旧修订号保存会被拒绝，避免覆盖别处的修改。
    stale=api.update_scheme(second['id'],second['current_revision_id'],{'settings':{'print.numbering':False}})
    assert stale['ok'] is False
    api.db.close()

"""Real frontend source integration without browser automation or synthetic pages."""
import re
import shutil
import subprocess
from pathlib import Path

from tidoc.api import Api

ROOT=Path(__file__).resolve().parents[1]
WEB=ROOT/'tidoc/web'


def test_registered_bridge_methods_exist_and_scripts_are_loaded():
    bridge=(WEB/'api.js').read_text('utf-8')
    calls=set(re.findall(r"call\('([a-z_]+)'",bridge))
    missing=sorted(method for method in calls if not callable(getattr(Api,method,None)))
    assert not missing
    html=(WEB/'index.html').read_text('utf-8')
    assert 'schema-form.js' in html and 'adapter-ui.js' in html
    assert html.index('adapter-ui.js')<html.index('app.js')


def test_live_workflow_uses_dynamic_backend_schemes_and_fields():
    source=(WEB/'app.js').read_text('utf-8')
    adapter=(WEB/'adapter-ui.js').read_text('utf-8')
    assert '北京理工大学' not in source and '工训楼' not in source
    assert 'AdapterUI.setup()' in source
    assert 'AdapterUI.decorateEntry' in source
    assert 'AdapterUI.cardActions(e)' in source
    assert 'AdapterUI.print(ids)' in source
    assert 'previewExport' in adapter and 'runExport' in adapter
    assert 'role_definition_revision_id' in adapter
    assert 'previewRebind' in adapter and 'applyRebind' in adapter
    assert 'scheme.revision_id' in adapter
    assert 'expected_version' in (ROOT/'tidoc/api.py').read_text('utf-8')


def test_forms_have_labels_and_focusable_linked_errors():
    source=(WEB/'schema-form.js').read_text('utf-8')
    assert 'label.htmlFor = id' in source
    assert "setAttribute('aria-describedby'" in source
    assert 'summary.tabIndex = -1' in source
    assert 'control.input.focus()' in source
    assert 'field.type === \'boolean\'' in source
    assert "type === 'integer'" in source


def test_hidden_rows_are_hidden_and_help_text_is_not_a_callout_box():
    css=(WEB/'styles.css').read_text('utf-8')
    # .form-row 自带 display:flex，会盖过 hidden 属性；条件字段和账户类型联动都靠 hidden。
    assert '.form-row[hidden]' in css
    source=(WEB/'schema-form.js').read_text('utf-8')
    assert "el('small', 'field-help')" in source
    assert "el('small', 'hint')" not in source


def test_scheme_details_reports_package_baseline_apart_from_local_overrides(api):
    scheme=api.scheme_details()['data']
    baseline=scheme['settings_baseline']
    # 默认输出的包默认值来自 scheme.default_outputs，而不是注册表里的空列表。
    assert baseline['print.default_outputs']==scheme['definition']['scheme']['default_outputs']
    api.update_scheme_settings(scheme['id'],scheme['current_revision_id'],{'print.numbering':False})
    changed=api.scheme_details(scheme['id'])['data']
    assert changed['definition']['effective_settings']['print.numbering'] is False
    assert changed['settings_baseline']['print.numbering'] is True


def test_scheme_page_saves_only_changed_settings_against_package_baseline():
    adapter=(WEB/'adapter-ui.js').read_text('utf-8')
    assert 'settings_baseline' in adapter
    assert 'filter((row) => row.isDirty())' in adapter
    assert 'confirmLeave' in adapter


def test_dialog_open_close_rules_live_in_the_shared_modal():
    source=(WEB/'app.js').read_text('utf-8')
    modal=source.split('function modal(',1)[1].split('function confirmDialog',1)[0]
    # 语义、焦点归还、仅最上层可交互、点遮罩必须"按下和松开都在遮罩上"、关闭前确认、子页面关闭后的回调。
    for needle in ("setAttribute('aria-modal', 'true')","opener.focus","syncModalLayers()","pressedOnMask","guard","_onResume","key &&"):
        assert needle in modal,needle
    # 弹窗打开时列表快捷键（/、n、t、Cmd+A）不作用到后面的页面。
    assert "if ($('#modalRoot').lastChild) return;" in source
    settings=source.split('async function buildSettings',1)[1].split('// 更新对话框里的可选组件',1)[0]
    assert 'confirm(' not in settings
    assert "key: 'settings'" in settings and '_onResume' in settings
    # 设置页的子页面叠在上面，不再先关掉设置页。
    assert 'm.close(); AdapterUI' not in settings
    adapter=(WEB/'adapter-ui.js').read_text('utf-8')
    assert 'guard: confirmLeave' in adapter
    assert 'mask.onclick' not in adapter and '_closeModal =' not in adapter


def test_javascript_parses_in_node_if_available():
    node=shutil.which('node')
    if node:
        for name in ('app.js','api.js','adapter-ui.js','schema-form.js'):
            subprocess.run([node,'--check',str(WEB/name)],check=True,capture_output=True)


def test_card_and_detail_keep_the_original_material_workflow():
    source=(WEB/'app.js').read_text('utf-8')
    adapter=(WEB/'adapter-ui.js').read_text('utf-8')
    card=source.split('function entryCard(',1)[1].split('right.querySelectorAll',1)[0]
    # 卡片仍是 发票/实付/付款/查验(/实物) 一行，带完成状态、右键打开和在线查验；方案只补自定义材料按钮。
    for needle in ("actionBtn('invoice'","actionBtn('paid'","actionBtn('pay'","actionBtn('inspect'","actionBtn('physical'",'AdapterUI.cardActions(e)'):
        assert needle in card,needle
    assert 'onlineVerificationFlow(e.id)' in source
    cards=adapter.split('function cardActions(',1)[1].split('function jobs(',1)[0]
    assert 'action-state' in cards and 'oncontextmenu' in cards
    assert 'data-card-materials' not in adapter
    # 详情页的材料分组由 app.js 渲染；方案信息只是附加的折叠区，不能隐藏或搬走原来的材料区与拖放区。
    decorate=adapter.split('function decorateEntry(',1)[1].split('function chooser(',1)[0]
    assert 'hidden=true' not in decorate and 'materialDrop' not in decorate
    # 方案信息区放在详情页最末，且不自动展开。
    assert 'body.append(section)' in decorate and 'section.open' not in decorate and 'before(' not in decorate
    assert 'data-add-att-role' in source and 'data-online-verification' in source


def test_list_entries_expose_role_counts_for_custom_material_cards(api):
    profile=api.profiles.create('Role','')
    entry_id=api.entries.create(profile['id'])
    api.db.conn.execute("INSERT INTO attachments(id,entry_id,type,original_name,stored_path,sha256,role_id,added_at) "
                        "VALUES('a1',?,'invoice_pdf','a.pdf','a.pdf','h1','invoice','now')",(entry_id,))
    api.db.conn.commit()
    listed={e['id']:e for e in api.entries.list()}[entry_id]
    assert listed['role_counts']=={'invoice':1}
    assert '_role_counts' not in listed


def test_tooltips_stay_above_dropdown_menus_and_option_tips_only_show_when_truncated():
    css=(WEB/'styles.css').read_text('utf-8')
    z=lambda selector:int(re.search(re.escape(selector)+r'\s*\{[^}]*?z-index:\s*(\d+)',css).group(1))
    assert z('.fast-tooltip')>z('.select-menu')>z('.entry-context-menu')
    select=(WEB/'select.js').read_text('utf-8')
    assert 'MENU_Z = 400' in select and z('.select-menu')==400
    menu=select.split('function renderMenu(',1)[1].split('function openMenu(',1)[0]
    assert 'title=' not in menu and 'data-tooltip-overflow' in menu
    app=(WEB/'app.js').read_text('utf-8')
    tooltips=app.split('function setupFastTooltips()',1)[1].split('let autoUpdateCheckTimer',1)[0]
    # 展开下拉时只显示列表内的提示；提示放在列表侧边；点击或按键后立即收起。
    for needle in ("'.select-menu:not([hidden])'","target.closest('.select-menu')","addEventListener('pointerdown', hide, true)","addEventListener('keydown', hide, true)"):
        assert needle in tooltips,needle


def test_titles_without_a_color_get_distinct_card_tints():
    css=(WEB/'styles.css').read_text('utf-8')
    adapter=(WEB/'adapter-ui.js').read_text('utf-8')
    app=(WEB/'app.js').read_text('utf-8')
    assert 'titleColors(State.titleProfiles)' in adapter
    # 卡片底色与分组标题共用抬头语义色。
    assert 'var(--tc-card, var(--panel))' in css and '.group-head:is(.title-blue' in css
    for color in ('blue','green','amber','purple','teal','red'):
        assert f'.title-{color} {{ --tc:' in css
    assert "' title-' + tcls" not in app and "(tcls ? ' ' + tcls : '')" in app


def test_card_has_no_checkbox_or_stripe_and_names_the_title():
    source=(WEB/'app.js').read_text('utf-8')
    css=(WEB/'styles.css').read_text('utf-8')
    card=source.split('function entryCard(',1)[1].split('function entryCards(',1)[0]
    # 选中靠点击卡片；抬头用底色区分，完整名称仍可悬浮读取。
    assert "entry-check" not in source and 'entry-check' not in css
    assert '切换选中' not in source and 'entryTitleTooltip(e)' in card
    assert 'entry-stripe' not in source and 'entry-stripe' not in css
    assert 'card.append(main, right)' in card and 'card.dataset.tooltip = entryTitleTooltip(e)' in card
    assert 'function entryTitleTooltip(' in source and '抬头：' in source
    # 卡片上不重复显示已知信息：单一报账人／已按报账人筛选、正在查看的批次。
    assert "State.profiles.length > 1 && !$('#filterProfile')?.value" in card
    assert 'batch.id !== focusedBatchId' in card


def test_entries_expose_their_own_verification_setting(api):
    profile=api.profiles.create('Verify','')
    entry_id=api.entries.create(profile['id'])
    assert api.entries.get(entry_id)['verification_visible'] is True
    assert {e['id']:e for e in api.entries.list()}[entry_id]['verification_visible'] is True


def test_titles_and_material_requirements_live_in_the_scheme_page():
    app=(WEB/'app.js').read_text('utf-8')
    adapter=(WEB/'adapter-ui.js').read_text('utf-8')
    api=(WEB/'api.js').read_text('utf-8')
    settings=app.split('async function buildSettings',1)[1].split('// 更新对话框里的可选组件',1)[0]
    # 设置页不再有「扩展」：抬头与材料要求属于方案，统一在方案页编辑并随「保存更改」一起提交。
    assert '扩展' not in settings and 'data-material-requirement' not in settings and 'setTitleProfiles' not in app
    assert "TITLES_KEY" in adapter and "REQUIREMENT_PREFIX" in adapter
    assert 'Api.updateScheme(scheme.id, scheme.revision_id' in adapter
    assert "updateScheme: (id,revision,changes) => call('update_scheme',id,revision,changes||{})" in api
    assert 'setMaterialRequirements' not in api and 'setTitleProfiles' not in api
    # 与普通设置共用未保存提示、恢复默认和一次性保存。
    assert 'titles_baseline' in adapter and 'requirements_baseline' in adapter
    assert "if (changes.titles) { await refreshTitleOptions(); renderEntries(); }" in adapter


def test_payee_and_pdf_options_follow_what_the_scheme_actually_does():
    """收款、材料 PDF、默认抬头相关的入口和设置，只在方案真的有对应功能时出现（用真实的前端源码和内置方案执行）。"""
    import json
    import pytest

    node=shutil.which('node')
    if not node:
        pytest.skip('需要 Node 执行前端源码')

    def definition(package, effective=None):
        base=ROOT/'tidoc/builtin_adapters'/package
        return {'outputs':json.loads((base/'outputs.json').read_text('utf-8'))['outputs'],
                'scheme':json.loads((base/'scheme.json').read_text('utf-8')),
                'effective_settings':effective or {}}

    cases={'bitfsae':definition('org.bitfsae.reimbursement'),
           'bitfsae_by_claimant':definition('org.bitfsae.reimbursement',{'print.payee_mode':'by_claimant'}),
           'generic':definition('org.tidoc.generic')}
    script=r"""
const vm=require('vm'),fs=require('fs');
const sandbox={console};vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8')+';this.AdapterUI=AdapterUI;',sandbox);
const A=sandbox.AdapterUI,cases=JSON.parse(process.argv[2]),out={};
for(const [name,def] of Object.entries(cases))out[name]={uses:A.usesPayee(def),modes:[...A.payeeModes(def)].sort(),
  payeeKey:A.settingApplies('print.payee_mode',def),pdfKey:A.settingApplies('print.numbering',def),titleKey:A.settingApplies('entry.default_title_id',def)};
console.log(JSON.stringify(out));
"""
    result=subprocess.run([node,'-e',script,str(WEB/'adapter-ui.js'),json.dumps(cases)],capture_output=True,text=True,encoding='utf-8',check=True)
    found=json.loads(result.stdout)
    # BITFSAE 默认是统一收款：要收款对象，但不出现「分别收款」。
    assert found['bitfsae']['uses'] and found['bitfsae']['modes']==['single'] and found['bitfsae']['payeeKey']
    assert found['bitfsae']['pdfKey'] and found['bitfsae']['titleKey']
    # 方案设置选了分别收款，才会出现按报账人指定。
    assert found['bitfsae_by_claimant']['modes']==['by_claimant']
    # 通用方案没有任何输出要收款对象，也没有配置抬头：这些入口和设置都不出现。
    assert not found['generic']['uses'] and found['generic']['modes']==[]
    assert not found['generic']['payeeKey'] and not found['generic']['titleKey'] and found['generic']['pdfKey']


def _run_adapter_ui(script, payload):
    """在 Node 里加载真实的 adapter-ui.js（不需要 DOM），把 payload 交给 script，返回它打印的 JSON。"""
    import json
    import pytest

    node=shutil.which('node')
    if not node:
        pytest.skip('需要 Node 执行前端源码')
    harness=r"""
const vm=require('vm'),fs=require('fs');
const sandbox={console,esc:value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))};vm.createContext(sandbox);
// 只在测试环境暴露内部渲染函数，生产界面不增加入口。
const source=fs.readFileSync(process.argv[1],'utf8').replace('return {setup,','return {diagnosticsMarkup,exportPreviewMarkup,setup,');
vm.runInContext(source+';this.AdapterUI=AdapterUI;',sandbox);
const A=sandbox.AdapterUI,input=JSON.parse(process.argv[2]);
"""+script
    result=subprocess.run([node,'-e',harness,str(WEB/'adapter-ui.js'),json.dumps(payload)],capture_output=True,text=True,encoding='utf-8',check=True)
    return json.loads(result.stdout)


def test_export_dialog_uses_current_scheme_defaults_for_historical_entries():
    out=_run_adapter_ui(r"""
const outputs=[{id:'note',default_selected:true},{id:'accept',default_selected:false},{id:'pdf',default_selected:false}];
const def=(configured)=>({effective_settings:configured?{'print.default_outputs':configured}:{},scheme:{default_outputs:['note']}});
const pick=(d,remembered)=>outputs.map(o=>A.defaultSelected(d,o,remembered)?o.id:null).filter(Boolean);
const base=JSON.stringify(['note','accept']);
console.log(JSON.stringify({
  noMemory:pick(def(['note','accept']),null),
  memory:pick(def(['note','accept']),{ids:['pdf'],base}),
  // 在设置里把默认输出改成别的：以新的为准，旧记忆作废。
  settingsChanged:pick(def(['note']),{ids:['pdf'],base}),
  packageDefault:pick(def(null),null),
  declaredFlag:pick({effective_settings:{},scheme:{}},null),
  explicitNone:pick(def([]),null),
  garbage:pick(def(['note']),{ids:'pdf',base:JSON.stringify(['note'])}),
  historical:outputs.filter(o=>A.defaultSelected(def(['note']),o,{ids:['note'],base:JSON.stringify(['note'])},def(['note','accept','pdf']))).map(o=>o.id),
  currentEmpty:outputs.filter(o=>A.defaultSelected(def(['note']),o,null,def([]))).map(o=>o.id),
  noConfigurationMemory:pick({effective_settings:{},scheme:{}},{ids:['pdf'],base:'null'}),
}));""",{})
    assert out['noMemory']==['note','accept']
    assert out['memory']==['note','accept']  # 本次临时选择不覆盖已配置的默认输出。
    assert out['settingsChanged']==['note']
    assert out['packageDefault']==['note']
    assert out['declaredFlag']==['note']        # 方案和设置都没配置时用输出自己的 default_selected
    assert out['explicitNone']==[]              # 明确保存「全不选」仍然是空选择
    assert out['garbage']==['note']
    assert out['historical']==['note','accept','pdf']
    assert out['currentEmpty']==[]
    assert out['noConfigurationMemory']==['pdf']


def test_export_dialog_shows_date_and_payee_rows_only_for_outputs_that_use_them():
    out=_run_adapter_ui(r"""
const single={effective_settings:{},scheme:{settings:{}}};
const byClaimant={effective_settings:{'print.payee_mode':'by_claimant'},scheme:{settings:{}}};
const sel=(definition,output,binding='s:r')=>({definition,output,binding});
const note={id:'note',type:'docx',payee_mode:'single'},cover={id:'cover',type:'docx',payee_mode:'none'},sheet={id:'sheet',type:'xlsx',payee_mode:'none'};
const pdf={id:'pdf',type:'pdf_bundle',payee_mode:'none'},generic={id:'generic_overview',type:'xlsx',payee_mode:'none'};
const state=(items)=>A.exportRowState(items);
console.log(JSON.stringify({
  nothing:state([]),
  word:state([sel(single,note)]),
  wordNoPayee:state([sel(single,cover)]),
  templatedExcel:state([sel(single,sheet)]),
  pdfOnly:state([sel(single,pdf)]),
  genericOnly:state([sel(null,generic,'')]),
  perClaimant:state([sel(byClaimant,note)]),
  mixed:state([sel(byClaimant,note),sel(single,note)]),
}));""",{})
    assert out['nothing']=={'date':False,'payee':False,'byClaimantOnly':False}
    assert out['word']=={'date':True,'payee':True,'byClaimantOnly':False}
    assert out['wordNoPayee']['date'] and not out['wordNoPayee']['payee']
    assert out['templatedExcel']['date'] and not out['templatedExcel']['payee']    # 适配包的 xlsx 模板也可以引用 export.date
    assert not out['pdfOnly']['date'] and not out['pdfOnly']['payee']
    assert not out['genericOnly']['date'] and not out['genericOnly']['payee']
    assert out['perClaimant']=={'date':True,'payee':True,'byClaimantOnly':True}
    assert out['mixed']['byClaimantOnly'] is False                                  # 只要有统一收款的输出，就要选本次收款对象


def test_one_generate_button_checks_first_warns_once_and_never_runs_a_blocked_plan():
    out=_run_adapter_ui(r"""
const blocked={ok:false,diagnostics:[{code:'A',severity:'blocked',message:'缺发票'},{code:'A',severity:'blocked',message:'缺发票'},{code:'B',severity:'warning',message:'金额不一致'}]};
const warn={ok:true,diagnostics:[{code:'B',severity:'warning',message:'金额不一致'}]};
const clean={ok:true,diagnostics:[]};
console.log(JSON.stringify({
  first:A.runStep(null,false),
  afterWarningShown:A.runStep(warn,true),
  warnedButPlanBlocked:A.runStep(blocked,true),
  warnedFlagReset:A.runStep(warn,false),
  clean:A.afterCheck(clean),warn:A.afterCheck(warn),blocked:A.afterCheck(blocked),failedCall:A.afterCheck(null),
  split:A.splitDiagnostics(blocked),
  requiredIsBlocking:A.splitDiagnostics({diagnostics:[{code:'R',severity:'required',message:'x'},{code:'I',severity:'info',message:'y'}]}),
}));""",{})
    assert out['first']=='check' and out['afterWarningShown']=='execute'
    assert out['warnedButPlanBlocked']=='check' and out['warnedFlagReset']=='check'
    assert (out['clean'],out['warn'],out['blocked'],out['failedCall'])==('execute','warn','stop','stop')
    assert [d['message'] for d in out['split']['blocking']]==['缺发票']            # 多个文件重复报的同一条问题只留一条
    assert [d['code'] for d in out['split']['warnings']]==['B']
    assert [d['code'] for d in out['requiredIsBlocking']['blocking']]==['R'] and [d['code'] for d in out['requiredIsBlocking']['warnings']]==['I']


def test_export_diagnostics_keep_all_invoice_sources_and_do_not_hide_blockers():
    out=_run_adapter_ui(r"""
const items=[
  {code:'PAID_DIFFERS',severity:'warning',message:'金额不同',target:'a',group_id:'docx'},
  {code:'PAID_DIFFERS',severity:'warning',message:'金额不同',target:'a',group_id:'pdf'},
  {code:'PAID_DIFFERS',severity:'warning',message:'金额不同',entry_id:'b',group_id:'pdf'},
  {code:'PAID_DIFFERS',severity:'required',message:'金额不同',entry_id:'b'},
];
const split=A.splitDiagnostics({diagnostics:items});
console.log(JSON.stringify({split,html:A.diagnosticsMarkup([...split.blocking,...split.warnings],[{id:'a',invoice_no:'001'},{id:'b',invoice_no:'002'}])}));
""",{})
    assert len(out['split']['blocking'])==1
    assert len(out['split']['warnings'])==1
    assert len(out['split']['warnings'][0]['sources'])==3
    assert out['html'].count('金额不同')==2  # 同名阻断项不会让先出现的提醒吞掉。
    assert '涉及 2 条发票' in out['html'] and '001、002' in out['html']
    assert 'class="hint' not in out['html']


def test_export_preview_files_are_separate_from_warnings_and_escape_private_text():
    out=_run_adapter_ui(r"""
const candidate={groups:[{group_id:'one',filename:'材料<&>.docx',payee:{name:'张<三>',account_tail:'1234'}},{group_id:'two',filename:'无账号.pdf',payee:{name:'单位'}}],files:[{group_id:'one',type:'docx'},{group_id:'two',type:'pdf_bundle'}],diagnostics:[{code:'W',severity:'warning',message:'核对<&>'},{code:'I',severity:'info',message:'仅供参考'}]};
console.log(JSON.stringify({files:A.exportPreviewMarkup(candidate),notes:A.diagnosticsMarkup(candidate.diagnostics),blocked:A.exportPreviewMarkup({...candidate,diagnostics:[{severity:'blocked'}]})}));
""",{})
    assert '预计生成 · 2 个文件' in out['files']
    assert '材料&lt;&amp;&gt;.docx' in out['files'] and '张&lt;三&gt;' in out['files']
    assert 'Word 文档' in out['files'] and '材料 PDF' in out['files']
    assert '账号尾号 1234' in out['files'] and '账号尾号 </small>' not in out['files']
    assert 'is-warning' not in out['files'] and 'class="hint' not in out['files']
    assert 'is-warning' in out['notes'] and 'is-info' in out['notes'] and '核对&lt;&amp;&gt;' in out['notes']
    assert out['blocked']==''


def test_merged_payee_diagnostics_name_every_missing_field():
    out=_run_adapter_ui(r"""
const diagnostics=['payee.bank_name','payee.account_number','payee.bank_name'].map(target=>({code:'MISSING_PAYEE_FIELD',severity:'required',message:'收款信息缺项',target}));
console.log(JSON.stringify(A.diagnosticsMarkup(diagnostics)));
""",{})
    assert out.count('收款信息缺项')==1
    assert '需补充：开户行、账号' in out
    assert 'payee.' not in out


def test_selected_cards_use_a_quiet_wash_not_a_heavy_ring():
    css=(WEB/'styles.css').read_text('utf-8')
    selected=css.split('.entry-card.selected {',1)[1].split('}',1)[0]
    assert 'var(--card-selected-wash)' in selected and 'border-color: var(--primary-line);' in selected
    assert 'box-shadow' not in selected  # the old 2px outer ring
    assert css.count('--card-selected-wash: rgba(') == 2  # light and dark themes
    # 选中另有一条左缘短竖线作形状线索，键盘焦点的光晕规则不能丢。
    bar=css.split('.entry-card.selected::before {',1)[1].split('}',1)[0]
    assert 'width: 3px' in bar and 'var(--primary)' in bar
    assert '.entry-card.selected:focus-visible {' in css


def test_card_title_dot_has_distinct_colours_in_both_themes():
    css=(WEB/'styles.css').read_text('utf-8')
    app=(WEB/'app.js').read_text('utf-8')
    colours=['blue','green','amber','purple','teal','red']
    light=[css.split(f'.title-{name} {{ --dot:',1)[1].split(';',1)[0] for name in colours]
    dark=[css.split(f':root[data-theme="dark"] .title-{name} {{ --dot:',1)[1].split(';',1)[0] for name in colours]
    assert len(set(light))==6 and len(set(dark))==6
    # 圆点和名称同属一项，名称过长换行时不会把圆点单独留在上一行。
    assert 'class="entry-title-wrap">${titleDot}<span class="entry-item-title"' in app
    # 按抬头分组或已筛选到某个抬头时整页同一个抬头，不画圆点。
    assert "State.groupBy !== 'title' && !$('#filterTitle')?.value" in app

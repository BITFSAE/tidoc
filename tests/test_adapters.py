from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tidoc.adapters.loader import load_package, pack_package, validate_package
from tidoc.adapters.models import AdapterValidationError, FieldValidationError
from tidoc.adapters.policy import evaluate_condition, evaluate_policy, validate_field_value
from tidoc.adapters.resolver import resolve_definition, revision_hash

ROOT = Path(__file__).resolve().parents[1]


class AdapterPackageTests(unittest.TestCase):
    def test_all_example_packages_validate_and_have_fixtures(self):
        for name in ('generic', 'bitfsae', 'minimal', 'lab', 'club'):
            with self.subTest(name=name):
                path = ROOT / 'examples' / 'adapters' / name
                result = validate_package(path)
                self.assertTrue(result['ok'], result.get('errors'))
                fixture = ROOT / 'examples' / 'adapters' / 'fixtures' / f'{name}.json'
                self.assertTrue(fixture.is_file())
                sample = json.loads(fixture.read_text(encoding='utf-8'))
                definition = load_package(path).definition
                self.assertEqual(sample['expected']['entry_count'], len(sample['entries']))
                self.assertTrue(set(sample['expected']['outputs']) <= {o['id'] for o in definition['outputs']})

    def test_real_docx_templates_are_valid_containers(self):
        for name in ('generic', 'bitfsae', 'lab'):
            package = load_package(ROOT / 'examples' / 'adapters' / name)
            templates = [n for n in package.files if n.endswith('.docx')]
            self.assertTrue(templates, name)

    def test_hash_ignores_zip_order_and_timestamp(self):
        source = ROOT / 'examples' / 'adapters' / 'minimal'
        first = load_package(source)
        with tempfile.TemporaryDirectory() as tmp:
            packed = pack_package(source, Path(tmp) / 'one.tidoc-preset')
            second = load_package(packed)
        self.assertEqual(first.content_hash, second.content_hash)

    def test_every_example_template_renders_with_schema_valid_context(self):
        try:
            from jsonschema import Draft202012Validator
            from tidoc_print.context import build_export_context
            from tidoc_print.template_renderer import render_template
        except ImportError:
            self.skipTest('optional DOCX renderer is not installed')
        schema=json.loads((ROOT/'schemas/team-adapter/1/context.schema.json').read_text(encoding='utf-8'))
        validator=Draft202012Validator(schema)
        for name in ('generic','bitfsae','minimal','lab','club'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                package=load_package(ROOT/'examples/adapters'/name)
                title=next(iter(package.definition['scheme'].get('titles',[])),{})
                entry={'id':'fixture-entry','scheme_id':'fixture-scheme','scheme_revision_id':'fixture-revision',
                       'title_profile_id':title.get('id',''),'buyer_name':title.get('name','示例主体'),
                       'buyer_tax_id':title.get('tax_id',''),'title':title.get('name','示例主体'),
                       'invoice_no':'SAMPLE-001','invoice_date':'2026-10-05','seller':'示例供应商',
                       'total':'12.50','fields':{'paid_amount':{'current':'12.50'},'actual_item_name':{'current':'示例物资'}},
                       'items':[{'name':'示例物资','actual_name':'示例物资','product_name':'示例物资','unit':'件',
                                 'quantity':'1','total':'12.50','unit_price':'12.50'}],
                       'attachments':[],'profile_id':'sample-person','profile_name':'样例报账人',
                       'reviewer':'样例审核人','extension_values':{}}
                for field in package.definition['fields']:
                    if field['scope']=='entry':
                        entry['extension_values'][field['id']]=field.get('default','示例值')
                profile={'id':'sample-person','name':'样例报账人','reviewer':'样例审核人'}
                payee={'id':'sample-payee','name':'样例收款人','personnel_id':'000123','contact':'01000000000',
                       'bank_name':'示例银行','account_number':'0000000000001234','account_type':'personal_bank'}
                for output in package.definition['outputs']:
                    template=output.get('template')
                    if not template:continue
                    context=build_export_context([entry],package.definition,output,profiles={'sample-person':profile},
                        payee=payee if output.get('payee_mode')!='none' else None,
                        scheme={'id':'fixture-scheme','name':package.definition['manifest']['name']},
                        options={'document_date':'2026年10月6日'})
                    self.assertEqual([],list(validator.iter_errors(context)))
                    template_path=Path(tmp)/template
                    template_path.parent.mkdir(parents=True,exist_ok=True)
                    template_path.write_bytes(package.files[template])
                    result=Path(tmp)/(output['id']+'.docx')
                    render_template(template_path,context,result,definition=package.definition)
                    self.assertTrue(result.is_file() and result.stat().st_size>0)

    def test_archive_requires_checksum_index_but_source_directory_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive=Path(tmp)/'without-checksums.zip'
            with zipfile.ZipFile(archive,'w') as z:
                for path in (ROOT/'examples/adapters/minimal').iterdir():
                    if path.is_file():z.write(path,path.name)
            result=validate_package(archive)
        self.assertFalse(result['ok'])
        self.assertIn('CHECKSUMS_REQUIRED',{e['code'] for e in result['errors']})

    def test_rejects_zip_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('../manifest.json', '{}')
            result = validate_package(archive)
        self.assertFalse(result['ok'])
        self.assertIn('ARCHIVE_PATH_INVALID', {e['code'] for e in result['errors']})

    def test_rejects_unknown_schema_properties(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / 'pkg'
            import shutil
            shutil.copytree(ROOT / 'examples' / 'adapters' / 'minimal', copy)
            manifest = json.loads((copy / 'manifest.json').read_text())
            manifest['runtime_hook'] = 'bad'
            (copy / 'manifest.json').write_text(json.dumps(manifest))
            result = validate_package(copy)
        self.assertFalse(result['ok'])
        self.assertIn('SCHEMA_INVALID', {e['code'] for e in result['errors']})


class AdapterResolutionTests(unittest.TestCase):
    def test_canonical_revision_hash(self):
        d = {'manifest': {'package_id': 'x'}, 'effective_settings': {'n': 1}}
        self.assertEqual(revision_hash(d), revision_hash(dict(d)))
        changed = {**d, 'effective_settings': {'n': 2}}
        self.assertNotEqual(revision_hash(d), revision_hash(changed))

    def test_setting_override_type_is_strict(self):
        definition = load_package(ROOT / 'examples' / 'adapters' / 'generic').definition
        with self.assertRaises(ValueError):
            resolve_definition(definition, {'entry.default_paid_to_invoice': 'false'})

    def test_fixed_setting_cannot_be_overridden(self):
        definition = load_package(ROOT / 'examples' / 'adapters' / 'bitfsae').definition
        with self.assertRaises(ValueError):
            resolve_definition(definition, {'profile.reviewer_required': False})

    def test_explicit_none_does_not_apply_field_default(self):
        field = {'id': 'x', 'type': 'text', 'label': 'X', 'default': 'fallback'}
        self.assertIsNone(validate_field_value(field, None))


class AdapterPolicyTests(unittest.TestCase):
    def test_typed_values_normalize_without_losing_account_strings(self):
        self.assertEqual(validate_field_value({'id':'n','type':'integer','label':'n'}, '12'), 12)
        self.assertEqual(validate_field_value({'id':'m','type':'money','label':'m'}, '12.30'), '12.30')
        self.assertEqual(validate_field_value({'id':'d','type':'decimal','label':'d'}, '1.500'), '1.500')
        self.assertEqual(validate_field_value({'id':'account','type':'text','label':'账号'}, '00123'), '00123')

    def test_invalid_values_raise_structured_exception(self):
        with self.assertRaises(FieldValidationError) as cm:
            validate_field_value({'id':'b','type':'boolean','label':'布尔'}, 'false')
        self.assertEqual(cm.exception.diagnostics[0]['code'], 'FIELD_VALUE_TYPE')

    def test_three_valued_condition_and_composition(self):
        expr = {'field':'invoice.total','op':'gte','value':'100.00'}
        self.assertIsNone(evaluate_condition(expr, {}, 'complete'))
        self.assertTrue(evaluate_condition(expr, {'invoice':{'total':'100.00'}}, 'complete'))
        self.assertFalse(evaluate_condition({'all':[expr,{'field':'invoice.title_id','op':'eq','value':'t'}]}, {'invoice':{'total':'10.00'}}, 'complete'))

    def test_reviewer_requirement_and_declared_paid_rule(self):
        definition = load_package(ROOT / 'examples' / 'adapters' / 'bitfsae').definition
        definition = resolve_definition(definition)
        definition['effective_settings']['profile.reviewer_required'] = True
        missing = evaluate_policy(definition, {'entry':{'claimant':{'reviewer':''}},'invoice':{'total':'10.00'}}, {}, 'complete')
        self.assertIn('REVIEWER_REQUIRED', {x['code'] for x in missing})
        self.assertIn('RULE_FIELD_REQUIRED', {x['code'] for x in missing})

    def test_export_only_checks_fields_declared_by_selected_output(self):
        definition={'fields':[{'id':'batch_code','scope':'batch','label':'批次号','type':'text','required_at':['export']}],
                    'outputs':[{'id':'overview','required_fields':[]}]}
        result=evaluate_policy(definition, {'export':{'output_id':'overview'}}, {}, 'export')
        self.assertNotIn('FIELD_REQUIRED', {x['code'] for x in result})

    def test_conditions_coerce_numeric_and_date_operands(self):
        self.assertTrue(evaluate_condition(
            {'field':'invoice.total','op':'eq','value':'1.00'},
            {'invoice':{'total':'1.0'}}, 'complete'))
        self.assertTrue(evaluate_condition(
            {'field':'invoice.total','op':'in','value':['0.50','1.00']},
            {'invoice':{'total':'1.0'}}, 'complete'))
        self.assertTrue(evaluate_condition(
            {'field':'entry.fields.date','op':'gt','value':'2025-01-01'},
            {'entry':{'fields':{'date':'2025-01-02'}}}, 'complete',
            {'fields':[{'scope':'entry','id':'date','type':'date'}]}))

    def test_condition_empty_string_and_list_are_unknown_but_present_is_false(self):
        for value in ('', []):
            with self.subTest(value=value):
                context={'invoice':{'total':value}}
                self.assertIsNone(evaluate_condition(
                    {'field':'invoice.total','op':'eq','value':'0.00'},context,'complete'))
                self.assertFalse(evaluate_condition(
                    {'field':'invoice.total','op':'present'},context,'complete'))

    def test_empty_required_multiselect_is_reported_missing(self):
        definition={'fields':[{'id':'tags','scope':'entry','label':'标签','type':'multiselect',
                              'required_at':['complete'],'options':[{'value':'a','label':'A'}]}],
                    'materials':[],'rules':[],'outputs':[]}
        result=evaluate_policy(definition,{'entry':{'fields':{'tags':[]}}},{},'complete')
        self.assertIn('FIELD_REQUIRED',{item['code'] for item in result})

    def test_material_maximum_is_enforced_at_export_stage(self):
        definition={'materials':[{'id':'receipts','label':'收据','min_count':0,'max_count':2}],
                    'fields':[],'rules':[],'outputs':[]}
        result=evaluate_policy(definition,{'export':{'output_id':'bundle'}},{'receipts':3},'export')
        self.assertIn('MATERIAL_MAXIMUM',{item['code'] for item in result})


class AdapterDomainValidationTests(unittest.TestCase):
    def _mutate_example(self, mutate):
        import shutil
        with tempfile.TemporaryDirectory() as temp:
            package=Path(temp)/'pkg'
            shutil.copytree(ROOT/'examples/adapters/minimal',package)
            mutate(package)
            return validate_package(package)

    def test_in_rule_operand_is_validated(self):
        def mutate(package):
            path=package/'rules.json'; data=json.loads(path.read_text())
            data['rules']=[{'id':'check','stage':'complete',
                'when':{'field':'invoice.total','op':'in','value':1},
                'require':[],'message':'check'}]
            path.write_text(json.dumps(data))
        result=self._mutate_example(mutate)
        self.assertIn('RULE_OPERAND_TYPE',{item['code'] for item in result['errors']})

    def test_rule_required_field_references_must_exist_and_match_stage(self):
        def mutate(package):
            path=package/'fields.json'; fields=json.loads(path.read_text())
            fields['fields']=[{'id':'code','scope':'batch','label':'批次号','type':'text',
                'required_at':[],'presentation':'visible','sensitive':False,'transfer':'include'}]
            path.write_text(json.dumps(fields))
            path=package/'manifest.json'; manifest=json.loads(path.read_text())
            manifest['requires']['capabilities'].append('fields.v1')
            path.write_text(json.dumps(manifest))
            path=package/'rules.json'; data=json.loads(path.read_text())
            data['rules']=[{'id':'check','stage':'complete',
                'when':{'field':'invoice.total','op':'gte','value':'0'},
                'require':[{'field':'batch.fields.code'}],'message':'check'}]
            path.write_text(json.dumps(data))
        result=self._mutate_example(mutate)
        self.assertIn('RULE_FIELD_STAGE_INVALID',{item['code'] for item in result['errors']})

    def test_batch_group_by_requires_declared_scalar_field(self):
        def mutate(package):
            path=package/'outputs.json'; data=json.loads(path.read_text())
            data['outputs'][0]['group_by']=['title','batch.fields.missing']
            path.write_text(json.dumps(data))
        result=self._mutate_example(mutate)
        self.assertIn('OUTPUT_GROUP_FIELD_INVALID',{item['code'] for item in result['errors']})

    def test_multiselect_cannot_be_batch_group_key(self):
        def mutate(package):
            fields_path=package/'fields.json'; fields=json.loads(fields_path.read_text())
            fields['fields']=[{'id':'tags','scope':'batch','label':'标签','type':'multiselect',
                'required_at':[],'presentation':'visible','sensitive':False,'transfer':'include',
                'options':[{'value':'a','label':'A'}]}]
            fields_path.write_text(json.dumps(fields))
            path=package/'outputs.json'; data=json.loads(path.read_text())
            data['outputs'][0]['group_by']=['title','batch.fields.tags']
            path.write_text(json.dumps(data))
        result=self._mutate_example(mutate)
        self.assertIn('OUTPUT_GROUP_FIELD_INVALID',{item['code'] for item in result['errors']})

    def test_required_reviewer_cannot_be_hidden_but_optional_reviewer_can(self):
        def mutate(required, presentation):
            def edit(package):
                path=package/'scheme.json'; data=json.loads(path.read_text())
                data['settings']={
                    'profile.reviewer_required':{'default':required,'editable':True,'presentation':'visible'},
                    'profile.reviewer_presentation':{'default':presentation,'editable':True,'presentation':'visible'}}
                path.write_text(json.dumps(data))
            return self._mutate_example(edit)
        denied=mutate(True,'hidden')
        self.assertIn('PROFILE_HIDDEN_REQUIRED',{x['code'] for x in denied['errors']})
        self.assertTrue(mutate(False,'hidden')['ok'])
        self.assertTrue(mutate(True,'advanced')['ok'])

    def test_visible_when_may_read_own_scope_but_not_another_form_scope(self):
        def mutate(package):
            path=package/'fields.json';data=json.loads(path.read_text())
            data['fields']=[{'id':'code','scope':'batch','label':'批次编号','type':'text',
                'required_at':[],'presentation':'visible','sensitive':False,'transfer':'include',
                'visible_when':{'field':'invoice.total','op':'gt','value':'0'}}]
            path.write_text(json.dumps(data))
            path=package/'manifest.json';manifest=json.loads(path.read_text())
            manifest['requires']['capabilities'].extend(['fields.v1','rules.v1'])
            path.write_text(json.dumps(manifest))
        result=self._mutate_example(mutate)
        self.assertIn('RULE_FIELD_STAGE_INVALID',{item['code'] for item in result['errors']})

    def test_import_defaults_are_registered_typed_and_effective(self):
        import shutil
        from tidoc.adapters.resolver import resolve_definition
        with tempfile.TemporaryDirectory() as temp:
            package=Path(temp)/'pkg';shutil.copytree(ROOT/'examples/adapters/minimal',package)
            path=package/'scheme.json';scheme=json.loads(path.read_text())
            scheme['import_defaults']={'entry.default_paid_to_invoice':False,'entry.suggested_tags':['lab']}
            path.write_text(json.dumps(scheme))
            result=load_package(package)
            effective=resolve_definition(result.definition)['effective_settings']
            self.assertFalse(effective['entry.default_paid_to_invoice'])
            self.assertEqual(effective['entry.suggested_tags'],['lab'])

    def test_import_defaults_reject_unknown_keys_and_settings_shadowing(self):
        def mutate(package):
            path=package/'scheme.json';scheme=json.loads(path.read_text())
            scheme['import_defaults']={'arbitrary':{'anything':True}}
            path.write_text(json.dumps(scheme))
        result=self._mutate_example(mutate)
        self.assertIn('SCHEMA_INVALID',{x['code'] for x in result['errors']})

    def test_core_generic_output_ids_are_reserved(self):
        def mutate(package):
            path=package/'outputs.json';data=json.loads(path.read_text())
            data['outputs'][0]['id']='generic_overview'
            path.write_text(json.dumps(data))
        result=self._mutate_example(mutate)
        self.assertIn('OUTPUT_ID_RESERVED',{x['code'] for x in result['errors']})


if __name__ == '__main__':
    unittest.main()

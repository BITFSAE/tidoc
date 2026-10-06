from tidoc.adapters.transfer import (
    ask_field_preview,
    field_can_transfer,
    project_extension_data,
    public_rule_projection,
    supported_rule_capabilities,
)
import json
import zipfile

from tidoc.services import bindle as bindle_service


def _definition():
    return {
        "manifest": {
            "format": "tidoc-team-adapter", "schema_version": "1.0",
            "package_id": "org.example.lab", "package_version": "1.0.0",
            "name": "Lab", "requires": {"adapter_api": 1, "capabilities": [
                "fields.v1", "material-roles.v1", "output.docx.v1", "payee.bank.v1", "rules.unknown.v9"
            ]},
        },
        "scheme": {
            "organization": {"name": "Private org", "storage_location": "/private/path"},
            "titles": [{"id": "lab", "name": "Lab", "tax_id": "123", "short_name": "L", "color": "blue"}],
            "settings": {"profile.reviewer_required": {"fixed": True}, "print.payee_mode": {"fixed": "single"}},
        },
        "fields": [
            {"id": "public", "scope": "entry", "label": "Purpose", "type": "text", "transfer": "include"},
            {"id": "private", "scope": "entry", "label": "Secret", "type": "text", "transfer": "never"},
            {"id": "ask", "scope": "entry", "label": "Phone", "type": "text", "transfer": "ask", "sensitive": True},
            {"id": "payee_info", "scope": "payee", "label": "Account", "type": "text", "transfer": "include"},
        ],
        "materials": {"roles": [
            {"id": "invoice", "label": "Invoice", "min_count": 1},
            {"id": "custom:org.example.lab:contract", "label": "Contract", "min_count": 1},
        ]},
        "rules": {"rules": [{"id": "need-contract", "stage": "complete", "require": {"role": "custom:org.example.lab:contract"}},
                               {"id": "private-condition", "stage": "complete", "when": {"field": "private"}}]},
        "outputs": [{"id": "doc", "template": "templates/private.docx"}],
        "effective_settings": {"profile.reviewer_required": True, "print.payee_mode": "single"},
    }


def test_public_rule_projection_keeps_completion_rules_and_roles_only():
    projection = public_rule_projection(_definition())
    assert projection["materials"][1]["id"] == "custom:org.example.lab:contract"
    assert [rule["id"] for rule in projection["rules"]] == ["need-contract", "private-condition"]
    assert projection["outputs"] == []
    assert projection["scheme"]["organization"] == {}
    assert "print.payee_mode" not in projection["scheme"]["settings"]
    assert "print.payee_mode" not in projection["effective_settings"]
    assert all("template" not in output for output in projection["outputs"])
    assert "payee.bank.v1" not in projection["manifest"]["requires"]["capabilities"]
    assert "rules.unknown.v9" in projection["manifest"]["requires"]["capabilities"]


def test_never_values_and_every_history_value_are_removed_but_ask_requires_field_choice():
    definition = _definition()
    values = [
        {"scope": "entry", "package_id": "org.example.lab", "field_id": "public", "value": "purpose"},
        {"scope": "entry", "package_id": "org.example.lab", "field_id": "private", "value": "current secret"},
        {"scope": "entry", "package_id": "org.example.lab", "field_id": "ask", "value": "13800000000"},
        {"scope": "payee", "package_id": "org.example.lab", "field_id": "payee_info", "value": "account"},
    ]
    history = [
        {"scope": "entry", "package_id": "org.example.lab", "field_id": "private", "old_value": "old secret", "new_value": "current secret"},
        {"scope": "entry", "package_id": "org.example.lab", "field_id": "ask", "old_value": "old phone", "new_value": "13800000000"},
        {"scope": "entry", "package_id": "org.example.lab", "field_id": "public", "old_value": "a", "new_value": "b"},
    ]
    projected, projected_history = project_extension_data(definition, values, history)
    assert [row["field_id"] for row in projected] == ["public"]
    assert [row["field_id"] for row in projected_history] == ["public"]
    allowed_key = "org.example.lab:entry:ask"
    projected, projected_history = project_extension_data(
        definition, values, history, include_ask_fields=[allowed_key]
    )
    assert {row["field_id"] for row in projected} == {"public", "ask"}
    assert {row["field_id"] for row in projected_history} == {"public", "ask"}
    assert not field_can_transfer(definition["fields"][1], "org.example.lab", ["*"])


def test_ask_preview_lists_only_populated_entry_fields_and_unknown_capabilities():
    definition = _definition()
    rows = ask_field_preview([(definition, [
        {"scope": "entry", "package_id": "org.example.lab", "field_id": "ask", "owner_id": "e1", "value": "x"},
        {"scope": "entry", "package_id": "org.example.lab", "field_id": "private", "owner_id": "e2", "value": "never"},
    ])])
    assert rows == [{"key": "org.example.lab:entry:ask", "package_id": "org.example.lab", "scope": "entry",
                     "field_id": "ask", "label": "Phone", "sensitive": True, "count": 1}]
    required, missing = supported_rule_capabilities(definition)
    assert "fields.v1" in required
    assert "payee.bank.v1" in missing
    assert "rules.unknown.v9" in missing


def test_v5_bindle_export_signs_public_adapter_projection_and_excludes_private_payloads(tmp_path, monkeypatch):
    class Rows:
        def fetchall(self):
            return []

        def fetchone(self):
            return ("scheme-id",)

    class Conn:
        def execute(self, *_args):
            return Rows()

    class DB:
        conn = Conn()

    class Entries:
        db = DB()

        def get(self, entry_id):
            return {
                "id": entry_id, "profile_id": "profile-id", "title": "Lab", "fields": {},
                "history": [], "attachments": [], "items": [], "tags": [], "scheme_revision_id": "local-rev",
            }

    class Root:
        attachments_dir = tmp_path / "attachments"

    class Attachments:
        data_root = Root()

    class Extensions:
        def list_values(self, *_args, **_kwargs):
            return [
                {"scope": "entry", "package_id": "org.example.lab", "field_id": "public", "value": "safe"},
                {"scope": "entry", "package_id": "org.example.lab", "field_id": "private", "value": "NEVER-EXPORT-VALUE"},
                {"scope": "entry", "package_id": "org.example.lab", "field_id": "ask", "value": "ASK-UNLESS-SELECTED"},
            ]

        def history(self, *_args, **_kwargs):
            return [
                {"scope": "entry", "package_id": "org.example.lab", "field_id": "private",
                 "old_value": "NEVER-EXPORT-OLD", "new_value": "NEVER-EXPORT-VALUE"},
                {"scope": "entry", "package_id": "org.example.lab", "field_id": "public",
                 "old_value": "old", "new_value": "safe", "changed_at": "2026-01-01"},
            ]

    class AdapterService:
        db = DB()
        extensions = Extensions()

        def get_revision(self, _revision_id):
            return _definition()

    monkeypatch.setattr(bindle_service, "build_summary", lambda *_args: {"entries": []})
    out = bindle_service.export_bindle(Entries(), Attachments(), ["entry-1"], tmp_path / "out.tidoc",
                                       {"profile-id": {"id": "profile-id", "name": "N", "reviewer": None,
                                                        "payee": "SECRET-PAYEE"}},
                                       adapter_service=AdapterService())
    with zipfile.ZipFile(out) as archive:
        payload = json.loads(archive.read("entries.json"))
        serialized = archive.read("entries.json").decode()
        manifest = json.loads(archive.read("signatures.json"))
    assert payload["bindle_version"] == 5
    assert manifest["minimum_receiver_capability"] == "bindle.v5"
    row = payload["entries"][0]
    assert row["adapter"]["definition"]["materials"][1]["id"] == "custom:org.example.lab:contract"
    assert [v["field_id"] for v in row["adapter"]["extension_values"]] == ["public"]
    assert [v["field_id"] for v in row["adapter"]["extension_history"]] == ["public"]
    for forbidden in ("NEVER-EXPORT", "ASK-UNLESS", "SECRET-PAYEE", "/private/path", "private.docx"):
        assert forbidden not in serialized
    tampered = tmp_path / "unlisted.tidoc"
    with zipfile.ZipFile(out) as source, zipfile.ZipFile(tampered, "w") as target:
        for name in source.namelist():
            target.writestr(name, source.read(name))
        target.writestr("unlisted.json", b"{}")
    inspected = bindle_service.inspect_bindle(tampered)
    assert "signature_coverage" in inspected["tampered"]


def _transfer_source(tmp_path):
    from copy import deepcopy
    from decimal import Decimal
    from tidoc.api import Api
    from tidoc.engine.models import ParsedInvoice
    from tidoc.adapters.registry import required_capabilities
    from tidoc.adapters.resolver import resolve_definition
    api=Api(tmp_path/'source')
    api.complete_adapter_setup(api.adapters.default_binding()[0])
    scheme=api.adapters.get_scheme()
    definition=deepcopy(scheme['definition'])
    definition['fields']=[{'id':name,'scope':'entry','label':name,'type':'text',
        'required_at':[],'presentation':'visible','sensitive':name!='public',
        'transfer':policy} for name,policy in [('public','include'),('phone','ask'),('private','never')]]
    definition['manifest']['requires']['capabilities']=required_capabilities(definition)
    revision=api.adapters.packages.store_revision(resolve_definition(definition),
        api.adapters.packages.revision_record(scheme['current_revision_id'])['content_hash'])
    api.adapters.packages.set_current_revision(scheme['id'],revision)
    person=api.profiles.create('虚构报账人','')
    eid=api.entries.create(person['id'],parsed=ParsedInvoice(total=Decimal('5.00'),invoice_no='PORTABLE-1'))
    api.adapters.save_extension_values('entry',eid,{'public':'项目甲','phone':'001234','private':'NEVER-SEND'})
    material=tmp_path/'invoice.xml';material.write_text('<invoice/>')
    api.attachments.add(eid,material,'invoice_xml')
    api.entries.recompute_status(eid)
    path=bindle_service.export_bindle(api.entries,api.attachments,[eid],tmp_path/'source.tidoc',
        {person['id']:person},include_ask_fields=['org.tidoc.generic:entry:phone'])
    return api,person,eid,path


def _rewrite_signed_archive(source_path, destination, mutate):
    from tidoc.services.bindle import ENTRIES_NAME, MANIFEST_NAME, SUMMARY_NAME
    from tidoc.services.signing import sign_bytes
    with zipfile.ZipFile(source_path) as archive:
        members={name:archive.read(name) for name in archive.namelist() if name!=MANIFEST_NAME}
        manifest=json.loads(archive.read(MANIFEST_NAME))
    payload=json.loads(members[ENTRIES_NAME])
    mutate(payload,manifest)
    members[ENTRIES_NAME]=json.dumps(payload,ensure_ascii=False,indent=2).encode('utf-8')
    manifest['signatures'][ENTRIES_NAME]=sign_bytes(members[ENTRIES_NAME])
    if SUMMARY_NAME in members:
        manifest['signatures'][SUMMARY_NAME]=sign_bytes(members[SUMMARY_NAME])
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in members.items():
            archive.writestr(name,data)
        archive.writestr(MANIFEST_NAME,json.dumps(manifest,ensure_ascii=False,indent=2))
    return destination


def _archive_with_comment(source_path,destination,comment):
    import struct
    raw=bytearray(source_path.read_bytes())
    end=raw.rfind(b'PK\x05\x06')
    assert end>=0
    old_length=struct.unpack_from('<H',raw,end+20)[0]
    assert old_length==0
    struct.pack_into('<H',raw,end+20,len(comment))
    raw.extend(comment)
    destination.write_bytes(raw)
    return destination


@__import__('pytest').mark.parametrize('version',[1,2,3,4])
def test_serialized_legacy_bindle_v1_to_v4_imports_without_adapter_activation(tmp_path,version):
    from tidoc.api import Api
    source,person,_entry_id,modern_path=_transfer_source(tmp_path)
    def downgrade(payload,manifest):
        payload['bindle_version']=version
        manifest['bindle_version']=version
        manifest.pop('minimum_receiver_capability',None)
        # v1 carries identity inline; later legacy versions also carry a profile table.
        if version==1:
            payload.pop('profiles',None)
        for row in payload['entries']:
            row.pop('adapter',None)
            if version>=2:
                row.pop('profile_name',None)
                row.pop('reviewer',None)
            if version<4:
                row.pop('created_at',None)
                row.pop('updated_at',None)
            for attachment in row.get('attachments',[]):
                attachment.pop('role_id',None)
                attachment.pop('role_definition_revision_id',None)
    legacy_path=_rewrite_signed_archive(modern_path,tmp_path/f'bindle-v{version}.tidoc',downgrade)
    target=Api(tmp_path/f'target-v{version}')
    try:
        inspected=target.inspect_bindle(legacy_path)['data']
        assert inspected['verified'] is True
        assert inspected['bindle_version']==version
        result=target.import_bindle(legacy_path,'')
        assert result['ok'] and result['data']['imported']==1
        imported=target.entries.get(result['data']['entry_ids'][0])
        assert target.profiles.get(imported['profile_id'])['name']==person['name']
        assert not imported.get('adapter_sources')
    finally:
        source.db.close()
        target.db.close()


def test_uninstalled_source_preserves_values_and_history_without_activating(tmp_path):
    from tidoc.api import Api
    source,person,eid,path=_transfer_source(tmp_path)
    target=Api(tmp_path/'target');target.complete_adapter_setup(target.adapters.default_binding()[0])
    claimant=target.profiles.create(person['name'],'')
    before=[s['id'] for s in target.adapters.list_schemes()]
    assert target.inspect_bindle(path)['ok']
    result=target.import_bindle(path,claimant['id'])
    assert result['ok'],result
    entry_id=result['data']['entry_ids'][0];entry=target.entries.get(entry_id)
    assert entry['scheme_revision_id'] is None
    assert not entry['completeness']['ready']
    assert entry['diagnostics'][0]['code']=='EXTERNAL_SCHEME_PENDING'
    assert [s['id'] for s in target.adapters.list_schemes()]==before
    # This is the installed package ID with a different public revision digest;
    # the exchange snapshot stays external and is not treated as an install.
    assert entry['adapter_sources'][0]['definition']['manifest']['package_id']=='org.tidoc.generic'
    assert entry['adapter_sources'][0]['source_revision_digest']!=target.adapters.get_scheme()['current_revision_id']
    from tidoc.adapters.transfer import content_hash,public_rule_projection
    installed_projection=public_rule_projection(target.adapters.get_scheme()['definition'])
    assert content_hash(entry['adapter_sources'][0]['definition'])!=content_hash(installed_projection)
    values={r['field_id']:r['value'] for r in entry['adapter_sources'][0]['extension_values']}
    assert values=={'public':'项目甲','phone':'001234'}
    assert target.entries.list()[0]['completeness']==entry['completeness']
    preview=target.preview_export([entry_id],['reimbursement'])
    assert not preview['ok']
    assert any(d['code']=='EXTERNAL_SCHEME_PENDING' for d in preview['diagnostics'])
    for choice,expected in [([],{'public'}),(['org.tidoc.generic:entry:phone'],{'public','phone'})]:
        outgoing=bindle_service.export_bindle(target.entries,target.attachments,[entry_id],
            tmp_path/('roundtrip-'+str(len(choice))+'.tidoc'),{claimant['id']:claimant},include_ask_fields=choice)
        with zipfile.ZipFile(outgoing) as archive:
            text=archive.read('entries.json').decode();payload=json.loads(text)['entries'][0]['adapter']
        assert {r['field_id'] for r in payload['extension_values']}==expected
        assert {r['field_id'] for r in payload['extension_history']}==expected
        assert 'NEVER-SEND' not in text
    source.db.close();target.db.close()


def test_future_capability_is_reported_missing_and_import_stays_external(tmp_path):
    from tidoc.api import Api
    source,person,_entry_id,modern_path=_transfer_source(tmp_path)
    future='rules.team_future.v99'
    def add_future_capability(payload,_manifest):
        adapter=payload['entries'][0]['adapter']
        adapter['definition']['manifest']['requires']['capabilities'].append(future)
    package=_rewrite_signed_archive(modern_path,tmp_path/'future-capability.tidoc',add_future_capability)
    target=Api(tmp_path/'future-target');target.complete_adapter_setup(target.adapters.default_binding()[0])
    before=[scheme['id'] for scheme in target.adapters.list_schemes()]
    try:
        inspected=target.inspect_bindle(package)['data']
        compatibility=inspected['entries'][0]['adapter_compatibility']
        assert future in compatibility['missing_capabilities']
        assert compatibility['available'] is False
        imported=target.import_bindle(package,'')
        assert imported['ok'] and imported['data']['imported']==1
        entry=target.entries.get(imported['data']['entry_ids'][0])
        assert entry['scheme_revision_id'] is None
        assert [scheme['id'] for scheme in target.adapters.list_schemes()]==before
        assert future in entry['adapter_sources'][0]['definition']['manifest']['requires']['capabilities']
    finally:
        source.db.close();target.db.close()


def test_empty_reviewer_same_name_identity_requires_explicit_preview_mapping(tmp_path):
    import pytest
    from tidoc.api import Api
    source,person,_entry_id,package=_transfer_source(tmp_path)
    target=Api(tmp_path/'identity-target');target.complete_adapter_setup(target.adapters.default_binding()[0])
    first=target.profiles.create(person['name'],'Reviewer A')
    second=target.profiles.create(person['name'],'Reviewer B')
    try:
        preview=bindle_service.inspect_bindle(package,target.entries,adapter_service=target.adapters)
        choice=preview['profile_mapping_preview'][person['id']]
        assert choice['status']=='ambiguous'
        assert {row['id'] for row in choice['candidates']}=={first['id'],second['id']}
        with pytest.raises(ValueError,match='多个匹配项'):
            bindle_service.import_bindle(target.entries,target.attachments,package,'',
                inspected=preview,adapter_service=target.adapters)
        selected=second['id']
        result=bindle_service.import_bindle(target.entries,target.attachments,package,'',
            options={'profile_mappings':{person['id']:selected}},
            inspected=preview,adapter_service=target.adapters)
        assert result['imported']==1
        imported=target.entries.get(result['entry_ids'][0])
        assert imported['profile_id']==selected
    finally:
        source.db.close();target.db.close()


def test_empty_reviewer_with_unique_same_name_reuses_existing_identity(tmp_path):
    from tidoc.api import Api
    source,person,_entry_id,package=_transfer_source(tmp_path)
    target=Api(tmp_path/'identity-unique-target');target.complete_adapter_setup(target.adapters.default_binding()[0])
    existing=target.profiles.create(person['name'],'')
    try:
        preview=bindle_service.inspect_bindle(package,target.entries,adapter_service=target.adapters)
        assert preview['profile_mapping_preview'][person['id']]['status']=='unique'
        result=bindle_service.import_bindle(target.entries,target.attachments,package,'',
            inspected=preview,adapter_service=target.adapters)
        assert result['imported']==1
        assert target.entries.get(result['entry_ids'][0])['profile_id']==existing['id']
    finally:
        source.db.close();target.db.close()


def test_import_rejects_local_entry_changes_after_serialized_preview(tmp_path):
    import pytest
    from decimal import Decimal
    from tidoc.api import Api
    from tidoc.engine.models import ParsedInvoice
    source,_person,_entry_id,package=_transfer_source(tmp_path)
    target=Api(tmp_path/'stale-target');target.complete_adapter_setup(target.adapters.default_binding()[0])
    claimant=target.profiles.create('Local claimant','Reviewer')
    existing=target.entries.create(claimant['id'],parsed=ParsedInvoice(invoice_no='PORTABLE-1',total=Decimal('5.00')))
    try:
        preview=bindle_service.inspect_bindle(package,target.entries,adapter_service=target.adapters)
        assert preview['entries'][0]['existing_entry_id']==existing
        target.entries.update_field(existing,'notes','changed after preview',claimant['id'])
        with pytest.raises(ValueError,match='预览后绑定包或本地条目已变化'):
            bindle_service.import_bindle(target.entries,target.attachments,package,claimant['id'],
                inspected=preview,adapter_service=target.adapters)
        assert target.entries.get(existing)['fields']['notes']['current']=='changed after preview'
        assert len(target.entries.list())==1
    finally:
        source.db.close();target.db.close()


def test_api_cache_same_size_mtime_archive_swap_fails_fresh_sha256(tmp_path):
    import hashlib
    import os
    from tidoc.api import Api
    source,_person,_entry_id,package=_transfer_source(tmp_path)
    def change_public_value(payload,_manifest):
        payload['entries'][0]['adapter']['extension_values'][0]['value']='项目乙'
    variant=_rewrite_signed_archive(package,tmp_path/'variant.tidoc',change_public_value)
    first=package
    second=variant
    if first.stat().st_size>second.stat().st_size:
        second=_archive_with_comment(variant,tmp_path/'second.tidoc',b'X'*(first.stat().st_size-variant.stat().st_size))
    elif second.stat().st_size>first.stat().st_size:
        first=_archive_with_comment(package,tmp_path/'first.tidoc',b'X'*(variant.stat().st_size-package.stat().st_size))
    assert first.stat().st_size==second.stat().st_size
    target=Api(tmp_path/'hash-target');target.complete_adapter_setup(target.adapters.default_binding()[0])
    claimant=target.profiles.create('Receiver','Reviewer')
    swap_path=tmp_path/'cached.tidoc'
    swap_path.write_bytes(first.read_bytes())
    try:
        preview=target.inspect_bindle(swap_path)
        assert preview['ok']
        original=swap_path.stat()
        original_digest=hashlib.sha256(swap_path.read_bytes()).hexdigest()
        replacement_digest=hashlib.sha256(second.read_bytes()).hexdigest()
        assert original_digest!=replacement_digest
        swap_path.write_bytes(second.read_bytes())
        os.utime(swap_path,ns=(original.st_atime_ns,original.st_mtime_ns))
        swapped=swap_path.stat()
        assert (swapped.st_size,swapped.st_mtime_ns)==(original.st_size,original.st_mtime_ns)
        result=target.import_bindle(swap_path,claimant['id'])
        assert result['ok'] is False
        assert '预览后绑定包' in result['error']
        assert not target.entries.list()
    finally:
        source.db.close();target.db.close()


def test_mixed_pinned_transfer_defaults_filter_entry_and_summary_together(tmp_path):
    from copy import deepcopy
    import pytest
    import pytest
    from decimal import Decimal
    from tidoc.api import Api
    from tidoc.adapters.resolver import resolve_definition
    from tidoc.engine.models import ParsedInvoice
    api=Api(tmp_path/'pinned-transfer')
    api.complete_adapter_setup(api.adapters.default_binding()[0])
    base=api.adapters.get_scheme()
    disabled=api.adapters.update_scheme_settings(base['id'],base['current_revision_id'],
        {'transfer.include_notes':False,'transfer.include_tags':False})
    disabled=api.adapters.get_scheme(disabled['id'])
    enabled=api.adapters.copy_scheme(disabled['id'],'Share notes and tags')
    enabled=api.adapters.update_scheme_settings(enabled['id'],enabled['current_revision_id'],
        {'transfer.include_notes':True,'transfer.include_tags':True})
    enabled=api.adapters.get_scheme(enabled['id'])
    profile=api.profiles.create('Fixture claimant','Reviewer')
    entry_ids=[]
    for index,scheme in enumerate((disabled,enabled),1):
        entry_id=api.entries.create(profile['id'],parsed=ParsedInvoice(
            invoice_no=f'PINNED-{index}',total=Decimal('1.00')),
            scheme_id=scheme['id'],scheme_revision_id=scheme['current_revision_id'])
        api.entries.update_field(entry_id,'notes',f'note-{index}',profile['id'])
        api.entries.set_meta(entry_id,tags=[f'tag-{index}'])
        attachment=tmp_path/f'attachment-{index}.txt'
        attachment.write_text(f'attachment-{index}')
        api.attachments.add(entry_id,attachment,'other',f'attachment-note-{index}')
        entry_ids.append(entry_id)
    try:
        archive=bindle_service.export_bindle(api.entries,api.attachments,entry_ids,
            tmp_path/'per-revision.tidoc',{profile['id']:profile},adapter_service=api.adapters)
        with zipfile.ZipFile(archive) as zf:
            payload=json.loads(zf.read('entries.json'))
            summary=json.loads(zf.read('summary.json'))
        entries={row['invoice_no']:row for row in payload['entries']}
        summaries={row['invoice_no']:row for row in summary['entries']}
        assert payload['options']=={'include_notes':None,'include_tags':None}
        excluded=entries['PINNED-1']
        included=entries['PINNED-2']
        assert 'notes' not in excluded['fields']
        assert all(item['field']!='notes' for item in excluded['history'])
        assert excluded['tags']==[] and excluded['attachments'][0]['note']==''
        assert 'notes' not in summaries['PINNED-1']
        assert included['fields']['notes']['current']=='note-2'
        assert any(item['field']=='notes' for item in included['history'])
        assert included['tags']==['tag-2'] and included['attachments'][0]['note']=='attachment-note-2'
        assert summaries['PINNED-2']['notes']=='note-2'

        overridden=bindle_service.export_bindle(api.entries,api.attachments,entry_ids,
            tmp_path/'global-override.tidoc',{profile['id']:profile},include_notes=True,
            include_tags=True,adapter_service=api.adapters)
        with zipfile.ZipFile(overridden) as zf:
            forced=json.loads(zf.read('entries.json'))
        assert forced['options']=={'include_notes':True,'include_tags':True}
        assert all(row['tags'] for row in forced['entries'])
        assert all('notes' in row['fields'] for row in forced['entries'])

        fixed_base=api.adapters.get_scheme(disabled['id'])
        definition=deepcopy(fixed_base['definition'])
        definition['scheme'].setdefault('settings',{})['transfer.include_notes']={
            'fixed':False,'editable':False,'presentation':'visible'}
        definition=resolve_definition(definition)
        package_hash=api.adapters.packages.revision_record(fixed_base['current_revision_id'])['content_hash']
        revision=api.adapters.packages.store_revision(definition,package_hash)
        fixed=api.adapters.packages.create_scheme('Fixed no-notes',revision)
        fixed_entry=api.entries.create(profile['id'],parsed=ParsedInvoice(invoice_no='PINNED-FIXED'),
            scheme_id=fixed['id'],scheme_revision_id=revision)
        with pytest.raises(ValueError,match='固定了 transfer.include_notes'):
            bindle_service.export_bindle(api.entries,api.attachments,[fixed_entry],
                tmp_path/'fixed-conflict.tidoc',{profile['id']:profile},include_notes=True,
                adapter_service=api.adapters)
    finally:
        api.db.close()


def test_mixed_pinned_transfer_defaults_filter_entry_and_summary_together(tmp_path):
    from copy import deepcopy
    import pytest
    from decimal import Decimal
    from tidoc.api import Api
    from tidoc.adapters.resolver import resolve_definition
    from tidoc.engine.models import ParsedInvoice
    api=Api(tmp_path/'pinned-transfer')
    api.complete_adapter_setup(api.adapters.default_binding()[0])
    base=api.adapters.get_scheme()
    disabled=api.adapters.update_scheme_settings(base['id'],base['current_revision_id'],
        {'transfer.include_notes':False,'transfer.include_tags':False})
    disabled=api.adapters.get_scheme(disabled['id'])
    enabled=api.adapters.copy_scheme(disabled['id'],'Share notes and tags')
    enabled=api.adapters.update_scheme_settings(enabled['id'],enabled['current_revision_id'],
        {'transfer.include_notes':True,'transfer.include_tags':True})
    enabled=api.adapters.get_scheme(enabled['id'])
    profile=api.profiles.create('Fixture claimant','Reviewer')
    entry_ids=[]
    for index,scheme in enumerate((disabled,enabled),1):
        entry_id=api.entries.create(profile['id'],parsed=ParsedInvoice(
            invoice_no=f'PINNED-{index}',total=Decimal('1.00')),
            scheme_id=scheme['id'],scheme_revision_id=scheme['current_revision_id'])
        api.entries.update_field(entry_id,'notes',f'note-{index}',profile['id'])
        api.entries.set_meta(entry_id,tags=[f'tag-{index}'])
        attachment=tmp_path/f'attachment-{index}.txt'
        attachment.write_text(f'attachment-{index}')
        api.attachments.add(entry_id,attachment,'other',f'attachment-note-{index}')
        entry_ids.append(entry_id)
    try:
        archive=bindle_service.export_bindle(api.entries,api.attachments,entry_ids,
            tmp_path/'per-revision.tidoc',{profile['id']:profile},adapter_service=api.adapters)
        with zipfile.ZipFile(archive) as zf:
            payload=json.loads(zf.read('entries.json'))
            summary=json.loads(zf.read('summary.json'))
        entries={row['invoice_no']:row for row in payload['entries']}
        summaries={row['invoice_no']:row for row in summary['entries']}
        assert payload['options']=={'include_notes':None,'include_tags':None}
        excluded=entries['PINNED-1']
        included=entries['PINNED-2']
        assert 'notes' not in excluded['fields']
        assert all(item['field']!='notes' for item in excluded['history'])
        assert excluded['tags']==[] and excluded['attachments'][0]['note']==''
        assert 'notes' not in summaries['PINNED-1']
        assert included['fields']['notes']['current']=='note-2'
        assert any(item['field']=='notes' for item in included['history'])
        assert included['tags']==['tag-2'] and included['attachments'][0]['note']=='attachment-note-2'
        assert summaries['PINNED-2']['notes']=='note-2'

        overridden=bindle_service.export_bindle(api.entries,api.attachments,entry_ids,
            tmp_path/'global-override.tidoc',{profile['id']:profile},include_notes=True,
            include_tags=True,adapter_service=api.adapters)
        with zipfile.ZipFile(overridden) as zf:
            forced=json.loads(zf.read('entries.json'))
        assert forced['options']=={'include_notes':True,'include_tags':True}
        assert all(row['tags'] for row in forced['entries'])
        assert all('notes' in row['fields'] for row in forced['entries'])

        fixed_base=api.adapters.get_scheme(disabled['id'])
        definition=deepcopy(fixed_base['definition'])
        definition['scheme'].setdefault('settings',{})['transfer.include_notes']={
            'fixed':False,'editable':False,'presentation':'visible'}
        definition=resolve_definition(definition)
        package_hash=api.adapters.packages.revision_record(fixed_base['current_revision_id'])['content_hash']
        revision=api.adapters.packages.store_revision(definition,package_hash)
        fixed=api.adapters.packages.create_scheme('Fixed no-notes',revision)
        fixed_entry=api.entries.create(profile['id'],parsed=ParsedInvoice(invoice_no='PINNED-FIXED'),
            scheme_id=fixed['id'],scheme_revision_id=revision)
        with pytest.raises(ValueError,match='固定了 transfer.include_notes'):
            bindle_service.export_bindle(api.entries,api.attachments,[fixed_entry],
                tmp_path/'fixed-conflict.tidoc',{profile['id']:profile},include_notes=True,
                adapter_service=api.adapters)
    finally:
        api.db.close()


def test_explicit_local_scheme_import_merges_fields_and_core_invoice_role(tmp_path):
    from tidoc.api import Api
    source,person,eid,path=_transfer_source(tmp_path)
    target=Api(tmp_path/'target');target.complete_adapter_setup(target.adapters.default_binding()[0])
    scheme=target.adapters.get_scheme()
    definition=source.adapters.context_for_entry(source.entries.get(eid))
    revision=target.adapters.packages.store_revision(definition,
        target.adapters.packages.revision_record(scheme['current_revision_id'])['content_hash'])
    target.adapters.packages.set_current_revision(scheme['id'],revision)
    claimant=target.profiles.create(person['name'],'')
    result=target.import_bindle(path,claimant['id'],options={'scheme_id':scheme['id']})
    assert result['ok'],result
    entry_id=result['data']['entry_ids'][0];entry=target.entries.get(entry_id)
    assert entry['extension_values']['phone']=='001234'
    assert entry['extension_values']['public']=='项目甲'
    assert entry['completeness']['ready']
    assert entry['attachments'][0]['role_definition_revision_id']==revision
    assert target.inspect_bindle(path)['ok']
    repeated=target.import_bindle(path,claimant['id'],options={'scheme_id':scheme['id']})
    assert repeated['ok'],repeated
    assert repeated['data']['imported']==0
    assert len(target.entries.list())==1
    source.db.close();target.db.close()


def test_other_material_cannot_map_to_invoice(tmp_path):
    import pytest
    source,person,eid,path=_transfer_source(tmp_path)
    entry=source.entries.get(eid)
    with pytest.raises(ValueError,match='替代发票'):
        bindle_service._validated_material_mapping(source.entries,eid,entry['scheme_revision_id'],
            'other','invoice','other','forged.pdf')
    source.db.close()

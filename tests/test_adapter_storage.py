"""Self-contained storage/migration tests; no private invoice data or UI mocks."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from tidoc.adapters.loader import load_package
from tidoc.adapters.models import AdapterPackage
from tidoc.adapters.resolver import resolve_definition
from tidoc.adapters.service import AdapterService
from tidoc.db.adapters import AdapterRepo, encode
from tidoc.db.database import Database
from tidoc.db.export_jobs import ExportJobRepo
from tidoc.db.extensions import ExtensionRepo
from tidoc.db.paths import DataRoot
from tidoc.db.payees import PayeeRepo
from tidoc.db.profiles import ProfileRepo
from tidoc.db.batches import BatchRepo


def write_package(path, version='1.0.0', *, settings=None, fields=None, roles=None):
    path.mkdir(parents=True,exist_ok=True)
    content={
        'manifest.json':{'format':'tidoc-team-adapter','schema_version':'1.0',
            'package_id':'org.test.storage','package_version':version,'name':'Storage fixture',
            'requires':{'adapter_api':1,'capabilities':['fields.v1','material-roles.v1','rules.v1']}},
        'scheme.json':{'titles':[],'organization':{},'settings':settings or {}},
        'fields.json':{'fields':fields or []},
        'materials.json':{'roles':roles or [{'id':'invoice','label':'发票','extensions':['.pdf','.xml'],
            'min_count':1,'max_count':None,'order':0,'quick_action':True,'reclassifiable':False,'presentation':'visible'}]},
        'rules.json':{'rules':[]},'outputs.json':{'outputs':[]},
    }
    for name,value in content.items():
        (path/name).write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    return path


@pytest.fixture
def storage(tmp_path):
    root=DataRoot(tmp_path/'data')
    db=Database(root.db_path)
    service=AdapterService(db,root)
    package=load_package(write_package(tmp_path/'source'))
    prepared=service._prepare_package(package,'test')
    with db.transaction():
        service._record_package(package,prepared,'test')
        revision=service.packages.store_revision(resolve_definition(package.definition),package.content_hash,commit=False)
        scheme=service.packages.create_scheme('Test',revision,is_default=True,commit=False)
    service._finish_operation(prepared)
    yield root,db,service,scheme,tmp_path/'source'
    db.close()


def entry(db,scheme=None):
    from tidoc.db.entries import EntryRepo
    profile=ProfileRepo(db).create('Fixture', '')
    return EntryRepo(db).create(profile['id'],scheme_id=scheme['id'] if scheme else None,
        scheme_revision_id=scheme['current_revision_id'] if scheme else None)


def legacy_db(path, version=11):
    # Freeze the actual v11 schema so committing v12 cannot change this input.
    source=(Path(__file__).parent/'fixtures'/'schema-v11.sql').read_text(encoding='utf-8')
    connection=sqlite3.connect(path)
    connection.executescript(source)
    connection.execute('UPDATE meta SET value=? WHERE key=?',(str(version),'schema_version'))
    connection.execute("INSERT INTO profiles(id,name,reviewer,created_at) VALUES('p','Legacy','Reviewer','2020')")
    connection.execute("INSERT INTO entries(id,profile_id,title,buyer_name,buyer_tax_id,created_at,updated_at) VALUES('e','p','Same','Same','','2020','2020')")
    connection.execute("INSERT INTO attachments(id,entry_id,type,added_at) VALUES('a','e','invoice_xml','2020')")
    connection.execute("INSERT INTO batches(id,name,created_at,updated_at) VALUES('b','Batch','2020','2020')")
    connection.execute("INSERT INTO batch_entries VALUES('b','e','Keep note','2020')")
    connection.commit()
    return connection


def test_future_database_refused_without_modifying(tmp_path):
    path=tmp_path/'future.sqlite'
    conn=sqlite3.connect(path)
    conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
    conn.execute("INSERT INTO meta VALUES('schema_version','99')")
    conn.commit();conn.close()
    original=path.read_bytes()
    with pytest.raises(ValueError,match='拒绝写入'):
        Database(path)
    assert path.read_bytes()==original
    assert not path.with_name(path.name+'-wal').exists()


def test_migration_real_v11_backup_and_constraints(tmp_path):
    path=tmp_path/'legacy.sqlite'
    conn=legacy_db(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute("INSERT INTO meta VALUES('wal_visible','committed')")
    conn.commit()
    db=Database(path)
    assert db.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]=='12'
    assert db.conn.execute("SELECT role_id FROM attachments WHERE id='a'").fetchone()[0]=='invoice'
    assert db.conn.execute('SELECT note FROM batch_entries').fetchone()[0]=='Keep note'
    assert not list(db.conn.execute('PRAGMA foreign_key_check'))
    backup=next((tmp_path/'backups').glob('*.sqlite'))
    with sqlite3.connect(backup) as copied:
        assert copied.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]=='11'
        assert copied.execute("SELECT value FROM meta WHERE key='wal_visible'").fetchone()[0]=='committed'
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE entries SET scheme_id='missing',scheme_revision_id='missing' WHERE id='e'")
    db.conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE batches SET default_scheme_id='missing',default_revision_id='missing' WHERE id='b'")
    db.conn.rollback();db.close();conn.close()


def test_atomic_migration_rolls_back_on_injected_failure(tmp_path,monkeypatch):
    import tidoc.db.schema as schema
    path=tmp_path/'atomic.sqlite'
    conn=legacy_db(path);conn.close()
    real=schema._migrate_v12
    def fail(connection):
        real(connection)
        raise OSError('disk full')
    monkeypatch.setattr(schema,'_migrate_v12',fail)
    with pytest.raises(OSError,match='disk full'):
        Database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]=='11'
        assert 'scheme_id' not in {r[1] for r in conn.execute('PRAGMA table_info(entries)')}
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='schemes'").fetchone()
        assert conn.execute('SELECT COUNT(*) FROM entries').fetchone()[0]==1
    monkeypatch.setattr(schema,'_migrate_v12',real)
    db=Database(path)
    assert db.conn.execute('SELECT COUNT(*) FROM entries').fetchone()[0]==1
    db.close()


def test_backup_failure_does_not_start_migration(tmp_path,monkeypatch):
    import tidoc.db.database as database
    path=tmp_path/'backup.sqlite'
    conn=legacy_db(path);conn.close()
    def fail(*args):
        raise OSError('backup failed')
    monkeypatch.setattr(database,'backup_connection',fail)
    with pytest.raises(OSError,match='backup failed'):
        Database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]=='11'
        assert 'scheme_id' not in {r[1] for r in conn.execute('PRAGMA table_info(entries)')}


def test_revisions_packages_immutable_and_binding_pair(storage):
    _,db,service,scheme,_=storage
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):
        db.conn.execute("UPDATE scheme_revisions SET definition_json='{}'")
    db.conn.rollback()
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):
        db.conn.execute("UPDATE adapter_packages SET package_version='9.0.0'")
    db.conn.rollback()
    other=service.copy_scheme(scheme['id'],'Other')
    assert other['current_revision_id']==scheme['current_revision_id']
    changed=service.update_scheme_settings(other['id'],other['current_revision_id'],{'print.numbering':False})
    target=entry(db,scheme)
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute('UPDATE entries SET scheme_revision_id=? WHERE id=?',(changed['current_revision_id'],target))
    db.conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute('UPDATE entries SET scheme_id=NULL WHERE id=?',(target,))
    db.conn.rollback()


def test_install_refreshes_digest_even_with_same_size_mtime(storage):
    _,db,service,_,path=storage
    preview=service.inspect_adapter(path)
    file=path/'manifest.json'
    stamp=file.stat()
    data=file.read_text().replace('Storage fixture','Changed fixture')
    assert len(data)==len(file.read_text())
    file.write_text(data)
    import os
    os.utime(file,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    before=db.conn.execute('SELECT COUNT(*) FROM schemes').fetchone()[0]
    with pytest.raises(ValueError,match='内容已经变化'):
        service.install_adapter(preview['preview_id'])
    assert db.conn.execute('SELECT COUNT(*) FROM schemes').fetchone()[0]==before


def test_changed_public_adapter_rejects_original_package_id(storage):
    _,_,service,scheme,_=storage
    changed=service.update_scheme_settings(scheme['id'],scheme['current_revision_id'],
                                           {'print.numbering':not scheme['definition']['effective_settings']['print.numbering']})
    with pytest.raises(ValueError,match='新的包 ID'):
        service.export_adapter(changed['id'],{'package_id':changed['package_id']})


def test_unchanged_metadata_is_ignored_and_export_keeps_original_digest(storage):
    _,_,service,scheme,_=storage
    source_hash=service.packages.revision_record(scheme['current_revision_id'])['content_hash']
    result=service.export_adapter(scheme['id'],{'package_id':scheme['package_id'],
        'name':scheme['definition']['manifest']['name']})
    assert result['changed'] is False
    exported=load_package(result['path'])
    assert exported.content_hash==source_hash
    assert exported.definition['manifest']['package_id']==scheme['package_id']


def test_unbound_source_adapter_values_are_read_only_form_history(storage,tmp_path):
    _,db,service,scheme,_=storage
    path=write_package(tmp_path/'entry-field','2.0.0',fields=[{'id':'purpose','scope':'entry',
        'label':'Purpose','type':'text','required_at':[],'presentation':'visible',
        'sensitive':False,'transfer':'include'}])
    preview=service.inspect_adapter(path)
    scheme=service.install_adapter(preview['preview_id'],{'mode':'update','scheme_id':scheme['id']})
    invoice=entry(db,scheme)
    source_definition=deepcopy(scheme['definition'])
    source_definition['manifest']['package_id']='org.example.previous'
    db.conn.execute('''INSERT INTO entry_adapter_sources VALUES(?,?,?,?,?)''',(invoice,'f'*64,scheme['current_revision_id'],
        json.dumps({'source_revision_id':scheme['current_revision_id'],'digest':'f'*64,'definition':source_definition,
            'extension_values':[{'scope':'entry','field_id':'purpose','package_id':'org.example.previous',
                                 'value':'Source purpose'}],'extension_history':[]}),
        '2026-10-06T00:00:00'))
    db.conn.commit()
    form=service.get_form_description('entry',invoice)
    assert 'purpose' not in form['values']
    historical=next(value for value in form['history_values'] if value['field_id']=='purpose')
    assert historical['value']=='Source purpose' and historical['read_only'] is True
    assert historical['source_digest']=='f'*64
    assert historical['definition_revision_id']==scheme['current_revision_id']


def test_same_package_version_different_contents_is_conflict(storage):
    _,_,service,_,path=storage
    manifest=json.loads((path/'manifest.json').read_text())
    manifest['name']='Other'
    (path/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='同版本'):
        service.inspect_adapter(path)


def test_updates_keep_local_override_new_defaults_and_old_entries(storage,tmp_path):
    _,db,service,scheme,_=storage
    invoice=entry(db,scheme)
    local=service.update_scheme_settings(scheme['id'],scheme['current_revision_id'],{'print.numbering':False})
    path=write_package(tmp_path/'new','2.0.0',settings={
        'print.numbering':{'default':True,'editable':True,'presentation':'visible'},
        'print.content_order':{'default':'role','editable':True,'presentation':'visible'}})
    preview=service.inspect_adapter(path)
    updated=service.install_adapter(preview['preview_id'],{'mode':'update','scheme_id':scheme['id']})
    assert updated['definition']['effective_settings']['print.numbering'] is False
    assert updated['definition']['effective_settings']['print.content_order']=='role'
    assert db.conn.execute('SELECT scheme_revision_id FROM entries WHERE id=?',(invoice,)).fetchone()[0]==scheme['current_revision_id']
    assert service.get_revision(local['current_revision_id'])['effective_settings']['print.numbering'] is False
    restored=service.rollback_scheme(scheme['id'],scheme['current_revision_id'],expected_revision=updated['current_revision_id'])
    assert restored['current_revision_id']==scheme['current_revision_id']
    assert not restored['overrides']['settings']


def test_new_fixed_setting_conflict_requires_resolution(storage,tmp_path):
    _,_,service,scheme,_=storage
    local=service.update_scheme_settings(scheme['id'],scheme['current_revision_id'],{'print.numbering':False})
    path=write_package(tmp_path/'fixed','2.0.0',settings={'print.numbering':{'fixed':True,'editable':False,'presentation':'visible'}})
    preview=service.inspect_adapter(path)
    assert any(c['id']=='setting:print.numbering' for c in preview['conflicts'][scheme['id']])
    with pytest.raises(ValueError,match='解决更新冲突'):
        service.install_adapter(preview['preview_id'],{'mode':'update','scheme_id':scheme['id']})
    updated=service.install_adapter(preview['preview_id'],{'mode':'update','scheme_id':scheme['id'],'resolutions':{'setting:print.numbering':'incoming'}})
    assert updated['definition']['effective_settings']['print.numbering'] is True
    with pytest.raises(ValueError,match='固定'):
        service.update_scheme_settings(scheme['id'],updated['current_revision_id'],{'print.numbering':False})


def test_setting_validation_and_head_optimism(storage):
    _,_,service,scheme,_=storage
    with pytest.raises(ValueError,match='类型'):
        service.update_scheme_settings(scheme['id'],scheme['current_revision_id'],{'print.numbering':'false'})
    with pytest.raises(ValueError,match='未注册'):
        service.update_scheme_settings(scheme['id'],scheme['current_revision_id'],{'network.secret':'x'})
    changed=service.update_scheme_settings(scheme['id'],scheme['current_revision_id'],{'print.numbering':False})
    with pytest.raises(ValueError,match='变化'):
        service.update_scheme_settings(scheme['id'],scheme['current_revision_id'],{'print.numbering':True})
    assert service.restore_setting_defaults(scheme['id'],changed['current_revision_id'])['definition']['effective_settings']['print.numbering'] is True


def test_copy_default_disable_preserves_old_references(storage):
    _,db,service,scheme,_=storage
    invoice=entry(db,scheme)
    account=service.save_payee(values={'name':'A','account_number':'00123'})
    service.set_scheme_payee(scheme['id'],account['id'])
    copy=service.copy_scheme(scheme['id'],'Local copy')
    assert copy['payee'] is None
    service.set_default_scheme(copy['id'])
    assert db.conn.execute('SELECT scheme_id FROM entries WHERE id=?',(invoice,)).fetchone()[0]==scheme['id']
    assert service.disable_scheme(scheme['id'])['disabled']
    assert service.context_for_entry(invoice)['manifest']['package_id']=='org.test.storage'
    with pytest.raises(ValueError,match='默认'):
        service.disable_scheme(copy['id'])


def test_payee_whole_objects_and_leading_zero(storage):
    _,db,service,scheme,_=storage
    profile=ProfileRepo(db).create('Same','')
    default=service.payees.create(name='Operator',account_number='',bank_name='')
    personal=service.payees.create(name='Claimant',account_number='0000123',bank_name='Personal bank',personnel_id='00005')
    service.set_scheme_payee(scheme['id'],default['id'])
    service.set_payee_mapping(scheme['id'],profile['id'],personal['id'])
    assert service.payees.resolve(scheme['id'],profile_id=profile['id'])['account_number']==''
    assert service.payees.resolve(scheme['id'],profile_id=profile['id'],mode='by_claimant')['account_number']=='0000123'
    assert service.payees.resolve(scheme['id'],profile_id='missing',mode='by_claimant') is None
    with pytest.raises(ValueError,match='仍被'):
        service.payees.delete(personal['id'])
    with pytest.raises(ValueError,match='字符串'):
        service.payees.create(account_number=123)


def test_forms_typed_values_optimistic_history_and_owner_cleanup(storage,tmp_path):
    _,db,service,scheme,_=storage
    path=write_package(tmp_path/'fields','2.0.0',fields=[{'id':'code','scope':'entry','label':'Code','type':'text','default':'fallback','required_at':[],'presentation':'visible','sensitive':False,'transfer':'include'}, {'id':'amount','scope':'entry','label':'Amount','type':'money','required_at':[],'presentation':'visible','sensitive':False,'transfer':'include'}])
    preview=service.inspect_adapter(path)
    updated=service.install_adapter(preview['preview_id'],{'mode':'update','scheme_id':scheme['id']})
    invoice=entry(db,updated)
    form=service.get_form_description('entry',invoice)
    saved=service.save_extension_values('entry',invoice,{'code':'','amount':'-0.0001'},expected_version=form['version'])
    assert saved['values']['code']==''
    assert saved['values']['amount']=='-0.0001'
    assert len(saved['history'])==2
    with pytest.raises(ValueError,match='修改'):
        service.save_extension_values('entry',invoice,{'code':'stale'},expected_version=form['version'])
    with pytest.raises(ValueError):
        service.save_extension_values('entry',invoice,{'amount':1.0})
    with pytest.raises(ValueError,match='不存在'):
        service.extensions.save_values('entry','orphan',updated['id'],updated['current_revision_id'],{'code':'bad'})
    with db.transaction():
        db.conn.execute('DELETE FROM entries WHERE id=?',(invoice,))
    assert not db.conn.execute("SELECT 1 FROM extension_values WHERE scope='entry'").fetchone()
    assert not db.conn.execute("SELECT 1 FROM extension_history WHERE scope='entry'").fetchone()


def test_batch_namespaces_independent_for_copies(storage,tmp_path):
    _,db,service,scheme,_=storage
    path=write_package(tmp_path/'batchfields','2.0.0',fields=[{'id':'code','scope':'batch','label':'Code','type':'text','required_at':[],'presentation':'visible','sensitive':False,'transfer':'never'}])
    p=service.inspect_adapter(path)
    scheme=service.install_adapter(p['preview_id'],{'mode':'update','scheme_id':scheme['id']})
    copy=service.copy_scheme(scheme['id'],'Copy')
    batch=BatchRepo(db).create('Batch')
    service.save_extension_values('batch',batch['id'],{'code':'Original'},scheme['id'])
    copy_form=service.get_form_description('batch',batch['id'],copy['id'])
    service.save_extension_values('batch',batch['id'],{'code':'Copy'},copy['id'],copy_form['version'])
    assert service.get_form_description('batch',batch['id'],scheme['id'])['values']['code']=='Original'
    assert service.get_form_description('batch',batch['id'],copy['id'])['values']['code']=='Copy'
    BatchRepo(db).delete(batch['id'])
    assert not db.conn.execute("SELECT 1 FROM extension_values WHERE scope='batch'").fetchone()


def test_typed_fields_work_for_every_owner_scope(storage):
    _,db,service,scheme,_=storage
    scopes=('scheme','payee','entry','batch','export')
    fields=[{'id':'amount_'+scope,'scope':scope,'label':scope,'type':'money','required_at':[],
             'presentation':'visible','sensitive':False,'transfer':'include'} for scope in scopes]
    # Keep this fixture self-contained while ensuring all owners bind the same immutable definition.
    definition=deepcopy(scheme['definition'])
    definition['fields']=fields
    revision=service.packages.store_revision(definition,
        service.packages.revision_record(scheme['current_revision_id'])['content_hash'])
    scheme=service.packages.set_current_revision(scheme['id'],revision)
    invoice=entry(db,scheme)
    batch=BatchRepo(db).create('Typed')
    payee=service.save_payee(values={'name':'Fixture'})
    export=service.jobs.create()
    owners={'scheme':scheme['id'],'payee':payee['id'],'entry':invoice,'batch':batch['id'],'export':export['id']}
    for scope,owner_id in owners.items():
        result=service.save_extension_values(scope,owner_id,{'amount_'+scope:'12.30'},scheme['id'])
        assert result['values']['amount_'+scope]=='12.30'
        assert service.get_form_description(scope,owner_id,scheme['id'])['definition']['fields']
    assert db.conn.execute('SELECT COUNT(*) FROM extension_values').fetchone()[0]==5


def test_resource_gc_requires_fresh_explicit_preview_and_preserves_referenced(storage):
    _,_,service,scheme,_=storage
    referenced=service.packages.revision_record(scheme['current_revision_id'])['content_hash']
    orphan='a'*64
    orphan_path=service.data_root.package_dir(orphan)
    orphan_path.mkdir(parents=True)
    (orphan_path/'unused.bin').write_bytes(b'not referenced')
    preview=service.preview_resource_gc()
    assert [item['content_hash'] for item in preview['candidates']]==[orphan]
    with pytest.raises(ValueError,match='失效'):
        service.cleanup_unreferenced_resources('invalid')
    (orphan_path/'new.bin').write_bytes(b'changed after preview')
    with pytest.raises(ValueError,match='变化'):
        service.cleanup_unreferenced_resources(preview['preview_id'])
    preview=service.preview_resource_gc()
    result=service.cleanup_unreferenced_resources(preview['preview_id'])
    assert result['removed']==[orphan]
    assert not orphan_path.exists()
    assert service.data_root.package_dir(referenced).exists()


def test_local_template_validation_cache_is_separate_from_immutable_package(storage):
    _,db,service,_,_=storage
    source=Path(__file__).parents[1]/'tidoc'/'builtin_adapters'/'org.tidoc.generic'
    package=load_package(source)
    prepared=service._prepare_package(package,'test')
    with db.transaction():
        service._record_package(package,prepared,'test')
    service._finish_operation(prepared)
    result=service.revalidate_templates('print-component-fixture')
    assert len(result['templates'])==1
    row=db.conn.execute('SELECT status,component_fingerprint FROM template_validation_cache').fetchone()
    assert row['status'] in ('valid','invalid','pending')
    assert row['component_fingerprint']=='print-component-fixture'
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):
        db.conn.execute("UPDATE adapter_packages SET diagnostics_json='[]'")
    db.conn.rollback()


def test_rebind_stale_and_atomic_failure(storage,monkeypatch):
    _,db,service,scheme,_=storage
    invoice=entry(db,scheme)
    changed=service.update_scheme_settings(scheme['id'],scheme['current_revision_id'],{'print.numbering':False})
    preview=service.preview_rebind([invoice],scheme['id'])
    db.conn.execute("UPDATE entry_fields SET current='changed' WHERE entry_id=? AND field='notes'",(invoice,));db.conn.commit()
    with pytest.raises(ValueError,match='已变化'):
        service.apply_rebind(preview['preview_id'])
    preview=service.preview_rebind([invoice],scheme['id'])
    from tidoc.db.entries import EntryRepo
    def fail(*args,**kwargs):
        raise OSError('injected status failure')
    monkeypatch.setattr(EntryRepo,'recompute_status',fail)
    with pytest.raises(OSError):
        service.apply_rebind(preview['preview_id'])
    assert db.conn.execute('SELECT scheme_revision_id FROM entries WHERE id=?',(invoice,)).fetchone()[0]==scheme['current_revision_id']
    assert not service.extensions.history('entry',invoice)


def test_rebind_removed_fields_keeps_read_only_history(storage,tmp_path):
    _,db,service,scheme,_=storage
    fields=[{'id':'code','scope':'entry','label':'Code','type':'text','required_at':[],'presentation':'visible','sensitive':False,'transfer':'include'}]
    p=service.inspect_adapter(write_package(tmp_path/'v2','2.0.0',fields=fields))
    scheme=service.install_adapter(p['preview_id'],{'mode':'update','scheme_id':scheme['id']})
    invoice=entry(db,scheme)
    service.save_extension_values('entry',invoice,{'code':'Original'})
    p=service.inspect_adapter(write_package(tmp_path/'v3','3.0.0'))
    updated=service.install_adapter(p['preview_id'],{'mode':'update','scheme_id':scheme['id'],'resolutions':{'fields:entry:code':'history'}})
    rebind=service.preview_rebind([invoice],scheme['id'])
    assert rebind['entries'][0]['history_values'][0]['value']=='Original'
    service.apply_rebind(rebind['preview_id'])
    form=service.get_form_description('entry',invoice)
    assert not form['values']
    assert form['history_values'][0]['value']=='Original'
    assert any(h['kind']=='rebind' for h in form['history'])


def test_explicit_material_role_mapping_counts_target_role(storage,tmp_path):
    _,db,service,_,_=storage
    old_role='custom:org.test.storage:old_document'
    new_role='custom:org.test.storage:new_document'
    role=lambda role_id:{'id':role_id,'label':role_id.rsplit(':',1)[-1],
        'extensions':['.pdf'],'min_count':1,'max_count':3,'order':1,
        'quick_action':True,'reclassifiable':True,'presentation':'visible'}
    path=write_package(tmp_path/'custom-role','2.0.0',roles=[
        {'id':'invoice','label':'发票','extensions':['.pdf','.xml'],'min_count':1,
         'max_count':None,'order':0,'quick_action':True,'reclassifiable':False,'presentation':'visible'},
        role(old_role)])
    preview=service.inspect_adapter(path)
    scheme=service.install_adapter(preview['preview_id'])
    invoice=entry(db,scheme)
    db.conn.execute('''INSERT INTO attachments(id,entry_id,type,original_name,stored_path,sha256,
        role_id,role_definition_revision_id,added_at) VALUES('custom-doc',?,'other','contract.pdf',
        'contract.pdf','digest',?,?, 'now')''',(invoice,old_role,scheme['current_revision_id']))
    db.conn.commit()
    target_definition=deepcopy(scheme['definition'])
    target_definition['materials']=[role(new_role) if item['id']==old_role else item
                                    for item in target_definition['materials']]
    target_revision=service.packages.store_revision(target_definition,
        service.packages.revision_record(scheme['current_revision_id'])['content_hash'])
    service.packages.set_current_revision(scheme['id'],target_revision)
    preview=service.preview_rebind([invoice],scheme['id'],target_revision,
                                   {'materials':{old_role:new_role}})
    mapped=preview['entries'][0]['attachments'][0]
    assert mapped['role_id']==new_role and mapped['type']=='other'
    assert not any(d.get('code')=='MATERIAL_REQUIRED' and d.get('target')==new_role
                   for d in preview['entries'][0]['diagnostics'])


def test_export_jobs_freeze_and_reference_revisions(storage):
    _,db,_,scheme,_=storage
    jobs=ExportJobRepo(db)
    job=jobs.create({'payee':{'account_number':'0001'}},[scheme['current_revision_id']],resources=[{'sha256':'x'}])
    jobs.complete(job['id'],[{'name':'result.docx'}])
    with pytest.raises(ValueError,match='不可修改'):
        jobs.update(job['id'],snapshot={'payee':{'account_number':'different'}})
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute('DELETE FROM scheme_revisions WHERE revision_id=?',(scheme['current_revision_id'],))
    db.conn.rollback()
    assert jobs.get(job['id'])['snapshot']['payee']['account_number']=='0001'
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):
        db.conn.execute("UPDATE export_jobs SET snapshot_json='{}' WHERE id=?",(job['id'],))
    db.conn.rollback()


def test_data_root_migration_rolls_back_all_directories(tmp_path,monkeypatch):
    root=DataRoot(tmp_path/'old')
    (root.adapter_packages_dir/'retained').mkdir()
    (root.export_jobs_dir/'job').mkdir()
    (root.backups_dir/'backup').write_text('backup')
    import shutil
    real=shutil.move
    calls=[]
    def fail(source,target):
        calls.append(source)
        if len(calls)==3:
            raise OSError('move failed')
        return real(source,target)
    monkeypatch.setattr(shutil,'move',fail)
    with pytest.raises(OSError):
        root.migrate_to(tmp_path/'new')
    assert (root.adapter_packages_dir/'retained').exists()
    assert (root.export_jobs_dir/'job').exists()
    assert (root.backups_dir/'backup').read_text()=='backup'
    monkeypatch.setattr(shutil,'move',real)
    new=root.migrate_to(tmp_path/'new')
    assert (new/'adapters/packages/retained').exists()
    assert (new/'export_jobs/job').exists()
    assert (new/'backups/backup').exists()


def test_legacy_preference_forwarding_whole_payee(storage):
    _,_,service,scheme,_=storage
    service.update_legacy_preference('tidoc.operator.bank_card','000123')
    service.update_legacy_preference('tidoc.operator.name','')
    payee=service.get_scheme()['payee']
    assert payee['name']=='' and payee['account_number']=='000123'
    service.update_legacy_preference('tidoc.defaultPaidToInvoiceTotal','0')
    assert service.get_scheme()['definition']['effective_settings']['entry.default_paid_to_invoice'] is False
    with pytest.raises(ValueError):
        service.update_legacy_preference('tidoc.ocr.secret','never')


def test_legacy_unified_payee_migrates_partial_values_without_joining(storage):
    _,db,service,scheme,_=storage
    service._migrate_payees(scheme['id'],{'tidoc.operator.name':'Only Name'})
    payee=service.get_scheme(scheme['id'])['payee']
    assert payee['name']=='Only Name'
    assert payee['personnel_id']==payee['contact']==payee['bank_name']==payee['account_number']==''
    assert db.conn.execute('SELECT COUNT(*) FROM payees').fetchone()[0]==1


def test_legacy_unified_payee_empty_preferences_do_not_create_record(storage):
    _,db,service,scheme,_=storage
    service._migrate_payees(scheme['id'],{'tidoc.operator.name':'','tidoc.operator.bank_card':''})
    assert service.get_scheme(scheme['id'])['payee'] is None
    assert db.conn.execute('SELECT COUNT(*) FROM payees').fetchone()[0]==0


def test_adapter_operation_inspection_status_and_cancellation(storage,monkeypatch):
    _,_,service,_,path=storage
    operation_id='inspect-test-uuid'
    preview=service.inspect_adapter(path,operation_id)
    status=service.operation_status(operation_id)
    assert preview['ok']
    assert status=={'operation_id':operation_id,'status':'completed','stage':'compare_schemes',
                    'message':'正在比对现有方案','completed':1,'total':1}
    import tidoc.adapters.loader as loader
    real=loader.load_package
    def cancel_during_inspect(*args,**kwargs):
        package=real(*args,**kwargs)
        service.cancel_operation('cancel-inspect')
        return package
    monkeypatch.setattr(loader,'load_package',cancel_during_inspect)
    with pytest.raises(RuntimeError,match='操作已取消'):
        service.inspect_adapter(path,operation_id='cancel-inspect')
    assert service.operation_status('cancel-inspect')['status']=='cancelled'


def test_cancelled_install_rolls_back_database_and_staged_resources(storage,tmp_path,monkeypatch):
    _,db,service,_,_=storage
    path=write_package(tmp_path/'cancel-install','2.0.0')
    preview=service.inspect_adapter(path)
    package=service._previews[preview['preview_id']]['package']
    real=service._record_package
    def cancel_after_record(*args,**kwargs):
        result=real(*args,**kwargs)
        service.cancel_operation('cancel-install')
        return result
    monkeypatch.setattr(service,'_record_package',cancel_after_record)
    with pytest.raises(RuntimeError,match='操作已取消'):
        service.install_adapter(preview['preview_id'],operation_id='cancel-install')
    assert service.packages.get_package(package.content_hash) is None
    assert not service.data_root.package_dir(package.content_hash).exists()
    assert service.operation_status('cancel-install')['status']=='cancelled'


def test_install_resource_progress_and_cancellation_use_requested_operation(storage,tmp_path,monkeypatch):
    _,db,service,_,_=storage
    path=write_package(tmp_path/'cancel-resources','2.0.0')
    preview=service.inspect_adapter(path)
    package=service._previews[preview['preview_id']]['package']
    before=db.conn.execute('SELECT COUNT(*) FROM schemes').fetchone()[0]
    checkpoint=service._operation_checkpoint
    observed=[]
    def cancel_after_first_resource(operation_id,stage=None,current=None,total=None):
        if stage=='write_resources' and current==1:
            observed.append(service.operation_status(operation_id))
            service.cancel_operation(operation_id)
        checkpoint(operation_id,stage,current,total)
    monkeypatch.setattr(service,'_operation_checkpoint',cancel_after_first_resource)
    with pytest.raises(RuntimeError,match='操作已取消'):
        service.install_adapter(preview['preview_id'],operation_id='cancel-resources')
    assert observed and observed[0]['operation_id']=='cancel-resources'
    assert observed[0]['status']=='running' and observed[0]['stage']=='write_resources'
    assert observed[0]['total']==len(package.files)
    assert service.operation_status('cancel-resources')['status']=='cancelled'
    assert service.packages.get_package(package.content_hash) is None
    assert db.conn.execute('SELECT COUNT(*) FROM schemes').fetchone()[0]==before
    assert not service.data_root.package_dir(package.content_hash).exists()
    assert not list(service.data_root.adapter_staging_dir.iterdir())


def test_cancelled_rebind_rolls_back_prior_entries(storage,monkeypatch):
    _,db,service,scheme,_=storage
    invoices=[entry(db,scheme),entry(db,scheme)]
    target=service.copy_scheme(scheme['id'],'Rebind target')
    preview=service.preview_rebind(invoices,target['id'])
    real=service._operation_checkpoint
    cancelled=False
    def cancel_after_one(operation_id,stage=None,current=None,total=None):
        nonlocal cancelled
        real(operation_id,stage,current,total)
        if operation_id=='cancel-rebind' and stage=='rebind_entries' and current==1 and not cancelled:
            cancelled=True
            service.cancel_operation(operation_id)
    monkeypatch.setattr(service,'_operation_checkpoint',cancel_after_one)
    with pytest.raises(RuntimeError,match='操作已取消'):
        service.apply_rebind(preview['preview_id'],'cancel-rebind')
    assert db.conn.execute('SELECT COUNT(*) FROM entries WHERE scheme_id=?',(scheme['id'],)).fetchone()[0]==2
    assert not service.extensions.history('entry',invoices[0])
    assert service.operation_status('cancel-rebind')['status']=='cancelled'


def test_deleting_export_job_record_leaves_output_files_for_explicit_api_cleanup(storage):
    _,_,service,_,_=storage
    job=service.jobs.create()
    result_dir=service.data_root.job_dir(job['id'])
    result_dir.mkdir()
    artifact=result_dir/'result.xlsx'
    artifact.write_bytes(b'export')
    service.jobs.delete(job['id'])
    with pytest.raises(ValueError,match='不存在'):
        service.jobs.get(job['id'])
    assert artifact.read_bytes()==b'export'

"""Package lifecycle, immutable revisions, migration and typed entity forms."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import inspect
import json
import sqlite3
import os
from pathlib import Path
import shutil
import threading
import uuid
from functools import wraps

from ..db.adapters import AdapterRepo, encode, now
from ..db.payees import PayeeRepo
from ..db.extensions import ExtensionRepo
from ..db.export_jobs import ExportJobRepo
from ..db.paths import DataRoot
from .cache import PreviewCache

LEGACY_KEYS = (
    'tidoc.titleProfiles','tidoc.materialRequirements','tidoc.defaultEntryTitle',
    'tidoc.defaultPaidToInvoiceTotal','tidoc.multiClaimantMode','tidoc.paymentScreenshotOcr',
    'tidoc.bindle.includeNotes','tidoc.bindle.includeTags',
    'tidoc.operator.name','tidoc.operator.student_id','tidoc.operator.contact',
    'tidoc.operator.bank_name','tidoc.operator.bank_card',
    'tidoc.print.numbering','tidoc.print.imageLayout','tidoc.print.contentOrder',
    'tidoc.print.amountBasis','tidoc.print.storageLocation','tidoc.print.defaultOutputs',
)
PREF_SETTINGS = {
    'tidoc.defaultPaidToInvoiceTotal':'entry.default_paid_to_invoice',
    'tidoc.bindle.includeNotes':'transfer.include_notes',
    'tidoc.bindle.includeTags':'transfer.include_tags',
    'tidoc.print.numbering':'print.numbering',
    'tidoc.print.imageLayout':'print.image_layout',
    'tidoc.print.contentOrder':'print.content_order',
    'tidoc.print.amountBasis':'print.amount_basis',
    'tidoc.print.defaultOutputs':'print.default_outputs',
}


def _digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def _tax(value):
    return ''.join(c for c in str(value or '').upper() if c.isalnum())


class AdapterOperationCancelled(RuntimeError):
    """Raised at a safe checkpoint when the caller cancels a local operation."""


def _tracked_operation(initial_stage):
    def decorate(method):
        signature=inspect.signature(method)
        @wraps(method)
        def wrapped(self,*args,operation_id=None,**kwargs):
            if operation_id is not None:
                kwargs['operation_id']=operation_id
            bound=signature.bind(self,*args,**kwargs)
            operation_id=bound.arguments.get('operation_id')
            if operation_id:
                self._start_operation(operation_id,initial_stage)
            try:
                result=method(*bound.args,**bound.kwargs)
                if operation_id:
                    self._finish_tracked_operation(operation_id,'completed')
                return result
            except AdapterOperationCancelled as exc:
                if operation_id:
                    self._finish_tracked_operation(operation_id,'cancelled',str(exc))
                raise
            except BaseException as exc:
                if operation_id:
                    self._finish_tracked_operation(operation_id,'failed',str(exc))
                raise
        return wrapped
    return decorate


class AdapterService:
    def __init__(self, db, data_root):
        self.db=db
        self.data_root=data_root if isinstance(data_root,DataRoot) else DataRoot(data_root)
        self.packages=AdapterRepo(db)
        self.payees=PayeeRepo(db)
        self.extensions=ExtensionRepo(db)
        self.jobs=ExportJobRepo(db)
        # Previews are short-lived and bounded; an expired one is reported as needing a new preview.
        self._previews=PreviewCache()
        self._rebind_previews=PreviewCache()
        self._resource_gc_previews=PreviewCache()
        self._operation_lock=threading.RLock()
        self._operations={}
        db.adapter_service=self

    def _start_operation(self,operation_id,stage):
        with self._operation_lock:
            current=self._operations.get(operation_id)
            if current and current['status']=='running':
                raise ValueError('该操作编号正在使用。')
            stamp=now()
            self._operations[operation_id]={'operation_id':operation_id,'status':'running','stage':stage,
                'progress':0.0,'current':0,'total':None,'cancel_requested':False,
                'started_at':stamp,'updated_at':stamp,'error':None}

    def _set_operation_progress(self,operation_id,stage,current=None,total=None):
        if not operation_id:
            return
        with self._operation_lock:
            state=self._operations.get(operation_id)
            if not state or state['status']!='running':
                return
            state['stage']=str(stage)
            if current is not None:
                state['current']=max(0,int(current))
            if total is not None:
                state['total']=max(0,int(total))
            if state['total']:
                state['progress']=min(0.99,max(0.0,state['current']/state['total']))
            state['updated_at']=now()

    def _operation_checkpoint(self,operation_id,stage=None,current=None,total=None):
        if not operation_id:
            return
        if stage is not None:
            self._set_operation_progress(operation_id,stage,current,total)
        with self._operation_lock:
            state=self._operations.get(operation_id)
            if state and state['cancel_requested']:
                raise AdapterOperationCancelled('操作已取消，未提交的更改已回滚。')

    def _finish_tracked_operation(self,operation_id,status,error=None):
        with self._operation_lock:
            state=self._operations.get(operation_id)
            if not state:
                return
            state['status']=status
            state['error']=error
            if status=='completed':
                state['progress']=1.0
            state['updated_at']=now()

    def operation_status(self,operation_id):
        with self._operation_lock:
            state=self._operations.get(operation_id)
            if not state:
                return None
            return {'operation_id':operation_id,'status':state['status'],'stage':state['stage'],
                    'message':state.get('error') or self._operation_message(state['stage']),'completed':state['current'],
                    'total':state['total']}

    get_operation_status=operation_status

    @staticmethod
    def _operation_message(stage):
        return {'inspect_package':'正在检查适配包','compare_schemes':'正在比对现有方案',
            'verify_preview':'正在复核安装预览','resolve_definition':'正在解析适配方案',
            'write_resources':'正在写入适配资源','commit_install':'正在保存方案',
            'validate_rebind':'正在校验重绑定预览','rebind_entries':'正在逐条重绑定材料与字段'}.get(stage,stage)

    def cancel_operation(self,operation_id):
        with self._operation_lock:
            state=self._operations.get(operation_id)
            if not state:
                raise ValueError('操作不存在或已过期。')
            if state['status']!='running':
                return {'operation_id':operation_id,'status':state['status'],'stage':state['stage'],
                        'message':state.get('error') or self._operation_message(state['stage']),'completed':state['current'],
                        'total':state['total']}
            state['cancel_requested']=True
            state['updated_at']=now()
            return {'operation_id':operation_id,'status':'running','stage':state['stage'],
                    'message':'正在请求取消','completed':state['current'],'total':state['total']}

    def _meta(self,key,default=None):
        row=self.db.conn.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return row[0] if row else default

    def _set_meta(self,key,value):
        self.db.conn.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))

    def _preferences(self):
        return {r[0]:r[1] for r in self.db.conn.execute('SELECT key,value FROM meta') if r[0] in LEGACY_KEYS}

    def _write_json(self,path,value):
        path=Path(path)
        temporary=path.with_suffix(path.suffix+'.tmp')
        with temporary.open('w',encoding='utf-8') as handle:
            handle.write(encode(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary,path)

    def _recover_operations(self):
        for path in self.data_root.adapter_imports_dir.glob('*.json'):
            try:
                operation=json.loads(path.read_text(encoding='utf-8'))
            except (ValueError,OSError):
                continue
            if operation.get('state') in ('committed','recovered'):
                continue
            # A crash may occur after commit but before the journal refresh.
            # Recover resource ownership only; never replay the user's install.
            package=self.packages.get_package(operation.get('content_hash'))
            operation['state']='committed' if package else 'recovered'
            stage=self.data_root.adapter_staging_dir / path.stem
            if stage.is_dir():
                shutil.rmtree(stage)
            if not package:
                self._remove_unreferenced_package_resource(operation.get('content_hash'))
            self._write_json(path,operation)

    def _remove_unreferenced_package_resource(self, content_hash):
        if not isinstance(content_hash,str) or len(content_hash)!=64 or any(c not in '0123456789abcdef' for c in content_hash):
            return False
        if self.packages.get_package(content_hash):
            return False
        target=self.data_root.package_dir(content_hash)
        if target.is_dir():
            shutil.rmtree(target)
            return True
        return False

    def _cleanup_failed_package_operation(self, prepared):
        target,journal,record=prepared
        try:
            if target.is_dir():
                self._remove_unreferenced_package_resource(record.get('content_hash'))
            stage=self.data_root.adapter_staging_dir/record.get('operation_id','')
            if stage.is_dir():
                shutil.rmtree(stage)
            record['state']='failed-cleaned'
            self._write_json(journal,record)
        except OSError:
            record['state']='cleanup-pending'
            self._write_json(journal,record)

    def _prepare_package(self,package,source,operation_id=None):
        journal_id=uuid.uuid4().hex
        stage=self.data_root.adapter_staging_dir/journal_id
        target=self.data_root.package_dir(package.content_hash)
        journal=self.data_root.adapter_imports_dir/(journal_id+'.json')
        record={'operation_id':journal_id,'content_hash':package.content_hash,
                'staging':str(stage.relative_to(self.data_root.root)),
                'target':str(target.relative_to(self.data_root.root)), 'source':source,'state':'preparing'}
        self._write_json(journal,record)
        try:
            stage.mkdir()
            total=len(package.files)
            for index,(name,data) in enumerate(package.files.items(),1):
                self._operation_checkpoint(operation_id,'write_resources',index-1,total)
                destination=stage/name
                if Path(name).is_absolute() or '..' in Path(name).parts or not destination.resolve().is_relative_to(stage.resolve()):
                    raise ValueError('适配资源路径无效。')
                destination.parent.mkdir(parents=True,exist_ok=True)
                with destination.open('wb') as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._operation_checkpoint(operation_id,'write_resources',index,total)
            record['state']='prepared'
            self._write_json(journal,record)
            if target.exists():
                # Repair missing/damaged resources only from a freshly verified
                # identical package; never replace a referenced valid directory.
                for name,data in package.files.items():
                    existing=target/name
                    if not existing.is_file() or existing.read_bytes()!=data:
                        raise ValueError('已安装适配资源缺失或损坏，请恢复数据备份。')
                shutil.rmtree(stage)
            else:
                os.replace(stage,target)
            record['state']='resources_ready'
            self._write_json(journal,record)
            return target,journal,record
        except BaseException:
            record['state']='cleanup-pending'
            self._write_json(journal,record)
            if stage.exists():
                shutil.rmtree(stage)
            if target.is_dir():
                self._remove_unreferenced_package_resource(package.content_hash)
            raise

    def _record_package(self,package,prepared,source='local'):
        target,journal,record=prepared
        package.definition['resource_hashes']={name:hashlib.sha256(data).hexdigest() for name,data in package.files.items() if name.startswith(('templates/','assets/'))}
        self.packages.store_package(package,target.relative_to(self.data_root.root),source,commit=False)

    def _finish_operation(self,prepared):
        _,journal,record=prepared
        record['state']='committed'
        self._write_json(journal,record)

    def bootstrap(self):
        from .loader import load_package
        from .resolver import resolve_definition
        self._recover_operations()
        builtin_root=Path(__file__).resolve().parents[1]/'builtin_adapters'
        paths=sorted(p.parent for p in builtin_root.rglob('manifest.json'))
        if not paths:
            raise ValueError('内置报账方案资源缺失，无法安全初始化。')
        builtin={}
        prepared=[]
        for path in paths:
            package=load_package(path,verify_templates=False)
            builtin[package.definition['manifest']['package_id']]=package
            prepared.append((package,self._prepare_package(package,'builtin')))
        if not {'org.tidoc.generic','org.bitfsae.reimbursement'} <= set(builtin):
            raise ValueError('内置通用或兼容方案缺失。')
        initialized=self._meta('adapter.bootstrap.complete')=='1'
        # The schema upgrade of this start already saved the same data; an empty database has nothing to restore.
        if (not initialized and self.db.db_path != ':memory:' and not self.db.migration_backup
                and self._has_legacy_data()):
            self.db.backup(self.data_root.backups_dir/('tidoc-before-adapters-'+uuid.uuid4().hex+'.sqlite'))
        with self.db.transaction():
            for package,resource in prepared:
                self._record_package(package,resource,'builtin')
            if not initialized:
                legacy=self._has_legacy_data()
                schemes={}
                for package_id in ('org.tidoc.generic','org.bitfsae.reimbursement'):
                    package=builtin[package_id]
                    definition=resolve_definition(package.definition)
                    revision=self.packages.store_revision(definition,package.content_hash,commit=False)
                    schemes[package_id]=self.packages.create_scheme(package.definition['manifest']['name'],revision,is_default=not legacy and package_id=='org.tidoc.generic',commit=False)
                if legacy:
                    package=builtin['org.bitfsae.reimbursement']
                    overrides=self._legacy_overrides(package.definition,self._preferences())
                    definition=self._resolve(package.definition,overrides)
                    revision=self.packages.store_revision(definition,package.content_hash,commit=False)
                    migrated=self.packages.create_scheme('原有报账方案',revision,overrides=overrides,is_default=True,commit=False)
                    self._set_meta('adapter.legacy.scheme_id',migrated['id'])
                    self._bind_legacy(migrated['id'],revision,definition)
                    self._migrate_payees(migrated['id'],self._preferences())
                    self._set_meta('adapter.setup.state','legacy_pending')
                else:
                    self._set_meta('adapter.setup.state','choose')
                self._set_meta('adapter.bootstrap.complete','1')
                self._set_meta('adapter.legacy','1' if legacy else '0')
        for _,resource in prepared:
            self._finish_operation(resource)
        self._follow_default_changes()
        return self.setup_state()

    # 核心默认值调整后，没人改过该设置的已有方案跟着新默认走（只影响之后新建的条目，已有条目沿用创建时的修订）。
    # 用户明确选过值、或方案包自己规定了值的，保持不动。每项调整只执行一次。
    DEFAULT_CHANGES=(('adapter.defaults.payment_ocr_manual','assist.payment_ocr','local'),)

    def _follow_default_changes(self):
        for meta_key,setting,old_default in self.DEFAULT_CHANGES:
            if self._meta(meta_key)=='1':
                continue
            for item in self.packages.list_schemes(True):
                try:
                    scheme=self.get_scheme(item['id'])
                    if setting in scheme['overrides'].get('settings',{}):
                        continue
                    if scheme['definition']['scheme'].get('settings',{}).get(setting):
                        continue
                    if scheme['definition']['effective_settings'].get(setting)==old_default:
                        self._change_definition(scheme['id'],scheme['current_revision_id'],deepcopy(scheme['overrides']))
                except (ValueError,KeyError,sqlite3.Error):
                    # 某个方案无法跟随新默认（例如包文件缺失）时保持原样，不能因此影响软件启动。
                    continue
            with self.db.transaction():
                self._set_meta(meta_key,'1')

    def _has_legacy_data(self):
        return bool(self._preferences()) or any(
            self.db.conn.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone()
            for table in ('entries','profiles','batches'))

    def _legacy_overrides(self,base,prefs):
        overrides={'settings':{}}
        if 'tidoc.titleProfiles' in prefs:
            titles=json.loads(prefs['tidoc.titleProfiles']) if prefs['tidoc.titleProfiles'] else []
            if not isinstance(titles,list):
                raise ValueError('旧抬头配置无效，已保留备份，请修正后继续迁移。')
            overrides['titles']=self._normalize_titles(titles,base['scheme'].get('titles',[]))
        for key,setting in PREF_SETTINGS.items():
            if key not in prefs:
                continue
            value=prefs[key]
            if setting in ('entry.default_paid_to_invoice','transfer.include_notes','transfer.include_tags','print.numbering'):
                value=self._legacy_bool(value)
            elif setting=='print.default_outputs':
                value=json.loads(value) if isinstance(value,str) else value
            overrides['settings'][setting]=value
        if 'tidoc.multiClaimantMode' in prefs:
            overrides['settings']['profile.default_view']='delegate' if self._legacy_bool(prefs['tidoc.multiClaimantMode']) else 'self'
        if 'tidoc.paymentScreenshotOcr' in prefs:
            overrides['settings']['assist.payment_ocr']='local' if self._legacy_bool(prefs['tidoc.paymentScreenshotOcr']) else 'manual'
        if 'tidoc.defaultEntryTitle' in prefs:
            name=prefs['tidoc.defaultEntryTitle']
            candidates=[t for t in overrides.get('titles',base['scheme'].get('titles',[])) if t['name']==name]
            overrides['settings']['entry.default_title_id']=candidates[0]['id'] if len(candidates)==1 else None
        if 'tidoc.materialRequirements' in prefs:
            requirements=json.loads(prefs['tidoc.materialRequirements']) if prefs['tidoc.materialRequirements'] else {}
            overrides.update(self._requirements_overrides(base,requirements))
        if 'tidoc.print.storageLocation' in prefs:
            overrides['organization']={'storage_location':prefs['tidoc.print.storageLocation']}
        return overrides

    @staticmethod
    def _legacy_bool(value):
        if type(value) is bool:
            return value
        if value in ('1','true',1):
            return True
        if value in ('0','false','',0,None):
            return False
        raise ValueError('旧设置布尔值无效。')

    def _migrate_payees(self,scheme_id,prefs):
        if self._meta('adapter.payees.migrated')=='1':
            return
        keys={'name':'tidoc.operator.name','personnel_id':'tidoc.operator.student_id','contact':'tidoc.operator.contact','bank_name':'tidoc.operator.bank_name','account_number':'tidoc.operator.bank_card'}
        if any(str(prefs.get(k) or '').strip() for k in keys.values()):
            values={k:prefs.get(v,'') for k,v in keys.items()}
            payee=self.payees.create(**values,commit=False)
            self.payees.set_default(scheme_id,payee['id'],commit=False)
            self._set_meta('adapter.legacy.operator_payee_id',payee['id'])
        for row in self.db.conn.execute('SELECT * FROM profiles'):
            # A profile has a name even when account fields were never filled;
            # only migrate an actual personal payment object.
            if any(row[k] for k in ('student_id','contact','bank_name','bank_card')):
                payee=self.payees.create(name=row['name'],personnel_id=row['student_id'] or '',contact=row['contact'] or '',bank_name=row['bank_name'] or '',account_number=row['bank_card'] or '',commit=False)
                self.payees.set_mapping(scheme_id,row['id'],payee['id'],commit=False)
        self._set_meta('adapter.payees.migrated','1')

    def _bind_legacy(self,scheme_id,revision,definition,replace=False):
        sql='SELECT * FROM entries' if replace else 'SELECT * FROM entries WHERE scheme_id IS NULL'
        for row in self.db.conn.execute(sql).fetchall():
            title_id=self._match_title(dict(row),definition)
            self.db.conn.execute('UPDATE entries SET scheme_id=?,scheme_revision_id=?,title_profile_id=?,status_engine_version=? WHERE id=?', (scheme_id,revision,title_id,'',row['id']))
        self.db.conn.execute('UPDATE batches SET default_scheme_id=?,default_revision_id=?'+('' if replace else ' WHERE default_scheme_id IS NULL'),(scheme_id,revision))

    @staticmethod
    def _match_title(entry,definition):
        name=(entry.get('buyer_name') or entry.get('title') or '').strip()
        tax=_tax(entry.get('buyer_tax_id'))
        candidates=[t for t in definition['scheme'].get('titles',[]) if t['name'].strip()==name and (not tax or _tax(t.get('tax_id'))==tax)]
        return candidates[0]['id'] if len(candidates)==1 else ''

    def setup_state(self):
        state=self._meta('adapter.setup.state','choose')
        scheme_id,revision=self.default_binding()
        return {'state':state,'ready':state=='ready','needs_selection':state=='choose',
                'needs_legacy_preferences':state=='legacy_pending','legacy':self._meta('adapter.legacy')=='1',
                'legacy_preference_keys':list(LEGACY_KEYS),'default_scheme_id':scheme_id,'revision_id':revision}

    adapter_setup_state=setup_state

    def complete_adapter_setup(self,scheme_id=None,legacy_preferences=None):
        state=self.setup_state()
        if state['ready']:
            return state
        if legacy_preferences is not None and (not isinstance(legacy_preferences,dict) or set(legacy_preferences)-set(LEGACY_KEYS)):
            raise ValueError('迁移桥只允许报账业务设置，不接收主题、路径或密钥。')
        with self.db.transaction():
            if state['needs_legacy_preferences']:
                migrated_id=self._meta('adapter.legacy.scheme_id')
                original=self.packages.get_scheme(migrated_id)
                for key,value in (legacy_preferences or {}).items():
                    if self._meta(key) is None:
                        self._set_meta(key,encode(value) if isinstance(value,(list,dict)) else ('1' if value is True else '0' if value is False else value))
                record=self.packages.revision_record(original['current_revision_id'])
                base=self.packages.get_package(record['content_hash'])['definition']
                overrides=self._legacy_overrides(base,self._preferences())
                definition=self._resolve(base,overrides)
                revision=self.packages.store_revision(definition,record['content_hash'],commit=False)
                self.packages.set_current_revision(migrated_id,revision,overrides=overrides,commit=False)
                self._bind_legacy(migrated_id,revision,definition,replace=True)
                operator_id=self._meta('adapter.legacy.operator_payee_id')
                operator_keys={'name':'tidoc.operator.name','personnel_id':'tidoc.operator.student_id','contact':'tidoc.operator.contact','bank_name':'tidoc.operator.bank_name','account_number':'tidoc.operator.bank_card'}
                prefs=self._preferences()
                values={k:prefs.get(v,'') for k,v in operator_keys.items()}
                if any(str(value or '').strip() for value in values.values()):
                    if operator_id:
                        self.payees.update(operator_id,**values,commit=False)
                    else:
                        payee=self.payees.create(**values,commit=False)
                        self.payees.set_default(migrated_id,payee['id'],commit=False)
                        self._set_meta('adapter.legacy.operator_payee_id',payee['id'])
                scheme_id=scheme_id or migrated_id
            if scheme_id:
                self.packages.set_default(scheme_id,commit=False)
            self._set_meta('adapter.setup.state','ready')
        return self.setup_state()

    def _resolve(self,base,overrides):
        from .resolver import resolve_definition
        from .registry import SETTINGS
        settings=overrides.get('settings',{})
        for key,value in settings.items():
            if key not in SETTINGS:
                raise ValueError('方案不能修改未注册的设置：'+key)
            descriptor=base['scheme'].get('settings',{}).get(key,{})
            if 'fixed' in descriptor or descriptor.get('editable') is False:
                raise ValueError('方案固定了该设置，不能覆盖：'+key)
            spec=SETTINGS[key]
            valid=(value is None and spec.get('nullable')) or {
                'boolean':lambda:type(value) is bool,
                'text':lambda:isinstance(value,str) and len(value)<=spec.get('max_length',10000),
                'select':lambda:isinstance(value,str) and value in spec.get('enum',[]),
                'multiselect':lambda:isinstance(value,list) and len(value)<=spec.get('max_items',10000) and
                    all(isinstance(v,str) and len(v)<=spec.get('max_length',10000) for v in value) and len(value)==len(set(value)),
            }.get(spec['type'],lambda:False)()
            if not valid:
                raise ValueError('设置值的类型或范围无效：'+key)
        definition=deepcopy(base)
        for key in ('titles','materials','rules','outputs','fields'):
            if key in overrides:
                if key=='titles':
                    definition['scheme']['titles']=deepcopy(overrides[key])
                else:
                    definition[key]=deepcopy(overrides[key])
        if 'organization' in overrides:
            definition['scheme'].setdefault('organization',{}).update(overrides['organization'])
        resolved=resolve_definition(definition,settings)
        title_id=resolved['effective_settings'].get('entry.default_title_id')
        if title_id and title_id not in {t['id'] for t in resolved['scheme'].get('titles',[])}:
            raise ValueError('默认抬头不在当前方案中。')
        if set(resolved['effective_settings'].get('print.default_outputs',[])) - {output['id'] for output in resolved.get('outputs',[])}:
            raise ValueError('默认输出不在当前方案中。')
        from .loader import validate_resolved_definition
        validate_resolved_definition(resolved)
        return resolved

    def list_schemes(self,include_disabled=False):
        return [self.get_scheme(s['id']) for s in self.packages.list_schemes(include_disabled)]

    def get_scheme(self,scheme_id=None):
        result=self.packages.get_scheme(scheme_id)
        result['payee']=self.payees.get_default(result['id'])
        result['scheme_payee_id']=result['payee']['id'] if result['payee'] else None
        result['default_payee_id']=result['scheme_payee_id']
        result['profile_payee_mappings']={r[0]:r[1] for r in self.db.conn.execute(
            'SELECT profile_id,payee_id FROM scheme_payee_links WHERE scheme_id=? AND profile_id IS NOT NULL',(result['id'],))}
        from .registry import SETTINGS
        result['settings_descriptions']={key:{**deepcopy(spec),
            **deepcopy(result['definition']['scheme'].get('settings',{}).get(key,{})),
            'value':result['definition']['effective_settings'][key],
            'overridden':key in result['overrides'].get('settings',{})} for key,spec in SETTINGS.items()}
        result['settings_catalog']=deepcopy(SETTINGS)
        result['revision_history']=self.revision_history(result['id'])
        return result

    def setting_baseline(self,scheme):
        """Effective settings as the package alone defines them, without local overrides."""
        from .resolver import resolve_definition
        record=self.packages.revision_record(scheme['current_revision_id'])
        base=self.packages.get_package(record['content_hash'])['definition']
        return resolve_definition(base)['effective_settings']

    def get_revision(self,revision_id):
        return self.packages.get_revision(revision_id)

    def default_binding(self):
        return self.packages.default_binding()

    def context_for_entry(self,entry):
        if isinstance(entry,str):
            row=self.db.conn.execute('SELECT * FROM entries WHERE id=?',(entry,)).fetchone()
            if not row:
                raise ValueError('条目不存在。')
            entry=dict(row)
        return self.get_revision(entry['scheme_revision_id']) if entry.get('scheme_revision_id') else self.get_revision(self.default_binding()[1])

    def set_default_scheme(self,scheme_id):
        with self.db.transaction():
            self.packages.set_default(scheme_id,commit=False)
        return self.get_scheme(scheme_id)

    def disable_scheme(self,scheme_id):
        with self.db.transaction():
            self.packages.disable(scheme_id,commit=False)
        return self.get_scheme(scheme_id)

    def enable_scheme(self,scheme_id):
        with self.db.transaction():
            self.packages.enable(scheme_id,commit=False)
        return self.get_scheme(scheme_id)

    def copy_scheme(self,scheme_id,name):
        source=self.get_scheme(scheme_id)
        with self.db.transaction():
            result=self.packages.create_scheme(name,source['current_revision_id'],overrides=source['overrides'],commit=False)
            # Copies intentionally have independent personal values/mappings.
        return self.get_scheme(result['id'])

    def revision_history(self,scheme_id):
        return self.packages.revision_history(scheme_id)

    def rollback_scheme(self,scheme_id,revision_id,expected_revision=None):
        with self.db.transaction():
            if not self.db.conn.execute('SELECT 1 FROM scheme_revision_links WHERE scheme_id=? AND revision_id=?',(scheme_id,revision_id)).fetchone():
                raise ValueError('不能回退到不属于该方案的修订。')
            definition=self.get_revision(revision_id)
            package=self.packages.get_package(self.packages.revision_record(revision_id)['content_hash'])
            overrides=self._overrides_from_definition(package['definition'],definition)
            self.packages.set_current_revision(scheme_id,revision_id,overrides=overrides,expected_revision=expected_revision,commit=False)
        return self.get_scheme(scheme_id)

    def _overrides_from_definition(self,base,definition):
        from .resolver import resolve_definition
        original=resolve_definition(base)
        overrides={'settings':{k:v for k,v in definition['effective_settings'].items() if original['effective_settings'].get(k)!=v}}
        for key in ('fields','materials','rules','outputs'):
            if definition.get(key)!=original.get(key):
                overrides[key]=deepcopy(definition[key])
        if definition['scheme'].get('titles')!=original['scheme'].get('titles'):
            overrides['titles']=deepcopy(definition['scheme'].get('titles',[]))
        if definition['scheme'].get('organization')!=original['scheme'].get('organization'):
            overrides['organization']=deepcopy(definition['scheme'].get('organization',{}))
        return overrides

    def _change_definition(self,scheme_id,expected_revision,overrides):
        scheme=self.get_scheme(scheme_id)
        if expected_revision != scheme['current_revision_id']:
            raise ValueError('方案已经变化，请刷新后保存。')
        record=self.packages.revision_record(expected_revision)
        base=self.packages.get_package(record['content_hash'])['definition']
        definition=self._resolve(base,overrides)
        with self.db.transaction():
            revision=self.packages.store_revision(definition,record['content_hash'],commit=False)
            self.packages.set_current_revision(scheme_id,revision,overrides=overrides,expected_revision=expected_revision,commit=False)
        return self.get_scheme(scheme_id)

    def update_scheme_settings(self,scheme_id,expected_revision,values,clear=()):
        """Save changed settings; keys listed in `clear` revert to the package default.

        None is an explicit empty value only for nullable settings (the default title). For every
        other setting None keeps its older meaning of "revert to the default".
        """
        from .registry import SETTINGS
        if not isinstance(values,dict):
            raise ValueError('设置必须是对象。')
        clear=() if clear is None else clear
        if not isinstance(clear,(list,tuple,set)) or any(not isinstance(key,str) for key in clear):
            raise ValueError('恢复默认的设置必须是名称列表。')
        if set(values)&set(clear):
            raise ValueError('同一项设置不能同时修改和恢复默认。')
        scheme=self.get_scheme(scheme_id)
        overrides=deepcopy(scheme['overrides'])
        settings=overrides.setdefault('settings',{})
        for key in clear:
            settings.pop(key,None)
        for key,value in values.items():
            if value is None and not SETTINGS.get(key,{}).get('nullable'):
                settings.pop(key,None)
            else:
                settings[key]=value
        return self._change_definition(scheme_id,expected_revision,overrides)

    def restore_setting_defaults(self,scheme_id,expected_revision,keys=None):
        overrides=self.get_scheme(scheme_id)['overrides'].get('settings',{})
        return self.update_scheme_settings(scheme_id,expected_revision,{},clear=list(keys if keys is not None else overrides))

    @staticmethod
    def _normalize_titles(titles,original=None):
        if not isinstance(titles,list):
            raise ValueError('抬头必须是数组。')
        result=[]
        seen=set()
        for title in titles:
            if not isinstance(title,dict) or not isinstance(title.get('name'),str) or not title['name'].strip():
                raise ValueError('抬头名称不能为空。')
            name=title['name'].strip()
            tax=_tax(title.get('tax_id',''))
            if (name,tax) in seen:
                continue
            seen.add((name,tax))
            old=[t for t in original or [] if t['name']==name and _tax(t.get('tax_id'))==tax]
            source=old[0] if len(old)==1 else {}
            result.append({'id':title.get('id') or source.get('id') or 'title_'+_digest([name,tax])[:12],
                'name':name,'tax_id':tax,'short_name':title.get('short_name',source.get('short_name',name)),
                'color':title.get('color',source.get('color','neutral'))})
        if len({t['id'] for t in result})!=len(result):
            raise ValueError('抬头 ID 不得重复。')
        return result

    def update_titles(self,profiles,scheme_id=None,expected_revision=None):
        scheme=self.get_scheme(scheme_id)
        overrides=deepcopy(scheme['overrides'])
        overrides['titles']=self._normalize_titles(profiles,scheme['definition']['scheme'].get('titles',[]))
        current=scheme['definition']['effective_settings'].get('entry.default_title_id')
        if current and current not in {t['id'] for t in overrides['titles']}:
            overrides.setdefault('settings',{})['entry.default_title_id']=None
        return self._change_definition(scheme['id'],expected_revision or scheme['current_revision_id'],overrides)

    @staticmethod
    def _requirements_overrides(definition,requirements):
        if not isinstance(requirements,dict) or set(requirements)-{'invoice','payment_screenshot','physical_image','inspection_pdf','paid_amount'}:
            raise ValueError('材料要求无效。')
        if any(type(v) is not bool for v in requirements.values()) or requirements.get('invoice') is False:
            raise ValueError('材料要求必须是布尔值，发票始终必需。')
        materials=deepcopy(definition['materials'])
        for role in materials:
            if role['id'] in requirements:
                role['min_count']=1 if requirements[role['id']] else 0
        rules=deepcopy(definition['rules'])
        if 'paid_amount' in requirements:
            rules=[r for r in rules if r['id']!='legacy_paid_amount_required']
            # Builtin paid requirement uses the declared paid rule; identify by
            # its requirement target rather than any team/package name.
            rules=[r for r in rules if not (r.get('require')==[{'field':'invoice.paid_amount'}] and r.get('when',{'all':[]})=={'all':[]})]
            if requirements['paid_amount']:
                rules.append({'id':'legacy_paid_amount_required','stage':'complete','when':{'all':[]},'require':[{'field':'invoice.paid_amount'}], 'message':'请填写实付金额。'})
        return {'materials':materials,'rules':rules}

    def material_requirements(self,scheme_id=None):
        definition=self.get_scheme(scheme_id)['definition']
        result={r['id']:r.get('min_count',0)>0 for r in definition['materials'] if r['id'] in ('invoice','payment_screenshot','physical_image','inspection_pdf')}
        result['paid_amount']=any(any(q.get('field') in ('invoice.paid_amount','entry.paid_amount') for q in r.get('require',[])) and r.get('when',{'all':[]})=={'all':[]} for r in definition['rules'])
        return result

    def update_material_requirements(self,requirements,scheme_id=None,expected_revision=None):
        scheme=self.get_scheme(scheme_id)
        overrides=deepcopy(scheme['overrides'])
        overrides.update(self._requirements_overrides(scheme['definition'],requirements))
        self._change_definition(scheme['id'],expected_revision or scheme['current_revision_id'],overrides)
        return self.material_requirements(scheme['id'])

    @staticmethod
    def _definition_changes(old,new):
        changes=[]
        for key in ('fields','materials','rules','outputs'):
            old_items={(f.get('scope',''),f['id']):f for f in old.get(key,[])}
            new_items={(f.get('scope',''),f['id']):f for f in new.get(key,[])}
            for item_id in sorted(set(old_items)|set(new_items)):
                before=old_items.get(item_id)
                after=new_items.get(item_id)
                if before!=after:
                    changes.append({'kind':key,'id':item_id[1],'scope':item_id[0],
                        'change':'added' if before is None else 'removed' if after is None else 'changed',
                        'before':before,'after':after})
        for key in ('titles','organization','settings'):
            before=old['scheme'].get(key)
            after=new['scheme'].get(key)
            if before!=after:
                changes.append({'kind':key,'change':'changed','before':before,'after':after})
        return changes

    def _update_conflicts(self,scheme,new):
        old_record=self.packages.revision_record(scheme['current_revision_id'])
        old=self.packages.get_package(old_record['content_hash'])['definition']
        conflicts=[]
        settings=scheme['overrides'].get('settings',{})
        for key,value in settings.items():
            descriptor=new['scheme'].get('settings',{}).get(key,{})
            if 'fixed' in descriptor or descriptor.get('editable') is False:
                conflicts.append({'id':'setting:'+key,'kind':'setting','key':key,'local':value,
                    'incoming':descriptor.get('fixed',descriptor.get('default')),
                    'message':'新版本固定了本地修改的设置，请确认采用新值或取消更新。'})
        for change in self._definition_changes(old,new):
            before,after=change.get('before'),change.get('after')
            if change['kind'] in ('fields','materials') and before:
                conflict=change['change']=='removed'
                if change['kind']=='fields' and after:
                    conflict=before['type']!=after['type'] or bool({o['value'] for o in before.get('options',[])}-{o['value'] for o in after.get('options',[])})
                if conflict:
                    conflicts.append({'id':change['kind']+':'+change.get('scope','')+':'+change['id'],
                        'kind':change['kind'],'field_id':change['id'],'scope':change.get('scope',''),
                        'before':before,'after':after,'message':'定义删除、类型或选项变化需要确认；原修订和值将保留。'})
        # Stable-ID structural local edits are merged three ways. Both editing
        # the same property differently is a conflict, never array-index merge.
        _,structural=self._merge_overrides(old,new,scheme['overrides'],{})
        conflicts.extend(structural)
        return conflicts

    def _merge_overrides(self,old,new,overrides,resolutions):
        result=deepcopy(overrides)
        conflicts=[]
        missing=object()
        def merge(before,local,incoming,path):
            if local==before:
                return deepcopy(incoming) if incoming is not missing else missing
            if incoming==before or incoming==local:
                return deepcopy(local) if local is not missing else missing
            if all(isinstance(v,dict) for v in (before,local,incoming)):
                merged={}
                for key in sorted(set(before)|set(local)|set(incoming)):
                    value=merge(before.get(key,missing),local.get(key,missing),incoming.get(key,missing),path+'/'+key)
                    if value is not missing:
                        merged[key]=value
                return merged
            if all(isinstance(v,list) for v in (before,local,incoming)) and all(isinstance(v,dict) and 'id' in v for arr in (before,local,incoming) for v in arr):
                ids=lambda arr:{(f.get('scope',''),f['id']):f for f in arr}
                b,l,n=ids(before),ids(local),ids(incoming)
                order=list(n)+[key for key in l if key not in n]
                merged=[]
                for key in order:
                    value=merge(b.get(key,missing),l.get(key,missing),n.get(key,missing),path+'/'+key[0]+':'+key[1])
                    if value is not missing:
                        merged.append(value)
                return merged
            conflict_id='local:'+path
            choice=resolutions.get(conflict_id)
            if choice in ('incoming','package','accept'):
                return deepcopy(incoming) if incoming is not missing else missing
            if choice in ('local','keep'):
                return deepcopy(local) if local is not missing else missing
            conflicts.append({'id':conflict_id,'kind':'local_override','path':path,
                'local':None if local is missing else local,'incoming':None if incoming is missing else incoming,
                'message':'本地和新包都修改了该定义，请选择要保留的内容。'})
            return deepcopy(local) if local is not missing else missing
        for key in ('titles','organization','fields','materials','rules','outputs'):
            if key in overrides:
                before=old['scheme'].get(key) if key in ('titles','organization') else old.get(key)
                incoming=new['scheme'].get(key) if key in ('titles','organization') else new.get(key)
                result[key]=merge(before,overrides[key],incoming,key)
        return result,conflicts

    @_tracked_operation('inspect_package')
    def inspect_adapter(self,path,operation_id=None):
        from .loader import load_package
        self._operation_checkpoint(operation_id,'inspect_package')
        package=load_package(path)
        self._operation_checkpoint(operation_id,'compare_schemes')
        manifest=package.definition['manifest']
        installed=self.packages.get_package(package_id=manifest['package_id'],package_version=manifest['package_version'])
        if installed and installed['content_hash']!=package.content_hash:
            raise ValueError('同版本适配包内容冲突，请让作者增加版本或使用新包 ID。')
        schemes=[s for s in self.list_schemes(True) if s['package_id']==manifest['package_id']]
        self._set_operation_progress(operation_id,'compare_schemes',0,len(schemes))
        preview_id=uuid.uuid4().hex
        changes={}
        conflicts={}
        for index,scheme in enumerate(schemes,1):
            self._operation_checkpoint(operation_id,'compare_schemes',index-1,len(schemes))
            record=self.packages.revision_record(scheme['current_revision_id'])
            old=self.packages.get_package(record['content_hash'])['definition']
            changes[scheme['id']]=self._definition_changes(old,package.definition)
            conflicts[scheme['id']]=self._update_conflicts(scheme,package.definition)
            self._operation_checkpoint(operation_id,'compare_schemes',index,len(schemes))
        preview={'preview_id':preview_id,'content_hash':package.content_hash,'manifest':deepcopy(manifest),
            'schemes':schemes,'changes':changes,'conflicts':conflicts,'diagnostics':package.diagnostics,
            'materials':deepcopy(package.definition['materials']),'outputs':deepcopy(package.definition['outputs']),
            'ok':True}
        self._previews[preview_id]={'path':str(Path(path).resolve()),'content_hash':package.content_hash,'preview':preview,
            'heads':{s['id']:s['current_revision_id'] for s in schemes}}
        return deepcopy(preview)

    @_tracked_operation('prepare_install')
    def install_adapter(self,preview_id,options=None,operation_id=None,**kwargs):
        from .loader import load_package
        self._operation_checkpoint(operation_id,'verify_preview')
        options={**(options or {}),**kwargs}
        saved=self._previews.get(preview_id)
        if not saved:
            raise ValueError('安装预览已失效，请重新选择适配包。')
        package=load_package(saved['path'])
        self._operation_checkpoint(operation_id,'resolve_definition')
        if package.content_hash!=saved['content_hash']:
            raise ValueError('预览后适配包内容已经变化，请重新预览。')
        mode=options.get('mode','install')
        if mode not in ('install','update','copy'):
            raise ValueError('安装方式无效。')
        scheme_id=options.get('scheme_id')
        overrides={}
        scheme=None
        if mode=='update':
            scheme=self.get_scheme(scheme_id)
            if saved['heads'].get(scheme['id'])!=scheme['current_revision_id']:
                raise ValueError('方案已变化，请重新预览更新。')
            if scheme['package_id']!=package.definition['manifest']['package_id']:
                raise ValueError('不同包不能作为同方案更新。')
            resolutions=options.get('resolutions') or {}
            conflicts=self._update_conflicts(scheme,package.definition)
            for conflict in conflicts:
                if resolutions.get(conflict['id']) not in ('incoming','package','accept','local','keep','history'):
                    raise ValueError('请先解决更新冲突：'+conflict['message'])
                if conflict['kind']=='setting' and resolutions[conflict['id']] in ('local','keep'):
                    raise ValueError('固定设置不能保留本地覆盖，请取消更新或采用包值。')
            old_record=self.packages.revision_record(scheme['current_revision_id'])
            old=self.packages.get_package(old_record['content_hash'])['definition']
            overrides,unresolved=self._merge_overrides(old,package.definition,scheme['overrides'],resolutions)
            if unresolved:
                raise ValueError('本地定义仍有未解决冲突。')
            for conflict in conflicts:
                if conflict['kind']=='setting':
                    overrides.setdefault('settings',{}).pop(conflict['key'],None)
        package.definition['resource_hashes']={name:hashlib.sha256(data).hexdigest() for name,data in package.files.items() if name.startswith(('templates/','assets/'))}
        definition=self._resolve(package.definition,overrides)
        prepared=self._prepare_package(package,'local',operation_id)
        try:
            self._operation_checkpoint(operation_id,'commit_install')
            with self.db.transaction():
                self._record_package(package,prepared)
                revision=self.packages.store_revision(definition,package.content_hash,commit=False)
                if scheme:
                    self.packages.set_current_revision(scheme['id'],revision,overrides=overrides,
                        expected_revision=saved['heads'][scheme['id']],commit=False)
                    result=self.packages.get_scheme(scheme['id'])
                else:
                    name=options.get('name') or package.definition['manifest']['name']
                    result=self.packages.create_scheme(name,revision,overrides=overrides,commit=False)
                if options.get('set_default'):
                    self.packages.set_default(result['id'],commit=False)
                self._operation_checkpoint(operation_id,'commit_install',1,1)
            self._finish_operation(prepared)
            self._previews.pop(preview_id,None)
            return self.get_scheme(result['id'])
        except BaseException:
            self._cleanup_failed_package_operation(prepared)
            raise

    def export_adapter(self,scheme_id,options=None):
        from .loader import load_package,pack_package
        from .resolver import resolve_definition
        from .registry import required_capabilities
        options=options or {}
        scheme=self.get_scheme(scheme_id)
        record=self.packages.revision_record(scheme['current_revision_id'])
        package=self.packages.get_package(record['content_hash'])
        definition=deepcopy(scheme['definition'])
        public_fields=[f for f in definition['fields'] if f['scope']=='scheme' and not f.get('sensitive') and f.get('transfer')=='include']
        public_values=self.extensions.get_values('scheme',scheme_id,scheme_id,scheme['package_id'],scheme['current_revision_id'])
        changed=_digest(definition)!=_digest(resolve_definition(package['definition'])) or any(f['id'] in public_values for f in public_fields)
        metadata={k:options[k] for k in ('name','author','description') if k in options}
        changed=changed or any(definition['manifest'].get(k)!=v for k,v in metadata.items())
        if changed and options.get('package_id')==scheme['package_id']:
            raise ValueError('修改后的公共适配包必须使用新的包 ID。')
        result={'scheme_id':scheme_id,'changed':changed,'requires_new_id':changed,
            'titles':definition['scheme'].get('titles',[]),'fields':definition['fields'],
            'materials':definition['materials'],'outputs':definition['outputs'],
            'excludes':['payees','private_values','payee_mappings','history','machine_preferences']}
        if changed and not options.get('package_id'):
            return {**result,'ok':True,'preview':True,'path':None,'message':'修改后的方案需要填写新的包 ID 才能导出。'}
        stage=self.data_root.adapter_staging_dir/('export_'+uuid.uuid4().hex)
        try:
            stage.mkdir()
            source=self.data_root.root/package['resource_path']
            original=load_package(source)
            for name,data in original.files.items():
                if name=='checksums.json':
                    continue
                target=stage/name
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(data)
            if changed:
                manifest=definition['manifest']
                manifest['package_id']=options['package_id']
                manifest['package_version']=options.get('package_version','1.0.0')
                manifest.update(metadata)
                manifest.setdefault('x_metadata',{})['source']={'package_id':scheme['package_id'],'package_version':scheme['package_version']}
                # Local setting values become public defaults, fixed descriptors
                # remain fixed. No personal actual values are embedded.
                descriptors=definition['scheme'].setdefault('settings',{})
                for key,value in definition['effective_settings'].items():
                    if 'fixed' not in descriptors.get(key,{}):
                        descriptors[key]={**descriptors.get(key,{}),'default':value,'editable':True,'presentation':descriptors.get(key,{}).get('presentation','visible')}
                for field in definition['fields']:
                    if field in public_fields and field['id'] in public_values:
                        field['default']=public_values[field['id']]
                    elif field.get('sensitive') or field['scope'] in ('payee','entry','batch','export'):
                        field.pop('default',None)
                old_prefix='custom:'+scheme['package_id']+':'
                new_prefix='custom:'+manifest['package_id']+':'
                def replace_roles(value):
                    if isinstance(value,dict):
                        return {k:replace_roles(v) for k,v in value.items()}
                    if isinstance(value,list):
                        return [replace_roles(v) for v in value]
                    if isinstance(value,str) and value.startswith(old_prefix):
                        return new_prefix+value[len(old_prefix):]
                    return value
                definition=replace_roles(definition)
                definition['manifest']['requires']['capabilities']=required_capabilities(definition)
                for name,value in [('manifest.json',definition['manifest']),('scheme.json',definition['scheme']),
                    ('fields.json',{'fields':definition['fields']}),('materials.json',{'roles':definition['materials']}),
                    ('rules.json',{'rules':definition['rules']}),('outputs.json',{'outputs':definition['outputs']})]:
                    (stage/name).write_text(encode(value),encoding='utf-8')
            output=Path(options.get('output_path') or self.data_root.exports_dir/(definition['manifest']['package_id']+'-'+definition['manifest']['package_version']+'.tidoc-preset'))
            if output.exists() and not options.get('overwrite'):
                output=output.with_name(output.stem+'-'+uuid.uuid4().hex[:8]+output.suffix)
            output.parent.mkdir(parents=True,exist_ok=True)
            written=pack_package(stage,output)
            return {**result,'ok':True,'preview':False,'path':str(written),'package_id':definition['manifest']['package_id']}
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def _form_binding(self,scope,owner_id,scheme_id=None,revision_id=None,allow_preview=False):
        if scope=='entry':
            row=self.db.conn.execute('SELECT scheme_id,scheme_revision_id FROM entries WHERE id=?',(owner_id,)).fetchone()
            if not row:
                raise ValueError('条目不存在。')
            scheme_id=scheme_id or row[0]
            revision_id=revision_id or row[1]
        elif scope=='scheme':
            scheme_id=scheme_id or owner_id
        elif scope=='batch':
            row=self.db.conn.execute('SELECT default_scheme_id,default_revision_id FROM batches WHERE id=?',(owner_id,)).fetchone()
            if not row and not allow_preview:
                raise ValueError('报账批次不存在。')
            if row and scheme_id is None:
                scheme_id=row[0]
            if not row and allow_preview and scheme_id is None:
                scheme_id=self.default_binding()[0]
        elif scope=='export' and allow_preview:
            if scheme_id is None:
                scheme_id=self.default_binding()[0]
        scheme=self.get_scheme(scheme_id)
        revision_id=revision_id or scheme['current_revision_id']
        if not self.db.conn.execute('SELECT 1 FROM scheme_revision_links WHERE scheme_id=? AND revision_id=?',(scheme['id'],revision_id)).fetchone():
            raise ValueError('修订不属于指定方案。')
        virtual_owner=(scope=='export' or scope=='batch') and allow_preview
        if not virtual_owner:
            self.extensions.check_owner(scope,owner_id,scheme['id'])
        return scheme['id'],revision_id

    def get_form_description(self,scope,owner_id,scheme_id=None,revision_id=None,values_override=None,_allow_preview=False):
        from .policy import validate_field_value
        scheme_id,revision_id=self._form_binding(scope,owner_id,scheme_id,revision_id,allow_preview=_allow_preview)
        definition=self.get_revision(revision_id)
        package_id=definition['manifest']['package_id']
        fields=deepcopy([f for f in definition['fields'] if f['scope']==scope])
        virtual_owner=scope in ('export','batch') and _allow_preview and not self.db.conn.execute(
            'SELECT 1 FROM '+('export_jobs' if scope=='export' else 'batches')+' WHERE id=?',(owner_id,)).fetchone()
        rows=[] if virtual_owner else self.extensions.list_values(scope,owner_id,scheme_id)
        compatible={} if virtual_owner else self.extensions.get_values(scope,owner_id,scheme_id,package_id,revision_id)
        values={f['id']:deepcopy(f.get('default')) for f in fields if 'default' in f}
        values.update(compatible)
        if values_override is not None:
            if not isinstance(values_override,dict):
                raise ValueError('预览字段值必须是对象。')
            by_id={field['id']:field for field in fields}
            unknown=set(values_override)-set(by_id)
            if unknown:
                raise ValueError('当前表单没有字段：'+', '.join(sorted(unknown)))
            for field_id,value in values_override.items():
                validate_field_value(by_id[field_id],value)
                values[field_id]=deepcopy(value)
        context={scope:{'fields':values}}
        if scope=='entry':
            row=self.db.conn.execute('SELECT * FROM entries WHERE id=?',(owner_id,)).fetchone()
            if row is None: raise ValueError('条目不存在。')
            entry=dict(row)
            core_fields={r['field']:r['current'] for r in self.db.conn.execute(
                'SELECT field,current FROM entry_fields WHERE entry_id=?',(owner_id,)).fetchall()}
            entry['paid_amount']=core_fields.get('paid_amount')
            entry['title_id']=entry.get('title_profile_id')
            context['entry']={**entry,'fields':{**core_fields,**values},'extensions':values}
            context['invoice']={'total':entry.get('total'),'paid_amount':core_fields.get('paid_amount'),
                                'title_id':entry.get('title_profile_id')}
        historical=[]
        for row in rows:
            source=self.get_revision(row['definition_revision_id'])
            field=next((f for f in source['fields'] if f['scope']==scope and f['id']==row['field_id']),None)
            if row['package_id']!=package_id or row['field_id'] not in compatible:
                historical.append({**row,'field':field,'read_only':True})
        current_fields={field['id']:field for field in fields}
        if scope=='entry' and self.db.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entry_adapter_sources'").fetchone():
            source_rows=self.db.conn.execute('''SELECT source_digest,revision_id,payload_json,received_at
                FROM entry_adapter_sources WHERE entry_id=? ORDER BY received_at,source_digest''',(owner_id,)).fetchall()
            for source_row in source_rows:
                try:
                    payload=json.loads(source_row['payload_json'])
                except (TypeError,ValueError):
                    continue
                if not isinstance(payload,dict):
                    continue
                source_definition=payload.get('definition')
                if not isinstance(source_definition,dict):
                    source_definition=payload.get('adapter',{}).get('definition') if isinstance(payload.get('adapter'),dict) else None
                if not isinstance(source_definition,dict):
                    continue
                source_package=(source_definition.get('manifest') or {}).get('package_id','')
                source_fields={f.get('id'):f for f in source_definition.get('fields',[])
                               if f.get('scope')=='entry' and isinstance(f,dict)}
                source_values=payload.get('extension_values',[])
                if not isinstance(source_values,list):
                    continue
                for source_value in source_values:
                    if not isinstance(source_value,dict):
                        continue
                    field_id=source_value.get('field_id')
                    field=source_fields.get(field_id)
                    if not field:
                        continue
                    value=source_value.get('value')
                    active=current_fields.get(field_id)
                    usable=bool(active and active.get('type')==field.get('type') and
                                source_package==package_id)
                    if usable:
                        try:
                            validate_field_value(active,value)
                        except (ValueError,TypeError):
                            usable=False
                    if usable:
                        continue
                    historical.append({'field_id':field_id,'value':value,'package_id':source_package,
                        'definition_revision_id':payload.get('source_revision_id') or source_row['revision_id'] or source_value.get('definition_revision_id',''),
                        'source_digest':source_row['source_digest'],'field':deepcopy(field),
                        'read_only':True,'source_received_at':source_row['received_at']})
        events=[] if virtual_owner else self.extensions.history(scope,owner_id,scheme_id)
        for event in events:
            source_revision=event.get('definition_revision_id')
            if not source_revision or event.get('kind') not in ('rebind','value'):
                continue
            try:
                source_definition=self.get_revision(source_revision)
            except ValueError:
                continue
            source_field=next((f for f in source_definition.get('fields',[])
                               if f.get('scope')==scope and f.get('id')==event.get('field_id')),None)
            target_field=current_fields.get(event.get('field_id'))
            if (not source_field or not target_field or source_field.get('type')!=target_field.get('type')
                    or event.get('package_id')!=package_id):
                for value in (event.get('old_value'),event.get('new_value')):
                    if value is not None:
                        historical.append({'field_id':event.get('field_id'),'value':value,
                            'definition_revision_id':source_revision,'package_id':event.get('package_id'),
                            'field':source_field,'read_only':True,'changed_at':event.get('changed_at')})
        for field in fields:
            field['value']=values.get(field['id'])
            try:
                validate_field_value(field,field['value'])
                field['errors']=[]
            except ValueError as exc:
                field['errors']=getattr(exc,'diagnostics',[{'message':str(exc)}])
            field['visible']=field.get('presentation','visible')!='hidden'
            if field.get('visible_when'):
                from .policy import evaluate_condition
                verdict=evaluate_condition(field['visible_when'],context,
                    stage='complete' if scope=='entry' else 'export',definition=definition)
                field['visible']=field['visible'] and verdict is True
                field['visibility_state']=verdict
        return {'scope':scope,'owner_id':owner_id,'scheme_id':scheme_id,'revision_id':revision_id,
                'package_id':package_id,'fields':fields,'values':values,'history_values':historical,
                'definition':{'revision_id':revision_id,'package_id':package_id,'fields':deepcopy(definition['fields'])},
                'version':_digest({'scope':scope,'owner_id':owner_id,'scheme_id':scheme_id,'revision_id':revision_id,'values':values}) if virtual_owner else self._form_version(scope,owner_id,scheme_id,package_id,revision_id),
                'history':events}

    def preview_form_description(self,scope,owner_id,values,scheme_id=None,revision_id=None):
        """Build a typed form preview from temporary values without writing them."""
        return self.get_form_description(scope,owner_id,scheme_id,revision_id,values_override=values,_allow_preview=True)

    def update_batch_output_settings(self,batch_id,settings,expected_updated_at=None):
        from ..db.batches import BatchRepo
        from .registry import SETTINGS
        from .resolver import _valid_setting
        if not isinstance(settings,dict): raise ValueError('批次输出设置必须是对象。')
        invalid=set(settings)-{key for key,spec in SETTINGS.items() if 'batch' in spec.get('scopes',[])}
        if invalid: raise ValueError('设置不适用于批次：'+', '.join(sorted(invalid)))
        for key,value in settings.items():
            if not _valid_setting(key,value): raise ValueError('设置值无效：'+key)
        batch=BatchRepo(self.db).get(batch_id)
        if not batch: raise ValueError('报账批次不存在。')
        bindings=set()
        if batch.get('default_scheme_id') and batch.get('default_revision_id'):
            bindings.add((batch['default_scheme_id'],batch['default_revision_id']))
        bindings.update((row['scheme_id'],row['scheme_revision_id']) for row in self.db.conn.execute(
            '''SELECT DISTINCT e.scheme_id,e.scheme_revision_id FROM batch_entries be
               JOIN entries e ON e.id=be.entry_id WHERE be.batch_id=? AND e.scheme_id IS NOT NULL''',(batch_id,)))
        for _scheme_id,revision_id in bindings:
            definition=self.get_revision(revision_id)
            for key,value in settings.items():
                descriptor=definition.get('scheme',{}).get('settings',{}).get(key,{})
                if descriptor and descriptor.get('editable') is not True:
                    raise ValueError('该方案设置为只读，不能由批次覆盖：'+key)
                if 'fixed' in descriptor and descriptor['fixed']!=value:
                    raise ValueError('设置与适用方案的固定值冲突：'+key)
        return BatchRepo(self.db).set_output_settings(batch_id,settings,expected_updated_at=expected_updated_at)

    def _form_version(self,scope,owner_id,scheme_id,package_id,revision_id):
        return _digest({'scope':scope,'owner_id':owner_id,'scheme_id':scheme_id,'package_id':package_id,
            'revision_id':revision_id,'values_version':self.extensions.version(scope,owner_id,scheme_id,package_id),
            'history':self.extensions.history(scope,owner_id,scheme_id)})

    def save_extension_values(self,scope,owner_id,values,scheme_id=None,expected_version=None,revision_id=None):
        form=self.get_form_description(scope,owner_id,scheme_id,revision_id)
        with self.db.transaction():
            if scope=='entry':
                bound=self.db.conn.execute('SELECT scheme_id,scheme_revision_id FROM entries WHERE id=?',(owner_id,)).fetchone()
                current_binding=(bound['scheme_id'],bound['scheme_revision_id']) if bound else (None,None)
            elif scope=='batch':
                exists=self.db.conn.execute('SELECT 1 FROM batches WHERE id=?',(owner_id,)).fetchone()
                current=self.packages.get_scheme(form['scheme_id']) if exists else None
                current_binding=(current['id'],current['current_revision_id']) if current else (None,None)
            else:
                current=self.get_scheme(form['scheme_id'])
                current_binding=(current['id'],current['current_revision_id'])
            if scope=='entry' and current_binding!=(form['scheme_id'],form['revision_id']):
                raise ValueError('条目方案修订已变化，请刷新后再填写。')
            live_version=self._form_version(scope,owner_id,form['scheme_id'],form['package_id'],form['revision_id'])
            if expected_version is not None and (expected_version!=live_version or
                    (revision_id is None and current_binding!=(form['scheme_id'],form['revision_id']))):
                raise ValueError('字段已被其他操作修改，请刷新后保存。')
            self.extensions.save_values(scope,owner_id,form['scheme_id'],form['revision_id'],values,commit=False)
            if scope=='entry':
                from ..db.entries import EntryRepo
                EntryRepo(self.db).recompute_status(owner_id,commit=False)
        return self.get_form_description(scope,owner_id,form['scheme_id'],form['revision_id'])

    def preview_resource_gc(self):
        """List only package directories with no immutable package/revision reference."""
        referenced={row[0] for row in self.db.conn.execute('SELECT content_hash FROM adapter_packages')}
        candidates=[]
        for path in sorted(self.data_root.adapter_packages_dir.iterdir()):
            if not path.is_dir() or path.name in referenced:
                continue
            try:
                resolved=path.resolve()
                if not resolved.is_relative_to(self.data_root.adapter_packages_dir.resolve()):
                    continue
                files=[p for p in resolved.rglob('*') if p.is_file()]
                candidates.append({'content_hash':path.name,'bytes':sum(p.stat().st_size for p in files),
                                   'file_count':len(files)})
            except OSError:
                continue
        preview_id=uuid.uuid4().hex
        digest=_digest(candidates)
        self._resource_gc_previews[preview_id]={'digest':digest,'hashes':[c['content_hash'] for c in candidates]}
        return {'preview_id':preview_id,'candidates':candidates,'count':len(candidates),
                'bytes':sum(c['bytes'] for c in candidates)}

    def cleanup_unreferenced_resources(self,preview_id,content_hashes=None):
        preview=self._resource_gc_previews.get(preview_id)
        if not preview:
            raise ValueError('资源清理预览已失效，请重新预览。')
        current=self.preview_resource_gc()
        if preview['digest']!=_digest(current['candidates']):
            self._resource_gc_previews.pop(preview_id,None)
            raise ValueError('资源清单已经变化，请重新预览后清理。')
        selected=set(content_hashes or preview['hashes'])
        if not selected.issubset(set(preview['hashes'])):
            raise ValueError('只能清理预览中列出的资源。')
        removed=[]
        for content_hash in sorted(selected):
            # Recheck inside the cleanup loop; package rows are immutable references.
            if self.packages.get_package(content_hash):
                continue
            target=self.data_root.package_dir(content_hash)
            if target.is_dir():
                shutil.rmtree(target)
                removed.append(content_hash)
        self._resource_gc_previews.pop(preview_id,None)
        return {'removed':removed,'count':len(removed)}

    def revalidate_templates(self,component_fingerprint=''):
        """Refresh local template validation after the optional print component changes."""
        from ..services.printing import component_status
        status=component_status(self.data_root.components_dir)
        if not component_fingerprint:
            component_fingerprint=_digest(status)
        reports=[]
        for row in self.db.conn.execute('SELECT content_hash,resource_path,definition_json FROM adapter_packages ORDER BY package_id,package_version'):
            definition=json.loads(row['definition_json'])
            # resource_path is stored relative to the data root, never to the process cwd.
            root=self.data_root.root/row['resource_path']
            for output in definition.get('outputs',[]):
                name=output.get('template')
                if output.get('type')!='docx' or not name:
                    continue
                path=(root/name).resolve()
                if not path.is_relative_to(root.resolve()) or not path.is_file():
                    result='invalid'; diagnostics=[{'code':'RESOURCE_REFERENCE_MISSING','severity':'blocked','file':name,'message':'模板资源不存在。'}]
                else:
                    try:
                        from tidoc_print.template_validation import validate_template
                        diagnostics=validate_template(path,definition)
                        result='invalid' if any(d.get('severity')=='blocked' for d in diagnostics) else 'valid'
                    except ImportError:
                        result='pending'; diagnostics=[{'code':'TEMPLATE_VALIDATION_UNAVAILABLE','severity':'warning','file':name,'message':'打印组件不可用，模板校验待执行。'}]
                template_hash=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ''
                with self.db.transaction():
                    self.db.conn.execute('''INSERT INTO template_validation_cache VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(content_hash,template_path,template_hash,component_fingerprint) DO UPDATE SET
                        status=excluded.status,diagnostics_json=excluded.diagnostics_json,validated_at=excluded.validated_at''',
                        (row['content_hash'],name,template_hash,str(component_fingerprint),result,encode(diagnostics),now()))
                reports.append({'content_hash':row['content_hash'],'template':name,'status':result,'diagnostics':diagnostics})
        return {'component_fingerprint':str(component_fingerprint),'templates':reports,
                'valid':sum(1 for r in reports if r['status']=='valid'),
                'invalid':sum(1 for r in reports if r['status']=='invalid'),
                'pending':sum(1 for r in reports if r['status']=='pending')}

    def get_extension_history(self,scope,owner_id,scheme_id=None):
        return self.extensions.history(scope,owner_id,scheme_id)

    def list_payees(self):
        return self.payees.list()

    def save_payee(self,payee_id=None,values=None):
        with self.db.transaction():
            return self.payees.update(payee_id,**(values or {}),commit=False) if payee_id else self.payees.create(**(values or {}),commit=False)

    def set_payee_mapping(self,scheme_id,profile_id,payee_id):
        with self.db.transaction():
            return self.payees.set_mapping(scheme_id,profile_id,payee_id,commit=False)

    def set_scheme_payee(self,scheme_id,payee_id):
        with self.db.transaction():
            return self.payees.set_default(scheme_id,payee_id,commit=False)

    def _entry_snapshot(self,entry_id):
        row=self.db.conn.execute('SELECT * FROM entries WHERE id=?',(entry_id,)).fetchone()
        if not row:
            raise ValueError('条目不存在。')
        entry=dict(row)
        entry['attachments']=[dict(r) for r in self.db.conn.execute('SELECT * FROM attachments WHERE entry_id=? ORDER BY id',(entry_id,))]
        entry['entry_fields']=[dict(r) for r in self.db.conn.execute('SELECT * FROM entry_fields WHERE entry_id=? ORDER BY field',(entry_id,))]
        entry['extension_values']=[dict(r) for r in self.db.conn.execute("SELECT * FROM extension_values WHERE scope='entry' AND owner_id=? ORDER BY scheme_id,package_id,field_id",(entry_id,))]
        entry['extension_history']=[dict(r) for r in self.db.conn.execute("SELECT * FROM extension_history WHERE scope='entry' AND owner_id=? ORDER BY id",(entry_id,))]
        entry['profile']=dict(self.db.conn.execute('SELECT * FROM profiles WHERE id=?',(entry['profile_id'],)).fetchone())
        entry['batch']=[dict(r) for r in self.db.conn.execute('SELECT * FROM batch_entries WHERE entry_id=?',(entry_id,))]
        return entry

    def _mapped_entry(self,entry,scheme_id,revision_id,mappings):
        from .policy import evaluate_policy,validate_field_value
        target=self.get_revision(revision_id)
        source=self.context_for_entry(entry)
        material_maps=mappings.get('materials',{})
        field_maps=mappings.get('fields',{})
        title_maps=mappings.get('titles',{})
        roles={r['id']:r for r in target['materials']}
        source_fields={f['id']:f for f in source['fields'] if f['scope']=='entry'}
        fields={f['id']:f for f in target['fields'] if f['scope']=='entry'}
        values={}
        histories=[]
        diagnostics=[]
        for row in entry['extension_values']:
            if row['package_id']!=source['manifest']['package_id'] or row['scheme_id']!=entry.get('scheme_id'):
                continue
            field_id=field_maps.get(row['field_id'],row['field_id'])
            old=source_fields.get(row['field_id'])
            new=fields.get(field_id)
            value=json.loads(row['value_json'])
            if old and new and old['type']==new['type'] and (source['manifest']['package_id']==target['manifest']['package_id'] or row['field_id'] in field_maps):
                try:
                    validate_field_value(new,value)
                    if field_id in values and values[field_id]!=value:
                        diagnostics.append({'code':'FIELD_MAPPING_COLLISION','severity':'required','message':'多个字段映射到同一字段，请修正映射。','target':field_id})
                    else:
                        values[field_id]=value
                    continue
                except ValueError as exc:
                    diagnostics.extend(getattr(exc,'diagnostics',[{'message':str(exc),'severity':'required'}]))
            histories.append({'field_id':row['field_id'],'value':value,'definition_revision_id':row['definition_revision_id']})
        title_id=title_maps.get(entry.get('title_profile_id'))
        if title_id is None:
            title_id=self._match_title(entry,target)
        if title_id and title_id not in {t['id'] for t in target['scheme'].get('titles',[])}:
            raise ValueError('目标抬头映射不存在。')
        attachments=[]
        counts={}
        for attachment in entry['attachments']:
            attachment=deepcopy(attachment)
            old_role=attachment['role_id'] or ('invoice' if attachment['type'] in ('invoice_pdf','invoice_xml') else attachment['type'])
            new_role=material_maps.get(old_role,old_role)
            role=roles.get(new_role)
            explicit=old_role in material_maps
            source_role_revision=attachment.get('role_definition_revision_id') or entry.get('scheme_revision_id')
            source_role=None
            if source_role_revision:
                try:
                    source_definition=self.get_revision(source_role_revision)
                    source_role=next((r for r in source_definition.get('materials',[]) if r.get('id')==old_role),None)
                except ValueError:
                    source_role=None
            same_definition=bool(source_role and role and source_role==role)
            same_revision=bool(source_role_revision and source_role_revision==revision_id)
            compatible=bool(role) and (same_revision or same_definition)
            if role and explicit:
                if old_role=='invoice' and new_role!='invoice' or old_role!='invoice' and new_role=='invoice':
                    raise ValueError('自定义或其他材料不能替代发票。')
                if role.get('extensions') and Path(attachment['original_name']).suffix.lower() not in role['extensions']:
                    raise ValueError('映射材料格式不符合目标角色要求。')
                if new_role=='invoice':
                    expected_type=attachment['type']
                    if expected_type not in ('invoice_pdf','invoice_xml'):
                        raise ValueError('只有发票 PDF 或 XML 可以映射到发票角色。')
                else:
                    expected_type='other' if new_role.startswith('custom:') else new_role
                old_type=attachment['type']
                attachment['type']=expected_type
                attachment['role_id']=new_role
                attachment['role_definition_revision_id']=None
                compatible=True
                if old_type!=expected_type:
                    attachment.update(recognition_version='',recognition_status='',recognized_value='',recognition_message='')
            elif compatible:
                attachment['role_id']=new_role
                attachment['role_definition_revision_id']=None
            else:
                attachment['role_id']=old_role
                attachment['role_definition_revision_id']=attachment.get('role_definition_revision_id') or source_role_revision
            if compatible:
                counts[new_role]=counts.get(new_role,0)+1
            attachments.append(attachment)
        removed_payment_screenshots=[a for a in entry['attachments'] if a['type']=='payment_screenshot']
        has_payment_after=any(a['type']=='payment_screenshot' for a in attachments)
        core_fields={r['field']:r['current'] for r in entry['entry_fields']}
        context={'invoice':{'total':entry['total'],'paid_amount':core_fields.get('paid_amount'),'title_id':title_id},
            'entry':{**entry,'fields':values,'paid_amount':core_fields.get('paid_amount'),'title_id':title_id,'claimant':entry['profile']},'title':{'id':title_id}}
        diagnostics.extend(evaluate_policy(target,context,counts))
        for role in roles.values():
            if role.get('max_count') is not None and counts.get(role['id'],0)>role['max_count']:
                diagnostics.append({'code':'MATERIAL_MAXIMUM','severity':'required','target':role['id'],'message':'材料数量超过目标角色允许数量。'})
        has_material=bool(attachments)
        complete=has_material and entry['check_status']!='blocked' and not any(d.get('severity')=='required' for d in diagnostics)
        status='complete' if complete else 'partial' if has_material else 'draft'
        fatal=any(d.get('code')=='FIELD_MAPPING_COLLISION' for d in diagnostics)
        changed_attachments={a.get('id'):a for a in attachments}
        payment_materials_changed=any(
            old.get('type')!=changed_attachments.get(old.get('id'),{}).get('type')
            and (old.get('type')=='payment_screenshot'
                 or changed_attachments.get(old.get('id'),{}).get('type')=='payment_screenshot')
            for old in entry['attachments']
        )
        return {'entry_id':entry['id'],'old_scheme_id':entry.get('scheme_id'),'old_revision_id':entry.get('scheme_revision_id'),
            'scheme_id':scheme_id,'revision_id':revision_id,'title_profile_id':title_id,'old_status':entry['status'],'status':status,
            'values':values,'history_values':histories,'attachments':attachments,'diagnostics':diagnostics,'fatal':fatal,
            'restore_paid_amount':bool(removed_payment_screenshots and not has_payment_after),
            'payment_materials_changed':payment_materials_changed,
            'affected_batches':[r['batch_id'] for r in entry['batch']]}

    def preview_rebind(self,entry_ids,scheme_id,revision_id=None,mappings=None):
        scheme=self.get_scheme(scheme_id)
        if scheme['disabled']:
            raise ValueError('不能应用停用方案。')
        revision_id=revision_id or scheme['current_revision_id']
        if not self.db.conn.execute('SELECT 1 FROM scheme_revision_links WHERE scheme_id=? AND revision_id=?',(scheme_id,revision_id)).fetchone():
            raise ValueError('目标修订不属于该方案。')
        mappings=mappings or {}
        if not isinstance(mappings,dict) or set(mappings)-{'fields','materials','titles'} or any(not isinstance(v,dict) for v in mappings.values()):
            raise ValueError('重绑定映射无效。')
        ids=list(dict.fromkeys(entry_ids))
        if not ids:
            raise ValueError('请选择要应用的条目。')
        if len(ids)>10000:
            raise ValueError('单次最多应用 10000 条。')
        snapshots={key:self._entry_snapshot(key) for key in ids}
        entries=[self._mapped_entry(entry,scheme_id,revision_id,mappings) for entry in snapshots.values()]
        preview_id=uuid.uuid4().hex
        result={'preview_id':preview_id,'scheme_id':scheme_id,'revision_id':revision_id,'entries':entries,
                'diagnostics':[d for e in entries for d in e['diagnostics']],
                'ok':not any(e.get('fatal') for e in entries),
                'blocking_diagnostics':[d for e in entries for d in e['diagnostics'] if d.get('code')=='FIELD_MAPPING_COLLISION']}
        self._rebind_previews[preview_id]={'snapshots':{key:_digest(value) for key,value in snapshots.items()},
            'target_head':scheme['current_revision_id'],'mappings':deepcopy(mappings),'result':result}
        return deepcopy(result)

    @_tracked_operation('validate_rebind')
    def apply_rebind(self,preview_id,operation_id=None):
        saved=self._rebind_previews.get(preview_id)
        if not saved:
            raise ValueError('应用预览失效，请重新预览。')
        result=saved['result']
        if not result.get('ok',True):
            raise ValueError('字段映射发生冲突，请修正映射后重新预览。')
        scheme=self.get_scheme(result['scheme_id'])
        if scheme['disabled'] or scheme['current_revision_id']!=saved['target_head']:
            raise ValueError('目标方案已变化，请重新预览。')
        from ..db.entries import EntryRepo
        total=len(result['entries'])
        with self.db.transaction():
            total=len(saved['snapshots'])
            for index,(entry_id,digest) in enumerate(saved['snapshots'].items(),1):
                self._operation_checkpoint(operation_id,'validate_rebind',index-1,total)
                if _digest(self._entry_snapshot(entry_id))!=digest:
                    raise ValueError('预览后条目、材料或字段已变化，请重新预览。')
                self._operation_checkpoint(operation_id,'validate_rebind',index,total)
            for index,entry in enumerate(result['entries'],1):
                self._operation_checkpoint(operation_id,'rebind_entries',index-1,total)
                entry_id=entry['entry_id']
                self.db.conn.execute('UPDATE entries SET scheme_id=?,scheme_revision_id=?,title_profile_id=?,status_engine_version=?,updated_at=? WHERE id=?',
                    (entry['scheme_id'],entry['revision_id'],entry['title_profile_id'],'',now(),entry_id))
                self.extensions.record_history('entry',entry_id,entry['scheme_id'],'',
                    {'scheme_id':entry['old_scheme_id'],'revision_id':entry['old_revision_id']},
                    {'scheme_id':entry['scheme_id'],'revision_id':entry['revision_id'],'mappings':saved['mappings']},
                    entry['revision_id'],self.get_revision(entry['revision_id'])['manifest']['package_id'],kind='rebind')
                for attachment in entry['attachments']:
                    old=self.db.conn.execute('SELECT type,role_id,role_definition_revision_id,added_at FROM attachments WHERE id=?',(attachment['id'],)).fetchone()
                    if (old['type'],old['role_id'],old['role_definition_revision_id'])!=(attachment['type'],attachment['role_id'],attachment['role_definition_revision_id']):
                        self.extensions.record_history('entry',entry_id,entry['scheme_id'],attachment['id'],
                            {'type':old['type'],'role_id':old['role_id'],'definition_revision_id':old['role_definition_revision_id']},
                            {'type':attachment['type'],'role_id':attachment['role_id'],'definition_revision_id':attachment['role_definition_revision_id']},
                            entry['revision_id'],kind='material')
                    self.db.conn.execute('''UPDATE attachments SET type=?,role_id=?,role_definition_revision_id=?,
                        recognition_version=?,recognition_status=?,recognized_value=?,recognition_message=? WHERE id=?''',
                        (attachment['type'],attachment['role_id'],attachment['role_definition_revision_id'],
                         attachment.get('recognition_version',''),attachment.get('recognition_status',''),
                         attachment.get('recognized_value',''),attachment.get('recognition_message',''),attachment['id']))
                for old_value in entry.get('history_values',[]):
                    source_revision=old_value.get('definition_revision_id')
                    if not source_revision:
                        continue
                    try:
                        source_definition=self.get_revision(source_revision)
                    except ValueError:
                        continue
                    self.extensions.record_history('entry',entry_id,entry['scheme_id'],old_value['field_id'],
                        None,old_value.get('value'),source_revision,
                        source_definition.get('manifest',{}).get('package_id',''),kind='rebind')
                if entry['values']:
                    self.extensions.save_values('entry',entry_id,entry['scheme_id'],entry['revision_id'],entry['values'],commit=False)
                if entry.get('restore_paid_amount'):
                    EntryRepo(self.db).restore_paid_amount_after_last_payment(entry_id,commit=False)
                if entry.get('payment_materials_changed'):
                    EntryRepo(self.db).refresh_payment_check(entry_id,commit=False)
                EntryRepo(self.db).recompute_status(entry_id,commit=False)
                self._operation_checkpoint(operation_id,'rebind_entries',index,total)
        self._rebind_previews.pop(preview_id,None)
        return {'changed':len(result['entries']),'entry_ids':[e['entry_id'] for e in result['entries']],
            'scheme_id':result['scheme_id'],'revision_id':result['revision_id']}

    def update_legacy_preference(self,key,value):
        """Forward old business writers to one authoritative current model."""
        if key not in LEGACY_KEYS:
            raise ValueError('此设置不属于报账方案兼容入口。')
        scheme=self.get_scheme()
        if key.startswith('tidoc.operator.'):
            field={'name':'name','student_id':'personnel_id','contact':'contact','bank_name':'bank_name','bank_card':'account_number'}.get(key.rsplit('.',1)[-1])
            if not field or not isinstance(value,str):
                raise ValueError('收款字段必须是字符串。')
            with self.db.transaction():
                selected=self.payees.get_default(scheme['id'])
                if selected:
                    result=self.payees.update(selected['id'],**{field:value},commit=False)
                else:
                    result=self.payees.create(**{field:value},commit=False)
                    self.payees.set_default(scheme['id'],result['id'],commit=False)
            return result
        if key=='tidoc.titleProfiles':
            return self.update_titles(json.loads(value) if isinstance(value,str) else value)
        if key=='tidoc.materialRequirements':
            return self.update_material_requirements(json.loads(value) if isinstance(value,str) else value)
        prefs={key:value}
        changes=self._legacy_overrides(scheme['definition'],prefs)
        overrides=deepcopy(scheme['overrides'])
        overrides.setdefault('settings',{}).update(changes.get('settings',{}))
        for section in ('organization','titles','materials','rules'):
            if section in changes:
                overrides[section]=changes[section]
        return self._change_definition(scheme['id'],scheme['current_revision_id'],overrides)

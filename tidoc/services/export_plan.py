"""Export planning, preflight, immutable snapshots and atomic delivery."""
from __future__ import annotations

import json
import shutil
import threading
import uuid
import zipfile
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path

from ..db.entries import EntryRepo
from ..db.profiles import ProfileRepo
from ..db.batches import BatchRepo
from ..db.export_jobs import ExportJobRepo
from ..adapters.cache import PreviewCache
from .export_context import (ROLE_TYPES, build_export_context, digest_data, filename_for,
                             get_path, json_data, project_entry, safe_name, title_key)
from .exports import (GENERIC_ATTACHMENTS, GENERIC_OVERVIEW, GENERIC_MATERIALS, confined_path,
                      resource_digest, write_attachment_zip, write_xlsx)


class ExportPreflightError(ValueError):
    def __init__(self,diagnostics):
        self.diagnostics=diagnostics
        super().__init__('；'.join(item['message'] for item in diagnostics if item.get('severity') in ('required','blocked')) or '导出预检未通过')


def _diagnostic(code,message,*,severity='blocked',**extra):
    return {'code':code,'severity':severity,'message':message,'stage':'export',**extra}


class ExportPlanner:
    def __init__(self,db,data_root,adapters):
        self.db=db;self.data_root=data_root;self.adapters=adapters
        self.root=Path(getattr(data_root,'root',data_root)).resolve()
        self.entries=EntryRepo(db);self.profiles=ProfileRepo(db);self.batches=BatchRepo(db)
        self.jobs=getattr(adapters,'jobs',None) or ExportJobRepo(db)
        # Plans hold full render contexts: keep few, drop idle ones. An expired plan must be previewed again.
        self._plans=PreviewCache();self._cancellations=OrderedDict();self._progress={};self._lock=threading.RLock()

    def _cancellation(self,job_id):
        # One event per job; the oldest are dropped so cancelled previews cannot accumulate.
        with self._lock:
            event=self._cancellations.get(job_id)
            if event is None:
                event=self._cancellations[job_id]=threading.Event()
                while len(self._cancellations)>256:
                    self._cancellations.popitem(last=False)
            return event

    def _set_progress(self,job_id,phase,completed=0,total=0):
        messages={'verifying_resources':'正在核对原始材料',
                  'rendering_core':'正在生成总览和附件包',
                  'rendering_print':'正在生成 Word 和材料 PDF',
                  'verifying_outputs':'正在检查生成文件',
                  'publishing':'正在保存交付文件',
                  'completed':'生成完成','failed':'生成失败','cancelled':'已取消生成'}
        status=phase if phase in ('completed','failed','cancelled') else 'running'
        with self._lock:
            self._progress[job_id]={'job_id':job_id,'status':status,'phase':phase,
                'message':messages[phase],'completed':completed,'total':total}
            if len(self._progress)>256:
                finished=next((key for key,value in self._progress.items()
                               if key!=job_id and value['status']!='running'),None)
                if finished:self._progress.pop(finished)

    def get_progress(self,job_id):
        # This endpoint stays usable while the API's database lock is held by
        # the rendering call. It only reads bounded in-memory task information.
        with self._lock:
            return deepcopy(self._progress.get(job_id))

    def _fields(self,scope,owner,scheme_id,definition,revision_id):
        if not owner or not scheme_id:
            return {}
        return self.adapters.extensions.get_values(scope,owner,scheme_id,definition.get('manifest',{}).get('package_id',''),revision_id=revision_id)

    def _load_entries(self,entry_ids):
        if not isinstance(entry_ids,list) or not entry_ids or len(entry_ids)>10000:
            raise ValueError('请选择 1–10000 个条目')
        loaded=[]
        for eid in dict.fromkeys(entry_ids):
            entry=self.entries.get(eid)
            if not entry: raise ValueError('选中的条目不存在，请重新选择。')
            entry=deepcopy(entry)
            entry['source_scheme_unbound']=bool(entry.get('adapter_sources') and not entry.get('scheme_revision_id'))
            if not entry.get('batch_id') and entry.get('batches'):
                entry['batch_id']=entry['batches'][0].get('id') or entry['batches'][0].get('batch_id')
            definition=self.adapters.context_for_entry(entry)
            if not entry.get('items'):
                from ..engine import RecognitionContext, parse_pdf
                for attachment in entry.get('attachments',[]):
                    if attachment.get('type')!='invoice_pdf':
                        continue
                    try:
                        path=self.data_root.attachments_dir/attachment['stored_path']
                        parsed=parse_pdf(path,context=RecognitionContext.from_definition(definition))
                        if parsed.items and (not parsed.invoice_no or parsed.invoice_no==entry.get('invoice_no','')):
                            entry['items']=[item.to_dict() for item in parsed.items]
                            break
                    except Exception:
                        continue
            if not entry.get('scheme_id') or not entry.get('scheme_revision_id'):
                entry['scheme_id'],entry['scheme_revision_id']=self.adapters.default_binding()
            revision=entry['scheme_revision_id'];sid=entry['scheme_id']
            entry['extension_values']=self._fields('entry',eid,sid,definition,revision)
            if entry.get('batch_id'):
                batch=self.batches.get(entry['batch_id'])
                batch={key:batch.get(key) for key in ('id','name','note','updated_at','output_settings')}
                batch['fields']=self._fields('batch',batch['id'],sid,definition,revision)
                entry['batch']=batch
            loaded.append((entry,definition))
        return loaded

    def _source_fingerprint(self,loaded,options):
        state=[]
        for entry,definition in loaded:
            sid=entry['scheme_id'];revision=entry['scheme_revision_id']
            profile=self.profiles.get(entry.get('profile_id')) if entry.get('profile_id') else {}
            scheme=self.adapters.get_scheme(sid)
            explicit=(options.get('payee_ids_by_binding') or {}).get(sid+':'+revision,(options.get('payee_ids_by_revision') or {}).get(revision,options.get('payee_id')))
            payees={mode:self.adapters.payees.resolve(sid,profile_id=entry.get('profile_id'),payee_id=explicit,batch_id=entry.get('batch_id'),mode=mode) for mode in ('single','by_claimant')}
            extensions={scope+':'+owner:self._fields(scope,owner,sid,definition,revision) for scope,owner in [('scheme',sid)]+[('payee',payee['id']) for payee in payees.values() if payee]}
            state.append({'entry':entry,'definition':definition,'profile':profile,'scheme_name':scheme['name'],'payees':payees,'extensions':extensions})
        return digest_data(state)

    def _effective_output(self,output,definition,options,batch_settings=None):
        from ..adapters.registry import SETTINGS
        from ..adapters.resolver import _valid_setting
        out=deepcopy(output)
        settings=definition.get('effective_settings') or {}
        descriptors=definition.get('scheme',{}).get('settings',{})
        binding=options.get('_binding_key')
        binding_options=(options.get('output_options_by_binding') or {}).get(binding,{})
        overrides=binding_options.get(output['id'],(options.get('output_options') or {}).get(output['id'])) or {}
        allowed={'amount_basis','payee_mode','sort_by','pdf'}
        if set(overrides)-allowed:
            raise ValueError('本次导出不能改写输出的列、材料角色或模板缺省值；请修改方案。')
        def setting(key,declared,explicit=None,has_explicit=False,scope='export'):
            spec=SETTINGS[key]
            descriptor=descriptors.get(key,{})
            value=settings.get(key,spec['default'])
            resolved=value if key in descriptors or value!=spec['default'] else declared
            if has_explicit:
                if scope not in spec.get('scopes',[]):
                    raise ValueError('设置不允许在此作用域覆盖：'+key)
                if not _valid_setting(key,explicit):
                    raise ValueError('输出设置值类型或范围无效：'+key)
                fixed=descriptor.get('fixed',value)
                if ('fixed' in descriptor or descriptor.get('editable') is False) and explicit!=fixed:
                    raise ValueError('方案固定或不可编辑的设置不能覆盖：'+key)
                resolved=explicit
            return resolved
        def value_for(name,declared):
            key='print.'+name
            resolved=setting(key,declared)
            if batch_settings and key in batch_settings:
                resolved=setting(key,resolved,batch_settings[key],True,'batch')
            explicit=options[name] if name in options else overrides.get(name)
            return setting(key,resolved,explicit,name in options or name in overrides)
        for name,fallback in [('amount_basis','invoice'),('payee_mode','none'),('sort_by','selection')]:
            out[name]=value_for(name,out.get(name,fallback))
        if output.get('payee_mode','none')=='none':
            if ('payee_mode' in options or 'payee_mode' in overrides) and out['payee_mode']!='none':
                raise ValueError('此输出不含收款信息，不能改为收款输出。')
            out['payee_mode']='none'
        if out['type']=='pdf_bundle':
            pdf=out.setdefault('pdf',{})
            changed=overrides.get('pdf') or {}
            mapping={'numbering':'print.numbering','image_layout':'print.image_layout','content_order':'print.content_order'}
            if set(changed)-set(mapping):
                raise ValueError('本次导出只能覆盖编号、图片布局或材料顺序。')
            for name,key in mapping.items():
                declared=pdf.get(name,SETTINGS[key]['default'])
                resolved=setting(key,declared)
                if batch_settings and key in batch_settings:
                    resolved=setting(key,resolved,batch_settings[key],True,'batch')
                if name in options:resolved=setting(key,resolved,options[name],True)
                if name in changed:resolved=setting(key,resolved,changed[name],True)
                pdf[name]=resolved
        return out

    def _template_resource(self,revision,output):
        if hasattr(self.adapters,'resource_root'):
            base=Path(self.adapters.resource_root(revision))
        else:
            record=self.adapters.packages.revision_record(revision)
            package=self.adapters.packages.get_package(record['content_hash'])
            base=self.root/package['resource_path']
        path=confined_path(base,output['template'])
        if not path.is_relative_to(self.root):
            raise ValueError('模板不在不可变方案资源目录内')
        return {'path':path.relative_to(self.root).as_posix(),'sha256':resource_digest(path),'kind':'template'}

    def _resources(self,entries,output):
        resources=[]
        settings=output.get('pdf') if output['type']=='pdf_bundle' else output.get('attachments') or {}
        settings=settings or {}
        roles=settings.get('roles') or settings.get('include_roles')
        if output['type']=='pdf_bundle' and not roles:roles=['invoice','payment_screenshot','inspection_pdf']
        for entry in entries:
            role_defs={material['id']:material for material in self.adapters.context_for_entry(entry).get('materials',[])}
            for index,att in enumerate(entry.get('attachments') or []):
                role=att.get('role_id') or ROLE_TYPES.get(att.get('type'),'other')
                if roles and role not in roles:continue
                if output['type'] in ('docx','xlsx'):continue
                if output['type']=='pdf_bundle' and role=='invoice' and att.get('type')=='invoice_xml':continue
                source=confined_path(self.root/'attachments',att['stored_path'])
                resources.append({'id':att.get('id') or f'{entry["id"]}-{index}','entry_id':entry['id'],'path':source.relative_to(self.root).as_posix(),'sha256':resource_digest(source),'role_id':role,'original_name':att.get('original_name') or source.name,'label':role_defs.get(role,{}).get('label') or att.get('role_label') or ''})
        return resources

    def _preflight(self,context,definition,output,raw_entries,resources,template=None,generic=False):
        from ..adapters.policy import evaluate_condition,evaluate_policy,validate_field_value
        from ..adapters.models import FieldValidationError
        from tidoc_print.context_validation import validate_context
        diagnostics=list(context['diagnostics'])
        diagnostics.extend({**item,'output_id':output['id']}
                           for item in validate_context(context))
        def add(code,message,**kwargs):
            diagnostics.append(_diagnostic(code,message,output_id=output['id'],**kwargs))
        raw_by_id={raw['id']:raw for raw in raw_entries}
        export_fields=context.get('export',{}).get('fields',{})
        declared_export={field['id']:field for field in definition.get('fields',[]) if field.get('scope')=='export'}
        for field_id,value in export_fields.items():
            if field_id not in declared_export:
                add('UNKNOWN_EXPORT_FIELD','本次导出字段不属于此方案修订。',target='export.fields.'+field_id)
                continue
            try:validate_field_value(declared_export[field_id],value)
            except FieldValidationError as exc:
                diagnostics.extend({**item,'output_id':output['id']} for item in exc.diagnostics)
        try:
            from datetime import date
            date.fromisoformat(context['export']['date'])
        except (ValueError,TypeError):add('INVALID_EXPORT_DATE','导出日期必须为 YYYY-MM-DD。',target='export.date')
        for entry in context['entries']:
            raw=raw_by_id[entry['id']]
            policy_context={**context,'entry':entry,'invoice':entry['invoice'],'definition':definition}
            if output.get('when'):
                applies=evaluate_condition(output['when'],policy_context,stage='export')
                if applies is not True:
                    add('OUTPUT_NOT_APPLICABLE' if applies is False else 'OUTPUT_CONDITION_MISSING','所选输出不适用于此条目。' if applies is False else '请补充输出适用条件信息。',entry_id=entry['id'])
            if not generic and raw.get('check_status')=='blocked':
                add('ENTRY_BLOCKED','条目存在核心阻断项，请先核对。',entry_id=entry['id'])
            if not generic and raw.get('source_scheme_unbound'):
                add('EXTERNAL_SCHEME_PENDING','来源方案尚未应用，请核对后选择本机报账方案。',entry_id=entry['id'],target='scheme')
            counts={}
            for att in raw.get('attachments') or []:
                if att.get('role_definition_revision_id') and att['role_definition_revision_id']!=raw.get('scheme_revision_id'):continue
                role=att.get('role_id') or ROLE_TYPES.get(att.get('type'),'other')
                counts[role]=counts.get(role,0)+1
            if not generic:
                for required in output.get('required_fields',[]):
                    if '.fields.' not in required and get_path(policy_context,required) in (None,''):
                        add('OUTPUT_FIELD_REQUIRED','请补充此输出需要的信息。',entry_id=entry['id'],target=required)
                relevant=deepcopy(definition)
                # Only scopes used by selected output block this output; no payee paragraph means no payee fields.
                references=set(output.get('required_fields') or [])
                if output['type']=='xlsx':
                    references.update(col.get('source') or col.get('field') or col.get('id') or '' for col in output.get('columns',[]))
                if template:
                    from .export_context import extension_defaults
                    with zipfile.ZipFile(self.root/template['path']) as archive:
                        text='\n'.join(archive.read(name).decode('utf-8','ignore') for name in archive.namelist() if name.endswith('.xml'))
                    references.update(f'{f["scope"]}.fields.{f["id"]}' for f in definition.get('fields',[]) if f'.fields.{f["id"]}' in text)
                if references:
                    selected={**output,'required_fields':sorted(references)}
                    relevant['outputs']=[selected]
                if output['payee_mode']=='none':
                    relevant['fields']=[field for field in relevant.get('fields',[]) if field['scope']!='payee']
                for stage in ('complete','export'):
                    for diagnostic in evaluate_policy(relevant,policy_context,counts,stage=stage):
                        diagnostics.append({**diagnostic,'entry_id':entry['id'],'output_id':output['id']})
            # Core nonoptional accounting invariants apply to every monetary writer.
            if output['type'] in ('docx','xlsx') and entry['total'] is None:
                add('MISSING_INVOICE_AMOUNT','发票金额缺失或无效，不能将其当作零。',entry_id=entry['id'])
            if output['amount_basis']=='paid' and entry['paid_amount'] is None:
                add('MISSING_PAID_AMOUNT','按实付导出需要填写有效实付金额，零值有效。',entry_id=entry['id'])
            if output['type']=='pdf_bundle' and 'invoice' in (output.get('pdf',{}).get('roles') or output.get('pdf',{}).get('include_roles') or ['invoice']):
                if not any(r['entry_id']==entry['id'] and r['role_id']=='invoice' and Path(r['path']).suffix.lower()=='.pdf' for r in resources):
                    add('MISSING_INVOICE_PDF','打印票面需要发票 PDF；XML 不能替代票面。',entry_id=entry['id'],target='invoice')
        if output['payee_mode']!='none' and context['payee'] is None:
            add('MISSING_PAYEE','请选择一个完整收款对象；分别收款请补齐报账人映射。',target='payee')
        payee=context.get('payee')
        if payee and output['payee_mode']!='none':
            required=['name'] if payee.get('account_type')=='none' else ['name','bank_name','account_number']
            for field in required:
                if not payee.get(field):add('MISSING_PAYEE_FIELD','收款信息缺项，请核对选定对象。',target='payee.'+field)
        if output['type']=='pdf_bundle':
            if not resources:add('NO_PRINT_MATERIALS','没有可生成 PDF 的材料。')
            for resource in resources:
                suffix=Path(resource['path']).suffix.lower()
                if suffix not in ('.pdf','.png','.jpg','.jpeg'):
                    add('UNCONVERTIBLE_MATERIAL','材料不能转换为 PDF，请使用附件整理包保留原文件。',entry_id=resource['entry_id'],target=resource['role_id'])
                else:
                    try:
                        if suffix=='.pdf':
                            from pypdf import PdfReader
                            reader=PdfReader(self.root/resource['path'])
                            if reader.is_encrypted or not reader.pages:raise ValueError('加密或空 PDF')
                        else:
                            from PIL import Image
                            with Image.open(self.root/resource['path']) as image:image.verify()
                    except ImportError:pass # external component completes structure validation
                    except Exception:add('INVALID_MATERIAL','材料损坏、加密或为空，请替换后再导出。',entry_id=resource['entry_id'],target=resource['role_id'])
        if output['type'] in ('docx','pdf_bundle'):
            from .printing import component_status
            status=component_status(self.data_root.components_dir)
            if not status.get('available'):add('PRINT_COMPONENT_REQUIRED','需要安装打印导出组件。')
            elif 2 not in status.get('ipc_versions',[]) or output['type'] not in status.get('renderers',[]):add('PRINT_COMPONENT_UPDATE_REQUIRED','打印导出组件缺少所需能力，请检查更新。')
        if template:
            try:
                from tidoc_print.template_validation import validate_template
                diagnostics.extend({**item,'output_id':output['id']} for item in validate_template(self.root/template['path'],definition,context))
            except ImportError:
                pass
        if output['type']=='xlsx':
            titles=[entry['title'] for entry in context['entries']]
            if len({title_key(title,entry['id']) for title,entry in zip(titles,context['entries'])})>1:
                sources={col.get('source') or col.get('field') or col.get('id') for col in output.get('columns') or GENERIC_OVERVIEW['columns']}
                if not sources.intersection({'title','row.title','entry.title.name','title.name'}):add('TITLE_COLUMN_REQUIRED','跨抬头总览必须包含抬头列。')
                if len({title['name'] for title in titles})<len({title_key(title,entry['id']) for title,entry in zip(titles,context['entries'])}) and not sources.intersection({'tax_id','row.tax_id','entry.title.tax_id','title.tax_id'}):
                    # Generic writer always includes an extra tax ID column when ambiguity exists.
                    if generic:output['columns']=deepcopy(output['columns'])+[{'id':'tax_id','source':'entry.title.tax_id','label':'税号','width':24}]
                    else:add('TAX_ID_COLUMN_REQUIRED','同名不同税号的总览必须包含税号列。')
        return diagnostics

    def preview(self,entry_ids,output_ids=None,options=None):
        options=json_data(options or {})
        binding_outputs=options.get('output_ids_by_binding')
        if binding_outputs is not None and (not isinstance(binding_outputs,dict) or any(not isinstance(value,list) or any(not isinstance(oid,str) for oid in value) for value in binding_outputs.values())):
            raise ValueError('分组输出选择必须为方案修订与输出 ID 列表。')
        if output_ids is None and options.get("output_ids") is not None:
            output_ids=options["output_ids"]
        loaded=self._load_entries(entry_ids)
        profiles={profile['id']:profile for profile in self.profiles.list()}
        by_revision={}
        for entry,definition in loaded:
            by_revision.setdefault((entry['scheme_id'],entry['scheme_revision_id']),{'entries':[],'definition':definition})['entries'].append(entry)
        requested=set(output_ids) if output_ids is not None else None
        diagnostics=[];files=[];groups=[];used=set();recognized=set()
        for sidrev,data in by_revision.items():
            sid,revision=sidrev;definition=data['definition'];scheme=self.adapters.get_scheme(sid)
            binding_key=sid+':'+revision
            binding_options={**options,'_binding_key':binding_key}
            selection=options.get('output_ids_by_binding')
            selected_for_binding=set(selection.get(binding_key,[])) if selection is not None else requested
            scheme={'id':sid,'name':scheme['name'],'fields':self._fields('scheme',sid,sid,definition,revision)}
            for declared in definition.get('outputs',[]):
                recognized.add(declared['id'])
                configured_defaults=definition.get('effective_settings',{}).get('print.default_outputs',definition.get('scheme',{}).get('default_outputs'))
                enabled=declared['id'] in configured_defaults if configured_defaults is not None else declared.get('default_selected',False)
                if selected_for_binding is not None and declared['id'] not in selected_for_binding or selected_for_binding is None and not enabled:continue
                output=self._effective_output(declared,definition,binding_options)
                subgroup={}
                group_by=output.get('group_by') or []
                references=json.dumps(output,ensure_ascii=False)
                batch_dependent='batch.fields.' in references or 'batch.name' in references or any(field.get('scope')=='batch' and 'export' in field.get('required_at',[]) for field in definition.get('fields',[]))
                if output['type']=='docx':
                    try:
                        template=self._template_resource(revision,output)
                        with zipfile.ZipFile(self.root/template['path']) as archive:
                            batch_dependent=batch_dependent or any(b'batch.' in archive.read(name) for name in archive.namelist() if name.endswith('.xml'))
                    except Exception:template=None
                for entry in data['entries']:
                    temporary_batch=(options.get('batch_fields_by_binding') or {}).get(binding_key)
                    if not entry.get('batch_id') and temporary_batch is not None:
                        from ..adapters.policy import validate_field_value
                        fields={field['id']:field for field in definition.get('fields',[]) if field['scope']=='batch'}
                        if not isinstance(temporary_batch,dict) or set(temporary_batch)-set(fields):
                            raise ValueError('本次批次信息包含未定义字段。')
                        for field_id,value in temporary_batch.items():
                            validate_field_value(fields[field_id],value)
                        entry=deepcopy(entry)
                        entry['batch']={'id':'','name':'未进批次（本次导出）','notes':'','fields':deepcopy(temporary_batch)}
                    batch_settings=(entry.get('batch') or {}).get('output_settings')
                    entry_output=self._effective_output(declared,definition,binding_options,batch_settings)
                    explicit_payee=(options.get('payee_ids_by_binding') or {}).get(binding_key,(options.get('payee_ids_by_revision') or {}).get(revision,options.get('payee_id'))) if entry_output['payee_mode']=='single' else None
                    payee=self.adapters.payees.resolve(sid,profile_id=entry.get('profile_id'),payee_id=explicit_payee,batch_id=entry.get('batch_id'),mode=entry_output['payee_mode'])
                    projected=project_entry(entry,profiles.get(entry.get('profile_id')),definition)
                    key=[('settings',digest_data(entry_output))]
                    if output['type'] in ('docx','pdf_bundle') or 'title' in group_by:key.append(title_key(projected['title'],entry['id']))
                    if entry_output['payee_mode']!='none':key.append(('payee',payee['id'] if payee else 'missing'))
                    if batch_dependent or 'batch' in group_by:key.append(('batch',entry.get('batch_id')))
                    for grouping in group_by:
                        if grouping=='claimant':key.append(('claimant',entry.get('profile_id')))
                        elif grouping.startswith('batch.fields.'):
                            value=get_path(projected,grouping)
                            if isinstance(value,(dict,list)):raise ValueError('批次分组字段必须是标量')
                            key.append((grouping,value))
                    subgroup.setdefault(repr(key),{'entries':[],'payee':payee,'output':entry_output})['entries'].append(entry)
                for key,group in subgroup.items():
                    raw=group['entries'];payee=deepcopy(group['payee'])
                    output=group['output']
                    if payee:payee['fields']=self._fields('payee',payee['id'],sid,definition,revision)
                    context_options={**options, "fields": (options.get("export_fields_by_binding") or {}).get(binding_key,(options.get("export_fields_by_revision") or {}).get(revision, options.get("fields", {})))}
                    context_options.pop("export_fields_by_revision",None)
                    for local_key in ('export_fields_by_binding','output_ids_by_binding','payee_ids_by_binding','payee_ids_by_revision','output_options_by_binding','batch_fields_by_binding','_binding_key'):
                        context_options.pop(local_key,None)
                    context=build_export_context(raw,definition,output,profiles,payee=payee,scheme=scheme,options=context_options)
                    if len(context['rows'])>50000:raise ValueError('导出明细行超过 50000，请缩小选择范围。')
                    group_id=digest_data([sid,revision,output['id'],key])[:16]
                    filename=filename_for(output,context)
                    if filename.casefold() in used:
                        path=Path(filename);filename=safe_name(path.stem,max_len=95)+'_'+group_id[:8]+path.suffix
                    used.add(filename.casefold())
                    item={'group_id':group_id,'output':deepcopy(output),'context':context,'filename':filename,'definition':definition,'resources':[]}
                    template=None
                    try:
                        item['resources']=self._resources(raw,output)
                        if output['type']=='docx':
                            template=self._template_resource(revision,output)
                            item['template']=template['path'];item['template_sha256']=template['sha256']
                    except Exception:
                        diagnostics.append(_diagnostic('MISSING_RESOURCE','所选输出的模板或材料缺失，请补齐后重新预览。',output_id=output['id'],group_id=group_id))
                    group_diags=self._preflight(context,definition,item['output'],raw,item['resources'],template)
                    diagnostics.extend({**diag,'group_id':group_id} for diag in group_diags)
                    files.append(item)
                    groups.append({'group_id':group_id,'scheme_id':sid,'revision_id':revision,'scheme_name':scheme['name'],'title':context['title'],'batch':context['batch'],'payee':{'id':payee['id'],'name':payee['name'],'account_tail':payee.get('account_number','')[-4:]} if payee else None,'entry_ids':[e['id'] for e in raw],'output_id':output['id'],'filename':filename,'totals':context['totals']})
            if selection is not None and selected_for_binding is not None:
                unknown=selected_for_binding-{out['id'] for out in definition.get('outputs',[])}
                if unknown:diagnostics.append(_diagnostic('UNKNOWN_OUTPUT','此方案没有这些输出：'+', '.join(sorted(unknown)),scheme_id=sid,revision_id=revision))
        for generic_output in (GENERIC_OVERVIEW,GENERIC_ATTACHMENTS):
            recognized.add(generic_output['id'])
            if requested is None or generic_output['id'] not in requested:continue
            output=self._effective_output(generic_output,{},options)
            raw=[{**entry,'extension_values':{},'batch':({**entry['batch'],'fields':{}} if entry.get('batch') else None)} for entry,definition in loaded]
            generic_options={key:value for key,value in options.items() if key not in ("fields","export_fields_by_revision","export_fields_by_binding","payee_id","payee_ids_by_binding","payee_ids_by_revision","output_ids_by_binding","output_options_by_binding","_binding_key")}
            context=build_export_context(raw,output=output,profiles=profiles,options=generic_options)
            group_id=digest_data([output['id'],entry_ids])[:16]
            resources=[]
            try:resources=self._resources(raw,output)
            except Exception:diagnostics.append(_diagnostic('MISSING_RESOURCE','附件已缺失，请补齐后重新预览。',output_id=output['id']))
            diagnostics.extend(self._preflight(context,{},output,raw,resources,generic=True))
            generic_filename=filename_for(output,context)
            if generic_filename.casefold() in used:
                p=Path(generic_filename);generic_filename=safe_name(p.stem,max_len=95)+'_'+group_id[:8]+p.suffix
            used.add(generic_filename.casefold())
            files.append({'group_id':group_id,'output':output,'context':context,'filename':generic_filename,'resources':resources})
            groups.append({'group_id':group_id,'scheme_id':None,'title':None,'batch':None,'payee':None,'entry_ids':entry_ids,'output_id':output['id'],'filename':files[-1]['filename'],'totals':context['totals']})
        recognized.add(GENERIC_MATERIALS['id'])
        if requested and GENERIC_MATERIALS['id'] in requested:
            subjects={}
            for entry,definition in loaded:
                projected=project_entry(entry,profiles.get(entry.get('profile_id')),definition)
                subjects.setdefault(title_key(projected['title'],entry['id']),[]).append(entry)
            for subject,raw in subjects.items():
                raw=[{**entry,'extension_values':{},'batch':({**entry['batch'],'fields':{}} if entry.get('batch') else None)} for entry in raw]
                output=self._effective_output(GENERIC_MATERIALS,{},options)
                roles=list(dict.fromkeys(['invoice']+[(att.get('role_id') or ROLE_TYPES.get(att.get('type'),'other')) for entry in raw for att in entry.get('attachments',[])]))
                output['pdf']['include_roles']=roles
                context=build_export_context(raw,output=output,profiles=profiles,options={key:value for key,value in options.items() if key in ('date','sort_by','numbering','image_layout','content_order')})
                group_id=digest_data([output['id'],subject,[entry['id'] for entry in raw]])[:16]
                resources=[]
                try:resources=self._resources(raw,output)
                except Exception:diagnostics.append(_diagnostic('MISSING_RESOURCE','附件已缺失，请补齐后重新预览。',output_id=output['id']))
                diagnostics.extend(self._preflight(context,{},output,raw,resources,generic=True))
                filename=filename_for(output,context)
                if filename.casefold() in used:
                    path=Path(filename);filename=safe_name(path.stem,max_len=95)+'_'+group_id[:8]+path.suffix
                used.add(filename.casefold())
                files.append({'group_id':group_id,'output':output,'context':context,'filename':filename,'resources':resources})
                groups.append({'group_id':group_id,'scheme_id':None,'title':context['title'],'batch':None,'payee':None,'entry_ids':[entry['id'] for entry in raw],'output_id':output['id'],'filename':filename,'totals':context['totals']})
        if requested and requested-recognized:
            diagnostics.append(_diagnostic('UNKNOWN_OUTPUT','未知输出：'+', '.join(sorted(requested-recognized))))
        if not files:diagnostics.append(_diagnostic('NO_OUTPUT_SELECTED','请选择至少一种输出。'))
        plan_id=uuid.uuid4().hex
        ok=not any(diag.get('severity') in ('blocked','required') for diag in diagnostics)
        revisions=sorted({entry['scheme_revision_id'] for entry,definition in loaded})
        from tidoc import __version__ as core_version
        from .printing import component_status
        component=component_status(self.data_root.components_dir)
        snapshot={'schema_version':1,'core_version':core_version,'component_version':component.get('version',''),'context_version':1,'files':files,'groups':groups,'diagnostics':diagnostics,'options':options,'entry_ids':list(dict.fromkeys(entry_ids)),'revision_ids':revisions,'fingerprint':self._source_fingerprint(loaded,options)}
        with self._lock:self._plans[plan_id]=snapshot
        return {'plan_id':plan_id,'groups':groups,'files':[{'group_id':item['group_id'],'output_id':item['output']['id'],'type':item['output']['type'],'filename':item['filename']} for item in files],'diagnostics':diagnostics,'ok':ok}

    def _job_dir(self,job_id):
        return self.root/'export_jobs'/job_id

    def _save_record_files(self,job):
        directory=self._job_dir(job['id']);directory.mkdir(parents=True,exist_ok=True)
        for name,value in [('context.json',job['snapshot']),('resources.json',job['resources']),('result.json',{key:job[key] for key in ('id','status','files','diagnostics')})]:
            temp=directory/(name+'.tmp')
            temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),'utf-8');temp.replace(directory/name)

    def _new_job(self,snapshot,job_id=None):
        resources=[]
        for item in snapshot['files']:
            resources.extend(item.get('resources') or [])
            if item.get('template'):resources.append({'path':item['template'],'sha256':item['template_sha256'],'kind':'template'})
        job=self.jobs.create(snapshot=snapshot,revision_ids=snapshot['revision_ids'],resources=resources,options=snapshot['options'],job_id=job_id,diagnostics=snapshot['diagnostics'])
        try:
            self._save_record_files(job)
        except Exception as exc:
            diagnostics=[_diagnostic('EXPORT_RECORD_FAILED','无法保存导出记录，请检查数据目录的空间与写入权限。')]
            self.jobs.fail(job['id'],diagnostics)
            self._set_progress(job['id'],'failed')
            shutil.rmtree(self._job_dir(job['id']),ignore_errors=True)
            raise ExportPreflightError(diagnostics) from exc
        return job

    def run(self,plan_id,output_dir=None):
        with self._lock:
            saved=self._plans.get(plan_id)
            if saved is None:raise ValueError('导出预览已失效，请重新预览。')
            snapshot=deepcopy(saved)
        if any(diag.get('severity') in ('blocked','required') for diag in snapshot['diagnostics']):raise ExportPreflightError(snapshot['diagnostics'])
        loaded=self._load_entries(snapshot['entry_ids'])
        if self._source_fingerprint(loaded,snapshot['options'])!=snapshot['fingerprint']:
            raise ExportPreflightError([_diagnostic('STALE_EXPORT_PLAN','预览后条目、批次、方案字段或收款信息已变化，请重新预览。')])
        try:
            job=self._new_job(snapshot,plan_id)
        finally:
            with self._lock:self._plans.pop(plan_id,None)
        return self._execute(job,output_dir)

    def _execute(self,job,output_dir=None):
        job_id=job['id'];event=self._cancellation(job_id)
        directory=self._job_dir(job_id)
        staging=directory/'staging';rendered=directory/'rendered'
        destination=(Path(output_dir).expanduser().resolve() if output_dir else self.data_root.exports_dir)/job_id
        def check():
            if event.is_set():raise RuntimeError('导出任务已取消')
        moved=False
        try:
            check()
            self.jobs.update(job_id,status='running')
            (directory/'destination.json').write_text(json.dumps({'directory':str(destination),'publish_directory':str(destination.parent/('.tidoc-'+job_id))}),'utf-8')
            staging.mkdir(parents=True,exist_ok=False)
            self._set_progress(job_id,'verifying_resources',0,len(job['resources']))
            for index,resource in enumerate(job['resources'],1):
                check();source=confined_path(self.root,resource['path'])
                if resource_digest(source)!=resource['sha256']:raise ValueError('原任务材料或模板已变化，不能按原数据重新生成。')
                self._set_progress(job_id,'verifying_resources',index,len(job['resources']))
            print_files=[];outputs=[]
            core_count=sum(item['output']['type'] in ('xlsx','attachment_zip') for item in job['snapshot']['files'])
            core_completed=0
            self._set_progress(job_id,'rendering_core',0,core_count)
            for item in job['snapshot']['files']:
                check();kind=item['output']['type'];path=staging/item['filename']
                if kind=='xlsx':write_xlsx(item['context'],item['output'],path)
                elif kind=='attachment_zip':write_attachment_zip(item['context'],item['output'],item.get('resources') or [],path,self.root)
                else:print_files.append(item)
                if kind in ('xlsx','attachment_zip'):
                    core_completed+=1
                    self._set_progress(job_id,'rendering_core',core_completed,core_count)
            if print_files:
                self._set_progress(job_id,'rendering_print')
                from .printing import execute_print_request
                request={'ipc_version':2,'job_id':job_id,'files':print_files,'resources_root':str(self.root),'output_dir':str(rendered),'timeout_seconds':job['options'].get('timeout_seconds',120)}
                execute_print_request(request,self.data_root.components_dir,cancel_check=event.is_set)
                for path in rendered.iterdir():shutil.move(str(path),str(staging/path.name))
                rendered.rmdir()
            # Validate every planned artifact before publishing any of them.
            self._set_progress(job_id,'verifying_outputs',0,len(job['snapshot']['files']))
            for index,item in enumerate(job['snapshot']['files'],1):
                check();path=staging/item['filename']
                if not path.is_file() or not path.stat().st_size:raise ValueError('输出文件缺失或为空')
                if item['output']['type'] in ('xlsx','attachment_zip','docx'):
                    with zipfile.ZipFile(path) as archive:
                        if archive.testzip():raise ValueError('输出文件结构损坏')
                outputs.append({'group_id':item['group_id'],'output_id':item['output']['id'],'type':item['output']['type'],'filename':item['filename'],'path':str(destination/item['filename']),'sha256':resource_digest(path)})
                self._set_progress(job_id,'verifying_outputs',index,len(job['snapshot']['files']))
            check();destination.parent.mkdir(parents=True,exist_ok=True)
            self._set_progress(job_id,'publishing')
            if destination.exists():raise ValueError('交付目录已存在，不能覆盖原文件')
            # Stage on the destination filesystem so publication is a single atomic rename.
            publish=destination.parent/('.tidoc-'+job_id)
            if publish.exists():raise ValueError('导出发布目录已存在')
            try:
                shutil.copytree(staging,publish);check();publish.rename(destination);moved=True
            finally:shutil.rmtree(publish,ignore_errors=True)
            job=self.jobs.complete(job_id,outputs,job['snapshot']['diagnostics'])
            self._save_record_files(job)
            self._set_progress(job_id,'completed',len(outputs),len(outputs))
            result_groups={}
            for file in outputs:
                item=next(item for item in job['snapshot']['files'] if item['group_id']==file['group_id'])
                group=result_groups.setdefault(file['group_id'],{'title':(item['context'].get('title') or {}).get('name',''),'group_id':file['group_id'],'files':{}})
                group['files'][file['output_id']]=file['path']
            return {**job,'results':list(result_groups.values()),'output_dir':str(destination)}
        except Exception as exc:
            if moved:
                current=self.jobs.get(job_id)
                if current['status']=='completed':raise
                shutil.rmtree(destination,ignore_errors=True)
            diagnostics=getattr(exc,'diagnostics',None) or [_diagnostic('EXPORT_CANCELED' if event.is_set() else 'EXPORT_FAILED',str(exc))]
            job=self.jobs.update(job_id,status='cancelled' if event.is_set() else 'failed',diagnostics=diagnostics)
            self._save_record_files(job)
            self._set_progress(job_id,job['status'])
            return job
        finally:
            shutil.rmtree(staging,ignore_errors=True);shutil.rmtree(rendered,ignore_errors=True)
            for abandoned in directory.glob('.tidoc-print-*'):shutil.rmtree(abandoned,ignore_errors=True)
            with self._lock:self._cancellations.pop(job_id,None)

    def list_jobs(self):
        return self.jobs.list()

    def get_job(self,job_id):
        return self.jobs.get(job_id)

    def regenerate(self,job_id,output_dir=None):
        original=self.jobs.get(job_id)
        snapshot=deepcopy(original['snapshot']);snapshot['reproduces_job_id']=job_id
        # No current entities are loaded: all rendering inputs come from the original snapshot.
        job=self._new_job(snapshot)
        return self._execute(job,output_dir)

    def cancel(self,job_id):
        self._cancellation(job_id).set()
        with self._lock:
            if job_id in self._plans:
                self._plans.pop(job_id)
        return {'job_id':job_id,'status':'cancel_requested'}

    def recover_jobs(self):
        recovered=[]
        for job in self.jobs.list(1000):
            if job['status']!='running':continue
            for name in ('staging','rendered'):shutil.rmtree(self._job_dir(job['id'])/name,ignore_errors=True)
            for abandoned in self._job_dir(job['id']).glob('.tidoc-print-*'):shutil.rmtree(abandoned,ignore_errors=True)
            destination_file=self._job_dir(job['id'])/'destination.json'
            if destination_file.is_file():
                saved=json.loads(destination_file.read_text('utf-8'))
                for key in ('publish_directory','directory'):
                    target=Path(saved[key])
                    if target.name in (job['id'],'.tidoc-'+job['id']):shutil.rmtree(target,ignore_errors=True)
            job=self.jobs.fail(job['id'],[_diagnostic('EXPORT_INTERRUPTED','上次导出被中断，可按原数据重新生成。')])
            self._save_record_files(job);recovered.append(job['id'])
        return recovered

"""IPC v2: JSON-only requests, confined resources, shared source/executable renderer."""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath

from . import __version__, missing_dependencies

LEGACY_SCHEME = 'builtin_adapters/org.bitfsae.reimbursement/scheme.json'
LEGACY_TEMPLATES = ('templates/报账说明模板.docx', 'templates/验收单模板.docx')


class ProtocolError(ValueError):
    def __init__(self, message, code='INVALID_PRINT_REQUEST', diagnostics=None):
        self.code = code
        self.diagnostics = diagnostics or [{'code':code,'severity':'blocked','message':message}]
        super().__init__(message)


def capabilities():
    missing = missing_dependencies()
    from .context import component_resource_path, context_field_catalog
    catalog_available = True
    try:
        context_field_catalog()
    except (OSError, ValueError):
        missing.append('resource:tidoc_print/context_fields.json')
        catalog_available = False
    schema_available = True
    try:
        from .context_validation import context_validator
        context_validator()
    except ImportError:
        if 'jsonschema' not in missing:
            missing.append('jsonschema')
        schema_available = False
    except (OSError, ValueError):
        missing.append('resource:schemas/team-adapter/1/context.schema.json')
        schema_available = False
    legacy_available = True
    for package, relative in [('tidoc_print', name) for name in LEGACY_TEMPLATES] + [('tidoc', LEGACY_SCHEME)]:
        try:
            component_resource_path(relative, package=package)
        except OSError:
            missing.append('resource:' + package + '/' + relative)
            legacy_available = False
    docx = schema_available and catalog_available and not any(name in missing for name in ('docx','docxtpl','jinja2'))
    pdf = schema_available and not any(name in missing for name in ('pypdf','PIL','reportlab'))
    return {'component':'tidoc_print','version':__version__,'ipc_versions':(([1] if legacy_available else [])+[2]) if schema_available else [],'context_versions':[1] if schema_available else [], 'renderers':(['docx'] if docx else []) + (['pdf_bundle'] if pdf else []), 'adapter_capabilities':(['output.docx.v1'] if docx else []) + (['output.pdf.v1'] if pdf else []), 'missing':missing, 'limits':{'timeout_seconds':120,'context_bytes':16*1024*1024,'loop_depth':3},'cancel':True}


def _safe_relative(value):
    if not isinstance(value,str) or not value:
        raise ProtocolError('文件路径为空')
    pure = PurePosixPath(value)
    if pure.is_absolute() or '..' in pure.parts or '.' in pure.parts or '\\' in value or ':' in value or any(not part or len(part.encode()) > 240 for part in pure.parts):
        raise ProtocolError('文件路径不安全', 'UNSAFE_RESOURCE_PATH')
    return pure


def _resource_path(root, relative):
    pure = _safe_relative(relative)
    target = Path(root).joinpath(*pure.parts).resolve()
    if not target.is_relative_to(Path(root).resolve()) or not target.is_file():
        raise ProtocolError('所需模板或材料缺失', 'MISSING_RESOURCE')
    return target


def _digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _component_resource(relative, *, package='tidoc_print'):
    from .context import component_resource_path
    try:
        return component_resource_path(relative, package=package)
    except OSError as exc:
        raise ProtocolError(str(exc), 'MISSING_COMPONENT_RESOURCE') from exc


def validate_request(request):
    if not isinstance(request,dict):
        raise ProtocolError('打印请求必须是 JSON 对象')
    if request.get('ipc_version',1) == 1:
        request = convert_v1_request(request)
    # roundtrip ensures source mode receives exactly the same data as external mode.
    try:
        request = json.loads(json.dumps(request,ensure_ascii=False,allow_nan=False))
    except (TypeError,ValueError) as exc:
        raise ProtocolError('打印请求必须是纯 JSON 数据') from exc
    if request.get('ipc_version') != 2 or not isinstance(request.get('job_id'),str) or not request['job_id']:
        raise ProtocolError('不支持的打印 IPC 版本')
    files = request.get('files')
    if not isinstance(files,list) or not files or len(files) > 1000:
        raise ProtocolError('打印计划文件数应为 1–1000')
    root = Path(request.get('resources_root') or '').resolve()
    if not root.is_dir() or not request.get('output_dir'):
        raise ProtocolError('缺少资源目录或输出目录')
    output_dir = Path(request['output_dir']).resolve()
    if output_dir == root or output_dir in root.parents:
        raise ProtocolError('输出目录不能覆盖资源目录')
    from .context import assert_pure_context,title_key
    names = set()
    for file_index, item in enumerate(files):
        if not isinstance(item,dict) or not isinstance(item.get('output'),dict):
            raise ProtocolError('计划缺少输出定义')
        output = item['output']
        if output.get('type') not in ('docx','pdf_bundle'):
            raise ProtocolError('打印组件只接收 Word 和材料 PDF')
        context = item.get('context')
        if not isinstance(context,dict):raise ProtocolError('模板上下文必须为 JSON 对象')
        try:
            assert_pure_context(context)
        except (TypeError, ValueError) as exc:
            raise ProtocolError(str(exc), 'INVALID_CONTEXT') from exc
        try:
            from .context_validation import validate_context
            diagnostics = validate_context(context, location=f'/files/{file_index}/context')
        except (OSError, ValueError, ImportError) as exc:
            raise ProtocolError('打印导出组件的上下文校验资源不可用，请修复组件。',
                                'INVALID_COMPONENT_RESOURCE') from exc
        if diagnostics:
            raise ProtocolError('打印上下文不符合公开协议。', 'INVALID_CONTEXT', diagnostics)
        if context.get('schema_version') != 1 or not isinstance(context.get('entries'),list):
            raise ProtocolError('不支持的模板上下文版本')
        filename = item.get('filename')
        _safe_relative(filename)
        if filename.casefold() in names:
            raise ProtocolError('计划文件名冲突')
        names.add(filename.casefold())
        # A single Word/PDF may not cross invoice subjects, even if directly called.
        subjects = set()
        for entry in context['entries']:
            if not isinstance(entry, dict) or not isinstance(entry.get('title'), dict):
                raise ProtocolError('条目缺少抬头对象', 'INVALID_CONTEXT')
            title = entry.get('title') or {}
            subjects.add(title_key(title,entry.get('id','')))
        schemes={(entry.get('scheme_id'),entry.get('revision_id')) for entry in context['entries']}
        if len(schemes)>1:raise ProtocolError('团队输出不能混合方案修订','REVISION_ISOLATION')
        if len(subjects)>1:
            raise ProtocolError('Word 和材料 PDF 必须按抬头隔离', 'TITLE_ISOLATION')
        if output.get('payee_mode') != 'none' and output.get('payee_mode') and context.get('payee') is None:
            raise ProtocolError('需要明确选择完整收款对象', 'MISSING_PAYEE')
        if output.get('type') == 'docx':
            path = _resource_path(root,item.get('template') or output.get('template'))
            if item.get('template_sha256') and _digest(path) != item['template_sha256']:
                raise ProtocolError('模板内容已变化','RESOURCE_CHANGED')
            from .template_validation import validate_template
            diagnostics = validate_template(path,definition=item.get('definition'),context=context)
            if diagnostics:
                raise ProtocolError('Word 模板校验失败',diagnostics=diagnostics)
        for resource in item.get('resources') or []:
            path = _resource_path(root,resource.get('path'))
            if resource.get('sha256') and _digest(path) != resource['sha256']:
                raise ProtocolError('材料内容已变化','RESOURCE_CHANGED')
    timeout = request.get('timeout_seconds',120)
    if not isinstance(timeout,(int,float)) or isinstance(timeout,bool) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ProtocolError('任务超时应为 0–120 秒')
    return request


def render_request(request, cancel_check=None):
    if not isinstance(request, dict):
        raise ProtocolError('打印请求必须是 JSON 对象')
    started = time.monotonic()
    def canceled():
        if cancel_check and cancel_check() or request.get('cancel_file') and Path(request['cancel_file']).exists():
            raise ProtocolError('导出任务已取消','EXPORT_CANCELED')
        timeout = request.get('timeout_seconds',120)
        if (isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and
                math.isfinite(timeout) and 0 < timeout <= 120 and
                time.monotonic()-started > timeout):
            raise ProtocolError('导出任务超时','EXPORT_TIMEOUT')
        return False
    canceled()
    request = validate_request(request)
    canceled()
    out = Path(request['output_dir']).resolve()
    if out.exists():
        raise ProtocolError('输出目录已存在，不能覆盖交付文件','OUTPUT_EXISTS')
    out.parent.mkdir(parents=True,exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.tidoc-print-',dir=out.parent))
    results = {}
    files = []
    try:
        for item in request['files']:
            canceled()
            dest = staging / item['filename']
            dest.parent.mkdir(parents=True,exist_ok=True)
            if item['output']['type']=='docx':
                from .template_renderer import render_template
                render_template(_resource_path(request['resources_root'],item.get('template') or item['output']['template']),item['context'],dest,definition=item.get('definition'))
                from docx import Document
                Document(dest)
            else:
                from .pdf_merge import render_pdf_bundle
                render_pdf_bundle(item['context'],item['output'],item.get('resources') or [],request['resources_root'],dest,cancel_check=canceled)
                from pypdf import PdfReader
                if not PdfReader(dest).pages:
                    raise ProtocolError('生成的材料 PDF 没有页面')
            canceled()
            final_path = str(out / item['filename'])
            files.append({'output_id':item['output']['id'],'type':item['output']['type'],'path':final_path,'filename':item['filename'],'sha256':_digest(dest)})
            title = (item['context'].get('title') or {}).get('name','')
            group_key = item.get('group_id') or title
            result = results.setdefault(group_key,{'title':title,'group_id':group_key,'files':{}})
            result['files'][item['output']['id']] = final_path
        canceled()
        staging.rename(out)
        return {'ipc_version':2,'job_id':request['job_id'],'results':list(results.values()),'files':files}
    except BaseException:
        shutil.rmtree(staging,ignore_errors=True)
        raise


def convert_v1_request(request):
    """Compatibility is a projection into v2, never a second renderer."""
    from .context import build_export_context, safe_name
    source = request.get('entries') or []
    if not source:
        raise ProtocolError('没有可打印的条目')
    options = request.get('options') or {}
    profiles = request.get('profiles') or {}
    reimburse_template, acceptance_template = [_component_resource(name) for name in LEGACY_TEMPLATES]
    paths = [reimburse_template, acceptance_template]
    for entry in source:
        for key in ('invoice_pdfs','payment_images','inspection_pdfs','physical_images'):
            paths += [Path(path).resolve() for path in entry.get(key,[])]
    root = Path(os.path.commonpath([str(path.parent) for path in paths]))
    # Organization values belong to the canonical installed builtin definition.
    builtin = _component_resource(LEGACY_SCHEME, package='tidoc')
    try:
        scheme = json.loads(builtin.read_text('utf-8'))
        if not isinstance(scheme, dict) or not isinstance(scheme.get('organization'), dict):
            raise ValueError('旧 IPC 的组织设置无效')
    except (OSError, ValueError) as exc:
        raise ProtocolError('旧 IPC 的内置方案资源无效', 'INVALID_COMPONENT_RESOURCE') from exc
    definition = {'scheme':scheme}
    organization = definition.get('scheme',{}).get('organization',{})
    group_entries = {}
    for index, source_entry in enumerate(source):
        entry = dict(source_entry)
        eid = entry.get('entry_id') or str(index)
        attachments = []
        for key, role, kind in (('invoice_pdfs','invoice','invoice_pdf'),('payment_images','payment_screenshot','payment_screenshot'),('inspection_pdfs','inspection_pdf','inspection_pdf'),('physical_images','physical_image','physical_image')):
            for ri,path in enumerate(entry.get(key,[])):
                attachments.append({'id':f'{eid}-{key}-{ri}','role_id':role,'type':kind,'original_name':Path(path).name,'stored_path':str(Path(path).resolve().relative_to(root))})
        data = {'id':eid,'title':entry.get('title',''),'title_profile_id':entry.get('title_profile_id') or ('legacy:'+str(entry.get('title',''))),'buyer_tax_id':entry.get('buyer_tax_id',''),'invoice_no':entry.get('invoice_no',''),'invoice_date':entry.get('invoice_date',''),'seller':entry.get('seller',''),'total':entry.get('total'),'total_present':entry.get('total_present'),'profile_id':eid,'profile_name':entry.get('profile_name',''),'reviewer':entry.get('reviewer',''),'fields':{'paid_amount':{'current':entry.get('paid_amount')}},'items':[{**item,'name':item.get('product_name') or item.get('actual_name','')} for item in entry.get('items',[])], 'attachments':attachments}
        old_payee = profiles.get(eid)
        payee = {'id':eid,'name':old_payee.get('person_name',''),'personnel_id':old_payee.get('student_id',''),'contact':old_payee.get('contact',''),'account_type':'personal_bank','bank_name':old_payee.get('bank_name',''),'account_number':old_payee.get('bank_card','')} if old_payee else None
        key = (data['title'], data['buyer_tax_id'], json.dumps(payee,sort_keys=True) if payee else '')
        group_entries.setdefault(key,[]).append((data,payee))
    files = []
    for group_index, ((title, tax, _), group) in enumerate(group_entries.items()):
        entries = [e for e,p in group]
        payee = group[0][1]
        typed_profiles = {e['id']:{'id':e['id'],'name':e.get('profile_name',''),'reviewer':e.get('reviewer','')} for e in entries}
        row_defaults = {'unit':'个','quantity':'1','quantity_mode':'sum_compat','storage_location':options.get('storage_location') or organization.get('storage_location','')}
        configurations = [
            ('reimburse_doc','make_reimburse_doc','docx','报账说明.docx',reimburse_template),
            ('acceptance_doc','make_acceptance_doc','docx','验收单.docx',acceptance_template),
            ('entry_bundle_pdf','make_entry_bundle_pdf','pdf_bundle','按条目材料拼接.pdf',None),
            ('invoice_pdf','make_invoice_pdf','pdf_bundle','发票拼接.pdf',None),
            ('payment_pdf','make_payment_pdf','pdf_bundle','付款截图拼接.pdf',None),
            ('inspection_pdf','make_inspection_pdf','pdf_bundle','查验单拼接.pdf',None),
        ]
        for oid,flag,kind,name,template in configurations:
            enabled = options.get(flag, flag in ('make_reimburse_doc','make_acceptance_doc','make_entry_bundle_pdf'))
            if not enabled:
                continue
            roles = {'invoice_pdf':['invoice'],'payment_pdf':['payment_screenshot'],'inspection_pdf':['inspection_pdf']}.get(oid,['invoice','payment_screenshot','inspection_pdf'])
            resources = [{'id':a['id'],'entry_id':e['id'],'role_id':a['role_id'],'path':a['stored_path'],'original_name':a['original_name']} for e in entries for a in e['attachments'] if a['role_id'] in roles]
            if kind=='pdf_bundle' and not resources:
                continue
            output = {'id':oid,'type':kind,'rows':'invoice_summary','amount_basis':'invoice','row_defaults':row_defaults,'payee_mode':'single' if payee and oid=='reimburse_doc' else 'none','pdf':{'roles':roles,'order':'entry','numbering':options.get('annotate',True),'batch_note':options.get('batch_note','')}}
            context = build_export_context(entries,definition=definition,output=output,profiles=typed_profiles,payee=payee,options={'date':options.get('document_date') or None})
            directory = safe_name(title)
            if len([k for k in group_entries if k[0]==title])>1:
                directory += '_' + hashlib.sha256(repr((title,tax,payee)).encode()).hexdigest()[:8]
            item = {'output':output,'context':context,'filename':directory+'/'+name,'resources':resources,'group_id':str(group_index)}
            if template:
                item['template']=str(template.resolve().relative_to(root))
            files.append(item)
    if not files:
        raise ProtocolError('没有选中的可生成输出')
    return {'ipc_version':2,'job_id':request.get('job_id') or uuid.uuid4().hex,'files':files,'output_dir':request.get('out_dir') or request.get('output_dir'),'resources_root':str(root),'timeout_seconds':request.get('timeout_seconds',120),**({'cancel_file':request['cancel_file']} if request.get('cancel_file') else {})}

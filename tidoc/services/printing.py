"""Core transport adapter. Both development and installed components use JSON IPC v2."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path

from .proc import hidden_window_options

from .updater import COMPONENT_PRINT, installed_component_info

_CAPABILITIES_CACHE = {}


def _command(executable=None):
    if executable:
        path=Path(executable)
        if path.suffix=='.app':
            candidates=list((path/'Contents'/'MacOS').iterdir())
            path=next((item for item in candidates if item.is_file() and os.access(item,os.X_OK)),path)
        return [str(path)]
    return [sys.executable,'-m','tidoc_print']


def _query_capabilities(executable):
    from .exports import resource_digest
    path=Path(executable)
    identity=resource_digest(path) if path.is_file() else str(path.stat().st_mtime_ns)
    key=(str(path),identity)
    if key in _CAPABILITIES_CACHE:
        return _CAPABILITIES_CACHE[key]
    try:
        proc=subprocess.run(_command(executable)+['--capabilities'],text=True,capture_output=True,timeout=10,check=False,**hidden_window_options())
        value=json.loads(proc.stdout) if proc.returncode==0 else {}
        if not isinstance(value,dict): value={}
    except (OSError,ValueError,subprocess.TimeoutExpired):
        value={}
    _CAPABILITIES_CACHE.clear();_CAPABILITIES_CACHE[key]=value
    return value


def component_status(components_dir=None):
    frozen=bool(getattr(sys,'frozen',False))
    tidoc_print=None
    source_info=None
    if not frozen:
        try:
            import tidoc_print
            from tidoc_print.protocol import capabilities
            info=capabilities()
            source_info=info
            if info['renderers']:
                return {'available':True,'mode':'python','version':tidoc_print.__version__,'missing':[],**info}
        except ImportError:
            pass
    installed=installed_component_info(components_dir,COMPONENT_PRINT) if components_dir else {}
    if installed.get('valid'):
        info=_query_capabilities(installed['executable'])
        return {'available':True,'mode':'external','path':installed['executable'],'version':installed.get('version',''),'missing':[],**info,'ipc_versions':info.get('ipc_versions',[1]),'renderers':info.get('renderers',[]),'needs_update':2 not in info.get('ipc_versions',[])}
    return {'available':False,'mode':'repair' if installed.get('needs_repair') else ('python' if tidoc_print else 'missing'),'missing':source_info.get('missing',[]) if source_info is not None else (tidoc_print.missing_dependencies() if tidoc_print else ['打印导出组件']),'needs_repair':bool(installed.get('needs_repair')),'error':('打印导出组件缺少所需资源，请修复组件。' if source_info is not None else installed.get('issue') or '打印导出组件未安装'),'ipc_versions':[],'renderers':[]}


def execute_print_request(request,components_dir=None,cancel_check=None):
    status=component_status(components_dir)
    if not status['available']:
        raise RuntimeError('打印导出组件未安装或缺少依赖：'+', '.join(status['missing']))
    if 2 not in status.get('ipc_versions',[]):
        raise RuntimeError('打印导出组件不支持此报账方案，请检查更新。')
    needed={item['output']['type'] for item in request['files']}
    if not needed.issubset(status.get('renderers',[])):
        raise RuntimeError('打印导出组件缺少所选输出能力，请检查更新。')
    timeout=float(request.get('timeout_seconds',120))
    if not 0<timeout<=120: raise ValueError('导出超时应为 0–120 秒')
    with tempfile.TemporaryDirectory(prefix='tidoc-print-ipc-') as tmp:
        inp=Path(tmp)/'input.json';result=Path(tmp)/'result.json';cancel=Path(tmp)/'cancel'
        payload=json.loads(json.dumps(request,ensure_ascii=False,allow_nan=False))
        payload['cancel_file']=str(cancel)
        inp.write_text(json.dumps(payload,ensure_ascii=False),'utf-8')
        proc=subprocess.Popen(_command(status.get('path'))+['--input',str(inp),'--result',str(result),'--timeout',str(timeout),'--cancel-file',str(cancel)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,**hidden_window_options())
        started=time.monotonic()
        try:
            while True:
                if cancel_check and cancel_check():
                    cancel.touch();proc.terminate()
                    raise RuntimeError('导出任务已取消')
                if time.monotonic()-started>timeout:
                    proc.terminate()
                    raise TimeoutError('导出任务超时')
                try:
                    stdout,stderr=proc.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if not result.exists():
                raise RuntimeError('打印组件未返回结果。')
            response=json.loads(result.read_text('utf-8'))
            if proc.returncode or not response.get('ok'):
                error=RuntimeError(response.get('error') or '打印组件执行失败。')
                error.diagnostics=response.get('diagnostics',[])
                raise error
            return response['data']
        finally:
            if proc.poll() is None:
                proc.kill();proc.communicate()


def _entry_to_print_payload(entry,attachments_dir,profile):
    """Legacy compatibility projection; new plans use export_context directly."""
    def paths(kind):
        return [str(Path(attachments_dir)/att['stored_path']) for att in entry.get('attachments',[]) if att.get('type')==kind]
    fields=entry.get('fields') or {}
    actual=(fields.get('actual_item_name',{}).get('current') or '').strip()
    items=list(entry.get('items') or [])
    if not items and paths('invoice_pdf'):
        try:
            from ..engine import parse_pdf
            parsed=parse_pdf(paths('invoice_pdf')[0])
            if parsed.items and (not parsed.invoice_no or parsed.invoice_no==entry.get('invoice_no','')):
                items=[item.to_dict() for item in parsed.items]
        except Exception:
            pass
    rendered=[]
    for index,item in enumerate(items):
        product=item.get('actual_name') or item.get('name','')
        rendered.append({'actual_name':actual if index==0 and actual else product,'product_name':product,'unit':item.get('unit') or '个','quantity':item.get('quantity') if item.get('quantity') not in (None,'') else '', 'total':item.get('total'),'seller':entry.get('seller',''),'invoice_no':entry.get('invoice_no','')})
    if not rendered:
        name=actual or '未填写品名'
        rendered=[{'actual_name':name,'product_name':name,'unit':'个','quantity':'1','total':entry.get('total'),'seller':entry.get('seller',''),'invoice_no':entry.get('invoice_no','')}]
    return {'entry_id':entry['id'],'title':entry.get('title',''),'invoice_no':entry.get('invoice_no',''),'invoice_date':entry.get('invoice_date',''),'seller':entry.get('seller',''),'total':entry.get('total'),'paid_amount':fields.get('paid_amount',{}).get('current'),'profile_name':profile.get('name',''),'reviewer':profile.get('reviewer',''),'items':rendered,'invoice_pdfs':paths('invoice_pdf'),'payment_images':paths('payment_screenshot'),'inspection_pdfs':paths('inspection_pdf')}


def build_prints(entries_repo,profiles_repo,attachments_dir,entry_ids,out_dir,options=None,components_dir=None,adapters=None):
    if adapters is not None:
        from .export_plan import ExportPlanner
        planner=ExportPlanner(entries_repo.db,adapters.data_root if hasattr(adapters,'data_root') else adapters.root,adapters)
        plan=planner.preview(entry_ids,options=(options or {}))
        job=planner.run(plan['plan_id'],out_dir)
        if job['status']!='completed': raise RuntimeError('打印导出失败：'+ '；'.join(d['message'] for d in job['diagnostics']))
        return {'results':job.get('results',[]),'job_id':job['job_id'],'files':job['files']}
    profiles={p['id']:p for p in profiles_repo.list()}
    options=dict(options or {})
    operator=options.pop('operator_profile',None)
    payloads=[];persons={}
    for eid in entry_ids:
        entry=entries_repo.get(eid)
        if not entry: raise ValueError('选中条目不存在')
        profile=profiles.get(entry.get('profile_id'),{})
        payloads.append(_entry_to_print_payload(entry,Path(attachments_dir),profile))
        # Whole object selection: explicit operator OR one profile, never mixed fields.
        person=operator if operator is not None else {'person_name':profile.get('name',''),'student_id':profile.get('student_id',''),'contact':profile.get('contact',''),'bank_name':profile.get('bank_name',''),'bank_card':profile.get('bank_card','')}
        persons[eid]=person
    from tidoc_print.protocol import convert_v1_request
    request=convert_v1_request({'entries':payloads,'profiles':persons,'options':options,'out_dir':str(out_dir)})
    return execute_print_request(request,components_dir)


def _build_prints_external(executable,entries,out_dir,options,profiles):
    from tidoc_print.protocol import convert_v1_request
    request=convert_v1_request({'entries':[_jsonable(e) for e in entries],'profiles':{k:_jsonable(v) for k,v in profiles.items()},'options':options or {},'out_dir':str(out_dir)})
    # Retained helper uses the same serialized request and executable entrypoint.
    with tempfile.TemporaryDirectory() as tmp:
        inp=Path(tmp)/'input.json';res=Path(tmp)/'result.json'
        inp.write_text(json.dumps(request,ensure_ascii=False),'utf-8')
        proc=subprocess.run(_command(executable)+['--input',str(inp),'--result',str(res)],capture_output=True,text=True,timeout=120,**hidden_window_options())
        if proc.returncode or not res.exists(): raise RuntimeError('打印组件执行失败')
        result=json.loads(res.read_text('utf-8'))
        if not result['ok']:raise RuntimeError(result['error'])
        return result['data']


def _jsonable(value):
    if is_dataclass(value):return _jsonable(asdict(value))
    if isinstance(value,Decimal):return str(value)
    if isinstance(value,dict):return {key:_jsonable(val) for key,val in value.items()}
    if isinstance(value,(list,tuple)):return [_jsonable(val) for val in value]
    return value

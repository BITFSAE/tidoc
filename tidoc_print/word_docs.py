"""Compatibility helpers using the same strict DOCX templates as IPC v2."""
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path

TEMPLATES_DIR=Path(__file__).parent/'templates'
DEFAULT_REIMBURSE_TEMPLATE=TEMPLATES_DIR/'报账说明模板.docx'
DEFAULT_ACCEPTANCE_TEMPLATE=TEMPLATES_DIR/'验收单模板.docx'


def _context(entries,document_date,profile=None,storage_location=''):
    from .context import build_export_context
    raw=[]
    for item in entries:
        entry=asdict(item)
        raw.append({'id':entry['entry_id'],'title':entry['title'],'title_profile_id':'legacy:'+entry['title'],'invoice_no':entry['invoice_no'],'invoice_date':entry['invoice_date'],'seller':entry['seller'],'total':entry['total'],'profile_name':entry['profile_name'],'reviewer':entry['reviewer'],'fields':{'paid_amount':{'current':entry['paid_amount']}},'items':[{**row,'name':row.get('product_name') or row.get('actual_name')} for row in entry['items']]})
    definition={'scheme':{'organization':{'department':'','purpose':'','storage_location':storage_location}}}
    builtin=Path(__file__).parent.parent/'tidoc'/'builtin_adapters'/'org.bitfsae.reimbursement'/'scheme.json'
    if builtin.exists():
        import json
        definition['scheme']=json.loads(builtin.read_text('utf-8'))
    payee=None
    if profile:
        payee={'id':'explicit','name':profile.person_name,'personnel_id':profile.student_id,'contact':profile.contact,'bank_name':profile.bank_name,'account_number':profile.bank_card,'account_type':'personal_bank'}
    return build_export_context(raw,definition=definition,output={'rows':'invoice_summary','row_defaults':{'unit':'个','quantity':'1','quantity_mode':'sum_compat','storage_location':storage_location}},payee=payee,options={'date':document_date})


def generate_reimburse_doc(entries,out_path,document_date,profile=None,template=DEFAULT_REIMBURSE_TEMPLATE):
    from .template_renderer import render_template
    return render_template(template,_context(entries,document_date,profile),out_path)


def generate_acceptance_doc(entries,out_path,document_date,storage_location='',template=DEFAULT_ACCEPTANCE_TEMPLATE):
    from .template_renderer import render_template
    return render_template(template,_context(entries,document_date,storage_location=storage_location),out_path)

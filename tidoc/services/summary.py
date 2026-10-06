"""Compatibility summary built from the same decimal-safe output projection."""
from __future__ import annotations
from .export_context import project_entry, exact_sum, decimal_value, title_key

SUMMARY_VERSION = 1


def _item_count(entry):
    items = entry.get('items') or []
    units = {item.get('unit') or None for item in items}
    quantity, missing = exact_sum(item.get('quantity') for item in items)
    return quantity if len(units) == 1 and None not in units and not missing else None


def build_entry_summary(entry):
    data = project_entry(entry)
    differs = data['paid_amount'] is not None and data['total'] is not None and decimal_value(data['paid_amount']) != decimal_value(data['total'])
    return {'invoice_no':data['invoice_no'],'invoice_date':data['invoice_date'],'seller':data['seller'],'total':data['total'],'item_count':_item_count(entry),'title':data['title']['name'],'buyer_tax_id':data['title']['tax_id'],'title_profile_id':data['title']['id'],'status':data['status'],'check_status':data['check_status'],'paid_amount':data['paid_amount'],'actual_item_name':data['actual_item_name'],'notes':data['notes'],'modified_fields':['paid_amount'] if differs else []}


def build_summary(entries_repo, entry_ids):
    records = [build_entry_summary(entry) for eid in dict.fromkeys(entry_ids) if (entry := entries_repo.get(eid))]
    total, missing = exact_sum(record['total'] for record in records)
    by_title = {}
    for record in records:
        label = record['title'] or '(未标注抬头)'
        if record['buyer_tax_id']:
            label += ' / ' + record['buyer_tax_id']
        by_title[label] = by_title.get(label,0)+1
    return {'summary_version':SUMMARY_VERSION,'count':len(records),'total':total if not missing else None,'known_total':total,'missing_total_count':missing,'by_title':by_title,'entries':records}

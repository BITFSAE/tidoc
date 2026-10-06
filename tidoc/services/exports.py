"""Shared configurable Excel and attachment writers, using the export projection."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from decimal import Decimal
from pathlib import Path, PurePosixPath
from xml.sax.saxutils import escape

from .export_context import (ROLE_LABELS, ROLE_TYPES, build_export_context, decimal_value,
                             exact_sum, format_filename, get_path, safe_name, title_key)

GENERIC_COLUMNS = [
    {'id': 'index', 'source': 'row.index', 'label': '序号', 'width': 7, 'format': 'number'},
    {'id': 'claimant', 'source': 'entry.claimant.name', 'label': '报账人', 'width': 12},
    {'id': 'reviewer', 'source': 'entry.claimant.reviewer', 'label': '审核人', 'width': 12},
    {'id': 'title', 'source': 'entry.title.name', 'label': '抬头', 'width': 18},
    {'id': 'status', 'source': 'entry.status', 'label': '状态', 'width': 10},
    {'id': 'materials', 'source': 'entry.completeness', 'label': '材料状态', 'width': 24},
    {'id': 'check_status', 'source': 'entry.check_status', 'label': '校验', 'width': 12},
    {'id': 'invoice_no', 'source': 'entry.invoice.number', 'label': '发票号码', 'width': 24},
    {'id': 'invoice_date', 'source': 'entry.invoice.date', 'label': '发票日期', 'width': 12, 'format': 'date'},
    {'id': 'seller', 'source': 'entry.invoice.seller', 'label': '销售方', 'width': 28},
    {'id': 'total', 'source': 'row.invoice_total', 'label': '价税合计', 'width': 12, 'format': 'money', 'total': 'sum'},
    {'id': 'paid_amount', 'source': 'row.paid_amount', 'label': '实付金额', 'width': 12, 'format': 'money', 'total': 'sum'},
    {'id': 'actual_item_name', 'source': 'row.actual_name', 'label': '实际物资名称', 'width': 22},
    {'id': 'notes', 'source': 'entry.notes', 'label': '备注', 'width': 34},
    {'id': 'attachments', 'source': 'entry.attachment_count', 'label': '附件数', 'width': 8, 'format': 'number'},
]
GENERIC_OVERVIEW = {'id': 'generic_overview', 'label': '总览 Excel', 'type': 'xlsx', 'rows': 'invoice_summary', 'payee_mode': 'none', 'amount_basis': 'invoice', 'filename': '报账总览.xlsx', 'columns': GENERIC_COLUMNS, 'xlsx': {'freeze_header': True, 'filter': True, 'totals': True}}
GENERIC_ATTACHMENTS = {'id': 'generic_attachments', 'label': '附件整理包', 'type': 'attachment_zip', 'rows': 'invoice_summary', 'payee_mode': 'none', 'filename': '附件整理包.zip', 'attachments': {'group_by': ['title', 'entry'], 'manifest': True}}
GENERIC_MATERIALS = {'id':'generic_materials','label':'通用材料 PDF','type':'pdf_bundle','rows':'invoice_summary','payee_mode':'none','amount_basis':'invoice','filename':'{title.short_name}_材料.pdf','pdf':{'include_roles':['invoice','payment_screenshot','physical_image','inspection_pdf','other'],'image_layout':'a4_landscape_2','content_order':'entry','numbering':True}}


def _safe_name(value, fallback='未命名', max_len=64):
    return safe_name(value, fallback, max_len)


def _xlsx_col(n):
    out = ''
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _xml_text(value):
    text = str(value if value is not None else '')
    return ''.join(char for char in text if char in '\n\t\r' or ord(char) >= 32)


def _cell(value, row, col, fmt='text', style=None):
    ref = f'{_xlsx_col(col)}{row}'
    if fmt in ('money', 'number') and decimal_value(value) is not None:
        number = decimal_value(value)
        # OOXML numeric cells use IEEE precision in Excel. Long exact values remain text.
        if len(number.as_tuple().digits) <= 15:
            return f'<c r="{ref}" s="{style if style is not None else (1 if fmt == "money" else 0)}"><v>{number:f}</v></c>'
    return f'<c r="{ref}" t="inlineStr"' + (f' s="{style}"' if style is not None else '') + f'><is><t xml:space="preserve">{escape(_xml_text(value))}</t></is></c>'


def _column_value(context, row, column):
    source = column.get('source') or column.get('field') or column['id']
    aliases = {'total': 'row.invoice_total', 'paid_amount': 'row.paid_amount', 'invoice_no': 'entry.invoice.number', 'invoice_date': 'entry.invoice.date', 'seller': 'entry.invoice.seller', 'title': 'entry.title.name', 'claimant': 'entry.claimant.name', 'reviewer': 'entry.claimant.reviewer', 'actual_item_name': 'row.actual_name', 'quantity': 'row.quantity', 'unit': 'row.unit', 'amount': 'row.amount', 'notes': 'entry.notes', 'index': 'row.index'}
    source = aliases.get(source, source)
    if source.startswith('rows.'):
        source = 'row.' + source[5:]
    if source.startswith('entries.'):
        source = 'entry.' + source[8:]
    data = {**context, 'row': row, 'entry': row['entry'], 'invoice': row['entry']['invoice'], 'claimant': row['entry']['claimant'], 'title': row['entry']['title']}
    # Invoice-level columns display once even in expanded detail mode.
    if not row.get('first_for_invoice') and source in ('entry.total', 'entry.paid_amount', 'entry.invoice.total', 'invoice.total', 'invoice.paid_amount'):
        return None
    value = get_path(data, source)
    if source == 'row.title': value = row['title']['name'] if isinstance(row['title'],dict) else row['title']
    if source == 'row.claimant': value = row['claimant']['name'] if isinstance(row['claimant'],dict) else row['claimant']
    if source.endswith('status'):
        value = {'draft':'草稿','partial':'部分材料','complete':'完整','pass':'校验通过','warning':'需确认','blocked':'问题严重'}.get(value, value)
    if isinstance(value, dict) and source.endswith('completeness'):
        return '齐全' if value.get('ready') else '待补：' + '、'.join(value.get('missing') or [])
    if isinstance(value, list):
        return '、'.join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return value


def _atomic_file(out_path, writer):
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.tidoc-', suffix=out.suffix, dir=out.parent)
    os.close(fd)
    try:
        writer(Path(temp))
        os.replace(temp, out)
    finally:
        Path(temp).unlink(missing_ok=True)
    return out


def write_xlsx(context, output, out_path):
    columns = output.get('columns') or GENERIC_COLUMNS
    if not columns or len(columns) > 100:
        raise ValueError('Excel 列数应为 1–100')
    for column in columns:
        if column.get('format', 'text') not in ('text', 'date', 'money', 'number') or column.get('total') not in (None, 'sum', 'count'):
            raise ValueError('不支持的 Excel 格式或合计操作')
        if not 1 <= float(column.get('width', 16)) <= 120:
            raise ValueError('Excel 列宽应为 1–120')
    rows = context.get('rows') or []
    settings = output.get('xlsx') or {}
    totals_enabled = settings.get('totals', output.get('totals', True))
    row_xml = ['<row r="1">' + ''.join(_cell(col.get('label') or col.get('title') or col['id'], 1, i, style=2) for i,col in enumerate(columns,1)) + '</row>']
    values = [[] for _ in columns]
    for ri, row in enumerate(rows, 2):
        cells = []
        for ci, col in enumerate(columns,1):
            value = _column_value(context, row, col)
            values[ci-1].append(value)
            cells.append(_cell(value, ri, ci, col.get('format','text')))
        row_xml.append(f'<row r="{ri}">' + ''.join(cells) + '</row>')
    if totals_enabled and any(c.get('total') for c in columns):
        ri = len(rows)+2
        cells = []
        for ci, col in enumerate(columns,1):
            operation = col.get('total')
            value = None
            if operation == 'sum':
                value, missing = exact_sum(v for v in values[ci-1] if v is not None)
                # Missing selected invoice values must never become a misleading grand total.
                source = col.get('source') or col.get('field') or col.get('id')
                if source in ('row.paid_amount','entry.paid_amount','paid_amount') and context['totals']['missing_paid']:
                    value = None
                if source in ('row.invoice_total','entry.invoice.total','invoice.total','total') and context['totals']['missing_invoice']:
                    value = None
            elif operation == 'count':
                value = sum(v not in (None,'') for v in values[ci-1])
            elif ci == 1:
                value = '合计'
            cells.append(_cell(value,ri,ci,col.get('format','text')))
        row_xml.append(f'<row r="{ri}">' + ''.join(cells) + '</row>')
    width_xml = ''.join(f'<col min="{i}" max="{i}" width="{float(col.get("width",16)):g}" customWidth="1"/>' for i,col in enumerate(columns,1))
    freeze = '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>' if settings.get('freeze_header', True) else ''
    filter_xml = f'<autoFilter ref="A1:{_xlsx_col(len(columns))}{len(rows)+1}"/>' if settings.get('filter', True) else ''
    sheet = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' + freeze + '<cols>' + width_xml + '</cols><sheetData>' + ''.join(row_xml) + '</sheetData>' + filter_xml + '</worksheet>'
    def write(temp):
        with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
            zf.writestr('_rels/.rels','<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
            zf.writestr('xl/workbook.xml','<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="报账总览" sheetId="1" r:id="rId1"/></sheets></workbook>')
            zf.writestr('xl/_rels/workbook.xml.rels','<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
            zf.writestr('xl/styles.xml','<?xml version="1.0"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs></styleSheet>')
            zf.writestr('xl/worksheets/sheet1.xml',sheet)
    return _atomic_file(out_path,write)


def confined_path(root, relative):
    pure = PurePosixPath(str(relative))
    if pure.is_absolute() or '..' in pure.parts or '\\' in str(relative) or ':' in str(relative):
        raise ValueError('资源路径必须位于资源目录内')
    root = Path(root).resolve()
    target = root.joinpath(*pure.parts).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError('资源缺失或越出资源目录')
    return target


def resource_digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_attachment_zip(context, output, resources, out_path, resources_root):
    settings = output.get('attachments') or {}
    roles = settings.get('roles') or settings.get('include_roles')
    group_by = settings.get('group_by', ['title','entry'])
    manifest = []
    used = set()
    def write(temp):
        with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as archive:
            for index,entry in enumerate(context['entries'],1):
                title = entry['title']
                title_id = hashlib.sha256(repr(title_key(title,entry['id'])).encode()).hexdigest()[:8]
                parts = [safe_name(title['short_name']) + '_' + title_id]  # mandatory subject isolation
                if 'claimant' in group_by:
                    parts.append(safe_name(entry['claimant']['name']) + '_' + safe_name(entry['claimant']['id'])[:8])
                if 'entry' in group_by:
                    parts.append(f'{index:03d}_' + safe_name(entry['invoice_no'],'无发票号',28) + '_' + safe_name(entry['seller'],'未识别销售方',28) + '_' + safe_name(entry['total'],'无金额',16))
                if settings.get('directory') or settings.get('directory_template'):
                    pattern = settings.get('directory') or settings['directory_template']
                    # Only authored separators create directories; values are sanitized independently.
                    dirs = []
                    for segment in pattern.split('/'):
                        if not segment or segment in ('.','..'):
                            raise ValueError('附件目录模板不安全')
                        dirs.append(format_filename(segment, {**context,'entry':entry,'title':title}))
                    parts += dirs
                counts = {}
                entry_resources = [r for r in resources if r.get('entry_id') == entry['id'] and (not roles or r.get('role_id') in roles)]
                for resource in entry_resources:
                    role = resource.get('role_id','other')
                    counts[role] = counts.get(role,0)+1
                    source = confined_path(resources_root,resource['path'])
                    if resource.get('sha256') and resource_digest(source) != resource['sha256']:
                        raise ValueError('附件内容已变化，请重新预览')
                    label = resource.get('label') or ROLE_LABELS.get(role,'附件')
                    pattern = settings.get('filename') or settings.get('filename_template')
                    name = format_filename(pattern,{**context,'entry':entry,'title':title,'material':{'label':label,'role_id':role,'name':resource.get('original_name',''),'index':counts[role]},'role':{'id':role,'label':label}}) if pattern else f'{safe_name(label)}_{counts[role]:02d}'
                    suffix = Path(resource.get('original_name') or source.name).suffix.lower()
                    if suffix and not name.lower().endswith(suffix):
                        name += suffix
                    arcname = '/'.join(parts+[name])
                    if arcname.casefold() in used:
                        name = Path(name).stem + '_' + safe_name(resource.get('id') or resource_digest(source))[:8] + suffix
                        arcname = '/'.join(parts+[name])
                    if arcname.casefold() in used:
                        raise ValueError('附件文件名重复')
                    used.add(arcname.casefold())
                    archive.write(source,arcname)
                    manifest.append({'entry_id':entry['id'],'invoice_no':entry['invoice_no'],'title':title,'claimant':entry['claimant']['name'],'role_id':role,'role_label':label,'filename':arcname,'original_name':resource.get('original_name',''),'sha256':resource.get('sha256') or resource_digest(source)})
                for missing in entry.get('completeness',{}).get('missing',[]):
                    manifest.append({'entry_id':entry['id'],'missing':missing})
            if settings.get('include_manifest',settings.get('manifest', True)):
                archive.writestr('manifest.json',json.dumps({'schema_version':1,'files':manifest},ensure_ascii=False,indent=2))
                archive.writestr('清单.txt','tidoc 附件整理包\n\n'+'\n'.join(row.get('filename') or row.get('entry_id','')+' 缺失：'+str(row.get('missing','')) for row in manifest))
    return _atomic_file(out_path,write)


def export_overview_xlsx(entries_repo, profile_lookup, entry_ids, out_path):
    entries = [entry for eid in dict.fromkeys(entry_ids) if (entry := entries_repo.get(eid))]
    context = build_export_context(entries,output=GENERIC_OVERVIEW,profiles=profile_lookup)
    return write_xlsx(context,GENERIC_OVERVIEW,out_path)


def export_attachment_zip(entries_repo, attachments_root, profile_lookup, entry_ids, out_path):
    entries = [entry for eid in dict.fromkeys(entry_ids) if (entry := entries_repo.get(eid))]
    resources = []
    for entry in entries:
        for index,att in enumerate(entry.get('attachments') or []):
            resources.append({'id':att.get('id') or f'{entry["id"]}-{index}','entry_id':entry['id'],'path':att['stored_path'],'role_id':att.get('role_id') or ROLE_TYPES.get(att.get('type'),'other'),'original_name':att.get('original_name','')})
    context = build_export_context(entries,output=GENERIC_ATTACHMENTS,profiles=profile_lookup)
    return write_attachment_zip(context,GENERIC_ATTACHMENTS,resources,out_path,attachments_root)

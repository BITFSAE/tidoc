"""Versioned, JSON-only output projection. No database or renderer objects here."""
from __future__ import annotations

import hashlib
import json
import re
import string
import sys
import unicodedata
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from functools import lru_cache

CONTEXT_VERSION = 1


def component_resource_path(relative, *, package='tidoc_print'):
    """Locate bundled resources without importing any core or rendering module."""
    from pathlib import Path, PurePosixPath

    if package not in ('tidoc_print', 'tidoc', 'schemas') or not isinstance(relative, str):
        raise ValueError('组件资源路径不安全')
    relative = PurePosixPath(relative)
    if (not relative.parts or relative.is_absolute() or '..' in relative.parts or
            '\\' in str(relative) or ':' in str(relative) or '\x00' in str(relative)):
        raise ValueError('组件资源路径不安全')
    if getattr(sys, 'frozen', False) and getattr(sys, '_MEIPASS', None):
        root = Path(sys._MEIPASS) / package
    else:
        root = Path(__file__).resolve().parent.parent / package
    candidate = root.joinpath(*relative.parts).resolve()
    if candidate.is_relative_to(root.resolve()) and candidate.is_file():
        return candidate
    # A frozen build must not accidentally find an adjacent source checkout.
    raise FileNotFoundError('打印导出组件缺少必需资源：' + package + '/' + str(relative))


def _context_catalogs():
    path=component_resource_path('context_fields.json')
    stat=path.stat()
    catalog,filenames=_read_context_catalogs(str(path),stat.st_mtime_ns,stat.st_size)
    return dict(catalog),filenames


@lru_cache(maxsize=8)
def _read_context_catalogs(path,mtime_ns,size):
    from pathlib import Path
    resource = json.loads(Path(path).read_text('utf-8'))
    if not isinstance(resource, dict) or set(resource) != {'fields', 'filename_fields'}:
        raise ValueError('打印上下文字段目录无效')
    catalog = resource['fields']
    valid_types = {'text', 'multiline', 'integer', 'decimal', 'money', 'date',
                   'boolean', 'select', 'multiselect', 'object', 'array'}
    if not isinstance(catalog, dict) or not catalog or any(
        not isinstance(key, str) or not isinstance(value, str) or value not in valid_types
        for key, value in catalog.items()
    ):
        raise ValueError('打印上下文字段目录无效')
    filename_fields = resource['filename_fields']
    def filename_type(key):
        if key.startswith('entry.'):
            key = 'entries[].' + key[len('entry.'):]
        return catalog.get(key)
    if (not isinstance(filename_fields, list) or not filename_fields or
            any(not isinstance(key, str) for key in filename_fields) or
            len(set(filename_fields)) != len(filename_fields) or
            any(filename_type(key) in (None, 'array', 'object') and
                key not in ('role.id', 'role.label') for key in filename_fields)):
        raise ValueError('打印文件名字段目录无效')
    for collection, alias in (('entries[]', 'entry'), ('rows[]', 'row')):
        for key, value in list(catalog.items()):
            if key == collection or key.startswith(collection + '.'):
                catalog[alias + key[len(collection):]] = value
    return catalog, tuple(filename_fields)


def context_field_catalog(definition=None):
    """Shared, lightweight catalog; live context keys never extend its API."""
    catalog, _ = _context_catalogs()
    for field in (definition or {}).get('fields', []):
        scope, name = field['scope'], field['id']
        catalog[f'{scope}.fields.{name}'] = field['type']
        if scope == 'entry':
            catalog[f'entries[].fields.{name}'] = field['type']
            catalog[f'rows[].entry.fields.{name}'] = field['type']
        elif scope == 'batch':
            catalog[f'entries[].batch.fields.{name}'] = field['type']
            catalog[f'rows[].entry.batch.fields.{name}'] = field['type']
        if scope == 'entry':
            catalog[f'row.entry.fields.{name}'] = field['type']
        elif scope == 'batch':
            catalog[f'entry.batch.fields.{name}'] = field['type']
            catalog[f'row.entry.batch.fields.{name}'] = field['type']
    return catalog


def filename_field_catalog():
    """Public filename variables, without widening the template namespace."""
    _, fields = _context_catalogs()
    return fields


def __getattr__(name):
    if name == 'FILENAME_FIELDS':
        return frozenset(filename_field_catalog())
    raise AttributeError(name)


ROLE_TYPES = {"invoice_pdf": "invoice", "invoice_xml": "invoice", "payment_screenshot": "payment_screenshot", "physical_image": "physical_image", "inspection_pdf": "inspection_pdf", "other": "other"}
ROLE_LABELS = {"invoice": "发票", "payment_screenshot": "付款截图", "physical_image": "实物图", "inspection_pdf": "查验单", "other": "附件"}


def decimal_value(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).strip())
        if result.is_finite() and len(result.as_tuple().digits) <= 100 and abs(result.adjusted()) <= 100:
            return result
    except (InvalidOperation, ValueError, TypeError):
        pass
    return None


def decimal_text(value):
    number = decimal_value(value)
    return format(number, "f") if number is not None else None


def exact_sum(values):
    numbers = [decimal_value(value) for value in values]
    known = [number for number in numbers if number is not None]
    with localcontext() as ctx:
        ctx.prec = 220
        total = sum(known, Decimal(0))
    # Partial sums are explicitly accompanied by missing counts, never passed as complete totals.
    return (format(total, "f") if known else None), len(numbers) - len(known)


def field_current(entry, name):
    value = (entry.get("fields") or {}).get(name)
    return value.get("current") if isinstance(value, dict) else value


def json_data(value):
    if isinstance(value, Decimal):
        return decimal_text(value)
    if value is None or type(value) in (str, bool, int, float):
        return value
    if isinstance(value, dict):
        return {str(key): json_data(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_data(val) for val in value]
    raise TypeError("Output contexts contain JSON data only")


def title_for_entry(entry, definition=None):
    titles = (definition or {}).get("scheme", {}).get("titles", [])
    title_id = entry.get("title_profile_id") or ""
    name = entry.get("buyer_name") or entry.get("title") or ""
    tax = entry.get("buyer_tax_id") or ""
    candidates = [title for title in titles if (title_id and title.get("id") == title_id) or (not title_id and title.get("name") == name and (not tax or title.get("tax_id") == tax))]
    configured = candidates[0] if len(candidates) == 1 else {}
    return {"id": configured.get("id") or title_id or "", "name": name or configured.get("name", ""), "tax_id": tax, "short_name": configured.get("short_name") or name or "未标注抬头", "color": configured.get("color", "neutral")}


def title_key(title, entry_id=""):
    name = unicodedata.normalize("NFKC", title.get("name", "")).strip()
    tax = re.sub(r"\s+", "", title.get("tax_id", "")).upper()
    return (name, tax or ("id:" + title["id"] if title.get("id") else "unconfirmed:" + entry_id))


def extension_defaults(definition, scope, values=None):
    result = {f["id"]: deepcopy(f.get("default")) for f in (definition or {}).get("fields", []) if f.get("scope") == scope}
    result.update(json_data(values or {}))
    return result


def project_batch(batch, definition=None):
    if not batch:
        return None
    return {'id': batch.get('id') or '', 'name': batch.get('name') or '',
            'notes': batch.get('notes') or batch.get('note') or '',
            'fields': extension_defaults(definition or {}, 'batch', batch.get('fields'))}


def project_entry(entry, profile=None, definition=None):
    profile = profile or entry.get("claimant") or {}
    definition = definition or {}
    actual = field_current(entry, "actual_item_name") or ""
    total = decimal_text(entry.get("total")) if entry.get("total_present") is not False else None
    paid = decimal_text(field_current(entry, "paid_amount"))
    role_defs = {r["id"]: r for r in definition.get("materials", [])}
    materials = []
    for att in entry.get("attachments", []):
        role = att.get("role_id") or ROLE_TYPES.get(att.get("type"), "other")
        materials.append({"id": att.get("id", ""), "role_id": role, "label": role_defs.get(role, {}).get("label") or ROLE_LABELS.get(role, "附件"), "name": att.get("original_name", ""), "type": att.get("type", "other")})
    items = []
    for index, item in enumerate(entry.get("items") or []):
        items.append({"index": index + 1, "actual_name": actual if index == 0 and actual else item.get("actual_name") or item.get("name", ""), "product_name": item.get("product_name") or item.get("actual_name") or item.get("name", ""), "unit": item.get("unit") or None, "quantity": decimal_text(item.get("quantity")), "total": decimal_text(item.get("total")), "unit_price": decimal_text(item.get("unit_price")), "spec": item.get("spec") or "", "is_summary": False, "defaults": []})
    return {"id": entry.get("id") or entry.get("entry_id", ""), "entry_id": entry.get("id") or entry.get("entry_id", ""), "scheme_id": entry.get("scheme_id") or "", "revision_id": entry.get("scheme_revision_id") or "", "title": title_for_entry(entry, definition), "batch": project_batch(entry.get("batch") or ({"id": entry["batch_id"], "name": entry.get("batch_name", "")} if entry.get("batch_id") else None), definition), "claimant": {"id": profile.get("id") or entry.get("profile_id") or "", "name": profile.get("name") or entry.get("profile_name") or "", "reviewer": profile.get("reviewer") or entry.get("reviewer") or ""}, "invoice": {"paid_amount": paid, "title_id": entry.get("title_profile_id") or "", "number": entry.get("invoice_no", ""), "date": entry.get("invoice_date", ""), "seller": entry.get("seller", ""), "total": total, "buyer_name": entry.get("buyer_name") or entry.get("title", ""), "buyer_tax_id": entry.get("buyer_tax_id", "")}, "title_id": entry.get("title_profile_id") or "", "actual_name": actual or (items[0]["actual_name"] if items else "未填写品名"), "product_name": items[0]["product_name"] if items else actual, "buyer_name": entry.get("buyer_name") or entry.get("title", ""), "buyer_tax_id": entry.get("buyer_tax_id", ""), "invoice_no": entry.get("invoice_no", ""), "invoice_date": entry.get("invoice_date", ""), "seller": entry.get("seller", ""), "total": total, "paid_amount": paid, "actual_item_name": actual, "notes": field_current(entry, "notes") or "", "status": entry.get("status", ""), "check_status": entry.get("check_status", ""), "completeness": json_data(entry.get("completeness") or {}), "created_at": entry.get("created_at", ""), "fields": extension_defaults(definition, "entry", entry.get("extension_values") or {}), "items": items, "materials": materials, "attachment_count": len(materials)}


def get_path(data, path, default=None):
    for part in path.split("."):
        if not isinstance(data, dict) or part not in data:
            return default
        data = data[part]
    return data


def rows_for_entries(entries, output):
    rows = []
    defaults = output.get("row_defaults") or {}
    for entry in entries:
        items = deepcopy(entry["items"])
        if not items:
            items = [{"actual_name": entry["actual_item_name"] or "未填写品名", "product_name": entry["actual_item_name"] or "未填写品名", "unit": None, "quantity": None, "total": entry["total"], "unit_price": None, "spec": "", "is_summary": True, "defaults": []}]
        if output.get("rows", "invoice_summary") == "invoice_summary":
            first = deepcopy(items[0])
            units = {it["unit"] for it in items}
            qty, missing = exact_sum(it["quantity"] for it in items)
            same_units = len(units) == 1 and None not in units
            first["quantity"] = qty if not missing and (same_units or defaults.get("quantity_mode") == "sum_compat") else None
            first["unit"] = first["unit"] if same_units or defaults.get("quantity_mode") == "sum_compat" else None
            first["total"] = entry["total"]
            first["unit_price"] = None
            if len(items) > 1:
                first["actual_name"] += "等"
                first["product_name"] += "等"
            items = [first]
        for item_index, item in enumerate(items):
            for field in ("unit", "quantity"):
                if item.get(field) is None and field in defaults:
                    item[field] = defaults[field]
                    item["defaults"].append(field)
            quantity = decimal_value(item.get("quantity"))
            amount = decimal_value(item.get("total"))
            if quantity is not None and quantity != 0 and amount is not None:
                with localcontext() as ctx:
                    ctx.prec = 220
                    item["unit_price"] = format(amount / quantity, "f")
            row = deepcopy(item)
            row.update({"id": entry["id"] + "-" + str(item_index), "invoice_date": entry["invoice_date"], "is_first": item_index == 0, "reviewer": entry["claimant"]["reviewer"], "tax_id": entry["title"]["tax_id"], "index": len(rows) + 1, "entry_id": entry["id"], "entry": deepcopy(entry), "claimant": entry["claimant"]["name"], "title": entry["title"]["name"], "invoice": deepcopy(entry["invoice"]), "seller": entry["seller"], "invoice_no": entry["invoice_no"], "paid_amount": entry["paid_amount"] if item_index == 0 else None, "invoice_total": entry["total"] if item_index == 0 else None, "amount": entry["paid_amount"] if output.get("amount_basis") == "paid" and item_index == 0 else (None if output.get("amount_basis") == "paid" else item.get("total")), "storage_location": defaults.get("storage_location", ""), "first_for_invoice": item_index == 0})
            rows.append(row)
    return rows


def build_export_context(entries, definition=None, output=None, profiles=None, payee=None, batch=None, scheme=None, options=None):
    definition = definition or {}
    output = output or {}
    options = options or {}
    projected = [project_entry(entry, (profiles or {}).get(entry.get("profile_id")), definition) if "invoice" not in entry else deepcopy(entry) for entry in entries]
    sorting = output.get("sort") or output.get("sort_by") or []
    if isinstance(sorting, str):
        sorting = [sorting]
    sort_paths = {"invoice_date": "invoice.date", "invoice_no": "invoice.number", "claimant": "claimant.name", "seller": "invoice.seller", "created_at": "created_at"}
    if sorting and "selection" not in sorting:
        projected.sort(key=lambda e: (*[str(get_path(e, sort_paths.get(key, key), "") or "") for key in sorting], e["id"]))
    titles = {title_key(e["title"], e["id"]) for e in projected}
    batches = {e["batch"]["id"] if e.get("batch") else None for e in projected}
    invoice_sum, missing_invoice = exact_sum(e["total"] for e in projected)
    paid_sum, missing_paid = exact_sum(e["paid_amount"] for e in projected)
    diagnostics = []
    for e in projected:
        if not e["items"]:
            diagnostics.append({"code": "SUMMARY_ROW", "severity": "warning", "target": e["id"], "message": "未识别明细，使用明确标记的发票汇总行。"})
        if e["total"] is not None and e["paid_amount"] is not None and decimal_value(e["total"]) != decimal_value(e["paid_amount"]):
            diagnostics.append({"code": "PAID_DIFFERS", "severity": "warning", "target": e["id"], "message": "实付金额与发票总额不同。"})
        if any(i["unit"] is None or i["quantity"] is None for i in e["items"]):
            diagnostics.append({"code": "MISSING_QUANTITY_UNIT", "severity": "warning", "target": e["id"], "message": "部分明细数量或单位缺失，输出空值或显式配置的缺省值。"})
    scheme_data = None
    if definition:
        organization={"name":"", "department":"", "purpose":"", "storage_location":"", **deepcopy(definition.get("scheme", {}).get("organization") or {})}
        scheme_data = {**organization, "organization": organization, "id": (scheme or {}).get("id") or (projected[0]["scheme_id"] if projected else ""), "name": (scheme or {}).get("name") or definition.get("manifest", {}).get("name", ""), "package_id": definition.get("manifest", {}).get("package_id", ""), "package_version": definition.get("manifest", {}).get("package_version", ""), "revision_id": projected[0]["revision_id"] if projected else "", "fields": extension_defaults(definition, "scheme", (scheme or {}).get("fields"))}
    payee_data = deepcopy(payee) if payee else None
    if payee_data is not None:
        payee_data["person_name"] = payee_data.get("name", "")
        payee_data["personnel_number"] = payee_data.get("personnel_id", "")
        payee_data["phone"] = payee_data.get("contact", "")
        payee_data["fields"] = extension_defaults(definition, "payee", payee_data.get("fields"))
    batch_data = project_batch(batch or (projected[0]["batch"] if projected and len(batches) == 1 else None), definition)
    basis = output.get("amount_basis", "invoice")
    selected_sum, selected_missing = (paid_sum, missing_paid) if basis == "paid" else (invoice_sum, missing_invoice)
    rows=rows_for_entries(projected,output)
    # Selection maps belong to the planner/job, never the template namespace.
    # Only settings applying to this output enter its frozen context.
    public_options = {key: deepcopy(options[key]) for key in (
        'date', 'document_date', 'amount_basis', 'sort_by', 'numbering',
        'image_layout', 'content_order', 'storage_location', 'annotate', 'batch_note'
    ) if key in options and options[key] is not None}
    return json_data({"schema_version": CONTEXT_VERSION, "scheme": scheme_data, "title": deepcopy(projected[0]["title"]) if projected and len(titles) == 1 else None, "batch": batch_data, "payee": payee_data, "export": {"date": options.get("date") or options.get("document_date") or date.today().isoformat(), "output_id": output.get("id", ""), "fields": extension_defaults(definition, "export", options.get("fields")), "options": public_options}, "entries": projected, "rows": rows, "totals": {"invoice": invoice_sum if not missing_invoice else None, "paid": paid_sum if not missing_paid else None, "known_invoice": invoice_sum, "known_paid": paid_sum, "selected": selected_sum if not selected_missing else None, "amount": selected_sum if not selected_missing else None, "missing_paid_count": missing_paid, "count": len(projected), "entry_count": len(projected), "row_count": len(rows), "missing_invoice": missing_invoice, "missing_paid": missing_paid, "missing_selected": selected_missing, "amount_basis": basis}, "diagnostics": diagnostics})


_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def safe_name(value, fallback="未命名", max_len=120):
    text = _BAD_CHARS.sub("_", str(value if value is not None else "")).strip(" ._") or fallback
    if _RESERVED.match(text):
        text = "_" + text
    # Bound bytes as well as characters for Windows and common Unix filesystems.
    while len(text.encode("utf-8")) > max_len * 2 or len(text) > max_len:
        text = text[:-1]
    return text or fallback


def format_filename(pattern, context):
    pieces = []
    filename_fields = filename_field_catalog()
    for literal, field, spec, conversion in string.Formatter().parse(pattern):
        if spec or conversion or field is not None and field not in filename_fields:
            raise ValueError(f"不支持的文件名变量：{field}")
        pieces.append(literal)
        if field is not None:
            pieces.append(safe_name(get_path(context, field), "未填写"))
    return safe_name("".join(pieces), max_len=120)


def filename_for(output, context, identity=""):
    extension = {"docx": ".docx", "pdf_bundle": ".pdf", "xlsx": ".xlsx", "attachment_zip": ".zip"}[output["type"]]
    name = format_filename(output.get("filename") or (output.get("label") or output["id"]) + extension, context)
    if not name.lower().endswith(extension):
        name += extension
    return name


def digest_data(data):
    return hashlib.sha256(json.dumps(json_data(data), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def assert_pure_context(context):
    """IPC and templates share a dependency-free size/type boundary."""
    def visit(value,depth=0):
        if depth>20:raise ValueError('上下文嵌套过深')
        if value is None or type(value) in (str,int,bool):
            if isinstance(value,str) and len(value)>100000:raise ValueError('上下文文字过长')
            return
        if type(value) is dict:
            if any(not isinstance(key,str) or key.startswith('_') for key in value):raise ValueError('上下文包含非法键')
            for val in value.values():visit(val,depth+1)
            return
        if type(value) is list and len(value)<=50000:
            for val in value:visit(val,depth+1)
            return
        raise TypeError('模板上下文只允许纯 JSON 数据，金额必须使用字符串')
    visit(context)
    if len(json.dumps(context,ensure_ascii=False).encode())>16*1024*1024:raise ValueError('上下文超过 16 MiB')

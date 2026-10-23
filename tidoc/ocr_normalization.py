"""Pure invoice OCR normalization shared by the core and optional component.

This module uses only the standard library. Reading saved OCR responses never
loads a cloud SDK or performs a network request.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_FIELD_CANDIDATES = {
    "invoice_no": ("invoiceNumber", "invoiceNo"),
    "invoice_code": ("invoiceCode",),
    "invoice_date": ("invoiceDate",),
    "seller": ("sellerName", "seller"),
    "buyer_name": ("purchaserName", "buyerName"),
    "buyer_tax_id": (
        "purchaserTaxNumber",
        "buyerTaxNumber",
        "buyerTaxNo",
        "buyerRegisterNum",
        "buyerIdNum",
    ),
    "total": ("totalAmount", "amountWithTax", "totalTaxIncludedAmount"),
}


_ITEM_CANDIDATES = {
    "name": ("itemName", "name", "goodsName"),
    "spec": ("specification", "specModel", "spec"),
    "unit": ("unit", "unitName"),
    "quantity": ("quantity", "qty"),
    "unit_price": ("unitPrice",),
    "amount": ("amount", "withoutTaxAmount"),
    "tax": ("tax", "taxAmount"),
}


def _pick(mapping, *keys) -> str:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _dec(value) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    text = str(value).replace(",", "").replace("¥", "").replace("￥", "").strip()
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _dec_text(value: Decimal) -> str:
    """去掉尾随零的数量/单价文本（OCR 常返回 2.00000000 之类）。"""
    text = f"{value:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def clean_item_name(raw: str) -> str:
    """去掉数电票货物名称里的「*分类*」前缀。"""
    return re.sub(r"^\*[^*]+\*", "", str(raw or "").strip()).strip()


def _collapse_repeated_item_name(raw: str) -> str:
    """Collapse an exact doubled OCR item label emitted around a discount row."""
    text = str(raw or "").strip()
    midpoint = len(text) // 2
    if (
        text.startswith("*")
        and midpoint
        and len(text) % 2 == 0
        and text[:midpoint] == text[midpoint:]
        and re.match(r'^\*[^*]+\*.+', text[:midpoint])
    ):
        return text[:midpoint]
    return text


def _normalize_date(raw: str) -> str:
    text = str(raw or "").strip()
    match = re.match(r"^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?$", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return text


def normalize_invoice_data(data: dict) -> dict:
    """把阿里云 data dict 规范化为核心可比较的结构（仅依赖标准库）。

    返回：
    {
      "invoice_no": "2412...", "invoice_date": "2026-01-31", "seller": "...",
      "buyer_name": "...", "buyer_tax_id": "...",
      "total": "123.45",              # 价税合计（含税）
      "item_sum": "123.45",           # 明细含税合计（闭合校验用）
      "closure_pass": true,           # 明细合计与价税合计一致到分
      "items": [{"name","actual_name","unit","quantity","total","spec"}, ...]
    }
    """
    fields = {
        key: _pick(data, *candidates)
        for key, candidates in _FIELD_CANDIDATES.items()
    }
    total = _money(_dec(fields["total"]))
    fields["total"] = f"{total:.2f}"
    if fields["invoice_date"]:
        fields["invoice_date"] = _normalize_date(fields["invoice_date"])

    items: list[dict] = []
    item_sum = Decimal("0")
    last: dict | None = None
    details = data.get("invoiceDetails") or []
    for index, detail in enumerate(details):
        if not isinstance(detail, dict):
            continue
        raw_name = _collapse_repeated_item_name(
            _pick(detail, *_ITEM_CANDIDATES["name"])
        )
        unit = _pick(detail, *_ITEM_CANDIDATES["unit"])
        quantity_text = _pick(detail, *_ITEM_CANDIDATES["quantity"])
        spec = _pick(detail, *_ITEM_CANDIDATES["spec"])
        line_total = _money(
            _dec(_pick(detail, *_ITEM_CANDIDATES["amount"]))
            + _dec(_pick(detail, *_ITEM_CANDIDATES["tax"]))
        )

        # A bare name fragment between a product and its matching discount is
        # a wrapped continuation, not a zero-price item. Require the following
        # discount to corroborate the exact completed name before joining it.
        following = details[index + 1] if index + 1 < len(details) else None
        if (
            raw_name and not raw_name.startswith('*') and last is not None
            and not any(_pick(detail, *_ITEM_CANDIDATES[key]) for key in
                        ('unit', 'quantity', 'spec', 'amount', 'tax', 'unit_price'))
            and isinstance(following, dict)
            and not _pick(following, *_ITEM_CANDIDATES['quantity'])
            and not _pick(following, *_ITEM_CANDIDATES['unit'])
            and _dec(_pick(following, *_ITEM_CANDIDATES['amount'])) < 0
            and re.sub(r'\s+', '', _pick(following, *_ITEM_CANDIDATES['name']))
                == re.sub(r'\s+', '', last['_raw_name'] + raw_name)
        ):
            last['_raw_name'] += raw_name
            last['name'] = last['_raw_name']
            last['actual_name'] = clean_item_name(last['name'])
            continue

        # 阿里云偶尔把折扣拆成紧随商品的空名称负数行。金额已经识别正确，
        # 此时应并回上一件商品；不能因名称为空直接丢弃，否则明细合计偏大。
        if (
            not raw_name
            and last is not None
            and not quantity_text
            and not unit
            and not spec
            and line_total < 0
        ):
            merged = _money(_dec(last["total"]) + line_total)
            last["total"] = f"{merged:.2f}"
            item_sum += line_total
            continue
        if not raw_name:
            continue

        # 价外费用类续行：与上一行同名且无数量的行，金额并入上一行
        if last is not None and not quantity_text and raw_name == last.get("_raw_name"):
            merged = _money(_dec(last["total"]) + line_total)
            last["total"] = f"{merged:.2f}"
            item_sum += line_total
            continue

        item = {
            "_raw_name": raw_name,
            "name": raw_name,
            "actual_name": clean_item_name(raw_name),
            "unit": unit,
            "quantity": _dec_text(_dec(quantity_text)) if quantity_text else "",
            "total": f"{line_total:.2f}",
            "spec": spec,
        }
        items.append(item)
        last = item
        item_sum += line_total

    for item in items:
        item.pop("_raw_name", None)

    item_sum = _money(item_sum)
    closure_pass = bool(items) and item_sum == total
    return {
        "invoice_no": fields["invoice_no"],
        "invoice_code": fields["invoice_code"],
        "invoice_date": fields["invoice_date"],
        "seller": fields["seller"],
        "buyer_name": fields["buyer_name"],
        "buyer_tax_id": fields["buyer_tax_id"],
        "total": fields["total"],
        "item_sum": f"{item_sum:.2f}",
        "closure_pass": closure_pass,
        "page_count": max(1, int(data.get("_tidoc_page_count") or 1)),
        "items": items,
    }

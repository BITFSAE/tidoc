"""发票金额闭合与抬头一致性校验。应用路径显式传入条目修订上下文。"""

from __future__ import annotations

import re
from contextvars import ContextVar
from decimal import Decimal

from .models import CHECK_BLOCKED, CHECK_PASS, CHECK_WARNING, CheckResult, ParsedInvoice
from .money import fmt_money, money

# Legacy standalone callers retain the old exported names. Their defaults come
# from the same bundled resource as migration and IPC v1, never a second catalog.
def _legacy_default_profiles():
    import json
    from tidoc_print.context import component_resource_path
    path = component_resource_path('builtin_adapters/org.bitfsae.reimbursement/scheme.json', package='tidoc')
    titles = {title['id']: title for title in json.loads(path.read_text('utf-8'))['titles']}
    return tuple((titles[key]['name'], titles[key].get('tax_id') or '')
                 for key in ('bit_university', 'bit_foundation'))


DEFAULT_TITLE_PROFILES = _legacy_default_profiles()
(TITLE_UNIVERSITY, TAX_ID_UNIVERSITY), (TITLE_FOUNDATION, TAX_ID_FOUNDATION) = DEFAULT_TITLE_PROFILES
SUPPORTED_TITLES = tuple(name for name, _ in DEFAULT_TITLE_PROFILES)
EXPECTED_BUYER_TAX_IDS = dict(DEFAULT_TITLE_PROFILES)
_legacy_titles = ContextVar("legacy_invoice_titles", default=DEFAULT_TITLE_PROFILES)


def set_title_profiles(profiles=None) -> tuple[tuple[str, str], ...]:
    """兼容旧独立调用者，仅修改当前执行上下文的抬头配置。

    profiles 接受 [{"name", "tax_id"}] 或 (名称, 税号) 序列；名称去重、去空白，
    税号按标准形态归一化。传 None 表示恢复内置默认；传空序列表示清空配置
    （清空后不做抬头范围提醒）。
    """
    if profiles is None:
        _legacy_titles.set(DEFAULT_TITLE_PROFILES)
        return DEFAULT_TITLE_PROFILES
    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for profile in profiles:
        if isinstance(profile, (tuple, list)):
            name, tax_id = str(profile[0] if len(profile) > 0 else ""), str(profile[1] if len(profile) > 1 else "")
        elif isinstance(profile, dict):
            name, tax_id = str(profile.get("name") or ""), str(profile.get("tax_id") or "")
        else:
            name, tax_id = str(profile or ""), ""
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        normalized.append((name, normalize_tax_id(tax_id)))
    _legacy_titles.set(tuple(normalized))
    return tuple(normalized)


def title_profiles(context=None) -> tuple[tuple[str, str], ...]:
    if context is None:
        # Compatibility for standalone callers. Application paths always supply context.
        return _legacy_titles.get()
    values = context.titles if hasattr(context, "titles") else context.get("scheme", {}).get("titles", [])
    return tuple((str(t.get("name", "")).strip(), normalize_tax_id(t.get("tax_id", "")))
                 if isinstance(t, dict) else (str(t[0]), normalize_tax_id(t[1])) for t in values)


def supported_titles(context=None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(name for name, _ in title_profiles(context)))


def expected_tax_ids(context=None) -> dict[str, str]:
    return {name: tax_id for name, tax_id in title_profiles(context) if tax_id}


def _title_by_tax_id(context=None) -> dict[str, str]:
    return {tax_id: name for name, tax_id in title_profiles(context) if tax_id}


def normalize_tax_id(value: str) -> str:
    """税号比较用标准形态：忽略空白/分隔符，并统一为大写。"""
    return re.sub(r"[^0-9A-Z]", "", str(value or "").upper())


def check_invoice(invoice: ParsedInvoice, expected_title: str = "", context=None) -> CheckResult:
    """对单张发票做金额闭合与抬头校验，返回 pass / warning / blocked。

    - blocked：抬头与所属分区不一致（会造成串账）。
    - warning：明细识别合计与发票总额不一致、缺明细、抬头无法识别等；
      这些通常是识别完整性问题，不阻断材料齐备。
    - pass：全部通过。
    """
    problems_blocked: list[str] = []
    problems_warning: list[str] = []

    # 明细金额仅用于提示识别完整性。发票总额取自票面关键信息，明细漏识别
    # 不应阻断后续材料整理、导出和打印。
    if invoice.items:
        item_sum = sum((item.total for item in invoice.items), Decimal("0"))
        diff = money(invoice.total - item_sum)
        if diff != Decimal("0.00"):
            problems_warning.append(
                f"明细识别合计与发票总额相差 {fmt_money(diff)}，"
                "可能是明细识别不完整，请以发票总额为准。"
            )
    else:
        problems_warning.append("未能自动识别物品明细，请确认或补充。")

    # 抬头与购买方税号识别。税号不参与材料齐备度，但会形成可恢复、可重识别的
    # 识别提醒，避免只凭名称把主体判断错。未配置任何抬头时不做抬头范围提醒。
    titles = supported_titles(context)
    if not invoice.buyer_name:
        problems_warning.append("未能识别购买方抬头。")
    elif titles and invoice.buyer_name not in titles:
        problems_warning.append(
            f"购买方抬头「{invoice.buyer_name}」不在已配置的抬头内。"
        )

    buyer_tax_id = normalize_tax_id(invoice.buyer_tax_id)
    expected_tax_id = expected_tax_ids(context).get(invoice.buyer_name)
    candidates = {tax for name, tax in title_profiles(context) if name == invoice.buyer_name and tax}
    if buyer_tax_id in candidates:
        expected_tax_id = buyer_tax_id
    if expected_tax_id:
        if not buyer_tax_id:
            problems_warning.append(
                f"未能识别「{invoice.buyer_name}」的购买方税号，应为 {expected_tax_id}，请核对。"
            )
        elif buyer_tax_id != expected_tax_id:
            recognized_title = _title_by_tax_id(context).get(buyer_tax_id)
            belongs_to = f"（该税号属于「{recognized_title}」）" if recognized_title else ""
            problems_warning.append(
                f"购买方税号「{invoice.buyer_tax_id}」与「{invoice.buyer_name}」不一致，"
                f"应为 {expected_tax_id}{belongs_to}，请核对。"
            )
    elif buyer_tax_id in _title_by_tax_id(context):
        tax_title = _title_by_tax_id(context)[buyer_tax_id]
        if invoice.buyer_name:
            problems_warning.append(
                f"购买方税号 {buyer_tax_id} 属于「{tax_title}」，"
                f"但识别到的抬头为「{invoice.buyer_name}」，请核对。"
            )
        else:
            problems_warning.append(
                f"购买方税号 {buyer_tax_id} 属于「{tax_title}」，但购买方抬头未识别，请核对。"
            )

    # 抬头与所属分区一致性（强隔离）
    if expected_title and invoice.buyer_name and invoice.buyer_name != expected_title:
        problems_blocked.append(
            f"发票抬头为「{invoice.buyer_name}」，与当前分区「{expected_title}」不一致，禁止混入。"
        )

    if problems_blocked:
        return CheckResult(CHECK_BLOCKED, "；".join(problems_blocked + problems_warning))
    if problems_warning:
        return CheckResult(CHECK_WARNING, "；".join(problems_warning))
    return CheckResult(CHECK_PASS, "")

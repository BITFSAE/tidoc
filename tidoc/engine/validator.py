"""发票校验：金额闭合 + 抬头一致性。移植自 invoice2docx/engine.py 的 validate_invoices。

设计文档第 7 节：两个抬头强隔离。这里的抬头一致性校验用于提示"这张发票的抬头
和它所属分区不符"。
"""

from __future__ import annotations

import re
from decimal import Decimal

from .models import CHECK_BLOCKED, CHECK_PASS, CHECK_WARNING, CheckResult, ParsedInvoice
from .money import fmt_money, money

# 设计文档第 7 节：默认受支持的两个抬头（北理工 / 教育基金会）。抬头与税号
# 可以在设置里按学校 / 单位维护，识别（parser）与校验（check_invoice）都按
# 当前生效的配置执行；这里的常量是未配置时的默认值。
TITLE_UNIVERSITY = "北京理工大学"
TITLE_FOUNDATION = "北京理工大学教育基金会"
SUPPORTED_TITLES = (TITLE_UNIVERSITY, TITLE_FOUNDATION)

# 购买方统一社会信用代码 / 纳税人识别号。发票抬头与税号共同确定报账主体；
# 名称识别正确但税号缺失或串到另一主体时，也必须留在“识别提醒”中供人工核对。
TAX_ID_UNIVERSITY = "12100000400009127B"
TAX_ID_FOUNDATION = "53100000500021676K"
EXPECTED_BUYER_TAX_IDS = {
    TITLE_UNIVERSITY: TAX_ID_UNIVERSITY,
    TITLE_FOUNDATION: TAX_ID_FOUNDATION,
}

# 生效配置：(抬头名称, 购买方税号)；税号允许为空，为空时只按抬头名称提示。
DEFAULT_TITLE_PROFILES = (
    (TITLE_UNIVERSITY, TAX_ID_UNIVERSITY),
    (TITLE_FOUNDATION, TAX_ID_FOUNDATION),
)
_title_profiles: tuple[tuple[str, str], ...] = DEFAULT_TITLE_PROFILES


def set_title_profiles(profiles=None) -> tuple[tuple[str, str], ...]:
    """覆盖进程内生效的抬头配置，返回规范化后的配置。

    profiles 接受 [{"name", "tax_id"}] 或 (名称, 税号) 序列；名称去重、去空白，
    税号按标准形态归一化。传 None 表示恢复内置默认；传空序列表示清空配置
    （清空后不做抬头范围提醒）。
    """
    global _title_profiles
    if profiles is None:
        _title_profiles = DEFAULT_TITLE_PROFILES
        return _title_profiles
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
    _title_profiles = tuple(normalized)
    return _title_profiles


def title_profiles() -> tuple[tuple[str, str], ...]:
    return _title_profiles


def supported_titles() -> tuple[str, ...]:
    return tuple(name for name, _ in _title_profiles)


def expected_tax_ids() -> dict[str, str]:
    return {name: tax_id for name, tax_id in _title_profiles if tax_id}


def _title_by_tax_id() -> dict[str, str]:
    return {tax_id: name for name, tax_id in expected_tax_ids().items()}


def normalize_tax_id(value: str) -> str:
    """税号比较用标准形态：忽略空白/分隔符，并统一为大写。"""
    return re.sub(r"[^0-9A-Z]", "", str(value or "").upper())


def check_invoice(invoice: ParsedInvoice, expected_title: str = "") -> CheckResult:
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
    titles = supported_titles()
    if not invoice.buyer_name:
        problems_warning.append("未能识别购买方抬头。")
    elif titles and invoice.buyer_name not in titles:
        problems_warning.append(
            f"购买方抬头「{invoice.buyer_name}」不在已配置的抬头内。"
        )

    buyer_tax_id = normalize_tax_id(invoice.buyer_tax_id)
    expected_tax_id = expected_tax_ids().get(invoice.buyer_name)
    if expected_tax_id:
        if not buyer_tax_id:
            problems_warning.append(
                f"未能识别「{invoice.buyer_name}」的购买方税号，应为 {expected_tax_id}，请核对。"
            )
        elif buyer_tax_id != expected_tax_id:
            recognized_title = _title_by_tax_id().get(buyer_tax_id)
            belongs_to = f"（该税号属于「{recognized_title}」）" if recognized_title else ""
            problems_warning.append(
                f"购买方税号「{invoice.buyer_tax_id}」与「{invoice.buyer_name}」不一致，"
                f"应为 {expected_tax_id}{belongs_to}，请核对。"
            )
    elif buyer_tax_id in _title_by_tax_id():
        tax_title = _title_by_tax_id()[buyer_tax_id]
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

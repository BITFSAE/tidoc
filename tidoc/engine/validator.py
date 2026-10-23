"""发票金额闭合与抬头一致性校验。应用路径显式传入条目修订上下文。"""

from __future__ import annotations

import re
from contextvars import ContextVar
from decimal import Decimal

from .fields import words_match_total
from .models import (
    CHECK_BLOCKED,
    CHECK_PASS,
    CHECK_WARNING,
    FIELD_CONFLICT,
    FIELD_MISSING,
    ISSUE_ADVISORY,
    ISSUE_STRONG,
    TOTAL_SOURCE_MAX_AMOUNT,
    CheckIssue,
    CheckResult,
    ParsedInvoice,
)
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


# 解析器记录了逐字段比对结果；这些字段名对应的界面用语。价税合计的冲突在金额校验里单独处理。
_FIELD_LABELS = {
    "invoice_no": "发票号码",
    "invoice_date": "开票日期",
    "seller": "销售方名称",
    "seller_tax_id": "销售方税号",
    "buyer_name": "购买方名称",
    "buyer_tax_id": "购买方税号",
}
# 缺失时值得提醒的字段。购买方名称已有「未能识别购买方抬头」；税号缺失常见于个人抬头，不提醒。
_MISSING_REMINDERS = ("invoice_no", "invoice_date", "seller")


def _total_overridden(invoice: ParsedInvoice) -> bool:
    """总额已被外部（例如人工修正过的条目）覆盖：解析时读到的总额不再代表当前值。"""
    return invoice.total_read is not None and money(invoice.total) != money(invoice.total_read)


def _issue(issues: list, code: str, level: str, message: str) -> None:
    issues.append(CheckIssue(code, level, message))


def _safe(value) -> str:
    """提醒文案用全角分号 ； 分隔多条，票面内容里的分号要换掉，否则会被拆成两条。"""
    return str(value).replace("；", ";")


def _conflict_message(label: str, check, shown: str = "") -> str:
    values: list[str] = []
    for value in check.candidates.values():
        if value and value not in values:
            values.append(_safe(value))
    quoted = "」与「".join(values)
    return f"{label}识别到两个不同的结果：「{quoted}」，已采用「{_safe(shown or check.value)}」，请核对。"


def recognition_issues(invoice: ParsedInvoice) -> list[CheckIssue]:
    """票面金额与字段的独立校验，只读取解析器记录的字段，手工构造的发票没有这些记录时不产生任何提醒。

    - 价税合计优先取自票面标签；取页面最大金额只是低可信兜底；
    - 不含税金额合计 + 税额合计 必须等于价税合计（精确到分）；
    - 大写金额折算后必须等于小写价税合计；
    - 两种读取方式读到的发票号、名称、税号、日期、价税合计不一致时给出冲突提醒；
    - 税率只做辅助估算，不要求税额等于不含税金额乘税率，因为逐行舍入、折扣、免税都会让两者有差异。
    红字发票的金额为负数，比较时按带符号的值闭合，大写金额没写「负」时按绝对值比较。
    """
    issues: list[CheckIssue] = []
    checks = invoice.field_checks
    total_check = checks.get("total")

    if invoice.total_present is False:
        if total_check is not None and total_check.basis == "unreadable":
            raw = next((value for value in total_check.candidates.values() if value), "")
            _issue(issues, "total_unreadable", ISSUE_STRONG, f"发票总额「{_safe(raw)}」无法读取，请手动填写。")
        else:
            _issue(issues, "total_missing", ISSUE_STRONG, "未能识别发票总额，请手动填写。")
    else:
        if not _total_overridden(invoice):
            if invoice.total_source == TOTAL_SOURCE_MAX_AMOUNT:
                _issue(
                    issues, "total_low_confidence", ISSUE_STRONG,
                    f"未能在票面上找到「价税合计」，暂用页面上最大的金额 {fmt_money(invoice.total)} "
                    "作为发票总额，可信度低，请核对。",
                )
            if total_check is not None and total_check.state == FIELD_CONFLICT:
                _issue(
                    issues, "total_conflict", ISSUE_STRONG,
                    _conflict_message("价税合计", total_check, fmt_money(invoice.total)),
                )
        if invoice.subtotal is not None and invoice.tax_total is not None:
            closure = invoice.subtotal + invoice.tax_total
            if closure != invoice.total:     # 两位小数的金额必须严格相等，不做四舍五入
                _issue(
                    issues, "total_not_closed", ISSUE_STRONG,
                    f"不含税金额合计 {fmt_money(invoice.subtotal)} 加税额合计 {fmt_money(invoice.tax_total)} "
                    f"为 {fmt_money(closure)}，与价税合计 {fmt_money(invoice.total)} 不一致，请核对金额。",
                )
        if invoice.total_in_words is not None:
            if not words_match_total(invoice.total_in_words, invoice.total):
                _issue(
                    issues, "words_mismatch", ISSUE_STRONG,
                    f"大写金额「{_safe(invoice.total_words_text)}」折合 {fmt_money(invoice.total_in_words)}，"
                    f"与价税合计 {fmt_money(invoice.total)} 不一致，请核对。",
                )
        elif invoice.total_words_text:
            _issue(
                issues, "words_unreadable", ISSUE_ADVISORY,
                f"大写金额「{_safe(invoice.total_words_text)}」无法读懂，未能与小写金额核对。",
            )
        if (
            invoice.subtotal is not None and invoice.tax_total is not None
            and invoice.tax_rates and invoice.subtotal != 0
        ):
            subtotal, tax = abs(invoice.subtotal), abs(invoice.tax_total)
            tolerance = Decimal("0.02") + Decimal("0.01") * len(invoice.items)
            low, high = min(invoice.tax_rates) / 100, max(invoice.tax_rates) / 100
            if tax < subtotal * low - tolerance or tax > subtotal * high + tolerance:
                rates = "、".join(f"{rate.normalize():f}%" for rate in invoice.tax_rates)
                _issue(
                    issues, "tax_rate_estimate", ISSUE_ADVISORY,
                    f"税额 {fmt_money(invoice.tax_total)} 与不含税金额 {fmt_money(invoice.subtotal)} 的比例"
                    f"（约 {(tax / subtotal * 100).quantize(Decimal('0.01'))}%）超出票面税率（{rates}）的范围，"
                    "仅供参考，请核对。",
                )

    for name, label in _FIELD_LABELS.items():
        check = checks.get(name)
        if check is None:
            continue
        # 条目里的值被人工修正后，解析时的冲突不再适用。
        if getattr(invoice, name) != check.value:
            continue
        if check.state == FIELD_CONFLICT:
            _issue(issues, f"{name}_conflict", ISSUE_STRONG, _conflict_message(label, check))
        elif check.state == FIELD_MISSING and not check.value and name in _MISSING_REMINDERS:
            level = ISSUE_STRONG if name == "invoice_no" else ISSUE_ADVISORY
            _issue(issues, f"{name}_missing", level, f"未能识别{label}，请手动填写。")
        if "checksum_failed" in check.flags:
            _issue(
                issues, f"{name}_checksum", ISSUE_ADVISORY,
                f"{label}「{_safe(check.value)}」的校验位不符，可能识别有误，请核对。",
            )
        if "invalid_date" in check.flags:
            _issue(
                issues, f"{name}_invalid", ISSUE_ADVISORY,
                f"{label}「{_safe(check.value)}」不是有效日期，请核对。",
            )
    return issues


def check_invoice(invoice: ParsedInvoice, expected_title: str = "", context=None) -> CheckResult:
    """对单张发票做金额闭合与抬头校验，返回 pass / warning / blocked。

    - blocked：抬头与所属分区不一致（会造成串账）。
    - warning：明细识别合计与发票总额不一致、缺明细、抬头无法识别，以及
      :func:`recognition_issues` 给出的票面金额 / 字段提醒（合计闭合、大写金额、字段冲突等）；
      这些通常是识别完整性问题，不阻断材料齐备。
    - pass：全部通过。
    """
    problems_blocked: list[str] = []
    problems_warning: list[str] = []

    issues = recognition_issues(invoice)
    strong_issues = [issue for issue in issues if issue.level == ISSUE_STRONG]
    advisory_issues = [issue for issue in issues if issue.level == ISSUE_ADVISORY]

    # 明细金额仅用于提示识别完整性。发票总额取自票面关键信息，明细漏识别
    # 不应阻断后续材料整理、导出和打印。
    if invoice.items:
        item_sum = sum((item.total for item in invoice.items), Decimal("0"))
        diff = money(invoice.total - item_sum)
        # 总额没读到时 total 只是占位的 0，拿它去比明细没有意义，改由「未能识别发票总额」提醒。
        if diff != Decimal("0.00") and invoice.total_present is not False:
            problems_warning.append(
                f"明细识别合计与发票总额相差 {fmt_money(diff)}，"
                "可能是明细识别不完整，请以发票总额为准。"
            )
    else:
        problems_warning.append("未能自动识别物品明细，请确认或补充。")
    problems_warning.extend(issue.message for issue in strong_issues)

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

    # 辅助级提示放在最后：税率估算、税号校验位、日期合法性只提示，不改变上面的判断。
    problems_warning.extend(issue.message for issue in advisory_issues)

    # 所有新提醒仍作为「识别提醒」（warning）出现；只有抬头与分区冲突才是 blocked。
    if problems_blocked:
        return CheckResult(CHECK_BLOCKED, "；".join(problems_blocked + problems_warning), issues)
    if problems_warning:
        return CheckResult(CHECK_WARNING, "；".join(problems_warning), issues)
    return CheckResult(CHECK_PASS, "", issues)

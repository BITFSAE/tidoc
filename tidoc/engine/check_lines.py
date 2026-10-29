"""识别提醒的类别与「已核对」状态。

条目的识别提醒存在 entries.check_message，多条之间用「；」分隔。每条开头带一个类别标记，
例如「[发票金额]」，列表按类别筛选、详情按类别分组都读这个标记，显示时去掉。
早期写入的提醒没有标记，按内容归类；重新识别或材料变化后会写上标记。

用户核对过的提醒按显示文字记在 entries.check_reviewed（JSON 数组）。提醒内容一变，
例如重新识别后金额不同，就不再算已核对，重新出现在「识别提醒」里。
"""

from __future__ import annotations

import json
import re

AMOUNT_PREFIX = "[发票金额]"
INVOICE_PREFIX = "[发票信息]"
TITLE_PREFIX = "[抬头税号]"
PAYMENT_CHECK_PREFIX = "[付款截图识别]"
INSPECTION_CHECK_PREFIX = "[查验单]"

# (类别 id, 界面名称, 写入时的标记)，顺序即界面上的排列顺序。
CATEGORIES = (
    ("amount", "发票金额", AMOUNT_PREFIX),
    ("invoice", "发票信息", INVOICE_PREFIX),
    ("title", "抬头税号", TITLE_PREFIX),
    ("payment", "付款截图", PAYMENT_CHECK_PREFIX),
    ("inspection", "查验单", INSPECTION_CHECK_PREFIX),
    ("other", "其他", ""),
)
CATEGORY_IDS = tuple(category for category, _label, _prefix in CATEGORIES)
_BY_PREFIX = {prefix: category for category, _label, prefix in CATEGORIES if prefix}
_PREFIX_RE = re.compile(r"^(\[[^\]]+\])\s*")

# 没有标记的旧提醒按关键词归类，先命中的类别优先：
# 「购买方税号与抬头不一致」归抬头税号，「明细识别合计与发票总额相差」归发票金额。
_LEGACY_RULES = (
    ("payment", ("付款截图",)),
    ("inspection", ("查验",)),
    ("title", ("购买方", "抬头", "分区")),
    ("amount", ("总额", "价税合计", "金额", "税额")),
    ("invoice", ("发票号", "日期", "销售方", "明细", "PDF", "XML", "校验位")),
)


def tag(prefix: str, text: str) -> str:
    return f"{prefix} {text}"


def split_message(message) -> list[str]:
    return [part.strip() for part in str(message or "").split("；") if part.strip()]


def strip_prefix(line: str) -> str:
    match = _PREFIX_RE.match(line)
    if match and match.group(1) in _BY_PREFIX:
        return line[match.end():]
    return line


def line_category(line: str) -> str:
    match = _PREFIX_RE.match(line)
    if match and match.group(1) in _BY_PREFIX:
        return _BY_PREFIX[match.group(1)]
    for category, words in _LEGACY_RULES:
        if any(word in line for word in words):
            return category
    return "other"


def reviewed_texts(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def check_lines(message, reviewed_raw="") -> list[dict]:
    """拆出逐条提醒：显示文字、类别、是否已核对。"""
    reviewed = set(reviewed_texts(reviewed_raw))
    lines = []
    for line in split_message(message):
        text = strip_prefix(line)
        lines.append({"text": text, "category": line_category(line), "reviewed": text in reviewed})
    return lines


def open_categories(check_status: str, lines: list[dict]) -> list[str]:
    """仍需核对的类别。只有 warning 状态的条目进「识别提醒」；严重问题另有视图。"""
    if check_status != "warning":
        return []
    if not lines:
        # 只有状态没有文字的旧数据，无法逐条核对，仍算待核对。
        return ["other"]
    open_ids = {line["category"] for line in lines if not line["reviewed"]}
    return [category for category in CATEGORY_IDS if category in open_ids]

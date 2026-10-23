"""金额处理：统一用 Decimal，避免浮点误差。移植自 invoice2docx/engine.py。"""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

MONEY = Decimal("0.01")


def parse_amount(value: str | int | float | Decimal | None) -> Decimal | None:
    """严格的金额转换：缺失或无法解析时返回 None，而不是 0。

    ``0`` 是合法金额（免税、全额折扣），所以「没有读到」和「读到了 0」必须能区分。
    发票总额、不含税合计、税额合计等字段用它转换；明细行里仅用于展示的数字仍可用
    宽松的 :func:`d`。
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    text = re.sub(r"[\s,，¥￥]", "", str(value))
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def d(value: str | int | float | Decimal | None) -> Decimal:
    """把任意来源的金额文本宽松地转成 Decimal，无法解析时返回 0。

    调用方需要区分「缺失 / 解析失败 / 真实的 0」时，改用 :func:`parse_amount`。
    """
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    text = str(value).replace(",", "").replace("¥", "").replace("￥", "").strip()
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def money(value: Decimal) -> Decimal:
    """四舍五入到分。"""
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def fmt_money(value: Decimal, currency: bool = False) -> str:
    prefix = "¥" if currency else ""
    return f"{prefix}{money(value):.2f}"


def fmt_decimal(value: Decimal | None, places: int = 8) -> str:
    """格式化数量 / 单价，去掉多余的尾随 0。"""
    if value is None:
        return ""
    quant = Decimal("1." + "0" * places)
    text = f"{value.quantize(quant, rounding=ROUND_HALF_UP):f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text

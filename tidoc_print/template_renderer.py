"""Restricted docxtpl rendering; templates never receive live Python objects."""
from __future__ import annotations

import json
from datetime import date as Date
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path

from docxtpl import DocxTemplate
from jinja2 import StrictUndefined, Undefined
from jinja2.sandbox import SandboxedEnvironment


class TemplateError(ValueError):
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        super().__init__("；".join(item["message"] for item in diagnostics))


def _decimal(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    result = Decimal(str(value))
    if not result.is_finite() or len(result.as_tuple().digits) > 100 or abs(result.adjusted()) > 100:
        raise ValueError("金额超出允许范围")
    return result


def money(value, currency=False):
    value = _decimal(value)
    if value is None:
        return ""
    with localcontext() as ctx:
        ctx.prec = 220
        return ("¥" if currency else "") + format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def number(value, places=8):
    value = _decimal(value)
    if value is None:
        return ""
    if not isinstance(places, int) or not 0 <= places <= 20:
        raise ValueError("小数位数应为 0–20")
    with localcontext() as ctx:
        ctx.prec = 220
        return format(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), "f").rstrip("0").rstrip(".") if places else format(value.quantize(Decimal(1), rounding=ROUND_HALF_UP), "f")


def date(value, style="iso"):
    if value is None or value == "":
        return ""
    text = str(value)
    if "年" in text:  # explicit legacy date; no reinterpretation
        return text
    parsed = Date.fromisoformat(text)
    if style == "chinese":
        return f"{parsed.year}年{parsed.month}月{parsed.day}日"
    if style != "iso":
        raise ValueError("未知日期样式")
    return parsed.isoformat()


def default(value, replacement="", boolean=False):
    if isinstance(value, Undefined):
        # Unlike Jinja's built-in default, misspellings must still fail.
        str(value)
    return replacement if value is None or boolean and not value else value


def join(values, separator="、"):
    if not isinstance(values, list) or len(values) > 20000:
        raise ValueError("join 仅接受有界列表")
    if any(type(v) not in (str, int, float, bool, type(None)) for v in values):
        raise ValueError("join 仅接受标量列表")
    return str(separator).join("" if value is None else str(value) for value in values)


def upper_rmb(value):
    amount = _decimal(value)
    if amount is None:
        return ""
    with localcontext() as ctx:
        ctx.prec = 220
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if abs(amount) >= Decimal("10000000000000000"):
        raise ValueError("人民币大写金额过大")
    digits = "零壹贰叁肆伍陆柒捌玖"
    def block(n):
        text, zero = "", False
        for place, unit in ((1000, "仟"), (100, "佰"), (10, "拾"), (1, "")):
            digit, n = divmod(n, place)
            if digit:
                if zero and text:
                    text += "零"
                text += digits[digit] + unit
                zero = False
            elif text:
                zero = True
        return text
    cents = int(abs(amount) * 100)
    integer, fraction = divmod(cents, 100)
    groups = []
    while integer:
        integer, group = divmod(integer, 10000)
        groups.append(group)
    result, previous_empty = "", False
    units = ("", "万", "亿", "兆")
    for i in range(len(groups) - 1, -1, -1):
        group = groups[i]
        if group:
            if result and (previous_empty or group < 1000):
                result += "零"
            result += block(group) + units[i]
            previous_empty = False
        elif result:
            previous_empty = True
    result = (result or "零") + "元"
    jiao, fen = divmod(fraction, 10)
    if jiao:
        result += digits[jiao] + "角"
    if fen:
        result += ("零" if not jiao else "") + digits[fen] + "分"
    if not fraction:
        result += "整"
    return ("负" if amount < 0 else "") + result


FILTERS = {"money": money, "number": number, "date": date, "default": default, "join": join, "upper_rmb": upper_rmb}


class DataSandbox(SandboxedEnvironment):
    def is_safe_attribute(self, obj, attr, value):
        return False

    def is_safe_callable(self, obj):
        return False

    def getattr(self, obj, attribute):
        if attribute.startswith("_"):
            return self.unsafe_undefined(obj, attribute)
        if isinstance(obj, dict):
            return obj[attribute] if attribute in obj else self.undefined(obj=obj, name=attribute)
        from jinja2.runtime import LoopContext
        if isinstance(obj, LoopContext) and attribute == "index":
            return obj.index
        return self.unsafe_undefined(obj, attribute)

    def getitem(self, obj, argument):
        if isinstance(obj, dict) and isinstance(argument, str) and not argument.startswith("_"):
            return obj[argument] if argument in obj else self.undefined(obj=obj, name=argument)
        return self.undefined(obj=obj, name=argument)


def make_environment():
    env = DataSandbox(undefined=StrictUndefined, autoescape=True, finalize=lambda value: "" if value is None else value)
    env.globals.clear()
    env.filters.clear()
    env.filters.update(FILTERS)
    env.tests.clear()
    return env


from .context import assert_pure_context


def render_template(path, context, out_path, definition=None):
    from .template_validation import validate_template
    assert_pure_context(context)
    diagnostics = validate_template(path, definition=definition, context=context)
    if diagnostics:
        raise TemplateError(diagnostics)
    template = DocxTemplate(str(path))
    template.render(context, jinja_env=make_environment(), autoescape=True)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    template.save(str(out_path))
    # Verify OOXML and literal template leftovers after rendering.
    from lxml import etree
    import zipfile
    with zipfile.ZipFile(out_path) as archive:
        for name in archive.namelist():
            if name.endswith(".xml"):
                xml = archive.read(name)
                etree.fromstring(xml, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    return out_path

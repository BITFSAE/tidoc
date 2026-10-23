"""票面字段的独立校验工具。

- 大写金额（壹佰贰拾陆元整、叁拾圆叁角陆分、负数壹佰元整……）折算成 Decimal；
- 统一社会信用代码校验位（GB 32100-2015）；
- 开票日期是否真实存在；
- 按标签读取「价税合计 / 不含税合计 / 税额合计」及大写金额，作为发票总额的首选来源。

只依赖标准库，不碰数据库和界面。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .models import (
    TOTAL_SOURCE_DERIVED,
    TOTAL_SOURCE_LABEL,
    TOTAL_SOURCE_MAX_AMOUNT,
    TOTAL_SOURCE_PAIR,
)
from .money import MONEY, parse_amount


def compact_text(value) -> str:
    """比较名称、号码时用的标准形态：全角转半角（NFKC），去掉所有空白。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")))


# --------------------------------------------------------------------------- 大写金额

_CN_DIGITS = {
    "零": 0, "〇": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9,
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_SMALL_UNITS = {"拾": 10, "十": 10, "佰": 100, "百": 100, "仟": 1000, "千": 1000}
_CN_AMOUNT_CHARSET = re.compile(r"[零〇壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整正一二两三四五六七八九十百千]+")
# 票面上的大写金额只用这些字；用于在文本里定位大写金额，比上面的容错字符集更严。
CN_AMOUNT_CHARS = "零〇壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整正"


def _parse_below_wan(text: str) -> int | None:
    """解析 0 到 9999 的汉字数，如「壹仟零伍拾叁」得 1053；空串得 0；写法不合规返回 None。"""
    total = 0
    digit: int | None = None
    last_unit = 10_000
    started = False
    for ch in text:
        value = _CN_DIGITS.get(ch)
        if value is not None:
            if value == 0:
                if digit is not None:      # 「壹零」这类写法不合规
                    return None
            else:
                if digit is not None:      # 连续两个非零数字
                    return None
                digit = value
            started = True
            continue
        unit = _CN_SMALL_UNITS.get(ch)
        if unit is None or unit >= last_unit:
            return None
        if digit is None:
            if unit == 10 and not started:  # 「拾伍圆」按 15 读
                digit = 1
            else:
                return None
        total += digit * unit
        digit = None
        last_unit = unit
        started = True
    return total + (digit or 0)


def _parse_below_yi(text: str) -> int | None:
    if not text:
        return 0
    if "万" in text:
        left, right = text.split("万", 1)
        if not left or "万" in right:
            return None
        high, low = _parse_below_wan(left), _parse_below_wan(right)
        if high is None or low is None:
            return None
        return high * 10_000 + low
    return _parse_below_wan(text)


def _parse_cn_integer(text: str) -> int | None:
    if "亿" in text:
        left, right = text.split("亿", 1)
        if not left or "亿" in right:
            return None
        high, low = _parse_below_yi(left), _parse_below_yi(right)
        if high is None or low is None:
            return None
        return high * 100_000_000 + low
    return _parse_below_yi(text)


def _parse_cn_fraction(text: str) -> Decimal | None:
    """解析「叁角陆分」「零伍分」「伍角整」；返回 0 到 0.99。"""
    jiao = fen = None
    digit: int | None = None
    zero_seen = False
    for index, ch in enumerate(text):
        value = _CN_DIGITS.get(ch)
        if value is not None:
            if digit is not None:
                return None
            if value == 0:
                zero_seen = True
            else:
                digit = value
            continue
        if ch in "整正":
            if index != len(text) - 1 or fen is not None or digit is not None:
                return None
            continue
        if ch not in "角分":
            return None
        if digit is None and not zero_seen:
            return None
        amount = digit or 0
        digit, zero_seen = None, False
        if ch == "角":
            if jiao is not None or fen is not None:
                return None
            jiao = amount
        else:
            if fen is not None:
                return None
            fen = amount
    if digit is not None:
        return None
    return (Decimal(jiao or 0) / 10 + Decimal(fen or 0) / 100).quantize(MONEY)


def parse_chinese_amount(text) -> Decimal | None:
    """把票面大写金额折算成 Decimal；无法读懂时返回 None。

    支持：壹佰贰拾陆元整、叁拾圆叁角陆分、壹万零伍佰圆整、叁角伍分、
    前缀「人民币」「⊗」，以及红字发票的「负」「（负数）」前缀。
    """
    if text is None:
        return None
    value = unicodedata.normalize("NFKC", str(text))
    value = re.sub(r"\s+", "", value)
    value = value.replace("⊗", "").replace("人民币", "")
    negative = False
    prefix = re.match(r"^\(?负数?\)?", value)
    if prefix:
        negative = True
        value = value[prefix.end():]
    if not value or not _CN_AMOUNT_CHARSET.fullmatch(value):
        return None
    yuan_match = re.fullmatch(r"(.*?)[圆元](.*)", value)
    if yuan_match:
        integer_text, rest = yuan_match.groups()
    else:
        integer_text, rest = "", value
        if "角" not in rest and "分" not in rest:
            return None
    if not any(ch in _CN_DIGITS or ch in _CN_SMALL_UNITS or ch in "万亿" for ch in integer_text + rest):
        return None
    integer = _parse_cn_integer(integer_text) if integer_text else 0
    if integer is None:
        return None
    if rest in ("", "整", "正"):
        fraction = Decimal("0.00")
    else:
        fraction = _parse_cn_fraction(rest)
        if fraction is None:
            return None
    amount = Decimal(integer) + fraction
    return -amount if negative else amount


def words_match_total(words: Decimal, total: Decimal) -> bool:
    """大写金额是否与小写总额一致。

    大写带「负」时按带符号的值比较；没写负号时按绝对值比较，避免红字发票被误报。
    """
    if words < 0:
        return words == total
    return words == abs(total)


# --------------------------------------------------------------------------- 税号校验位

USCC_CHARSET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
_USCC_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
_ID_CARD_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CARD_CHECK = "10X98765432"


def uscc_check_char(prefix17: str) -> str | None:
    """按 GB 32100-2015 计算前 17 位对应的校验码；含字符集以外的字符时返回 None。"""
    if len(prefix17) != 17 or any(ch not in USCC_CHARSET for ch in prefix17):
        return None
    total = sum(USCC_CHARSET.index(ch) * weight for ch, weight in zip(prefix17, _USCC_WEIGHTS))
    return USCC_CHARSET[(31 - total % 31) % 31]


def is_valid_uscc(code: str) -> bool:
    code = str(code or "").upper()
    return len(code) == 18 and uscc_check_char(code[:17]) == code[17]


def _is_valid_id_card(code: str) -> bool:
    if not re.fullmatch(r"\d{17}[\dX]", code):
        return False
    if not is_valid_iso_date(f"{code[6:10]}-{code[10:12]}-{code[12:14]}") or not 1900 <= int(code[6:10]) <= 2100:
        return False
    total = sum(int(ch) * weight for ch, weight in zip(code[:17], _ID_CARD_WEIGHTS))
    return _ID_CARD_CHECK[total % 11] == code[17]


def tax_id_checksum_ok(value: str) -> bool | None:
    """税号校验位是否通过。

    True 表示通过；False 表示是 18 位疑似统一社会信用代码但校验位或字符不符；
    None 表示不适用：不是 18 位（15 位旧税号、个人抬头等），或是合法的个人身份证号。
    """
    code = re.sub(r"[^0-9A-Z]", "", str(value or "").upper())
    if len(code) != 18:
        return None
    if _is_valid_id_card(code):
        return None
    return is_valid_uscc(code)


# --------------------------------------------------------------------------- 日期

def is_valid_iso_date(value: str) -> bool:
    """YYYY-MM-DD 且是日历上真实存在的日期。"""
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(value or ""))
    if not match:
        return False
    try:
        date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------- 价税合计相关字段

_HS = r"[^\S\r\n]*"
_CUR = r"[¥￥]"
# 与 parser 里原有的金额匹配一致：容忍千分位、PDF 把数字拆开的空格，且不跨行。
_AMOUNT = r"(-?(?:[0-9,，][^\S\r\n]*)+(?:\.[^\S\r\n]*(?:[0-9][^\S\r\n]*){1,2})?)"
_AMOUNT_END = r"(?=\r?\n|[^\S\r\n]*[¥￥]|[^0-9\s.]|$)"

# 价税合计（大写） 叁拾圆叁角陆分 （小写）¥30.36
_LABELED_RE = re.compile(
    r"价税合计" + _HS + r"[（(]" + _HS + r"大写" + _HS + r"[）)]\s*"
    r"(?P<words>[^\r\n¥￥]*?)\s*"
    r"[（(]" + _HS + r"小写" + _HS + r"[）)]" + _HS + r"[:：]?" + _HS
    + _CUR + _HS + _AMOUNT + _AMOUNT_END
)
# 价税合计（小写） ¥30.36
_SMALL_ONLY_RE = re.compile(
    r"价税合计" + _HS + r"[（(]" + _HS + r"小写" + _HS + r"[）)]" + _HS + r"[:：]?" + _HS
    + _CUR + _HS + _AMOUNT + _AMOUNT_END
)
# 叁拾圆叁角陆分 ¥30.36  —— 部分 PDF 把标签和取值拆开，只剩「大写 + 小写」同行
_WORDS_RE = r"(?P<words>(?:[（(]?负数?[）)]?)?⊗?[" + CN_AMOUNT_CHARS + r"]{2,})"
_PAIR_RE = re.compile(
    _WORDS_RE + _HS + r"(?:[（(]" + _HS + r"小写" + _HS + r"[）)])?" + _HS + _CUR + _HS + _AMOUNT + _AMOUNT_END
)
_WORDS_LINE_RE = re.compile(r"^" + _HS + _WORDS_RE + _HS + r"$", re.M)
# 合 计  ¥100.00  ¥13.00
_ROW_RE = re.compile(
    r"(?<!税)合" + _HS + r"计" + _HS + _CUR + _HS + _AMOUNT + _HS + _CUR + _HS + _AMOUNT + _AMOUNT_END
)
_TWO_AMOUNT_LINE_RE = re.compile(
    r"^" + _HS + _CUR + _HS + _AMOUNT + _HS + _CUR + _HS + _AMOUNT + _HS + r"$"
)
_ALL_AMOUNTS_RE = re.compile(
    r"[¥￥][^\S\r\n]*"
    r"(-?(?:[0-9,，][^\S\r\n]*)+"
    r"(?:\.[^\S\r\n]*(?:[0-9][^\S\r\n]*){1,2})?)"
    r"(?=\r?\n|[^\S\r\n]*[¥￥]|[^0-9\s.]|$)"
)
_RATE_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)[^\S\r\n]*%")
_EXEMPT_RE = re.compile(r"免税|不征税|免征|零税率|\*\*\*")


@dataclass
class TotalFields:
    """从一份文本里按标签读到的总额相关字段。读不到的字段为 None。"""

    total: Decimal | None = None
    total_how: str = ""            # TOTAL_SOURCE_LABEL / TOTAL_SOURCE_PAIR
    words_text: str = ""
    words: Decimal | None = None
    subtotal: Decimal | None = None
    tax_total: Decimal | None = None
    rows_how: str = ""             # "label"（合计标签）/ "row"（紧邻大写金额行的两个金额）
    amounts: list = field(default_factory=list)   # 页面上所有 ¥ 金额，仅供兜底和印证
    rates: list = field(default_factory=list)     # 页面上出现的税率（百分数）；0 表示免税 / 不征税


def _amount(text: str) -> Decimal | None:
    return parse_amount(re.sub(r"\s", "", text))


def _has_numeral(words: str) -> bool:
    return any(ch in _CN_DIGITS or ch in "拾佰仟万亿角分" for ch in words)


def read_total_fields(text: str) -> TotalFields:
    """读取价税合计（小写 / 大写）、不含税合计与税额合计。"""
    text = text or ""
    found = TotalFields()
    found.amounts = [value for value in (_amount(raw) for raw in _ALL_AMOUNTS_RE.findall(text)) if value is not None]

    pair_span: tuple[int, int] | None = None
    for match in _LABELED_RE.finditer(text):
        amount = _amount(match.group(2))
        if amount is None:
            continue
        found.total, found.total_how = amount, TOTAL_SOURCE_LABEL
        words = match.group("words").strip()
        if words and _has_numeral(words):
            found.words_text = words
            found.words = parse_chinese_amount(words)
        pair_span = match.span()
        break
    if found.total is None:
        for match in _SMALL_ONLY_RE.finditer(text):
            amount = _amount(match.group(1))
            if amount is not None:
                found.total, found.total_how = amount, TOTAL_SOURCE_LABEL
                break
    if found.total is None or not found.words_text:
        for match in _PAIR_RE.finditer(text):
            words = match.group("words")
            value = parse_chinese_amount(words)
            amount = _amount(match.group(2))
            if value is None or amount is None or not re.search(r"[圆元角分]", words):
                continue
            if found.total is None:
                found.total, found.total_how = amount, TOTAL_SOURCE_PAIR
            if not found.words_text:
                found.words_text, found.words = words, value
            pair_span = pair_span or match.span()
            break
    if not found.words_text:
        for match in _WORDS_LINE_RE.finditer(text):
            words = match.group("words")
            value = parse_chinese_amount(words)
            if value is not None and re.search(r"[圆元角分]", words):
                found.words_text, found.words = words, value
                break

    # 不含税合计、税额合计：优先「合计」标签；否则取紧挨在大写金额行前面的两个金额。
    row = _ROW_RE.search(text)
    if row:
        found.subtotal, found.tax_total = _amount(row.group(1)), _amount(row.group(2))
        found.rows_how = "label"
    elif pair_span is not None:
        lines_before = text[:pair_span[0]].rstrip().splitlines()
        if lines_before:
            two = _TWO_AMOUNT_LINE_RE.match(lines_before[-1])
            if two:
                found.subtotal, found.tax_total = _amount(two.group(1)), _amount(two.group(2))
                found.rows_how = "row"
    if found.subtotal is None or found.tax_total is None:
        found.subtotal = found.tax_total = None
        found.rows_how = ""

    rates: list[Decimal] = []
    for raw in _RATE_RE.findall(text):
        rate = parse_amount(raw)
        if rate is not None and 0 <= rate <= 100 and rate not in rates:
            rates.append(rate)
    if _EXEMPT_RE.search(text) and Decimal("0") not in rates:
        rates.append(Decimal("0"))
    found.rates = sorted(rates)
    return found


def select_total(found: TotalFields) -> tuple[Decimal | None, str]:
    """按可信度从高到低选出总额，返回 (金额, 来源)；一个都读不到时返回 (None, "")。

    顺序：小写标签 / 大写+小写同行 > 大写金额或合计行与页面金额互相印证 > 取页面最大金额（低可信）。
    """
    if found.total is not None:
        return found.total, found.total_how
    closure = None
    if found.subtotal is not None and found.tax_total is not None:
        closure = found.subtotal + found.tax_total
    if found.words is not None and (found.words == closure or found.words in found.amounts):
        return found.words, TOTAL_SOURCE_DERIVED
    if closure is not None and closure in found.amounts:
        return closure, TOTAL_SOURCE_DERIVED
    if found.amounts:
        return max(found.amounts, key=abs), TOTAL_SOURCE_MAX_AMOUNT
    return None, ""

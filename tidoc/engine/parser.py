"""发票解析：优先 XML，其次 PDF 文本。移植并精简自 invoice2docx/engine.py。

对外只暴露两个入口：
- parse_xml(path)  解析电子发票 XML
- parse_pdf(path)  解析发票 PDF 文本
以及 parse_invoice_files(...) —— 给一组附件路径，自动选最优来源合并。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader

from .models import ParsedInvoice, ParsedItem
from .money import d, money


def clean_item_name(name: str) -> str:
    """去掉发票物资名称里的 *分类* 星号段。"""
    return re.sub(r"\*[^*]+\*", "", name).strip()


def _child_text(elem: ET.Element, tag: str) -> str:
    found = elem.find(".//" + tag)
    return found.text.strip() if found is not None and found.text else ""


# --------------------------------------------------------------------------- XML

def parse_xml(path: str | Path) -> ParsedInvoice:
    path = Path(path)
    root = ET.parse(path).getroot()

    invoice = ParsedInvoice(
        invoice_no=_child_text(root, "EIid"),
        invoice_date=_child_text(root, "IssueTime") or _child_text(root, "RequestTime")[:10],
        seller=_child_text(root, "SellerName"),
        buyer_name=_child_text(root, "BuyerName"),
        buyer_tax_id=_child_text(root, "BuyerIdNum"),
        total=d(_child_text(root, "TotalTax-includedAmount")),
        source="xml",
    )

    parents = [elem for elem in root.iter() if elem.find("ItemName") is not None]
    last: ParsedItem | None = None
    for elem in parents:
        raw_name = _child_text(elem, "ItemName")
        spec = _child_text(elem, "SpecMod")
        unit = _child_text(elem, "MeaUnits")
        quantity_text = _child_text(elem, "Quantity")
        line_total = d(_child_text(elem, "Amount")) + d(_child_text(elem, "ComTaxAm"))

        # 跨行拆分的同名条目：金额并入上一条
        if last and not quantity_text and raw_name == last.name:
            last.total += line_total
            continue

        quantity = d(quantity_text) if quantity_text else None
        item = ParsedItem(
            name=raw_name,
            actual_name=clean_item_name(raw_name),
            unit=unit,
            quantity=quantity,
            total=line_total,
            spec=spec,
        )
        invoice.items.append(item)
        last = item

    return invoice


# --------------------------------------------------------------------------- PDF


def _pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _pdf_layout_text(path: Path) -> str:
    """Extract a second text view that preserves item-column indentation."""
    reader = PdfReader(str(path))
    return "\n".join(
        (page.extract_text(extraction_mode="layout") or "")
        for page in reader.pages
    )


# 数电普通发票里购买方/销售方信息块通常以「名称:」开头，紧随其后是抬头与税号；
# 一张发票有两个这样的块：第一个是销售方，第二个是购买方（顺序随版面可变）。
# 这里用「名称:」标签 + 税号正则做到版面无关的稳定抽取。
_TAX_ID_RE = re.compile(r"[0-9A-Z]{15,20}")
# 统一社会信用代码固定 18 位；发票号码(EIid) 为 20 位数字，靠长度区分。
_USCC_RE = re.compile(r"[0-9A-Z]{18,}")
_DIGIT_RUN_RE = re.compile(r"(?<!\d)(?:\d[^\S\r\n]*){20,}")


def _compact_digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _extract_invoice_no(text: str) -> str:
    """Extract invoice numbers even when PDF text separates every digit.

    Several tax-platform PDFs keep the invoice number visually intact but expose
    it to pypdf as ``2 6 4 4 ...``.  Restrict the fallback to exactly twenty
    digits so ordinary dates, tax ids, and bank account numbers are not chosen.
    """
    labeled = re.search(
        r"发票号码[:：]?[^\d\r\n]*((?:\d[^\S\r\n]*){20,})",
        text or "",
    )
    if labeled:
        value = _compact_digits(labeled.group(1))
        if len(value) == 20:
            return value
    match = re.search(r"(?<!\d)\d{20}(?!\d)", text or "")
    if match:
        return match.group(0)
    for spaced in _DIGIT_RUN_RE.finditer(text or ""):
        value = _compact_digits(spaced.group(0))
        if len(value) == 20:
            return value
    return ""


def _extract_invoice_date(text: str) -> str:
    """Read dates from both contiguous and character-spaced PDF text."""
    match = re.search(
        r"((?:\d[^\S\r\n]*){4})[^\S\r\n]*年[^\S\r\n]*"
        r"((?:\d[^\S\r\n]*){1,2})[^\S\r\n]*月[^\S\r\n]*"
        r"((?:\d[^\S\r\n]*){1,2})[^\S\r\n]*日",
        text or "",
    )
    if not match:
        return ""
    year = _compact_digits(match.group(1))
    month = _compact_digits(match.group(2))
    day = _compact_digits(match.group(3))
    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"


def _normalize_party_name(value: str) -> str:
    return re.sub(r"\s+", "", value or "").strip()


def _extract_layout_parties(text: str) -> tuple[str, str, str, str] | None:
    """Read the side-by-side buyer/seller block from layout-preserving text.

    The three supplied invoices expose the left buyer and right seller in one
    text row.  The ordinary text stream loses those roles and reverses or even
    picks up the vertical tax-stamp text, while the layout stream still carries
    the ``购 ... 名称`` / ``销 ... 名称`` anchors.
    """
    lines = [line for line in (text or "").splitlines() if line.strip()]
    buyer = seller = ""
    for line in lines:
        if "名称" not in line or "购" not in line or "销" not in line:
            continue
        direct = re.search(
            r"购\s+名称[:：]\s*(.*?)\s+销\s+名称[:：]\s*(.*?)\s*$",
            line,
        )
        if direct:
            buyer = _normalize_party_name(direct.group(1))
            seller = _normalize_party_name(direct.group(2))
            if buyer and seller:
                break
        # Some PDFs emit the seller name before the right-column ``销 名称``
        # label.  A large layout gap separates it from the buyer name.
        split = re.search(
            r"购\s+名称[:：]\s*(.*?)\s{3,}(.*?)销\s+名称",
            line,
        )
        if split:
            buyer = _normalize_party_name(split.group(1))
            seller = _normalize_party_name(split.group(2))
            if buyer and seller:
                break

    if not buyer or not seller:
        return None
    buyer_tax_id = seller_tax_id = ""
    tax_label = re.compile(
        r"统一\s*社会\s*信用\s*代码\s*/\s*纳税人\s*识别号\s*[:：]"
    )
    roles_located = False
    for line in lines:
        labels = list(tax_label.finditer(line))
        if len(labels) < 2:
            continue
        # Layout text preserves the left buyer and right seller columns.  Read
        # each value from its own column so an empty buyer cell cannot consume
        # the seller's tax id as the first global candidate.
        values: list[str] = []
        for index, label in enumerate(labels[:2]):
            end = labels[index + 1].start() if index + 1 < len(labels) else len(line)
            candidate = _TAX_ID_RE.search(line[label.end():end])
            values.append(candidate.group(0) if candidate else "")
        buyer_tax_id, seller_tax_id = values
        roles_located = True
        break
    if not roles_located:
        tax_ids = _collect_tax_ids(lines)
        buyer_tax_id = tax_ids[0] if len(tax_ids) >= 1 else ""
        seller_tax_id = tax_ids[1] if len(tax_ids) >= 2 else ""
    return seller, seller_tax_id, buyer, buyer_tax_id


def _collect_tax_ids(lines: list[str]) -> list[str]:
    """按出现顺序收集 18 位统一社会信用代码候选。

    处理两种版面：
    - 销售方 / 购买方两码连写在同一行（36 位 = 两个 18 位码），按 18 位切分。
    - 两码分行出现。
    发票号码(EIid, 20 位数字)、订单号等因长度非 18 的倍数被自动排除。
    """
    out: list[str] = []
    for line in lines:
        for run in _USCC_RE.findall(line):
            L = len(run)
            if L == 18:
                out.append(run)
            elif L % 18 == 0 and L // 18 <= 2:  # 至多销售方+购买方两码连写
                for i in range(0, L, 18):
                    out.append(run[i:i + 18])
            # 其它长度（如 20 位发票号）忽略
    return out


def _normalize_party_lines(lines: list[str]) -> list[str]:
    """把竖排单字（销/售/方/信/息/购/买/备/注，每个单字独占一行）合并成一行的预处理。

    pypdf / Aspose 提取的发票 PDF 常把竖排标题拆成 N 个单字行，干扰后续识别。
    """
    out: list[str] = []
    buf = ""
    for line in lines:
        if len(line) == 1 and "\u4e00" <= line <= "\u9fff":
            buf += line
            continue
        if buf:
            out.append(buf); buf = ""
        out.append(line)
    if buf:
        out.append(buf)
    return out


def _normalize_label(line: str) -> str:
    return re.sub(r"\s+", "", line).replace("：", ":")


def _looks_like_invoice_meta(line: str) -> bool:
    norm = _normalize_label(line)
    if not norm:
        return True
    # Some invoice generators place all field labels first and the actual values
    # later in the text stream.  While waiting for the two party names, do not
    # mistake an unrelated labelled value such as ``下载次数：1`` or
    # ``校验码：...`` for a company/person name.
    if re.match(r"^[\u4e00-\u9fffA-Za-z/（）()]{1,16}:", norm):
        return True
    if re.fullmatch(r"\d{20}", norm):
        return True
    if re.fullmatch(r"\d{4}年\d{2}月\d{2}日", norm):
        return True
    if _TAX_ID_RE.fullmatch(norm):
        return True
    return any(k in norm for k in (
        "发票", "开票日期", "项目名称", "统一社会信用", "纳税人识别号",
        "购买方信息", "买方信息", "销售方信息", "名称:", "合计", "备注", "开票人",
    ))


def _split_combined_party_names(line: str) -> list[str]:
    """拆分同一行里连写的销售方/购买方名称。"""
    from .validator import SUPPORTED_TITLES

    if line in SUPPORTED_TITLES:
        return [line]
    for title in sorted(SUPPORTED_TITLES, key=len, reverse=True):
        if title and title in line and line != title:
            before, after = line.split(title, 1)
            parts: list[str] = []
            if before.strip():
                parts.append(before.strip())
            parts.append(title)
            if after.strip():
                parts.append(after.strip())
            return parts
    return [line]


def _extract_explicit_role_parties(lines: list[str]) -> tuple[str, str, str, str] | None:
    """Read party names when each ``名称`` is explicitly scoped by a role heading.

    Some invoice text streams keep a clean ``购买方信息 -> 名称`` / ``销售方信息 -> 名称``
    order even though neither party is one of Tidoc's supported titles.  Preserve those
    roles instead of falling back to the generic two-name ordering heuristic.
    """
    current_role = ""
    parties = {
        "buyer": ["", ""],
        "seller": ["", ""],
    }
    for line in lines:
        norm = _normalize_label(line)
        if norm in {"购买方信息", "买方信息"}:
            current_role = "buyer"
            continue
        if norm == "销售方信息":
            current_role = "seller"
            continue
        if not current_role:
            continue

        name_match = re.match(r"名\s*称\s*[:：]\s*(.*)", line)
        if name_match:
            tail = name_match.group(1).strip()
            if not tail or _looks_like_invoice_meta(tail):
                continue
            tax_match = _TAX_ID_RE.search(tail)
            if tax_match:
                parties[current_role] = [tail[:tax_match.start()].strip(), tax_match.group(0)]
            else:
                parties[current_role][0] = tail
            continue

        if "统一社会信用" in norm or "纳税人识别号" in norm:
            tax_match = _TAX_ID_RE.search(norm)
            if tax_match:
                parties[current_role][1] = tax_match.group(0)

    buyer_name, buyer_tax_id = parties["buyer"]
    seller_name, seller_tax_id = parties["seller"]
    if buyer_name and seller_name:
        return seller_name, seller_tax_id, buyer_name, buyer_tax_id
    return None


def _extract_parties(lines: list[str]) -> tuple[str, str, str, str]:
    """从文本行里抽销售方与购买方：名称 + 税号。

    兼容三种版面：
    1) ``名称:抬头名税号`` 同行连写 —— 本行抓到抬头名 + 末尾税号。
    2) ``名称:抬头名`` 同行连写，税号隔几行在「统一社会信用代码: XXX」行末尾。
    3) ``名称:`` 单独成行、抬头名与税号都在下方几行 —— 数电普通发票的纵向标题块。
       此时连续两个 ``名称:`` 标签下方对应两个抬头/税号依次排列，例如：
           名称:
           名称:
           购买方抬头          （← 给「名称:」#0）
           销售方抬头          （← 给「名称:」#1）
           统一社会信用代码: 购买方税号
           统一社会信用代码: 销售方税号
    """
    explicit = _extract_explicit_role_parties(lines)
    if explicit:
        return explicit

    parties: list[tuple[str, str]] = []  # (name, tax_id)
    pending = 0          # 仍在等待抬头配对的「名称:」数量
    for line in lines:
        m = re.match(r"名\s*称\s*[:：]\s*(.*)", line)
        if m:
            tail_text = m.group(1).strip()
            label_count = len(re.findall(r"名\s*称\s*[:：]", line))
            if label_count > 1:
                pending += label_count
                continue
            if (not tail_text) or _looks_like_invoice_meta(tail_text) or tail_text in {"购", "销", "买", "售"}:
                pending += 1
                continue
            tail_tax = _TAX_ID_RE.search(tail_text)
            if tail_tax:
                parties.append((tail_text[:tail_tax.start()].strip(), tail_tax.group(0)))
                continue
            parties.append((tail_text, ""))
            continue
        if pending > 0:
            # 抬头名候选：跳过空行、税号行（"统一社会信用..."）、竖排单字残留
            if _looks_like_invoice_meta(line):
                continue
            for name in _split_combined_party_names(line):
                if pending <= 0:
                    break
                if _looks_like_invoice_meta(name):
                    continue
                m2 = _TAX_ID_RE.search(name)
                if m2:
                    parties.append((name[:m2.start()].strip(), m2.group(0)))
                else:
                    parties.append((name, ""))
                pending -= 1
                continue

    # 第二轮：补税号。数电发票里税号可能与「名称/代码」不同行（常与另一方的税号
    # 连写在同一行，或单独成行），故全局按出现顺序收集 18 位统一社会信用代码，
    # 再按顺序补给尚缺税号的抬头（名称与税号在同一版面里顺序一致）。
    tax_ids = _collect_tax_ids(lines)
    ti = 0
    for i, (nm, tid) in enumerate(parties):
        if not tid and ti < len(tax_ids):
            parties[i] = (nm, tax_ids[ti])
            ti += 1
        elif tid:
            ti += 1

    seller_name = seller_tax_id = buyer_name = buyer_tax_id = ""
    if len(parties) >= 2:
        # 数电普通发票的两个抬头通常是「销售方」+「购买方」。版面排版有时销售方在前、
        # 有时购买方在前；用 SUPPORTED_TITLES 直接识别本校抬头作为购买方，更稳。
        first, second = parties[0], parties[1]
        from .validator import SUPPORTED_TITLES
        if any(t and t in first[0] for t in SUPPORTED_TITLES):
            buyer_name, buyer_tax_id = first
            seller_name, seller_tax_id = second
        elif any(t and t in second[0] for t in SUPPORTED_TITLES):
            buyer_name, buyer_tax_id = second
            seller_name, seller_tax_id = first
        else:
            # 都不是已知购买方抬头——按出现顺序保留原默认（销售在前）
            seller_name, seller_tax_id = first
            buyer_name, buyer_tax_id = second
    elif len(parties) == 1:
        from .validator import SUPPORTED_TITLES
        only_name, only_tax = parties[0]
        if any(t and t in only_name for t in SUPPORTED_TITLES):
            buyer_name, buyer_tax_id = only_name, only_tax
        else:
            seller_name, seller_tax_id = only_name, only_tax
    return seller_name, seller_tax_id, buyer_name, buyer_tax_id


def _extract_pdf_buyer(lines: list[str]) -> tuple[str, str]:
    # 旧路径兼容：只返回购买方
    _, _, buyer_name, buyer_tax_id = _extract_parties(lines)
    if buyer_name:
        return buyer_name, buyer_tax_id
    # 兜底：用日期行的下一两行
    tax_id_pattern = _TAX_ID_RE
    for idx, line in enumerate(lines):
        if re.fullmatch(r"\d{4}年\d{2}月\d{2}日", line) and idx + 2 < len(lines):
            maybe_name = lines[idx + 1].strip()
            maybe_tax_id = lines[idx + 2].strip()
            if maybe_name and tax_id_pattern.fullmatch(maybe_tax_id):
                return maybe_name, maybe_tax_id
    return "", ""


def _extract_pdf_seller(lines: list[str], buyer_tax_id: str, buyer_name: str) -> str:
    seller_name, *_ = _extract_parties(lines)
    if seller_name:
        return seller_name
    # 旧路径兜底
    if not buyer_name or not buyer_tax_id:
        return ""
    for idx, line in enumerate(lines):
        if buyer_name in line and not line.startswith("*"):
            seller_name = line.replace(buyer_name, "", 1).strip()
            tax_line = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
            if tax_line.endswith(buyer_tax_id):
                return seller_name
    for idx, line in enumerate(lines):
        if buyer_tax_id and line == buyer_tax_id and idx + 1 < len(lines):
            return lines[idx + 1]
    return ""


def _parse_amount_tax_line(line: str) -> tuple[str, Decimal | None, Decimal, Decimal, int] | None:
    """从一行里抽出 (单位, 数量, 金额(不含税), 税额, 名称末尾位置)。

    数电普通发票明细行的常见版式（按列从左到右）：
        *分类*名称  规格型号  单位  数量  单价  金额  税率%  税额
    名称、规格都可能是多段；下面用「税率% 税额」锚定行末，
    再向前倒推各列，对版面不齐的兼容性更好。
    """
    # 1) 完整版式：行首带星号分类，到行末固定 7 列尾巴
    m = re.search(
        r"(\*.+?)\s+(\S+)\s+(\S+)\s+(-?\d+(?:\.\d+)?)\s+"
        r"(-?\d+(?:\.\d+)?)\s+(-?\d+\.\d{2})\s+"
        r"(-?\d+(?:\.\d+)?)%\s+(-?\d+(?:\.\d+)?)\s*$",
        line,
    )
    if m:
        raw_name = m.group(1)
        unit = m.group(3)
        quantity = d(m.group(4)) if m.group(4) else None
        amount_wo_tax = d(m.group(6))
        tax_amount = d(m.group(8))
        return unit, quantity, amount_wo_tax, tax_amount, m.start(1) + len(raw_name)

    # 2) 折行版式：行首带星号分类，尾部只带「金额 税率% 税额」3 段（同名折行修正）
    m = re.search(
        r"(\*.+?)\s+(?:\S+\s+)*?(-?\d+\.\d{2})\s+"
        r"(-?\d+(?:\.\d+)?)%\s+(-?\d+(?:\.\d+)?)\s*$",
        line,
    )
    if m:
        raw_name = m.group(1)
        return "", None, d(m.group(2)), d(m.group(4)), m.start(1) + len(raw_name)

    return None


def _joined_quantity(joined: str, amount: Decimal) -> Decimal | None:
    """Split an integer quantity from a unit price concatenated by PDF text."""
    dot_index = joined.find(".")
    for split_at in range(1, dot_index):
        quantity = d(joined[:split_at])
        unit_price = d(joined[split_at:])
        if quantity > 0 and money(quantity * unit_price) == money(amount):
            return quantity
    return None


def _packed_quantity_amount(value: str) -> tuple[Decimal, Decimal] | None:
    """Split ``quantity + unit price + amount`` using the amount closure."""
    last_dot = value.rfind(".")
    if last_dot < 0 or len(value) - last_dot != 3:
        return None
    integer_tail = re.search(r"\d+$", value[:last_dot])
    if not integer_tail:
        return None
    for integer_digits in range(1, len(integer_tail.group(0)) + 1):
        amount_start = last_dot - integer_digits
        amount = d(value[amount_start:])
        quantity = _joined_quantity(value[:amount_start], amount)
        if quantity is not None:
            return quantity, amount
    return None


def _parse_loose_amount_tax_line(line: str) -> tuple[str, Decimal | None, Decimal, Decimal] | None:
    """解析被 PDF 文本流拆乱的金额行。

    常见坏形态：
    - ``13%台 113.82 14.80113.821``（税率、单位、金额、税额粘连）
    - ``... 个 1 94.69 94.69 12.3113%``（税额和税率反向粘连）
    """
    # A few issuers insert spaces around decimal points in the PDF text stream
    # (``23. 01`` / ``2. 99``), although the rendered invoice is normal.
    compact = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", line)
    compact = re.sub(r"\s+", " ", compact).strip()

    # Layout extraction can concatenate quantity, unit price, and amount into
    # one numeric token, e.g. ``168.316831683168368.32 1% 0.68``.
    packed = re.search(
        r"(?P<unit>[\u4e00-\u9fffA-Za-z]{1,4})\s+"
        r"(?P<packed>-?\d+(?:\.\d+){2,})\s+"
        r"(?P<rate>\d+(?:\.\d+)?)%\s+"
        r"(?P<tax>-?\d+(?:\.\d+)?)\s*$",
        compact,
    )
    if packed:
        quantity_amount = _packed_quantity_amount(packed.group("packed"))
        if quantity_amount:
            quantity, amount = quantity_amount
            return (
                packed.group("unit"),
                quantity,
                amount,
                d(packed.group("tax")),
            )

    # Quantity and unit price can also be concatenated while the line amount
    # remains separate: ``公斤 115.5663716814159 15.57 13% 2.02``.
    joined_price = re.search(
        r"(?P<unit>[\u4e00-\u9fffA-Za-z]{1,4})\s+"
        r"(?P<joined>\d\d+\.\d+)\s+"
        r"(?P<amount>-?\d+\.\d{2})\s+"
        r"(?P<rate>\d+(?:\.\d+)?)%\s+"
        r"(?P<tax>-?\d+(?:\.\d+)?)\s*$",
        compact,
    )
    if joined_price:
        amount = d(joined_price.group("amount"))
        quantity = _joined_quantity(joined_price.group("joined"), amount)
        if quantity is not None:
            return (
                joined_price.group("unit"),
                quantity,
                amount,
                d(joined_price.group("tax")),
            )

    # Wrapped item name/spec on previous lines, with all numeric columns on this line:
    # ``CM639 件 1 106.74 106.74 13% 13.88``.
    m = re.search(
        r"(?P<unit>[\u4e00-\u9fffA-Za-z]{1,4})\s+"
        r"(?P<quantity>-?\d+(?:\.\d+)?)\s+"
        r"(?P<unit_price>-?\d+(?:\.\d+)?)\s+"
        r"(?P<amount>-?\d+\.\d{2})\s+"
        r"(?P<rate>\d+(?:\.\d+)?)%\s+"
        r"(?P<tax>-?\d+(?:\.\d+)?)\s*$",
        compact,
    )
    if m:
        return m.group("unit"), d(m.group("quantity")), d(m.group("amount")), d(m.group("tax"))

    m = re.search(
        r"(?P<rate>\d+(?:\.\d+)?)%(?P<unit>[\u4e00-\u9fffA-Za-z]{1,4})\s+"
        r"(?P<amount>-?\d+\.\d{2})\s+(?P<tail>-?\d+\.\d{2})",
        compact,
    )
    if m:
        return m.group("unit"), None, d(m.group("amount")), d(m.group("tail"))

    m = re.search(r"(?P<tax>-?\d+\.\d{2})(?P<rate>\d+(?:\.\d+)?)%\s*$", compact)
    if m:
        before = compact[:m.start()]
        nums = re.findall(r"-?\d+(?:\.\d+)?", before)
        amount = d(nums[-1]) if nums else Decimal("0")
        quantity = d(nums[-3]) if len(nums) >= 3 else None
        unit_match = re.search(r"([\u4e00-\u9fffA-Za-z]{1,4})\s+-?\d+(?:\.\d+)?\s+-?\d+\.\d{2}\s*$", before)
        unit = unit_match.group(1) if unit_match else ""
        return unit, quantity, amount, d(m.group("tax"))

    return None


def _normalize_name(parts: list[str]) -> str:
    return re.sub(r"\s+", "", "".join(parts)).strip()


def _strip_overlaid_item_headers(line: str) -> str:
    """Remove table headers that some PDFs interleave with the first item row.

    A few invoice generators place the column labels and their values in separate
    PDF text layers.  ``pypdf`` then exposes a visual row such as::

        *分类*结构胶项目名称 规格型号 单 位支 数 量165.04... 金 额65.05 ...

    The values remain usable after removing the labels.  Restrict this cleanup to
    starred item rows containing ``项目名称`` so ordinary product names are left
    untouched.
    """
    if not line.lstrip().startswith("*") or "项目名称" not in line:
        return line
    cleaned = line
    for label in (
        r"项目\s*名称",
        r"规格\s*型号",
        r"单\s*位",
        r"数\s*量",
        r"单\s*价",
        r"金\s*额",
        r"税率\s*/\s*征收率",
        r"税\s*额",
    ):
        cleaned = re.sub(label, " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


_PDF_UNIT_SUFFIXES = (
    "公斤", "千克", "平方米", "立方米",
    "个", "件", "只", "台", "套", "张", "片", "盒", "包", "把", "次",
    "支", "瓶", "卷", "米", "组", "枚", "块", "本", "辆", "条", "根",
    "批", "箱", "桶", "对", "份",
)


def _normalize_pdf_unit(unit: str) -> str:
    """Remove a trailing model letter that the layout text fused into a Chinese unit."""
    value = str(unit or "").strip()
    for suffix in _PDF_UNIT_SUFFIXES:
        if value.endswith(suffix) and re.fullmatch(rf"[A-Za-z]+{re.escape(suffix)}", value):
            return suffix
    return value


def _parse_pdf_items(lines: list[str], *, layout: bool = False) -> list[ParsedItem]:
    """从 PDF 文本行里抽物品明细。

    每条明细一行首部带 ``*分类*名称``，遇到统计行（合计 / 价税合计 / 收款人...）终止。
    ``layout=True`` 时保留 pypdf 版面文本的前导空格：首列续行补到商品名，
    缩进到规格列的续行不会误拼进商品名。
    """
    _SKIP_PREFIXES = (
        "合计", "价税合计", "购买时间", "收款人", "复核人", "开票人", "备注",
        "名称:", "统一社会信用", "电子发票", "小计", "项目名称",
    )

    items: list[ParsedItem] = []
    last: ParsedItem | None = None
    last_base_name = ""
    pending_name_parts: list[str] = []
    allow_layout_suffix = False
    layout_blank_lines = 0

    def layout_name_part(raw_line: str) -> str:
        stripped = raw_line.strip()
        if not layout:
            return stripped
        return re.split(r"\s{2,}", stripped, maxsplit=1)[0].strip()

    def append_layout_name_suffix(raw_line: str) -> bool:
        if not layout or last is None:
            return False
        indent = len(raw_line) - len(raw_line.lstrip())
        if indent > 2:
            return False  # 规格型号列续行，不属于商品名
        suffix = layout_name_part(raw_line)
        if (
            not suffix
            or suffix in {"购", "买", "方", "信", "息", "销", "售", "备", "注"}
            or re.fullmatch(r"[\W_]+|\d+", suffix)
        ):
            return False
        if not last.actual_name.endswith(suffix):
            # A specification column can sit after a short first-line product
            # fragment. Put the continuation before that final token so
            # ``...核 不含线`` + ``心板`` becomes ``...核心板 不含线``.
            raw_head, raw_tail = (
                last.name.rsplit(" ", 1) if " " in last.name else (last.name, "")
            )
            if (
                raw_tail
                and "*" in raw_head
                and last.name.rfind(" ") > last.name.rfind("*")
            ):
                last.name = f"{raw_head}{suffix} {raw_tail}"
                last.actual_name = clean_item_name(last.name)
            else:
                last.name += suffix
                last.actual_name += suffix
        return True

    def make_item(name: str, unit: str, quantity, amount, tax):
        actual_name = clean_item_name(name)
        normalized_unit = _normalize_pdf_unit(unit)
        if (
            normalized_unit
            and normalized_unit not in _PDF_UNIT_SUFFIXES
            and actual_name.endswith(normalized_unit)
        ):
            # On rows without a unit, the loose tail matcher can capture the
            # final words of the item name (for example ``价外费用``) as a unit.
            normalized_unit = ""
        item = ParsedItem(
            name=name,
            actual_name=actual_name,
            unit=normalized_unit,
            quantity=(
                quantity
                if quantity is not None
                else Decimal("1") if normalized_unit else None
            ),
            total=money(amount + tax),
        )
        return item

    def is_item_boundary(line: str) -> bool:
        compact = _normalize_label(line)
        return any(compact.startswith(prefix) for prefix in _SKIP_PREFIXES)

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if allow_layout_suffix:
                layout_blank_lines += 1
                if layout_blank_lines > 1:
                    allow_layout_suffix = False
            continue
        layout_blank_lines = 0
        if is_item_boundary(line):
            allow_layout_suffix = False
            continue
        if pending_name_parts:
            loose = _parse_loose_amount_tax_line(line)
            if loose:
                unit, quantity, amount, tax = loose
                raw_name = _normalize_name(pending_name_parts)
                item = make_item(raw_name, unit, quantity, amount, tax)
                items.append(item)
                last = item
                last_base_name = item.actual_name
                pending_name_parts = []
                allow_layout_suffix = layout
                continue
            if not re.search(r"\d+\.\d{2}|\d+%", line):
                part = layout_name_part(raw_line)
                if not layout or len(raw_line) - len(raw_line.lstrip()) <= 2:
                    pending_name_parts.append(part)
                continue

        if not line.startswith("*"):
            if allow_layout_suffix:
                allow_layout_suffix = append_layout_name_suffix(raw_line)
            continue
        item_line = _strip_overlaid_item_headers(line) if layout else line
        has_overlaid_headers = item_line != line
        # 部分发票把折扣行按“同品名 税率 折扣金额 折扣税额”输出，
        # 例如 ``*衡器*电子秤 13%-0.88 -0.12``。
        discount = re.fullmatch(
            r"(?P<name>\*.+?)\s+(?P<rate>\d+(?:\.\d+)?)%"
            r"\s*(?P<amount>-\d+\.\d{2})\s+(?P<tax>-\d+\.\d{2})",
            item_line,
        )
        if discount and last and clean_item_name(discount.group("name")) == last_base_name:
            last.total = money(
                last.total + d(discount.group("amount")) + d(discount.group("tax"))
            )
            allow_layout_suffix = False
            continue
        parsed = _parse_amount_tax_line(item_line)
        if parsed and (not parsed[0] or parsed[1] is None):
            # The generic folded-line regex can match the tail but lose the
            # unit/quantity. Prefer the amount-closure parser when it can recover them.
            loose = _parse_loose_amount_tax_line(item_line)
            if loose:
                unit, quantity, amount, tax = loose
                unit_marker = re.search(
                    rf"\s+{re.escape(unit)}\s*(?=-?\d)", item_line
                ) if unit else None
                if has_overlaid_headers and unit_marker:
                    raw_name = item_line[:unit_marker.start()].strip()
                elif layout:
                    raw_name = layout_name_part(raw_line)
                else:
                    raw_name = item_line[:unit_marker.start()].strip() if unit_marker else item_line[:parsed[4]].strip()
                item = make_item(raw_name, unit, quantity, amount, tax)
                items.append(item)
                last = item
                last_base_name = item.actual_name
                allow_layout_suffix = True
                continue
        if not parsed:
            loose = _parse_loose_amount_tax_line(item_line)
            if loose:
                unit, quantity, amount, tax = loose
                marker = re.search(r"\d+(?:\.\d+)?%[\u4e00-\u9fffA-Za-z]{1,4}\s+-?\d+\.\d{2}", item_line)
                unit_marker = re.search(
                    rf"\s+{re.escape(unit)}\s*(?=-?\d)", item_line
                ) if unit else None
                raw_name = (
                    item_line[:item_line.find(unit)].strip()
                    if has_overlaid_headers and unit
                    else layout_name_part(raw_line) if layout
                    else item_line[:unit_marker.start()].strip() if unit_marker
                    else item_line[:marker.start()].strip() if marker
                    else item_line
                )
                item = make_item(raw_name, unit, quantity, amount, tax)
                items.append(item)
                last = item
                last_base_name = item.actual_name
                allow_layout_suffix = layout
                continue
            pending_name_parts = [layout_name_part(raw_line)]
            allow_layout_suffix = False
            continue
        unit, quantity, amount, tax, name_end = parsed
        # raw_name 从行首到「名称末尾位置」（正则1 给出）截取
        raw_name = (
            item_line[:name_end].strip()
            if has_overlaid_headers
            else layout_name_part(raw_line) if layout
            else line[:name_end].strip()
        )
        base_name = clean_item_name(raw_name)
        if not raw_name and last:
            # 名称没拿到版面给空——把这个修正并入上一条
            last.total = money(last.total + amount + tax)
            allow_layout_suffix = True
            continue
        if not unit and last and base_name == last_base_name:
            last.total = money(last.total + amount + tax)
            allow_layout_suffix = True
            continue
        item = make_item(raw_name, unit, quantity, amount, tax)
        items.append(item)
        last = item
        last_base_name = base_name
        allow_layout_suffix = True

    return items


def _parse_columnar_pdf_items(lines: list[str]) -> list[ParsedItem]:
    """兜底解析按列输出、每个单元格独占一行的发票明细。

    部分平台发票的可复制文本不是按视觉行排列，而是依次输出“品名列、
    规格列、单位列、数量列……”；pypdf 的 layout 模式又会忽略这些文本。
    这里只在常规逐行解析完全没有结果时启用，并且只接受能明确闭合的一条
    商品记录。若检测到多个不同品名，宁可保留识别提醒，也不猜测列对应关系。
    """
    number = re.compile(r"-?\d+(?:\.\d+)?")
    rate = re.compile(r"-?\d+(?:\.\d+)?%")
    unit = re.compile(r"[\u4e00-\u9fffA-Za-z]{1,4}")

    for start, line in enumerate(lines):
        if not line.startswith("*"):
            continue

        # 锚定连续的“单位、数量、单价、金额、税额、税率”六列。
        unit_index = -1
        for idx in range(start + 1, len(lines) - 5):
            tail = lines[idx:idx + 6]
            if (
                unit.fullmatch(tail[0])
                and all(number.fullmatch(value) for value in tail[1:5])
                and rate.fullmatch(tail[5])
            ):
                unit_index = idx
                break
        if unit_index < 0:
            continue

        prefix = lines[start:unit_index]
        star_indexes = [i for i, value in enumerate(prefix) if value.startswith("*")]
        blocks: list[list[str]] = []
        for offset, block_start in enumerate(star_indexes):
            block_end = star_indexes[offset + 1] if offset + 1 < len(star_indexes) else len(prefix)
            blocks.append(prefix[block_start:block_end])

        # 规格型号通常是单位前最后一个短 token，不属于商品名。
        if blocks:
            final = blocks[-1]
            if len(final) > 1 and re.fullmatch(r"[\w./+×*()-]{1,32}", final[-1]):
                final.pop()
        normalized_blocks = [_normalize_name(block) for block in blocks if block]
        if not normalized_blocks:
            continue
        first_name = normalized_blocks[0]
        # 京东等开票方会为折扣行重复一次品名；不同品名意味着多条商品，
        # 此时简单的单行兜底无法可靠配对各列。
        if any(name != first_name for name in normalized_blocks[1:]):
            continue

        quantity = d(lines[unit_index + 1])
        amount = d(lines[unit_index + 3])
        tax = d(lines[unit_index + 4])
        total = amount + tax

        # 紧随主记录的“折扣金额、折扣税额、税率”三列并入该商品。
        pos = unit_index + 6
        while (
            pos + 2 < len(lines)
            and number.fullmatch(lines[pos])
            and number.fullmatch(lines[pos + 1])
            and rate.fullmatch(lines[pos + 2])
        ):
            total += d(lines[pos]) + d(lines[pos + 1])
            pos += 3

        return [ParsedItem(
            name=first_name,
            actual_name=clean_item_name(first_name),
            unit=lines[unit_index],
            quantity=quantity,
            total=money(total),
        )]

    return []


def parse_pdf(path: str | Path) -> ParsedInvoice:
    path = Path(path)
    text = _pdf_text(path)
    invoice = _parse_invoice_text(text, source="pdf")
    try:
        layout_text = _pdf_layout_text(path)
        layout_invoice = _parse_invoice_text(layout_text, source="pdf-layout")
        if not invoice.invoice_no:
            invoice.invoice_no = layout_invoice.invoice_no
        if not invoice.invoice_date:
            invoice.invoice_date = layout_invoice.invoice_date
        layout_parties = _extract_layout_parties(layout_text)
        if layout_parties:
            invoice.seller, _seller_tax_id, invoice.buyer_name, invoice.buyer_tax_id = layout_parties

        layout_items = _parse_pdf_items(layout_text.splitlines(), layout=True)
        layout_total = sum((item.total for item in layout_items), Decimal("0"))
        if layout_items and money(layout_total - invoice.total) == Decimal("0.00"):
            invoice.items = layout_items
    except Exception:
        # 版面提取只是商品名续行增强；不支持时继续使用普通文本解析结果。
        pass
    return invoice


def _parse_invoice_text(text: str, source: str = "pdf") -> ParsedInvoice:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = _normalize_party_lines(lines)

    invoice_no = _extract_invoice_no(text)
    invoice_date = _extract_invoice_date(text)

    seller_name, seller_tax_id, buyer_name, buyer_tax_id = _extract_parties(lines)
    if not seller_name:
        # 兜底走旧的「日期行下两行就是购买方」启发式
        buyer_name, buyer_tax_id = _extract_pdf_buyer(lines)
        seller_name = _extract_pdf_seller(lines, buyer_tax_id, buyer_name)

    # Only allow horizontal spacing after the currency sign.  ``\s*`` also crosses a
    # newline, so a trailing ``¥`` in the tax line could consume a bank/account number
    # on the next line and turn it into an enormous invoice total.
    raw_amounts = re.findall(
        r"[¥￥][^\S\r\n]*"
        r"(-?(?:[0-9,，][^\S\r\n]*)+"
        r"(?:\.[^\S\r\n]*(?:[0-9][^\S\r\n]*){1,2})?)"
        r"(?=\r?\n|[^\S\r\n]*[¥￥]|[^0-9\s.]|$)",
        text,
    )
    amounts = [d(re.sub(r"[\s,，]", "", value)) for value in raw_amounts]
    total = max(amounts) if amounts else Decimal("0")

    invoice = ParsedInvoice(
        invoice_no=invoice_no,
        invoice_date=invoice_date,
        seller=seller_name,
        buyer_name=buyer_name,
        buyer_tax_id=buyer_tax_id,
        total=total,
        source=source,
    )
    items = _parse_pdf_items(lines)
    if not items:
        items = _parse_columnar_pdf_items(lines)
    invoice.items.extend(items)
    return invoice


# --------------------------------------------------------------------------- 合并入口

def _is_electronic_invoice_xml(root: ET.Element) -> bool:
    """电子发票 XML 的根节点带 EIid / SellerName / BuyerName 等字段。"""
    for tag in ("EIid", "SellerName", "BuyerName"):
        if root.find(f".//{tag}") is not None:
            return True
    return False


def _aspose_text(path: Path) -> str:
    """把 Aspose PDF→XML 的版面 Glyphs 按行重建为 PDF 文本流形态。

    每个 Glyphs 的 RenderTransform 形如 ``a b c d x y``（x/y 为屏幕坐标系，页顶为 0，
    向下为正）。处理步骤：
    1. 按四舍五入整数 y 聚类成行（兼容 y 微小抖动）。
    2. 若一行内相邻 Glyphs 的 x 间距过大（> 200），视为双列布局（销售方/购买方
       信息块常见）——按该空隙拆成两行：左列紧接右列。
    """
    root = ET.parse(path).getroot()
    rows: dict[int, list[tuple[float, str]]] = {}
    for g in root.iter("Glyphs"):
        tx = g.find("Text")
        if tx is None or not tx.text:
            continue
        rt = (g.get("RenderTransform") or "").split()
        x, y = 0.0, 0.0
        if len(rt) >= 6:
            try:
                x, y = float(rt[4]), float(rt[5])
            except ValueError:
                pass
        else:
            origin = g.find("Origin")
            if origin is not None:
                x = float(origin.get("X") or 0)
        rows.setdefault(int(round(y)), []).append((x, tx.text))

    lines: list[str] = []
    for y in sorted(rows.keys()):
        sorted_row = sorted(rows[y], key=lambda p: p[0])
        # 双列检测：在排序后的 Glyphs 中找相邻 gap > 200 的位置，拆成左右两段
        groups: list[list[tuple[float, str]]] = [[sorted_row[0]]]
        for prev, cur in zip(sorted_row, sorted_row[1:]):
            if cur[0] - prev[0] > 200:
                groups.append([cur])
            else:
                groups[-1].append(cur)
        if len(groups) == 1:
            lines.append(" ".join(p[1] for p in groups[0]))
        else:
            lines.append(" ".join(p[1] for p in groups[0]))
            lines.append(" ".join(p[1] for p in groups[1]))
    return "\n".join(lines)


def parse_aspose_xml(path: str | Path) -> ParsedInvoice:
    """解析 Aspose PDF-转-XML 的版面 XML（Glyphs/Text 流）。

    把 Glyphs 按 PDF 坐标重排成行，再走与 PDF 相同的 `_parse_invoice_text` 启发式，
    用来兜底那些 pypdf 抽取破碎、但 Aspose 能拿到完整版面文字的发票。
    """
    path = Path(path)
    return _parse_invoice_text(_aspose_text(path), source="aspose-xml")


def parse_invoice_files(xml_path: str | Path | None = None, pdf_path: str | Path | None = None) -> ParsedInvoice:
    """给定 XML / PDF 路径（可只给其一），返回最优解析结果。

    两类 XML：
    - 电子发票 XML（根节点含 EIid/SellerName/...）走 parse_xml，结构化可靠。
    - Aspose 从 PDF 转出来的版面 XML（Glyphs/Text 流）走 parse_aspose_xml，是 PDF 文本流的替代。
    自动识别：检测根节点的标签名决定走哪条。
    """
    if xml_path:
        root = ET.parse(xml_path).getroot()
        if _is_electronic_invoice_xml(root):
            invoice = parse_xml(xml_path)
            if pdf_path:
                invoice.source = "xml+pdf"
            return invoice
        # 否则当作 Aspose 版面 XML
        invoice = parse_aspose_xml(xml_path)
        if pdf_path:
            invoice.source = "aspose-xml+pdf"
        return invoice
    if pdf_path:
        return parse_pdf(pdf_path)
    raise ValueError("至少需要提供 XML 或 PDF 之一。")

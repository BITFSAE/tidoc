"""Read item columns from PDF text positions when plain/layout text interleaves them."""
from __future__ import annotations

import re
from copy import copy
from difflib import SequenceMatcher
import unicodedata
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader
from pypdf.generic import ArrayObject, ByteStringObject, ContentStream, NameObject, NumberObject, TextStringObject

from .models import ParsedItem
from .money import money


def _compact(value: str) -> str:
    return re.sub(r'\s+', '', value)


def name_has_column_spill(current: ParsedItem, candidate: ParsedItem) -> bool:
    """Allow shortening only when every removed fragment belongs to a column."""
    old, new, spec, unit = (
        _compact(unicodedata.normalize('NFKC', value))
        for value in (current.actual_name, candidate.actual_name, candidate.spec, candidate.unit)
    )
    if not new or not spec or old == new:
        return False
    for operation, start, end, _new_start, _new_end in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if operation == 'equal':
            continue
        if operation != 'delete':
            return False
        fragment = old[start:end]
        if fragment in spec:
            continue
        if unit and fragment.endswith(unit) and fragment[:-len(unit)] in spec:
            continue
        return False
    return True


def align_positioned_items(current: list[ParsedItem], candidates: list[ParsedItem]):
    """Match row amounts, allowing only corroborated same-product discounts."""
    aligned = []
    offset = 0
    for candidate in candidates:
        if offset >= len(current):
            return []
        first = current[offset]
        amount = first.total
        offset += 1
        while amount > candidate.total and offset < len(current):
            discount = current[offset]
            if (
                discount.total >= 0 or discount.quantity is not None or discount.unit
                or _compact(discount.actual_name) != _compact(candidate.actual_name)
                or not (_compact(first.actual_name) == _compact(candidate.actual_name)
                        or name_has_column_spill(first, candidate))
            ):
                break
            amount += discount.total
            offset += 1
        if amount != candidate.total:
            return []
        aligned.append((first, candidate))
    return aligned if offset == len(current) else []


def _page_spans(page) -> list[tuple[float, float, str]]:
    # pypdf can flush a text fragment with the preceding fragment's matrix.
    # Identity transforms flush its buffer at each show-text boundary without
    # moving anything. Work on an in-memory copy and preserve original font
    # bytes: serializing decoded strings would change custom-font character codes.
    def font_bytes(value):
        if isinstance(value, TextStringObject):
            return ByteStringObject(value.original_bytes)
        if isinstance(value, ArrayObject):
            return ArrayObject([font_bytes(part) for part in value])
        return value

    original_page = page
    source = page.get_contents()
    if source is not None:
        stream = ContentStream(source, page.pdf)
        identity = ([NumberObject(n) for n in (1, 0, 0, 1, 0, 0)], b'cm')
        operations = []
        for operands, operator in stream.operations:
            operands = [font_bytes(value) for value in operands]
            if operator in (b'Tj', b'TJ'):
                operations.extend((identity, (operands, operator), identity))
            else:
                operations.append((operands, operator))
        stream.operations = operations
        page = copy(page)
        page[NameObject('/Contents')] = stream
    spans = _extract_spans(page)
    # Character-by-character generators need pypdf's normal text joining. A
    # fragmented header cannot anchor columns reliably; retain that reader.
    if source is not None and not any('项目名称' in _compact(text) for _x, _y, text in spans):
        return _extract_spans(original_page)
    return spans


def _extract_spans(page) -> list[tuple[float, float, str]]:
    spans = []
    last_end = None

    def before(operator, _args, cm, tm):
        nonlocal last_end
        if operator == b'ET':
            last_end = (list(tm), list(cm))

    def visit(text, cm, tm, _font, _size):
        if not text.strip():
            return
        # Some generators flush text after resetting Tm at ET. The last text
        # object's matrix still locates that fragment; (0, 0) does not.
        if tm[4:] == [0, 0] and last_end is not None:
            tm, cm = last_end
        if abs(cm[1]) + abs(cm[2]) > 0.01:
            return  # Rotated stamps are not item columns.
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        spans.append((x, y, text.strip()))

    page.extract_text(visitor_text=visit, visitor_operand_before=before)
    return spans


def read_positioned_items(path: Path) -> list[ParsedItem]:
    """Return a candidate only when every positioned item row can be parsed.

    The caller must also compare row amounts against its existing result. Names
    follow PDF stream order within the first column: some fonts report the next
    baseline for a continuation, so sorting names by y can attach it to a discount.
    """
    from .parser import _parse_pdf_items, _PDF_UNIT_SUFFIXES, clean_item_name

    rows: list[ParsedItem] = []
    left: float | None = None
    for page in PdfReader(str(path)).pages:
        spans = _page_spans(page)
        headers = [y for _x, y, text in spans if '项目名称' in _compact(text)]
        if not headers:
            return []
        top = max(headers)
        stars = [(x, y, text) for x, y, text in spans if text.startswith('*') and y < top - 2]
        if not stars and not rows:
            return []
        if stars:
            left = min(x for x, _y, _text in stars)
        if left is None:
            return []
        footers = [
            y for x, y, text in spans
            if y < top - 8 and x < float(page.mediabox.width) * 0.3
            and _compact(text) in {'合', '合计', '小计', '价税合计(大写)', '价税合计（大写）'}
        ]
        if not footers:
            return []
        bottom = max(footers)
        body = []
        previous_is_name = False
        for span in spans:
            x, y, text = span
            in_name_column = abs(x - left) < 3
            if not in_name_column and _compact(text) in {'购', '买', '销', '售', '方', '信', '息'}:
                continue  # Vertical party labels may carry displaced text matrices.
            in_table = bottom + 5 < y < top - 2
            # A wrapped name can report a baseline below the printed table.
            # Require contiguous name fragments in the source stream there;
            # footer labels and later notes must never extend the product name.
            name_continuation = (
                in_name_column and previous_is_name and 0 < y < top - 2
                and not text.startswith('*')
                and _compact(text) not in {'合', '合计', '小计', '备注', '备', '注'}
            )
            if in_table or name_continuation:
                body.append(span)
            previous_is_name = in_name_column and (in_table or name_continuation)
        groups = [[span, [], []] for span in body if abs(span[0] - left) < 3 and span[2].startswith('*')]
        current = None
        for span in body:
            head = next((group for group in groups if span is group[0]), None)
            if head is not None:
                current = head
                current[1].append(span[2])
            elif abs(span[0] - left) < 3:
                if current is not None:
                    current[1].append(span[2])
                elif rows:
                    rows[-1].name += span[2].replace('\n', '')
                    rows[-1].actual_name = clean_item_name(rows[-1].name)
                else:
                    return []
            else:
                eligible = [group for group in groups if span[1] <= group[0][1] + 5]
                if eligible:
                    eligible[-1][2].append(span)

        anchors = {
            _compact(text): x for x, y, text in spans
            if abs(y - top) < 3 and _compact(text) in {'规格型号', '单位', '数量'}
        }
        # Some fonts expose 「单 位」 as two separate text fragments.
        header = [(x, _compact(text)) for x, y, text in spans if abs(y - top) < 3]
        if '单位' not in anchors:
            for x, text in header:
                if text == '单' and any(0 < other_x - x < 30 and other == '位'
                                         for other_x, other in header):
                    anchors['单位'] = x
        for _head, names, cells in groups:
            if any('¥' in text or '￥' in text for _x, _y, text in cells):
                return []  # A subtotal has leaked into the row; keep the other reader.
            name = ''.join(names).replace('\n', '')
            unit_cells = spec_cells = []
            if '单位' in anchors:
                unit_x = anchors['单位']
                spec_x = anchors.get('规格型号', (left + unit_x) / 2)
                start = (spec_x + unit_x) / 2
                end = (anchors.get('数量', unit_x + 70) + unit_x) / 2
                unit_cells = sorted(
                    [cell for cell in cells if start <= cell[0] < end], key=lambda cell: -cell[1]
                )
                spec_cells = sorted(
                    [cell for cell in cells if spec_x - 10 <= cell[0] < start],
                    key=lambda cell: -cell[1],
                )
            # Parse the numeric tail without model fragments (which can contain
            # numbers and hyphens resembling quantities or negative amounts).
            numeric_cells = [cell for cell in cells if cell not in spec_cells and cell not in unit_cells]
            values = []
            if unit_cells:
                values.append(''.join(_compact(cell[2]) for cell in unit_cells))
            for _x, _y, text in sorted(numeric_cells, key=lambda cell: cell[0]):
                values.append(_compact(text) if re.fullmatch(r'[\d\s.,+\-]+', text) else text.replace('\n', ''))
            parsed = _parse_pdf_items([name + '     ' + '     '.join(values)], layout=True)
            if len(parsed) != 1:
                return []
            item = parsed[0]
            item.name = name
            item.actual_name = clean_item_name(name)
            if '单位' in anchors:
                item.unit = ''.join(_compact(cell[2]) for cell in unit_cells)
                item.spec = ''.join(cell[2] for cell in spec_cells)
            else:
                units = [text for _x, _y, text in cells if text in _PDF_UNIT_SUFFIXES]
                if len(units) == 1:
                    item.unit = units[0]
            rows.append(item)

    merged: list[ParsedItem] = []
    for item in rows:
        if (
            merged and item.total < Decimal('0') and not item.unit and item.quantity is None
            and re.sub(r'\s+', '', item.name) == re.sub(r'\s+', '', merged[-1].name)
        ):
            merged[-1].total = money(merged[-1].total + item.total)
        else:
            merged.append(item)
    return merged

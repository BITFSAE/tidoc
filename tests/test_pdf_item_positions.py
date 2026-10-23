"""Fictional text-layer regressions: columns, wrapped units and conservative adoption."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tidoc.engine import parser
from tidoc.engine import pdf_items


def positioned(monkeypatch, pages):
    fake_pages = [SimpleNamespace(mediabox=SimpleNamespace(width=600), spans=spans) for spans in pages]
    monkeypatch.setattr(pdf_items, 'PdfReader', lambda _: SimpleNamespace(pages=fake_pages))
    monkeypatch.setattr(pdf_items, '_page_spans', lambda page: page.spans)
    return pdf_items.read_positioned_items('fictional.pdf')


def table(*body):
    return [
        (30, 250, '项目名称'), (130, 250, '规格型号'),
        (220, 250, '单位'), (290, 250, '数量'),
        *body, (30, 100, '合计'),
    ]


def test_positioned_name_uses_stream_order_and_keeps_dimension_stars(monkeypatch):
    rows = positioned(monkeypatch, [table(
        (10, 240, '*绝缘制品*测试胶带'),
        # The first continuation reports a lower y than the discount's head.
        (10, 205, '19mm*20.1m*0.18mm'),
        (10, 220, '*绝缘制品*测试胶带'), (10, 185, '19mm*20.1m*0.18mm'),
        (130, 240, 'MODEL-1'), (225, 240, '卷'), (300, 240, '1'),
        (360, 240, '10.00'), (420, 240, '10.00'), (480, 240, '13%'), (550, 240, '1.30'),
        (420, 220, '-1.00'), (480, 220, '13%'), (550, 220, '-0.13'),
    )])
    assert len(rows) == 1
    assert rows[0].actual_name == '测试胶带19mm*20.1m*0.18mm'
    assert rows[0].unit == '卷'
    assert rows[0].total == Decimal('10.17')


def test_positioned_unit_wrap_and_no_quantity_cell(monkeypatch):
    rows = positioned(monkeypatch, [table(
        (10, 240, '*电线电缆*测试线材'), (130, 240, '100m'),
        (220, 240, '100米/'), (230, 227, '卷'), (300, 240, '1'),
        (360, 240, '10.00'), (420, 240, '10.00'), (480, 240, '1%'), (550, 240, '0.10'),
        (10, 200, '*金属制品*专用手工具'), (130, 200, '胶枪'), (225, 200, '把'),
        (420, 200, '20.00'), (480, 200, '1%'), (550, 200, '0.20'),
    )])
    assert [row.unit for row in rows] == ['100米/卷', '把']
    assert rows[1].quantity is None
    assert [row.total for row in rows] == [Decimal('10.10'), Decimal('20.20')]


def test_positioned_same_name_positive_rows_remain_separate(monkeypatch):
    body = []
    for y in (240, 210):
        body.extend([(10, y, '*金属制品*螺母'), (225, y, '件'), (300, y, '2'),
                     (360, y, '5.00'), (420, y, '10.00'), (480, y, '13%'), (550, y, '1.30')])
    rows = positioned(monkeypatch, [table(*body)])
    assert len(rows) == 2
    assert [row.total for row in rows] == [Decimal('11.30')] * 2


def test_positioned_cross_page_suffix_precedes_discount_merge(monkeypatch):
    rows = positioned(monkeypatch, [
        table((10, 240, '*电子元件*USB转换芯'), (225, 240, '个'), (300, 240, '1'),
              (360, 240, '3.19'), (420, 240, '3.19'), (480, 240, '13%'), (550, 240, '0.41')),
        table((10, 240, '片'), (10, 220, '*电子元件*USB转换芯片'),
              (420, 220, '-0.16'), (480, 220, '13%'), (550, 220, '-0.02')),
    ])
    assert len(rows) == 1
    assert rows[0].actual_name == 'USB转换芯片'
    assert rows[0].total == Decimal('3.42')


def test_text_flushed_after_end_uses_previous_text_matrix():
    def extract(**callbacks):
        callbacks['visitor_operand_before'](b'ET', [], [1, 0, 0, 1, 0, 0], [1, 0, 0, 1, 220, 240])
        callbacks['visitor_text']('卷', [1, 0, 0, 1, 0, 0], [1, 0, 0, 1, 0, 0], None, 9)
    assert pdf_items._page_spans(SimpleNamespace(extract_text=extract)) == [(220, 240, '卷')]


@pytest.mark.parametrize('candidate_total', ['11.30', '12.30'])
def test_positioned_adoption_keeps_existing_quantity_and_does_not_shorten_name(monkeypatch, tmp_path, candidate_total):
    text = '*电子元件*NTC热敏电阻     个 12 0.833333 10.00 13% 1.30\n价税合计（小写）¥11.30'
    monkeypatch.setattr(parser, '_pdf_text', lambda _: text)
    monkeypatch.setattr(parser, '_pdf_layout_text', lambda _: text)
    candidate = parser.ParsedItem(name='*电子元件*热敏电阻', actual_name='热敏电阻',
                                  unit='母个', quantity=None, total=Decimal(candidate_total))
    monkeypatch.setattr(pdf_items, 'read_positioned_items', lambda _: [candidate])
    invoice = parser.parse_pdf(tmp_path / 'fictional.pdf')
    assert invoice.items[0].actual_name == 'NTC热敏电阻'
    assert invoice.items[0].quantity == Decimal('12')
    assert invoice.items[0].unit == '个'
    assert invoice.items[0].total == Decimal('11.30')


def test_positioned_name_does_not_include_footer_or_notes_in_same_column(monkeypatch):
    rows = positioned(monkeypatch, [[
        *table((10, 240, '*电子元件*测试芯片'),
               (225, 240, '个'), (300, 240, '1'), (360, 240, '10.00'),
               (420, 240, '10.00'), (480, 240, '13%'), (550, 240, '1.30')),
        (10, 80, '合计'), (10, 50, '这是备注，不属于商品名称'),
    ]])
    assert rows[0].actual_name == '测试芯片'
    assert rows[0].total == Decimal('11.30')


def test_positioned_unit_does_not_replace_compound_prefix_with_only_suffix(monkeypatch, tmp_path):
    current = parser.ParsedItem(name='*电线电缆*测试线', actual_name='测试线',
                                unit='100米/', quantity=Decimal('1'), total=Decimal('11.30'))
    original = parser.ParsedInvoice(total=Decimal('11.30'), items=[current])
    monkeypatch.setattr(parser, '_pdf_text', lambda _: '')
    monkeypatch.setattr(parser, '_pdf_layout_text', lambda _: '')
    monkeypatch.setattr(parser, '_parse_invoice_text', lambda *_args, **_kwargs: original)
    candidate = parser.ParsedItem(name='*电线电缆*测试线', actual_name='测试线',
                                  unit='卷', quantity=Decimal('1'), total=Decimal('11.30'))
    monkeypatch.setattr(pdf_items, 'read_positioned_items', lambda _: [candidate])
    invoice = parser.parse_pdf(tmp_path / 'fictional.pdf')
    assert invoice.items[0].unit == '100米/'

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
    assert pdf_items._page_spans(SimpleNamespace(extract_text=extract, get_contents=lambda: None)) == [(220, 240, '卷')]


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


def test_same_font_position_changes_keep_custom_font_bytes_and_source(tmp_path):
    pytest.importorskip('reportlab')
    from pypdf import PdfReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    path = tmp_path / 'fictional-columns.pdf'
    drawing = canvas.Canvas(str(path), pagesize=(600, 400))
    text = drawing.beginText()
    text.setFont('STSong-Light', 9)
    text.setTextOrigin(10, 260)
    text.textOut('项目名称')
    for x, value in [(10, '*纸制品*测试纸碗'), (130, '20只'), (225, '袋'), (300, '1')]:
        text.setTextOrigin(x, 240)
        text.textOut(value)
    drawing.drawText(text)
    drawing.save()
    original_file = path.read_bytes()
    page = PdfReader(path).pages[0]
    original_stream = page.get_contents().get_data()
    spans = pdf_items._page_spans(page)
    assert [(x, value) for x, y, value in spans if y == 240] == [
        (10, '*纸制品*测试纸碗'), (130, '20只'), (225, '袋'), (300, '1'),
    ]
    assert page.get_contents().get_data() == original_stream
    assert path.read_bytes() == original_file


def test_character_fragmented_header_keeps_normal_text_spans(tmp_path):
    pytest.importorskip('reportlab')
    from pypdf import PdfReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    path = tmp_path / 'fictional-character-spans.pdf'
    drawing = canvas.Canvas(str(path), pagesize=(600, 400))
    text = drawing.beginText()
    text.setFont('STSong-Light', 9)
    for offset, char in enumerate('项目名称'):
        text.setTextOrigin(30 + offset * 9, 250)
        text.textOut(char)
    drawing.drawText(text)
    drawing.save()
    spans = pdf_items._page_spans(PdfReader(path).pages[0])
    assert spans == pdf_items._extract_spans(PdfReader(path).pages[0])
    assert '项目名称' in ''.join(value for _x, _y, value in spans)


def test_positioned_model_wrap_and_discount_keep_quantity(monkeypatch):
    rows = positioned(monkeypatch, [table(
        (10, 240, '*日用杂品*测试收纳箱'), (10, 230, '白色'),
        (130, 240, 'MODEL-'), (130, 230, '200'),
        (225, 240, '个'), (300, 240, '1'), (360, 240, '10.00'),
        (420, 240, '10.00'), (480, 240, '13%'), (550, 240, '1.30'),
        (10, 210, '*日用杂品*测试收纳箱'), (10, 200, '白色'),
        (420, 210, '-1.00'), (480, 210, '13%'), (550, 210, '-0.13'),
    )])
    assert len(rows) == 1
    assert rows[0].actual_name == '测试收纳箱白色'
    assert rows[0].spec == 'MODEL-200'
    assert rows[0].quantity == Decimal('1')
    assert rows[0].total == Decimal('10.17')


@pytest.mark.parametrize('old,new,spec', [
    ('测试泡沫 外翼（单', '测试泡沫', '外翼（单侧）加工件'),
    ('测试纸碗20只加厚20只', '测试纸碗加厚20只', '20只'),
    ('测试耗材 C0-500-', '测试耗材', 'S01-C0-500-SPL'),
])
def test_positioned_removes_only_corroborated_spec_fragments(monkeypatch, tmp_path, old, new, spec):
    current = parser.ParsedItem(name='*测试*' + old, actual_name=old,
                                unit='件', quantity=Decimal('1'), total=Decimal('11.30'))
    original = parser.ParsedInvoice(total=Decimal('11.30'), items=[current])
    monkeypatch.setattr(parser, '_pdf_text', lambda _: '')
    monkeypatch.setattr(parser, '_pdf_layout_text', lambda _: '')
    monkeypatch.setattr(parser, '_parse_invoice_text', lambda *_args, **_kwargs: original)
    monkeypatch.setattr(parser, '_reconcile_fields', lambda *_args: None)
    candidate = parser.ParsedItem(name='*测试*' + new, actual_name=new, spec=spec,
                                  unit='件', quantity=Decimal('1'), total=Decimal('11.30'))
    monkeypatch.setattr(pdf_items, 'read_positioned_items', lambda _: [candidate])
    invoice = parser.parse_pdf(tmp_path / 'fictional.pdf')
    assert invoice.items[0].actual_name == new
    assert invoice.items[0].spec == spec


@pytest.mark.parametrize('discount_name,expected_count', [('测试箱白色', 1), ('其他商品', 2)])
def test_adoption_merges_only_matching_discount(monkeypatch, tmp_path, discount_name, expected_count):
    current = parser.ParsedItem(name='*测试*测试箱MODEL-1个白色', actual_name='测试箱MODEL-1个白色',
                                unit='个', quantity=Decimal('1'), total=Decimal('11.30'))
    discount = parser.ParsedItem(name='*测试*' + discount_name, actual_name=discount_name,
                                 unit='', quantity=None, total=Decimal('-1.13'))
    original = parser.ParsedInvoice(total=Decimal('10.17'), items=[current, discount])
    monkeypatch.setattr(parser, '_pdf_text', lambda _: '')
    monkeypatch.setattr(parser, '_pdf_layout_text', lambda _: '')
    monkeypatch.setattr(parser, '_parse_invoice_text', lambda *_args, **_kwargs: original)
    monkeypatch.setattr(parser, '_reconcile_fields', lambda *_args: None)
    candidate = parser.ParsedItem(name='*测试*测试箱白色', actual_name='测试箱白色', spec='MODEL-1',
                                  unit='个', quantity=Decimal('1'), total=Decimal('10.17'))
    monkeypatch.setattr(pdf_items, 'read_positioned_items', lambda _: [candidate])
    invoice = parser.parse_pdf(tmp_path / 'fictional.pdf')
    assert len(invoice.items) == expected_count
    if expected_count == 1:
        assert invoice.items[0].actual_name == '测试箱白色'
        assert invoice.items[0].total == Decimal('10.17')
        assert invoice.items[0].quantity == Decimal('1')

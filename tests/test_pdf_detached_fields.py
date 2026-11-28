"""Fictional PDFs with amounts, rates and currency emitted out of visual order."""
from decimal import Decimal

import pytest

from tidoc.engine import ParsedInvoice, parse_pdf
from tidoc.engine.models import FIELD_VERIFIED
from tidoc.engine.fields import uscc_check_char


BUYER = '测试采购单位'
SELLER = '测试材料有限公司'
BUYER_ID = '9111000071000001A'
BUYER_ID += uscc_check_char(BUYER_ID)
SELLER_ID = '91110000MA00ABCDE'
SELLER_ID += uscc_check_char(SELLER_ID)
NUMBER = '26110000000000000009'


def make_detached_pdf(path, *, rate='13', tax='1.30', total='11.30', words='壹拾壹圆叁角整'):
    pytest.importorskip('reportlab')
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    drawing = canvas.Canvas(str(path), pagesize=(610, 400))
    text = drawing.beginText()
    text.setFont('STSong-Light', 9)

    def put(x, y, value):
        text.setTextOrigin(x, y)
        text.textOut(value)

    # Static labels precede all values; each textOut shares the same text object.
    for x, value in [(50, '项目名称'), (122, '规格型号'), (189, '单  位'),
                     (263, '数  量'), (333, '单  价'), (408, '金  额'),
                     (448, '税率/征收率'), (551, '税  额')]:
        put(x, 238, value)
    for x in (32, 317):
        put(x, 294, '名称：')
        put(x, 268, '统一社会信用代码/纳税人识别号：')
    for x, y, value in [(441, 356, '发票号码：'), (441, 340, '开票日期：'),
                        (21, 305, '购买方信息'), (305, 305, '销售方信息'),
                        (200, 356, '电子发票（普通发票）'), (418, 110.06, '（小写）'),
                        (50, 110.06, '价税合计（大写）'), (53, 128, '合         计')]:
        put(x, y, value)
    for i, value in enumerate('下载次数：1'):
        put(593, 305 - i * 9, value)
    for x, value in [(285.5, '1'), (338.5, '10.00'), (411.5, '10.00'),
                     (562, tax), (463.25, rate + '%'), (15.59, '*金属制品*测试螺丝'),
                     (121, 'M4*12'), (198.5, '个')]:
        put(x, 228.24, value)
    put(121, 215.84, '-100个')
    for x, y, value in [(55, 294, BUYER), (483, 356, NUMBER),
                        (483, 340, '2026年11月27日'), (340, 294, SELLER),
                        (177, 110, words), (458, 111, total),
                        (411.5, 128, '10.00'), (562, 128, tax),
                        (167, 268, BUYER_ID), (452, 268, SELLER_ID),
                        (452, 111, '¥'), (404.9, 128, '¥'), (555.5, 128, '¥')]:
        put(x, y, value)
    drawing.drawText(text)
    drawing.save()
    return path


@pytest.mark.parametrize('rate,tax,total,words', [
    ('13', '1.30', '11.30', '壹拾壹圆叁角整'),
    ('1', '0.10', '10.10', '壹拾圆壹角整'),
])
def test_detached_values_are_recovered_from_positions(tmp_path, rate, tax, total, words):
    path = make_detached_pdf(tmp_path / 'fictional.pdf', rate=rate, tax=tax, total=total, words=words)
    before = path.read_bytes()
    parsed = parse_pdf(path)
    assert parsed.invoice_no == NUMBER
    assert (parsed.buyer_name, parsed.seller) == (BUYER, SELLER)
    assert (parsed.buyer_tax_id, parsed.seller_tax_id) == (BUYER_ID, SELLER_ID)
    assert parsed.total == Decimal(total)
    assert parsed.subtotal + parsed.tax_total == parsed.total == parsed.total_in_words
    assert parsed.tax_rates == [Decimal(rate)]
    assert parsed.field_checks['total'].state == FIELD_VERIFIED
    assert parsed.field_checks['buyer_name'].state == FIELD_VERIFIED
    assert len(parsed.items) == 1
    assert parsed.items[0].actual_name == '测试螺丝'
    assert parsed.items[0].spec == 'M4*12-100个'
    assert parsed.items[0].quantity == 1
    assert parsed.items[0].total == Decimal(total)
    assert path.read_bytes() == before


@pytest.mark.parametrize('changes', [{'words': '玖拾圆整'}, {'tax': '9.99'}])
def test_positioned_total_requires_both_independent_checks(tmp_path, changes):
    parsed = parse_pdf(make_detached_pdf(tmp_path / 'inconsistent.pdf', **changes))
    assert not parsed.total_present


@pytest.mark.parametrize('mode', ['missing', 'saved', 'manual', 'cleared', 'manual_title'])
def test_reparse_repairs_missing_total_and_punctuation_title(api, tmp_path, mode):
    path = make_detached_pdf(tmp_path / 'fictional.pdf')
    profile = api.profiles.create('测试报账人', '')
    entry_id = api.entries.create(profile['id'], parsed=ParsedInvoice(
        invoice_no=NUMBER, buyer_name='：', seller='下载次数',
        total=Decimal('0'), total_present=False, source='pdf',
    ))
    api.attachments.add(entry_id, path, 'invoice_pdf')
    api.entries.update_field(entry_id, 'actual_item_name', '人工品名', profile['id'])
    api.entries.update_field(entry_id, 'paid_amount', '12.00', profile['id'])
    if mode == 'saved':
        api.db.conn.execute('UPDATE entries SET total=? WHERE id=?', ('12.00', entry_id))
        api.db.conn.commit()
    elif mode in {'manual', 'cleared'}:
        api.entries.correct_locked_field(entry_id, 'total', '12.00', profile['id'])
        if mode == 'cleared':
            api.db.conn.execute('UPDATE entries SET total=? WHERE id=?', ('', entry_id))
            api.db.conn.commit()
    elif mode == 'manual_title':
        api.entries.correct_locked_field(entry_id, 'title', '用户分区', profile['id'])
    before = api.entries.get(entry_id)
    result = api.reparse_entries([entry_id])
    assert result['ok'] and result['data']['results'][0]['ok'], result
    entry = api.entries.get(entry_id)
    assert entry['total'] == ('11.30' if mode in {'missing', 'manual_title'} else before['total'])
    assert entry['title'] == ('用户分区' if mode == 'manual_title' else BUYER)
    assert entry['buyer_name'] == BUYER
    assert entry['seller'] == SELLER
    assert entry['fields']['paid_amount'] == before['fields']['paid_amount']
    assert entry['fields']['actual_item_name']['current'] == '人工品名'
    assert entry['items'][0]['spec'] == 'M4*12-100个'

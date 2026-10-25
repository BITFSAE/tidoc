"""查验单尾页筛选：有效材料保留、源文件不变、拼接编号和重生成一致。"""
import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from tidoc_print.blank_pages import inspection_pages
from tidoc_print.pdf_merge import render_pdf_bundle
from tests.pdf_fixtures import make_remark_overflow


@pytest.mark.parametrize(('tail', 'icc'), [('label', True), ('blank', False), ('full_label', False)])
def test_removes_empty_remark_table_overflow(tmp_path, tail, icc):
    source = make_remark_overflow(tmp_path/'overflow.pdf', tail, icc=icc)
    assert len(inspection_pages(PdfReader(source))) == 1


@pytest.mark.parametrize('tail', ['amount', 'note', 'image', 'fill', 'body_line', 'no_previous_label', 'unknown_color'])
def test_preserves_actual_content_and_uncertain_tables(tmp_path, tail):
    source = make_remark_overflow(tmp_path/'overflow.pdf', tail)
    assert len(inspection_pages(PdfReader(source))) == 2


def _make_inspection(path, tail='blank', *, pages=2):
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    for index in range(pages):
        c.setFont('Helvetica', 9)
        c.drawString(30, 820, 'Invoice verification')
        c.drawString(30, 20, 'https://example.invalid/verify')
        c.drawRightString(560, 20, f'{index + 1}/{pages}')
        if index == 0 or tail == 'body':
            c.drawString(50, 700, 'Verification result: valid')
        elif tail == 'inline_image':
            c._code.append('q 100 0 0 100 50 500 cm BI /W 1 /H 1 /BPC 8 /CS /RGB /F /AHx ID FF0000> EI Q')
        elif tail == 'image':
            from reportlab.lib.utils import ImageReader
            c.drawImage(ImageReader(Image.new('RGB', (20, 20), 'red')), 50, 500, 100, 100)
        elif tail == 'line':
            c.line(50, 500, 300, 500)
        elif tail == 'fill':
            c.setFillColorRGB(0, 0, 0)
            c.rect(50, 500, 100, 100, fill=1, stroke=0)
        elif tail == 'white':
            c.setFillColorRGB(1, 1, 1)
            c.rect(0, 0, 595, 842, fill=1, stroke=0)
        elif tail == 'unknown':
            c._code.append('BX UnknownPaint EX')
        elif tail == 'annotation':
            c.linkURL('https://example.invalid', (50, 500, 300, 550))
        c.showPage()
    c.save()
    return path


@pytest.mark.parametrize('tail', ['blank', 'white'])
def test_removes_only_contiguous_blank_tails(tmp_path, tail):
    source = _make_inspection(tmp_path/'source.pdf', tail, pages=3)
    assert len(inspection_pages(PdfReader(source))) == 1


@pytest.mark.parametrize('tail', ['body', 'inline_image', 'image', 'line', 'fill', 'annotation', 'unknown'])
def test_preserves_sparse_text_images_graphics_and_annotations(tmp_path, tail):
    source = _make_inspection(tmp_path/'source.pdf', tail)
    assert len(inspection_pages(PdfReader(source))) == 2


def test_preserves_first_page_and_internal_blank_pages(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    path = tmp_path/'blank.pdf'
    writer.write(path)
    assert len(inspection_pages(PdfReader(path))) == 1
    body = PdfReader(_make_inspection(tmp_path/'body.pdf', 'body'))
    writer.add_page(body.pages[-1])
    writer.add_blank_page(width=595, height=842)
    writer.write(path)
    assert len(inspection_pages(PdfReader(path))) == 2


def test_preserves_rotated_pages_and_handles_cancellation(tmp_path):
    source = _make_inspection(tmp_path/'source.pdf')
    reader = PdfReader(source)
    reader.pages[-1].rotate(90)
    assert len(inspection_pages(reader)) == 2
    with pytest.raises(RuntimeError, match='取消'):
        inspection_pages(PdfReader(source), cancel_check=lambda: True)


def test_repeated_amount_near_edge_is_not_a_header(tmp_path):
    path = tmp_path/'amount.pdf'
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    for _ in range(2):
        c.setFont('Helvetica', 9)
        c.drawString(30, 20, '20.08')
        c.showPage()
    c.save()
    assert len(inspection_pages(PdfReader(path))) == 2


def test_browser_scaled_header_uses_printed_font_size(tmp_path):
    path = tmp_path/'scaled.pdf'
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    for index in range(2):
        c.scale(0.5, 0.5)
        c.setFont('Helvetica', 18)
        c.drawString(60, 1640, 'Verification header')
        if index == 0:
            c.drawString(60, 1400, 'Valid invoice')
        c.showPage()
    c.save()
    assert len(inspection_pages(PdfReader(path))) == 1


def test_unreadable_content_is_preserved(tmp_path, monkeypatch):
    source = _make_inspection(tmp_path/'source.pdf')
    reader = PdfReader(source)

    def fail(**kwargs):
        raise ValueError('unknown encoding')

    monkeypatch.setattr(reader.pages[-1], 'extract_text', fail)
    assert len(inspection_pages(reader)) == 2


@pytest.mark.parametrize('order', ['entry', 'role'])
def test_bundle_filters_only_inspection_and_renumbers(tmp_path, order):
    source = _make_inspection(tmp_path/'source.pdf')
    before = source.read_bytes()
    resources = [
        {'path': source.name, 'entry_id': 'one', 'role_id': role}
        for role in ['invoice', 'inspection_pdf', 'custom:test:contract']
    ]
    output = {'pdf': {'include_roles': [r['role_id'] for r in resources], 'content_order': order,
                      'numbering': True, 'skip_blank_inspection_tails': True}}
    result = render_pdf_bundle({'entries': [{'id': 'one'}]}, output, resources, tmp_path, tmp_path/'out.pdf')
    pages = PdfReader(result).pages
    assert len(pages) == 5
    if order == 'entry':
        for i, page in enumerate(pages, 1):
            assert f'No.1-{i}/{len(pages)}' in page.extract_text()
    else:
        assert 'No.2-1/1' in pages[2].extract_text()
    assert source.read_bytes() == before

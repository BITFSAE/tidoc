"""供 PDF 筛选和导出回归共用的虚构查验单。"""
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas


def make_remark_overflow(path, tail='label', *, icc=True):
    """虚构查验单：空备注行跨页，用带色彩空间的填充矩形画边框。"""
    import io
    from pypdf.generic import DictionaryObject, NameObject, ArrayObject, DecodedStreamObject, NumberObject
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(842, 595))
    c.setFont('STSong-Light', 11)
    c.drawString(50, 450, '查验结果：一致')
    if tail != 'no_previous_label':
        c.drawString(26, 30, '备')
        c.drawString(26, 14, '注')
    c.showPage()
    c._code.extend([
        'q 22 22 797 550 re W n /Cs1 cs 1 1 1 sc 22 532 797 480 re f',
        '0.666667 0.666667 0.666667 sc',
        '22 612 797 0.8 re f',  # 页外边框。
        '22 532 797 0.8 re f',
        '22 532 0.8 81 re f 40 532 0.8 81 re f 818 532 0.8 81 re f Q',
    ])
    c.setFont('STSong-Light', 11)
    if tail in ('label', 'no_previous_label', 'unknown_color'):
        c.drawString(26, 563, '注')
    elif tail == 'full_label':
        c.drawString(26, 563, '备注')
    elif tail == 'amount':
        c.drawString(60, 563, '20.08')
    elif tail == 'note':
        c.drawString(60, 563, '备注：已核对')
    elif tail == 'image':
        from reportlab.lib.utils import ImageReader
        c.drawImage(ImageReader(Image.new('RGB', (20, 20), 'red')), 60, 540, 20, 20)
    elif tail == 'fill':
        c.setFillColorRGB(0.6, 0.6, 0.6)
        c.rect(60, 540, 20, 20, fill=1, stroke=0)
    elif tail == 'body_line':
        c.setFillColorRGB(0.6, 0.6, 0.6)
        c.rect(30, 200, 780, 0.8, fill=1, stroke=0)
    c.showPage()
    c.save()
    reader = PdfReader(buf)
    writer = PdfWriter()
    space = NameObject('/DeviceRGB')
    if icc:
        from PIL import ImageCms
        profile = DecodedStreamObject()
        profile.set_data(ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
        profile[NameObject('/N')] = NumberObject(3)
        space = ArrayObject([NameObject('/ICCBased'), writer._add_object(profile)])
    if tail == 'unknown_color':
        space = NameObject('/Unrecognized')
    for page in reader.pages:
        added = writer.add_page(page)
        added['/Resources'][NameObject('/ColorSpace')] = DictionaryObject({NameObject('/Cs1'): space})
    writer.write(path)
    return path

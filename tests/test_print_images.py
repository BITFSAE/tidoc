"""付款截图和实物图转 PDF：JPEG 原样嵌入，其他格式不再先编码成 PNG。"""

import pytest
from PIL import Image

import tidoc_print

pytestmark = pytest.mark.skipif(not tidoc_print.is_available(), reason="打印组件依赖未安装")


def _image_streams(pdf_path):
    """页面资源里的图片对象（含原始编码字节和滤镜），不经过解码。"""
    from pypdf import PdfReader

    streams = []
    for page in PdfReader(str(pdf_path)).pages:
        xobjects = (page.get("/Resources") or {}).get("/XObject") or {}
        for ref in xobjects.values():
            obj = ref.get_object()
            if obj.get("/Subtype") == "/Image":
                streams.append(obj)
    return streams


def test_jpeg_is_embedded_without_recompressing(tmp_path):
    from tidoc_print.pdf_merge import images_to_pdf

    source = tmp_path / "photo.jpg"
    Image.effect_noise((320, 240), 60).convert("RGB").save(source, quality=90)
    out = images_to_pdf([source], tmp_path / "out.pdf")

    [image] = _image_streams(out)
    assert list(image["/Filter"]) == ["/DCTDecode"]
    assert image._data == source.read_bytes()                     # 与原文件逐字节相同，没有重新压缩


def test_png_gray_rgba_and_cmyk_inputs_still_produce_pages(tmp_path):
    from tidoc_print.pdf_merge import images_to_pdf

    paths = []
    for name, mode, colour in [
        ("shot.png", "RGB", (10, 120, 200)),
        ("alpha.png", "RGBA", (200, 20, 20, 128)),
        ("gray.jpg", "L", 120),
        ("cmyk.jpg", "CMYK", (0, 80, 200, 20)),
        ("palette.gif", "P", 3),
    ]:
        path = tmp_path / name
        Image.new(mode, (200, 300), colour).save(path)
        paths.append(path)

    out = images_to_pdf(paths, tmp_path / "out.pdf", images_per_page=2)

    assert len(_image_streams(out)) == len(paths)
    assert b"ASCII85Decode" not in out.read_bytes()               # 不再多套一层 ASCII85


def test_image_pdf_stays_small_for_large_screenshots(tmp_path):
    from tidoc_print.pdf_merge import images_to_pdf

    source = tmp_path / "shot.png"
    Image.new("RGB", (1290, 2796), (255, 255, 255)).save(source)
    out = images_to_pdf([source], tmp_path / "out.pdf")
    assert out.stat().st_size < 200_000


def test_jpeg_variants_keep_their_real_size_in_the_pdf(tmp_path):
    from tidoc_print.pdf_merge import images_to_pdf

    for name, options in [("progressive.jpg", {"progressive": True}), ("optimized.jpg", {"optimize": True}),
                          ("full-chroma.jpg", {"subsampling": 0})]:
        source = tmp_path / name
        Image.effect_noise((200, 300), 60).convert("RGB").save(source, **options)
        [image] = _image_streams(images_to_pdf([source], tmp_path / (name + ".pdf")))
        assert (image["/Width"], image["/Height"]) == (200, 300)          # 不是 reportlab 的占位小图


def test_unreadable_jpeg_headers_fall_back_to_conversion(tmp_path, monkeypatch):
    from tidoc_print import pdf_merge

    source = tmp_path / "odd.jpg"
    Image.effect_noise((200, 300), 60).convert("RGB").save(source)
    monkeypatch.setattr(pdf_merge, "_jpeg_embeddable", lambda path: False)
    [image] = _image_streams(pdf_merge.images_to_pdf([source], tmp_path / "out.pdf"))
    assert list(image["/Filter"]) == ["/FlateDecode"]
    assert (image["/Width"], image["/Height"]) == (200, 300)

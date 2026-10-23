"""二维码取票：解码、链接校验、联网规则与错误分类。

全部使用虚构数据：二维码由 zxing-cpp 现生成，发票号码、时间和校验码都是编造的；联网部分用假的
opener 替换，测试不会真的访问网络。
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
import urllib.parse
import zipfile
from pathlib import Path

import pytest

zxingcpp = pytest.importorskip("zxingcpp")
Image = pytest.importorskip("PIL.Image")

from tidoc.services import qr_fetch as qr  # noqa: E402

HOST = "dppt.fictional.chinatax.gov.cn"
INVOICE_NO = "12345678901234567890"
ISSUED = "20200102030405"
CS = f"2_{INVOICE_NO}_{ISSUED}0001AAB12"       # 校验串 0001AAB12，末 4 位 AB12
LINK_URL = f"https://{HOST}:8443/v/{CS}"


# ----------------------------------------------------------------------------- 夹具

def make_qr_image(text: str, scale: int = 6):
    barcode = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode)
    raw = zxingcpp.write_barcode_to_image(barcode, scale=scale)
    height, width = raw.shape[:2]
    return Image.frombytes("L", (width, height), bytes(raw))


def to_bytes(image, fmt="PNG", **kwargs) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def noisy_page(width, height, seed=7):
    """带文字般杂乱纹理的大图，模拟手机截图的背景。"""
    import random

    rng = random.Random(seed)
    page = Image.new("L", (width, height), 235)
    pixels = page.load()
    for _ in range(width * height // 40):
        x, y = rng.randrange(width), rng.randrange(height)
        pixels[x, y] = rng.randrange(60, 200)
    return page


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None, chunk_limit=None):
        self.status = status
        self.headers = email.message.Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self._stream = io.BytesIO(body)
        self.body = body

    def read(self, size=-1):
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """记录每次请求；handler(request) 返回 FakeResponse 或要抛出的异常。"""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        result = self.handler(request)
        if isinstance(result, BaseException):
            raise result
        if result.status >= 300:
            raise urllib.error.HTTPError(request.full_url, result.status, "x", result.headers, io.BytesIO(result.body))
        return result


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """默认任何联网都会失败；需要联网的用例自己换成 FakeOpener。"""
    def blocked():
        raise AssertionError("test attempted to open a real network connection")

    monkeypatch.setattr(qr, "_build_opener", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: blocked())


def install(monkeypatch, handler) -> FakeOpener:
    opener = FakeOpener(handler)
    monkeypatch.setattr(qr, "_build_opener", lambda: opener)
    return opener


def make_xml(invoice_no=INVOICE_NO, with_id=True) -> bytes:
    ident = f"<EIid>{invoice_no}</EIid>" if with_id else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><EInvoice><Header>{ident}</Header></EInvoice>'.encode()


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


PDF_BYTES = b"%PDF-1.4\n% fictional\n%%EOF\n"


def platform_handler(xml=None, pdf=PDF_BYTES, query=None):
    """模拟平台：导出接口按 Wjgs 返回 XML(ZIP) 或 PDF，查询接口返回 query（dict）。"""
    xml_zip = make_zip({"dzfp_fictional.xml": make_xml()}) if xml is None else xml

    def handler(request):
        parts = urllib.parse.urlsplit(request.full_url)
        if parts.path.endswith("/exportDzfpwjEwm"):
            fmt = urllib.parse.parse_qs(parts.query)["Wjgs"][0]
            if fmt == "XML":
                return FakeResponse(200, xml_zip, {"Content-Type": "application/zip;charset=utf-8"})
            return FakeResponse(200, pdf, {"Content-Type": "application/pdf;charset=utf-8"})
        if parts.path.endswith("/queryFpjcxxb"):
            body = json.dumps(query if query is not None else {"code": "0", "message": "bad"}).encode()
            return FakeResponse(200, body, {"Content-Type": "application/json;charset=UTF-8"})
        raise AssertionError(f"unexpected request: {request.full_url}")

    return handler


def link() -> qr.InvoiceLink:
    return qr.parse_invoice_link(LINK_URL)


# ----------------------------------------------------------------------------- 1. 解码

def test_decodes_plain_qr_png():
    texts = qr.decode_qr_texts(to_bytes(make_qr_image(LINK_URL)))
    assert texts == [LINK_URL]


def test_decodes_jpeg_and_transparent_png_and_rgb():
    image = make_qr_image(LINK_URL, scale=5)
    assert qr.decode_qr_texts(to_bytes(image, "JPEG", quality=60)) == [LINK_URL]
    rgba = Image.new("RGBA", image.size, (0, 0, 0, 0))     # 透明底，二维码暗块不透明
    dark = image.point(lambda v: 255 if v < 128 else 0)
    rgba.paste((0, 0, 0, 255), mask=dark)
    assert qr.decode_qr_texts(to_bytes(rgba)) == [LINK_URL]
    assert qr.decode_qr_texts(to_bytes(image.convert("RGB"), "BMP")) == [LINK_URL]


def test_decodes_small_qr_in_corner_of_large_noisy_screenshot():
    page = noisy_page(1260, 2800)
    code = make_qr_image(LINK_URL, scale=2)
    page.paste(code, (1260 - code.width - 30, 2800 - code.height - 40))
    texts = qr.decode_qr_texts(to_bytes(page, "JPEG", quality=70), accept=lambda t: t == LINK_URL)
    assert LINK_URL in texts


def test_decodes_rotated_low_contrast_and_tiny_qr():
    base = make_qr_image(LINK_URL, scale=4)
    assert qr.decode_qr_texts(to_bytes(base.rotate(90, expand=True))) == [LINK_URL]
    faint = base.point(lambda v: 120 + v // 4)             # 灰度范围压缩到 120..183
    assert LINK_URL in qr.decode_qr_texts(to_bytes(faint))
    tiny = make_qr_image(LINK_URL, scale=1)                # 每个模块 1 像素，需放大再试
    assert LINK_URL in qr.decode_qr_texts(to_bytes(tiny), accept=lambda t: t == LINK_URL)


def test_picks_invoice_link_among_several_qr_codes():
    payment = make_qr_image("https://example.invalid/pay/123", scale=4)
    ticket = make_qr_image(LINK_URL, scale=4)
    canvas = Image.new("L", (payment.width + ticket.width + 60, max(payment.height, ticket.height) + 40), 255)
    canvas.paste(payment, (20, 20))
    canvas.paste(ticket, (payment.width + 40, 20))
    assert qr.find_invoice_link(to_bytes(canvas)).invoice_no == INVOICE_NO


def test_image_without_qr_code_is_reported():
    with pytest.raises(qr.QrFetchError) as caught:
        qr.find_invoice_link(to_bytes(noisy_page(400, 400)))
    assert caught.value.code == "no_qr" and caught.value.stage == "decode"


@pytest.mark.parametrize("data", [b"", b"not an image", b"\x89PNG\r\n\x1a\n broken"])
def test_unreadable_images_are_reported(data):
    with pytest.raises(qr.QrFetchError) as caught:
        qr.find_invoice_link(data)
    assert caught.value.code == "image_unreadable"


def test_image_with_too_many_pixels_is_rejected(monkeypatch):
    monkeypatch.setattr(qr, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(qr.QrFetchError) as caught:
        qr.find_invoice_link(to_bytes(make_qr_image(LINK_URL)))
    assert caught.value.code == "image_unreadable"


def test_oversized_image_file_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(qr, "MAX_IMAGE_BYTES", 10)
    path = tmp_path / "big.png"
    path.write_bytes(b"x" * 11)
    with pytest.raises(qr.QrFetchError) as caught:
        qr.read_image_file(path)
    assert caught.value.code == "image_unreadable"
    with pytest.raises(qr.QrFetchError):
        qr.read_image_file(tmp_path / "missing.png")


HEIC_HEADER = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1"


def test_heic_without_converter_gets_a_specific_error(monkeypatch):
    monkeypatch.setattr(qr, "_convert_heic", lambda data: None)
    with pytest.raises(qr.QrFetchError) as caught:
        qr.find_invoice_link(HEIC_HEADER + b"payload")
    assert caught.value.code == "image_unsupported"


def test_heic_uses_converter_when_available(monkeypatch):
    png = to_bytes(make_qr_image(LINK_URL))
    monkeypatch.setattr(qr, "_convert_heic", lambda data: png)
    assert qr.find_invoice_link(HEIC_HEADER + b"payload").invoice_no == INVOICE_NO


def test_missing_decoder_is_reported(monkeypatch):
    def fail():
        raise qr.QrFetchError("decoder_unavailable", "missing")

    monkeypatch.setattr(qr, "_load_decoder", fail)
    with pytest.raises(qr.QrFetchError) as caught:
        qr.find_invoice_link(b"anything")
    assert caught.value.code == "decoder_unavailable"


# ----------------------------------------------------------------------------- 2. 链接校验

def test_parses_ticket_link_forms():
    parsed = qr.parse_invoice_link(LINK_URL)
    assert (parsed.host, parsed.port, parsed.invoice_no, parsed.issued_at, parsed.check_code) == (
        HOST, 8443, INVOICE_NO, ISSUED, "AB12")
    assert parsed.delivery_url == LINK_URL
    # 默认端口、?cs= 写法、校验串长度 4~40 都可以。
    assert qr.parse_invoice_link(f"https://{HOST}/v/{CS}").port == 443
    assert qr.parse_invoice_link(f"https://{HOST}:8443/qrcode?cs={CS}&jrxt=ABCDE").check_code == "AB12"
    assert qr.parse_invoice_link(f"https://{HOST}:8443/v/2_{INVOICE_NO}_{ISSUED}ZZZZ").check_code == "ZZZZ"
    assert qr.parse_invoice_link(f"  {LINK_URL}\n").invoice_no == INVOICE_NO


@pytest.mark.parametrize("text", [
    "https://example.invalid/v/" + CS,                               # 非白名单域名
    f"https://{HOST}.evil.example/v/{CS}",                           # 白名单域名做前缀
    f"https://evil.example/{HOST}/v/{CS}",
    f"https://notchinatax.gov.cn/v/{CS}",
    f"https://dppt.fictional.chinatax.gov.cn.cn/v/{CS}",
    f"https://{HOST}@evil.example/v/{CS}",                           # 账号密码写法
    f"https://evil.example@{HOST}/v/{CS}",
    f"http://{HOST}:8443/v/{CS}",                                    # 明文 http 不接受
    f"ftp://{HOST}/v/{CS}",
    f"https://{HOST}:9443/v/{CS}",                                   # 非允许端口
    f"https://{HOST}:80/v/{CS}",
    f"https://{HOST}:8443/other/{CS}",                               # 不是取票路径
    f"https://{HOST}:8443/",
    "weixin://wxpay/bizpayurl?pr=abc",
    "just some text",
    "",
])
def test_non_ticket_links_are_rejected(text):
    with pytest.raises(qr.QrFetchError) as caught:
        qr.parse_invoice_link(text)
    assert caught.value.code == "not_invoice_link" and caught.value.stage == "link"


def test_invoice_face_qr_is_recognised():
    with pytest.raises(qr.QrFetchError) as caught:
        qr.parse_invoice_link("01,32,,12345678,100.00,20200102,12345678901234567890,ABCD,")
    assert caught.value.code == "invoice_face_qr"


@pytest.mark.parametrize("path", [
    "/v/3_" + INVOICE_NO + "_" + ISSUED + "AAAA",        # 版本号变化
    "/v/2_" + INVOICE_NO[:-1] + "_" + ISSUED + "AAAA",   # 号码位数变化
    "/v/2_" + INVOICE_NO + "_" + ISSUED[:-1] + "AAAA",
    "/v/2_" + INVOICE_NO + "_" + ISSUED + "AB",           # 校验串过短
    "/v/2_" + INVOICE_NO + "_20201399990000AAAA",         # 日期不合法
])
def test_ticket_token_layout_change_is_reported_as_platform_change(path):
    with pytest.raises(qr.QrFetchError) as caught:
        qr.parse_invoice_link(f"https://{HOST}:8443{path}")
    assert caught.value.code == "platform_changed"


def test_find_invoice_link_prefers_the_most_informative_error(monkeypatch):
    other = make_qr_image("https://example.invalid/x", scale=4)
    assert pytest.raises(qr.QrFetchError, qr.find_invoice_link, to_bytes(other)).value.code == "not_invoice_link"
    face = make_qr_image("01,32,,1,2,3,4,5,", scale=4)
    assert pytest.raises(qr.QrFetchError, qr.find_invoice_link, to_bytes(face)).value.code == "invoice_face_qr"


# ----------------------------------------------------------------------------- 3. 联网下载

def test_success_downloads_xml_then_pdf_with_minimal_honest_requests(monkeypatch):
    opener = install(monkeypatch, platform_handler())
    result = qr.fetch_invoice_files(link())

    assert [f.kind for f in result.files] == ["xml", "pdf"]
    assert [f.filename for f in result.files] == [f"{INVOICE_NO}.xml", f"{INVOICE_NO}.pdf"]
    assert result.files[0].data == make_xml() and result.files[1].data == PDF_BYTES
    assert result.warnings == [] and result.host == HOST and result.invoice_no == INVOICE_NO

    # 只请求两次导出接口：不查询、不写下载记录、不带浏览器身份头。
    assert len(opener.requests) == 2
    for request in opener.requests:
        parts = urllib.parse.urlsplit(request.full_url)
        assert request.get_method() == "GET" and request.data is None
        assert (parts.scheme, parts.hostname, parts.port) == ("https", HOST, 8443)
        assert parts.path == "/kpfw/fpjfzz/v1/exportDzfpwjEwm"
        headers = {k.lower(): v for k, v in request.header_items()}
        assert headers["user-agent"].startswith("tidoc/")
        assert not {"origin", "referer", "cookie", "authorization"} & set(headers)
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(opener.requests[0].full_url).query)
    assert params["Wjgs"] == ["XML"] and params["Fphm"] == [INVOICE_NO]
    assert params["Kprq"] == [ISSUED] and params["Jym"] == ["AB12"]


def test_raw_xml_without_zip_is_accepted(monkeypatch):
    install(monkeypatch, platform_handler(xml=make_xml()))
    assert qr.fetch_invoice_files(link()).files[0].data == make_xml()


def test_only_whitelisted_hosts_are_requested_even_for_odd_links(monkeypatch):
    install(monkeypatch, platform_handler())
    tampered = qr.InvoiceLink("example.invalid", 443, INVOICE_NO, ISSUED, "AB12", CS)
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(tampered)
    assert caught.value.code == "untrusted_redirect"


@pytest.mark.parametrize("location, expected", [
    ("https://example.invalid/file", "untrusted_redirect"),                       # 跳到别的主机
    (f"http://{HOST}:8443/kpfw/fpjfzz/v1/exportDzfpwjEwm", "untrusted_redirect"),  # https 降到 http
    (f"https://{HOST}:9443/x", "untrusted_redirect"),                              # 非允许端口
    (f"https://{HOST}@example.invalid/x", "untrusted_redirect"),
    ("https://evil.example/" + HOST, "untrusted_redirect"),
])
def test_redirects_leaving_the_allowlist_are_refused(monkeypatch, location, expected):
    opener = install(monkeypatch, lambda request: FakeResponse(302, b"", {"Location": location}))
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == expected
    assert len(opener.requests) == 1          # 没有对非白名单地址发出请求


def test_redirect_inside_the_allowlist_is_followed_and_limited(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.full_url)
        if len(calls) == 1:
            query = urllib.parse.urlsplit(request.full_url).query
            return FakeResponse(302, b"", {"Location": f"/kpfw/fpjfzz/v1/exportDzfpwjEwm?{query}"})
        return platform_handler()(request)

    install(monkeypatch, handler)
    assert qr.fetch_invoice_files(link()).files
    assert calls[1] == calls[0]                       # 相对地址在同一主机上继续请求

    install(monkeypatch, lambda request: FakeResponse(302, b"", {"Location": "/again"}))
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == "platform_changed"

    install(monkeypatch, lambda request: FakeResponse(302, b"", {}))     # 没有 Location
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == "platform_changed"


def test_oversized_downloads_are_refused(monkeypatch):
    monkeypatch.setattr(qr, "MAX_DOWNLOAD_BYTES", 1000)
    # 声明长度超限：不读取正文。
    install(monkeypatch, lambda r: FakeResponse(200, b"x" * 10, {"Content-Type": "application/pdf", "Content-Length": "5000"}))
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == "too_large" and caught.value.stage == "content"
    # 没有声明长度，边读边计数。
    install(monkeypatch, lambda r: FakeResponse(200, b"%PDF-" + b"x" * 5000, {"Content-Type": "application/pdf"}))
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == "too_large"


def test_download_that_stalls_past_the_total_deadline_is_a_network_error(monkeypatch):
    monkeypatch.setattr(qr, "TOTAL_TIMEOUT", -1)
    install(monkeypatch, lambda r: FakeResponse(200, b"%PDF-" + b"x" * 100, {"Content-Type": "application/pdf"}))
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == "network"


@pytest.mark.parametrize("status, code", [
    (412, "blocked"), (403, "blocked"), (401, "blocked"),
    (429, "rate_limited"),
    (404, "platform_changed"), (405, "platform_changed"),
    (500, "network"), (503, "network"),
])
def test_http_failures_are_classified(monkeypatch, status, code):
    install(monkeypatch, lambda r: FakeResponse(status, b"<html>no</html>", {"Content-Type": "text/html"}))
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == code


@pytest.mark.parametrize("failure", [
    urllib.error.URLError("dns failure"),
    TimeoutError("timed out"),
    ConnectionResetError("reset"),
])
def test_connection_failures_are_network_errors(monkeypatch, failure):
    install(monkeypatch, lambda r: failure)
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == "network" and caught.value.stage == "download"


def test_non_pdf_xml_content_is_refused(monkeypatch):
    # 内容类型是 PDF，但字节不是。
    install(monkeypatch, platform_handler(pdf=b"GIF89a not a pdf"))
    result = qr.fetch_invoice_files(link())
    assert [f.kind for f in result.files] == ["xml"]
    assert any("PDF" in w for w in result.warnings)

    # 两种格式都拿不到合格内容：报 bad_content。
    install(monkeypatch, lambda r: FakeResponse(200, b"MZ\x90\x00 executable", {"Content-Type": "application/octet-stream"}))
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == "bad_content" and caught.value.stage == "content"

    # 内容类型不在允许范围内（例如图片、脚本）。
    install(monkeypatch, lambda r: FakeResponse(200, b"%PDF-1.4", {"Content-Type": "image/png"}))
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == "bad_content"

    # 缺少内容类型同样拒绝。
    install(monkeypatch, lambda r: FakeResponse(200, b"%PDF-1.4"))
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == "bad_content"


def test_html_or_json_instead_of_a_file_means_the_platform_changed(monkeypatch):
    install(monkeypatch, lambda r: FakeResponse(200, b"<!DOCTYPE html><html></html>", {"Content-Type": "text/html"}))
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == "platform_changed"


@pytest.mark.parametrize("entries, expected", [
    ({}, "bad_content"),                                                          # 空压缩包
    ({"readme.txt": b"hello"}, "bad_content"),                                    # 没有 XML
    ({"a.xml": b"<root/>" + b" " * (qr.MAX_XML_BYTES + 10)}, "bad_content"),      # 解压后过大
    ({"a.xml": b'<!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'}, "bad_content"),  # 声明实体
    ({"a.xml": b"<unclosed>"}, "bad_content"),                                    # 不是合法 XML
    ({"a.xml": b"plain text"}, "bad_content"),
    ({"a.xml": make_xml("99999999999999999999")}, "bad_content"),                 # 号码与二维码不符
])
def test_xml_archives_are_validated(monkeypatch, entries, expected):
    install(monkeypatch, platform_handler(xml=make_zip(entries)))
    result = None
    try:
        result = qr.fetch_invoice_files(link())
    except qr.QrFetchError as exc:
        assert exc.code == expected
    else:
        # XML 被丢弃，但 PDF 仍可取回；此时必须有提示，且不含该 XML。
        assert [f.kind for f in result.files] == ["pdf"]
        assert any("XML" in w for w in result.warnings)


def test_corrupt_and_encrypted_zip_members_are_refused(monkeypatch):
    install(monkeypatch, platform_handler(xml=b"PK\x03\x04 truncated garbage"))
    result = qr.fetch_invoice_files(link())
    assert [f.kind for f in result.files] == ["pdf"]

    archive = make_zip({"a.xml": make_xml()})
    flagged = bytearray(archive)
    flagged[6] |= 0x1                         # 本地头加密标志
    central = flagged.rfind(b"PK\x01\x02")
    flagged[central + 8] |= 0x1               # 中央目录加密标志
    install(monkeypatch, platform_handler(xml=bytes(flagged)))
    assert [f.kind for f in qr.fetch_invoice_files(link()).files] == ["pdf"]


def test_xml_without_invoice_number_field_is_accepted_with_a_warning(monkeypatch):
    install(monkeypatch, platform_handler(xml=make_zip({"a.xml": make_xml(with_id=False)})))
    result = qr.fetch_invoice_files(link())
    assert [f.kind for f in result.files] == ["xml", "pdf"]
    assert any("核对" in w for w in result.warnings)


def test_missing_pdf_still_returns_xml_with_a_warning(monkeypatch):
    def handler(request):
        if "Wjgs=PDF" in request.full_url:
            return FakeResponse(200, b"<html></html>", {"Content-Type": "application/pdf"})
        return platform_handler()(request)

    install(monkeypatch, handler)
    result = qr.fetch_invoice_files(link())
    assert [f.kind for f in result.files] == ["xml"]
    assert any("PDF" in w for w in result.warnings)


def test_empty_response_means_invalid_link_after_the_platform_agrees(monkeypatch):
    def handler(request):
        if request.full_url.endswith("exportDzfpwjEwm") or "exportDzfpwjEwm" in request.full_url:
            return FakeResponse(200, b"", {})
        return platform_handler(query={"code": "0", "message": "请求参数异常，查询失败！"})(request)

    opener = install(monkeypatch, handler)
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == "link_invalid" and caught.value.stage == "download"
    queries = [r for r in opener.requests if "queryFpjcxxb" in r.full_url]
    assert len(queries) == 1 and queries[0].get_method() == "POST"
    assert json.loads(queries[0].data) == {"Cs": CS}


def test_empty_response_without_a_usable_query_is_still_invalid_link(monkeypatch):
    def handler(request):
        if "exportDzfpwjEwm" in request.full_url:
            return FakeResponse(200, b"", {})
        return FakeResponse(500, b"oops", {"Content-Type": "text/plain"})

    install(monkeypatch, handler)
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == "link_invalid"


def test_valid_ticket_but_empty_download_means_the_download_interface_changed(monkeypatch):
    def handler(request):
        if "exportDzfpwjEwm" in request.full_url:
            return FakeResponse(200, b"", {})
        return platform_handler(query={"code": "1", "fphm": INVOICE_NO, "kprq": ISSUED, "jym": "AB12"})(request)

    install(monkeypatch, handler)
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == "platform_changed"


def test_platform_supplied_parameters_are_used_for_a_retry(monkeypatch):
    seen = []

    def handler(request):
        if "exportDzfpwjEwm" in request.full_url:
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            seen.append((params["Jym"][0], params["Kprq"][0]))
            if params["Jym"][0] != "ZZ99":
                return FakeResponse(200, b"", {})
            return platform_handler()(request)
        return platform_handler(query={"code": "1", "fphm": INVOICE_NO, "kprq": "2020-01-02 03:04:05", "jym": "ZZ99"})(request)

    install(monkeypatch, handler)
    result = qr.fetch_invoice_files(link())
    assert [f.kind for f in result.files] == ["xml", "pdf"]
    assert seen[0] == ("AB12", ISSUED) and seen[1] == ("ZZ99", ISSUED)
    assert seen[2][0] == "ZZ99"               # PDF 沿用查询得到的参数，不再重复查询


def test_query_for_another_invoice_is_not_trusted(monkeypatch):
    def handler(request):
        if "exportDzfpwjEwm" in request.full_url:
            return FakeResponse(200, b"", {})
        return platform_handler(query={"code": "1", "fphm": "99999999999999999999", "kprq": ISSUED, "jym": "AB12"})(request)

    install(monkeypatch, handler)
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == "platform_changed"


@pytest.mark.parametrize("message, code", [
    ("请输入验证码", "blocked"),
    ("操作过于频繁，请稍后再试", "rate_limited"),
])
def test_captcha_and_rate_limit_messages_are_reported_not_bypassed(monkeypatch, message, code):
    def handler(request):
        if "exportDzfpwjEwm" in request.full_url:
            return FakeResponse(200, b"", {})
        return platform_handler(query={"code": "0", "message": message})(request)

    opener = install(monkeypatch, handler)
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == code
    # 报错后不再继续请求任何接口。
    assert all("saveDzfpxzjlxx" not in r.full_url for r in opener.requests)
    assert len([r for r in opener.requests if "exportDzfpwjEwm" in r.full_url]) == 1


def test_query_blocked_by_the_platform_is_reported(monkeypatch):
    def handler(request):
        if "exportDzfpwjEwm" in request.full_url:
            return FakeResponse(200, b"", {})
        return FakeResponse(412, b"<script></script>", {"Content-Type": "text/html"})

    install(monkeypatch, handler)
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == "blocked"


def test_end_to_end_from_image_bytes(monkeypatch):
    install(monkeypatch, platform_handler())
    result = qr.fetch_invoice_from_image(to_bytes(make_qr_image(LINK_URL)))
    assert result.invoice_no == INVOICE_NO and len(result.files) == 2


# ----------------------------------------------------------------------------- 证书兜底（macOS）

def _cert_error():
    import ssl

    return urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))


def test_certificate_failure_falls_back_to_system_curl_on_macos(monkeypatch):
    opener = install(monkeypatch, lambda r: _cert_error())
    monkeypatch.setattr(qr.sys, "platform", "darwin")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        head = Path(command[command.index("-D") + 1])
        out = Path(command[command.index("-o") + 1])
        parsed = urllib.parse.urlsplit(command[-1])
        if parsed.path.endswith("exportDzfpwjEwm"):
            fmt = urllib.parse.parse_qs(parsed.query)["Wjgs"][0]
            body, ctype = (make_zip({"a.xml": make_xml()}), "application/zip") if fmt == "XML" else (PDF_BYTES, "application/pdf")
        head.write_bytes(f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\n\r\n".encode())
        out.write_bytes(body)
        return type("Done", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr(qr.subprocess, "run", fake_run)
    result = qr.fetch_invoice_files(link())
    assert [f.kind for f in result.files] == ["xml", "pdf"]
    assert len(opener.requests) == 2
    for command in commands:
        assert "--max-redirs" in command and command[command.index("--max-redirs") + 1] == "0"
        assert "--proto" in command and "=https" in command
        assert "--location" not in command and "-L" not in command


def test_certificate_failure_elsewhere_is_a_network_error(monkeypatch):
    install(monkeypatch, lambda r: _cert_error())
    monkeypatch.setattr(qr.sys, "platform", "win32")
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    assert caught.value.code == "network"


# ----------------------------------------------------------------------------- 错误清单

def test_every_error_code_has_message_and_stage():
    assert set(qr.ERROR_MESSAGES) == set(qr.ERROR_STAGES)
    assert {s for s in qr.ERROR_STAGES.values()} == {"decode", "link", "download", "content"}
    for code, message in qr.ERROR_MESSAGES.items():
        error = qr.QrFetchError(code, "detail")
        assert str(error) == message and error.code == code and error.detail == "detail"
        assert "http" not in message.lower()           # 提示文案里不带链接


def test_user_visible_errors_never_contain_the_ticket_link(monkeypatch):
    install(monkeypatch, lambda r: FakeResponse(200, b"", {}))
    with pytest.raises(qr.QrFetchError) as caught:
        qr.fetch_invoice_files(link())
    for text in (str(caught.value), caught.value.detail):
        assert CS not in text and INVOICE_NO not in text


# ----------------------------------------------------------------------------- API

def test_api_stages_files_for_the_existing_import_flow(api, tmp_path, monkeypatch):
    install(monkeypatch, platform_handler())
    image = tmp_path / "qr.png"
    image.write_bytes(to_bytes(make_qr_image(LINK_URL)))

    reply = api.fetch_invoice_by_qr(str(image))

    assert reply["ok"] is True
    data = reply["data"]
    assert data["host"] == HOST and data["invoice_no"] == INVOICE_NO and data["warnings"] == []
    assert [f["kind"] for f in data["files"]] == ["xml", "pdf"]
    for path in data["paths"]:
        assert Path(path).is_file() and api.data_root.dropped_dir in Path(path).parents
    assert Path(data["paths"][0]).read_bytes() == make_xml()

    # 暂存文件可以直接走现有的批量导入预览，也能用现有的清理方法删掉。
    preview = api.scan_files(data["paths"])["data"]
    assert preview["total_files"] == 2
    assert api.cleanup_dropped_files(data["paths"])["data"]["deleted"] == 2


def test_api_preview_only_never_touches_the_network(api, tmp_path):
    image = tmp_path / "qr.png"
    image.write_bytes(to_bytes(make_qr_image(LINK_URL)))
    reply = api.fetch_invoice_by_qr(str(image), {"preview_only": True})       # autouse 夹具会让联网直接报错
    assert reply == {"ok": True, "data": {"preview_only": True, "host": HOST, "invoice_no": INVOICE_NO}}


def test_api_reports_staged_errors_with_a_browser_fallback(api, tmp_path, monkeypatch):
    install(monkeypatch, lambda r: FakeResponse(412, b"<script></script>", {"Content-Type": "text/html"}))
    image = tmp_path / "qr.png"
    image.write_bytes(to_bytes(make_qr_image(LINK_URL)))

    reply = api.fetch_invoice_by_qr(str(image))

    assert reply["ok"] is False and reply["code"] == "blocked" and reply["stage"] == "download"
    assert reply["error"] == qr.ERROR_MESSAGES["blocked"]
    assert reply["open_url"] == LINK_URL
    assert not any(api.data_root.dropped_dir.rglob("*.pdf"))                 # 失败时不留文件


@pytest.mark.parametrize("case, code", [
    ("no_qr", "no_qr"),
    ("not_an_image", "image_unreadable"),
    ("other_qr", "not_invoice_link"),
])
def test_api_errors_before_networking_have_no_browser_link(api, tmp_path, case, code):
    payloads = {
        "no_qr": lambda: to_bytes(noisy_page(300, 300)),
        "not_an_image": lambda: b"not an image",
        "other_qr": lambda: to_bytes(make_qr_image("https://example.invalid/not-a-ticket")),
    }
    image = tmp_path / "x.png"
    image.write_bytes(payloads[case]())
    reply = api.fetch_invoice_by_qr(str(image))
    assert reply["ok"] is False and reply["code"] == code and "open_url" not in reply


def test_api_handles_a_missing_file(api, tmp_path):
    reply = api.fetch_invoice_by_qr(str(tmp_path / "missing.png"))
    assert reply["ok"] is False and reply["code"] == "image_unreadable"


def test_api_method_is_exposed_to_the_bridge(api):
    assert "fetch_invoice_by_qr" in dir(api)

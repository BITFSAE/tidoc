"""二维码取票：识别商家给的取票二维码，从税务平台取回发票 PDF / XML。

流程分四步，每一步失败都会给出明确的错误码（见 ERROR_MESSAGES），用户随时可以回到手工导入：

1. decode  —— 从图片里解出二维码文本（zxing-cpp + Pillow，必要时放大、缩小、分块再试）。
2. link    —— 校验文本是不是国家税务总局电子发票服务平台的取票链接，并取出下载所需参数。
3. download—— 联网下载 XML（平台以 ZIP 包装）和 PDF。
4. content —— 校验下载内容确实是 PDF / XML，并用 XML 里的发票号码核对二维码，再交给调用方
              走现有导入流程。

联网规则（只在用户把二维码图片交给软件时发生，没有任何后台请求）：

- 只访问二维码里解出的、主机名属于 chinatax.gov.cn 的链接，且必须是 https，端口只允许 443 / 8443；
  二维码里的 http 链接直接拒绝，不会发出明文请求。
- 重定向每一跳都重新按同样规则检查，跳到白名单之外、或从 https 降到 http 一律拒绝。
- 每个请求有超时和总时限，单个响应上限 10 MB；只接受魔数和内容类型都是 PDF / XML 的内容；
  下载内容一律当作不可信数据，不执行、不展开到磁盘，ZIP 只在内存里读取其中的 XML。
- 请求只带诚实的 User-Agent（tidoc/版本号），不带 Cookie、Origin、Referer 等浏览器身份。
- 不向任何第三方服务发送数据；请求里只有二维码链接自带的发票号码、开票时间和校验码。
- 不绕过验证码、限流或浏览器校验；遇到时返回错误，让用户改用浏览器下载后手工导入。

下载地址的获得方式：税务平台的取票网页是一个需要执行脚本的单页应用，页面上的“下载”按钮最终
请求 ``/kpfw/fpjfzz/v1/exportDzfpwjEwm``，参数全部来自二维码链接本身，不需要先打开网页，也不需要
调用平台的“写入下载记录”接口（已用真实样本验证）。直接下载失败时才会只读查询
``/kpfw/fpjfzz/v1/queryFpjcxxb``，用它判断链接是否失效，并取得服务端记录的开票时间和校验码重试一次。
这两个接口都不是对外公布的正式接口，平台改版时可能失效，此时返回 ``platform_changed`` 并提示用户
改用浏览器下载。
"""

from __future__ import annotations

import io
import http.client
import socket
import threading
from contextvars import ContextVar
from contextlib import contextmanager, nullcontext
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

from .updater import USER_AGENT, _is_certificate_error

class FetchCancellation:
    """Cancel the next request and interrupt any registered socket or process."""

    def __init__(self):
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.abort = None

    def check(self):
        if self.event.is_set():
            raise QrFetchError("cancelled", "cancelled by user")

    def cancel(self):
        self.event.set()
        with self.lock:
            abort = self.abort
        if abort:
            try:
                abort()
            except OSError:
                pass  # The response may already have closed on the worker thread.

    @contextmanager
    def watching(self, abort):
        with self.lock:
            self.abort = abort
        try:
            if self.event.is_set():
                abort()
            self.check()
            yield
            self.check()
        finally:
            with self.lock:
                self.abort = None


_cancellation = ContextVar("qr_fetch_cancellation", default=None)


def _check_cancelled():
    token = _cancellation.get()
    if token:
        token.check()


def _abort_response(response):
    # Shutdown wakes a blocked read; close alone may wait for that read's lock.
    stream = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(stream, "_sock", None)
    if sock:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    response.close()


# ----------------------------------------------------------------------------- 限制

MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_IMAGE_PIXELS = 80_000_000
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_XML_BYTES = 5 * 1024 * 1024
MAX_QR_TEXT_CHARS = 2048
REQUEST_TIMEOUT = 15          # 单次连接 / 读取超时（秒）
TOTAL_TIMEOUT = 45            # 单个文件从请求到读完的总时限（秒）
DECODE_TIME_BUDGET = 8.0      # 解码尝试的总时限（秒）
MAX_REDIRECTS = 3
READ_CHUNK = 64 * 1024

# ----------------------------------------------------------------------------- 白名单

# 依据：真实取票二维码解出的链接都是 https://dppt.<地区>.chinatax.gov.cn:8443/v/<令牌>，
# 页面标题为“国家税务总局电子发票服务平台”；chinatax.gov.cn 属国家税务总局的 gov.cn 域名。
# 各地平台的子域名写法没有公开清单，因此放行 chinatax.gov.cn 的各级子域名（仍只限 https 和指定端口），
# 再靠令牌格式（2_<20 位发票号码>_<14 位时间>…）过滤掉其他页面的二维码。
_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)+chinatax\.gov\.cn$")
ALLOWED_PORTS = frozenset({443, 8443})
# 取票令牌 cs：2_<20 位发票号码>_<14 位开票时间><4~40 位校验串>；平台下载接口的 Jym 是校验串的末 4 位。
_CS_RE = re.compile(r"^2_(?P<invoice_no>\d{20})_(?P<issued>\d{14})(?P<check>[0-9A-Za-z]{4,40})$")
# 二维码里是 /v/<cs>，平台会再跳转到 /qrcode?cs=<cs>；两种写法都收。
_QUERY_FORM_PATHS = frozenset({"", "/", "/qrcode"})
_EXPORT_PATH = "/kpfw/fpjfzz/v1/exportDzfpwjEwm"
_QUERY_PATH = "/kpfw/fpjfzz/v1/queryFpjcxxb"
# 发票票面上的二维码是逗号分隔的发票信息，如 “01,32,...”，不含下载地址。
_FACE_QR_RE = re.compile(r"^\d{2},\d{2},")

_PDF_CONTENT_TYPES = frozenset({
    "application/pdf", "application/octet-stream", "binary/octet-stream",
    "application/x-download", "application/download",
})
_XML_CONTENT_TYPES = frozenset({
    "application/zip", "application/x-zip-compressed", "application/xml", "text/xml",
    "application/octet-stream", "binary/octet-stream",
    "application/x-download", "application/download",
})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# ----------------------------------------------------------------------------- 错误

# 文案先写中文；界面语言切换由前端按 code 翻译。
ERROR_MESSAGES: dict[str, str] = {
    "cancelled": "已取消取票。",
    "decoder_unavailable": "二维码识别组件不可用，请改为手动导入发票文件。",
    "image_unreadable": "无法读取这张图片，请换一张截图或照片再试。",
    "image_unsupported": "暂不支持这种图片格式（HEIC），请截图后再试，或转存为 JPG / PNG。",
    "no_qr": "图片里没有找到二维码，请换一张更清晰、包含完整二维码的图片。",
    "not_invoice_link": "这个二维码不是国家税务总局电子发票服务平台的取票链接，暂时不能自动取票。",
    "invoice_face_qr": "这是发票票面上的二维码，只含发票信息、不含下载地址。请扫描商家给的取票二维码，或直接导入发票文件。",
    "platform_changed": "税务平台的页面或接口可能已改版，软件无法获得下载地址。请用浏览器打开链接下载，再导入文件。",
    "link_invalid": "这个取票链接已失效，或链接中的发票信息有误，平台没有返回文件。请向商家重新索取二维码。",
    "blocked": "税务平台拒绝了软件的自动下载请求。请用浏览器打开链接下载，再把文件拖入软件。",
    "rate_limited": "税务平台提示访问过于频繁，请稍后再试。",
    "network": "网络连接失败或超时，请检查网络后重试。",
    "untrusted_redirect": "税务平台把请求转到了其他网站，软件已拒绝继续。请用浏览器打开链接下载，再导入文件。",
    "bad_content": "下载到的内容不是有效的 PDF 或 XML 文件，已丢弃。",
    "too_large": "下载内容超过大小上限（10 MB），已丢弃。",
}

ERROR_STAGES: dict[str, str] = {
    "cancelled": "download",
    "decoder_unavailable": "decode",
    "image_unreadable": "decode",
    "image_unsupported": "decode",
    "no_qr": "decode",
    "not_invoice_link": "link",
    "invoice_face_qr": "link",
    "platform_changed": "download",
    "link_invalid": "download",
    "blocked": "download",
    "rate_limited": "download",
    "network": "download",
    "untrusted_redirect": "download",
    "bad_content": "content",
    "too_large": "content",
}

# 这些错误发生时链接已经通过白名单校验，可以让用户改用浏览器打开继续。
BROWSER_FALLBACK_CODES = frozenset({
    "platform_changed", "link_invalid", "blocked", "rate_limited", "network",
    "untrusted_redirect", "bad_content", "too_large",
})


class QrFetchError(Exception):
    """分阶段的取票错误。``str(exc)`` 是给用户看的中文说明，``detail`` 只含技术细节（不含链接）。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.stage = ERROR_STAGES[code]
        self.message = ERROR_MESSAGES[code]
        self.detail = detail
        super().__init__(self.message)


# ----------------------------------------------------------------------------- 数据

@dataclass(frozen=True)
class InvoiceLink:
    """通过白名单校验的取票链接，以及下载所需的参数。"""

    host: str
    port: int
    invoice_no: str
    issued_at: str      # 开票时间 yyyyMMddHHmmss
    check_code: str     # 链接令牌末尾的校验码（平台下载接口要求的 Jym）
    cs: str = ""        # 完整令牌，用于向平台查询取票信息

    @property
    def url(self) -> str:
        return f"https://{self.host}:{self.port}"

    @property
    def delivery_url(self) -> str:
        """二维码里的取票链接（已通过校验），取票失败时可交给浏览器让用户手动下载。"""
        return f"{self.url}/v/{self.cs}" if self.cs else self.url


@dataclass
class FetchedFile:
    kind: str           # "xml" | "pdf"
    filename: str
    data: bytes


@dataclass
class QrFetchResult:
    host: str
    invoice_no: str
    files: list[FetchedFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Response:
    status: int
    headers: dict[str, str]   # 键为小写
    body: bytes


# ----------------------------------------------------------------------------- 1. 解码二维码

def _load_decoder():
    try:
        import zxingcpp
        from PIL import Image
    except ImportError as exc:  # 组件缺失时让用户回到手工导入
        raise QrFetchError("decoder_unavailable", str(exc)) from exc
    return zxingcpp, Image


def _is_heic(data: bytes) -> bool:
    return data[4:8] == b"ftyp" and data[8:12] in {
        b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1",
    }


def _convert_heic(data: bytes) -> bytes | None:
    """macOS 自带 sips 能读 HEIC；其他系统没有可靠的内置解码器，返回 None。"""
    if sys.platform != "darwin" or not Path("/usr/bin/sips").is_file():
        return None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "in.heic", Path(tmp) / "out.png"
            src.write_bytes(data)
            proc = subprocess.run(
                ["/usr/bin/sips", "-s", "format", "png", str(src), "--out", str(dst)],
                capture_output=True, timeout=30, check=False,
            )
            if proc.returncode != 0 or not dst.is_file():
                return None
            return dst.read_bytes()
    except (OSError, subprocess.SubprocessError):
        return None


def _open_image(data: bytes, Image):
    if len(data) > MAX_IMAGE_BYTES:
        raise QrFetchError("image_unreadable", "image file too large")
    if _is_heic(data):
        converted = _convert_heic(data)
        if converted is None:
            raise QrFetchError("image_unsupported", "heic")
        data = converted
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_IMAGE_PIXELS:
            raise QrFetchError("image_unreadable", "image has too many pixels")
        img.load()
    except QrFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 — Pillow 对坏图会抛多种异常
        raise QrFetchError("image_unreadable", type(exc).__name__) from exc
    return img


def _to_gray(img, Image):
    """转灰度；带透明通道的图先铺白底，避免透明区域变成黑色吞掉二维码。"""
    if img.mode in {"RGBA", "LA"} or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        base = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        base.alpha_composite(rgba)
        img = base
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:  # noqa: BLE001 — 方向信息损坏时按原图继续
        pass
    return img.convert("L")


def _candidate_images(gray, Image):
    """依次产生解码尝试用的图像：原图、增强对比度、缩放、分块。每个候选只在被需要时才生成。"""
    from PIL import ImageOps

    width, height = gray.size
    yield "original", gray
    yield "contrast", ImageOps.autocontrast(gray, cutoff=1)

    longest = max(width, height)
    shortest = min(width, height)
    for limit in (1600, 900):
        if longest > limit * 1.25:
            scale = limit / longest
            yield f"down{limit}", gray.resize(
                (max(1, round(width * scale)), max(1, round(height * scale))), Image.LANCZOS
            )
    if shortest < 500:
        yield "up2", gray.resize((width * 2, height * 2), Image.BICUBIC)
    if shortest < 250:
        yield "up3", gray.resize((width * 3, height * 3), Image.BICUBIC)

    # 手机截图里二维码可能只占一角：用重叠窗口逐块再找一遍。
    if shortest >= 300:
        for frac in (0.6, 0.4, 0.25):
            win_w, win_h = max(120, round(width * frac)), max(120, round(height * frac))
            step_x, step_y = max(1, win_w // 2), max(1, win_h // 2)
            xs = list(range(0, max(1, width - win_w) + 1, step_x))
            ys = list(range(0, max(1, height - win_h) + 1, step_y))
            if xs[-1] + win_w < width:
                xs.append(width - win_w)
            if ys[-1] + win_h < height:
                ys.append(height - win_h)
            for top in ys:
                for left in xs:
                    tile = gray.crop((left, top, min(width, left + win_w), min(height, top + win_h)))
                    if max(tile.size) < 700:
                        tile = tile.resize((tile.width * 2, tile.height * 2), Image.BICUBIC)
                    yield f"tile{frac}", tile


def _read_qr_texts(zxingcpp, image, binarizer=None) -> list[str]:
    kwargs = {"formats": zxingcpp.BarcodeFormat.QRCode}
    if binarizer is not None:
        kwargs["binarizer"] = binarizer
    try:
        results = zxingcpp.read_barcodes(image, **kwargs)
    except Exception:  # noqa: BLE001 — 单个候选失败不影响其他候选
        return []
    return [r.text for r in results if r.text and len(r.text) <= MAX_QR_TEXT_CHARS]


def decode_qr_texts(data: bytes, accept=None) -> list[str]:
    """从图片字节里解出二维码文本（可能有多个）。

    ``accept(text)`` 返回真时立即停止继续尝试，用于“找到取票链接就够了”；
    图片里同时有收款码等其他二维码时，也会继续找到取票链接为止。
    """
    zxingcpp, Image = _load_decoder()
    gray = _to_gray(_open_image(data, Image), Image)
    deadline = time.monotonic() + DECODE_TIME_BUDGET
    seen: list[str] = []

    def collect(texts: list[str]) -> bool:
        for text in texts:
            if text not in seen:
                seen.append(text)
        return bool(accept and any(accept(text) for text in texts))

    for _name, candidate in _candidate_images(gray, Image):
        if collect(_read_qr_texts(zxingcpp, candidate)):
            return seen
        if time.monotonic() > deadline:
            break
    # 对比度很低或光照不均时，换二值化方式再试几次原图。
    for binarizer in (zxingcpp.Binarizer.GlobalHistogram, zxingcpp.Binarizer.FixedThreshold):
        if time.monotonic() > deadline:
            break
        if collect(_read_qr_texts(zxingcpp, gray, binarizer)):
            return seen
    return seen


# ----------------------------------------------------------------------------- 2. 校验链接

def parse_invoice_link(text: str) -> InvoiceLink:
    """校验二维码文本是不是受支持的取票链接，失败抛 QrFetchError。"""
    text = (text or "").strip()
    if _FACE_QR_RE.match(text):
        raise QrFetchError("invoice_face_qr")
    try:
        parts = urllib.parse.urlsplit(text)
        hostname = (parts.hostname or "").lower()
        port = parts.port
    except ValueError as exc:
        raise QrFetchError("not_invoice_link", "unparsable url") from exc
    # 只收 https；带账号密码、非白名单主机或端口一律拒绝（http 链接不降级访问）。
    if parts.scheme.lower() != "https" or "@" in parts.netloc:
        raise QrFetchError("not_invoice_link", "scheme or userinfo")
    if not _HOST_RE.fullmatch(hostname):
        raise QrFetchError("not_invoice_link", "host not allowed")
    if port is None:
        port = 443
    if port not in ALLOWED_PORTS:
        raise QrFetchError("not_invoice_link", "port not allowed")

    cs = ""
    if parts.path.startswith("/v/"):
        cs = urllib.parse.unquote(parts.path[3:])
    elif parts.path in _QUERY_FORM_PATHS:
        values = urllib.parse.parse_qs(parts.query).get("cs", [])
        if len(values) == 1:
            cs = values[0]
    if not cs:
        raise QrFetchError("not_invoice_link", "no ticket token")
    match = _CS_RE.fullmatch(cs)
    if match is None:
        raise QrFetchError("platform_changed", "ticket token layout changed")
    try:
        datetime.strptime(match["issued"], "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise QrFetchError("platform_changed", "ticket token date invalid") from exc
    return InvoiceLink(
        host=hostname,
        port=port,
        invoice_no=match["invoice_no"],
        issued_at=match["issued"],
        check_code=match["check"][-4:],
        cs=cs,
    )


def find_invoice_link(data: bytes) -> InvoiceLink:
    """解码图片并返回第一个有效的取票链接；没有时抛出最能说明原因的错误。"""
    def is_link(text: str) -> bool:
        try:
            parse_invoice_link(text)
        except QrFetchError:
            return False
        return True

    texts = decode_qr_texts(data, accept=is_link)
    if not texts:
        raise QrFetchError("no_qr")
    # 都不是有效取票链接时，报最能说明原因的那个：白名单主机上的格式变化 > 票面二维码 > 不是取票链接。
    priority = {"platform_changed": 0, "invoice_face_qr": 1}
    errors: list[QrFetchError] = []
    for text in texts:
        try:
            return parse_invoice_link(text)
        except QrFetchError as exc:
            errors.append(exc)
    raise min(errors, key=lambda exc: priority.get(exc.code, 2))


# ----------------------------------------------------------------------------- 3. 联网下载

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自动跟随重定向；由 _http_request 逐跳检查白名单后自己跟随。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _build_opener():
    # build_opener 保留默认的 ProxyHandler，和更新检查一样沿用系统 / 环境变量里的代理设置。
    return urllib.request.build_opener(_NoRedirect)


def _require_allowed_url(url: str) -> None:
    """每次请求前（含重定向后的每一跳）重新检查：https、白名单主机、允许的端口。"""
    try:
        parts = urllib.parse.urlsplit(url)
        hostname = (parts.hostname or "").lower()
        port = parts.port or 443
    except ValueError as exc:
        raise QrFetchError("untrusted_redirect", "unparsable redirect") from exc
    if (
        parts.scheme.lower() != "https"
        or "@" in parts.netloc
        or not _HOST_RE.fullmatch(hostname)
        or port not in ALLOWED_PORTS
    ):
        raise QrFetchError("untrusted_redirect", "redirect target not allowed")


def _read_limited(resp, max_bytes: int, deadline: float) -> bytes:
    declared = resp.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise QrFetchError("too_large", "content-length")
    chunks: list[bytes] = []
    total = 0
    while True:
        _check_cancelled()
        try:
            chunk = resp.read(READ_CHUNK)
        except (OSError, ValueError, http.client.HTTPException):
            _check_cancelled()
            raise
        _check_cancelled()
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise QrFetchError("too_large", "stream")
        if time.monotonic() > deadline:
            raise QrFetchError("network", "total timeout")
        chunks.append(chunk)
    return b"".join(chunks)


def _lower_headers(headers) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in headers.items()} if headers else {}


def _urllib_once(url: str, body: bytes | None, max_bytes: int, deadline: float) -> _Response:
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        _check_cancelled()
        with _build_opener().open(request, timeout=REQUEST_TIMEOUT) as resp:
            token = _cancellation.get()
            if token:
                with token.watching(lambda: _abort_response(resp)):
                    return _Response(resp.status, _lower_headers(resp.headers), _read_limited(resp, max_bytes, deadline))
            return _Response(resp.status, _lower_headers(resp.headers), _read_limited(resp, max_bytes, deadline))
    except urllib.error.HTTPError as exc:
        token = _cancellation.get()
        try:
            with token.watching(lambda: _abort_response(exc)) if token else nullcontext():
                # 错误响应也可能读到一半停住，仍允许用户取消。
                _check_cancelled()
                try:
                    data = b"" if exc.code in _REDIRECT_STATUSES else exc.read(READ_CHUNK)
                except (OSError, ValueError, http.client.HTTPException):
                    _check_cancelled()
                    data = b""
                return _Response(exc.code, _lower_headers(exc.headers), data)
        finally:
            exc.close()


def _curl_once(url: str, body: bytes | None, max_bytes: int, deadline: float) -> _Response:
    """macOS 打包版可能缺根证书（和更新检查同样的问题），改用系统 curl 的信任链重试一次。
    不跟随重定向，重定向交给 _http_request 逐跳检查。"""
    curl = "/usr/bin/curl" if Path("/usr/bin/curl").exists() else "curl"
    total = max(5, int(deadline - time.monotonic()))
    with tempfile.TemporaryDirectory() as tmp:
        body_path, head_path = Path(tmp) / "body", Path(tmp) / "head"
        command = [
            curl, "--silent", "--show-error", "--proto", "=https", "--max-redirs", "0",
            "--max-time", str(total), "--max-filesize", str(max_bytes),
            "--user-agent", USER_AGENT, "-D", str(head_path), "-o", str(body_path),
        ]
        if body is not None:
            command += ["-H", "Content-Type: application/json", "--data-binary", body.decode("utf-8")]
        command.append(url)
        try:
            token = _cancellation.get()
            if token is None:
                proc = subprocess.run(command, capture_output=True, timeout=total + 5, check=False)
            else:
                with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
                    with token.watching(proc.kill):
                        proc.communicate(timeout=total + 5)
        except (OSError, subprocess.SubprocessError) as exc:
            raise QrFetchError("network", "curl failed") from exc
        if proc.returncode == 63:
            raise QrFetchError("too_large", "curl max-filesize")
        if proc.returncode != 0 or not head_path.is_file():
            raise QrFetchError("network", f"curl exit {proc.returncode}")
        head_text = head_path.read_text("utf-8", errors="replace")
        blocks = [b for b in re.split(r"\r?\n\r?\n", head_text) if b.strip().startswith("HTTP/")]
        if not blocks:
            raise QrFetchError("network", "curl no response head")
        lines = blocks[-1].strip().splitlines()
        try:
            status = int(lines[0].split()[1])
        except (IndexError, ValueError) as exc:
            raise QrFetchError("network", "curl bad status line") from exc
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        data = body_path.read_bytes() if body_path.is_file() else b""
        if len(data) > max_bytes:
            raise QrFetchError("too_large", "curl stream")
        return _Response(status, headers, data)


def _request_once(url: str, body: bytes | None, max_bytes: int, deadline: float) -> _Response:
    try:
        return _urllib_once(url, body, max_bytes, deadline)
    except QrFetchError:
        raise
    except (urllib.error.URLError, ssl.SSLError, OSError) as exc:
        _check_cancelled()
        if _is_certificate_error(exc):
            if sys.platform == "darwin":
                return _curl_once(url, body, max_bytes, deadline)
            raise QrFetchError("network", "certificate verification failed") from exc
        raise QrFetchError("network", type(exc).__name__) from exc
    except http.client.HTTPException as exc:
        _check_cancelled()
        raise QrFetchError("network", type(exc).__name__) from exc


def _http_request(url: str, body: bytes | None = None, max_bytes: int | None = None) -> _Response:
    """发出请求并手动跟随重定向，每一跳都重新校验白名单。``body`` 非空时为 JSON POST。"""
    deadline = time.monotonic() + TOTAL_TIMEOUT
    max_bytes = MAX_DOWNLOAD_BYTES if max_bytes is None else max_bytes
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        _check_cancelled()
        _require_allowed_url(current)
        response = _request_once(current, body, max_bytes, deadline)
        if response.status in _REDIRECT_STATUSES:
            location = response.headers.get("location", "")
            if not location:
                raise QrFetchError("platform_changed", "redirect without location")
            current = urllib.parse.urljoin(current, location)
            continue
        return response
    raise QrFetchError("platform_changed", "too many redirects")


def _export_url(link: InvoiceLink, file_format: str) -> str:
    now = str(int(time.time() * 1000))
    query = urllib.parse.urlencode({
        "Wjgs": file_format,
        "Jym": link.check_code,
        "Fphm": link.invoice_no,
        "Kprq": link.issued_at,
        "Czsj": now,
        "fileName": "",
        "timeStampId": now,
    })
    return f"{link.url}{_EXPORT_PATH}?{query}"


# ----------------------------------------------------------------------------- 4. 校验内容

def _content_type(response: _Response) -> str:
    return response.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _safe_platform_message(body: bytes) -> str:
    """平台有时在 200 里返回 JSON 说明原因；只取出一小段纯文本用于技术细节。"""
    try:
        payload = json.loads(body[:4096].decode("utf-8", errors="replace"))
    except ValueError:
        return ""
    message = payload.get("message") if isinstance(payload, dict) else ""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(message or "")).strip()
    return text[:80]


def _classify_http_failure(response: _Response) -> QrFetchError:
    status = response.status
    if status in {401, 403, 412}:
        return QrFetchError("blocked", f"http {status}")
    if status == 429:
        return QrFetchError("rate_limited", "http 429")
    if status >= 500:
        return QrFetchError("network", f"http {status}")
    return QrFetchError("platform_changed", f"http {status}")


def _classify_unexpected_body(response: _Response) -> QrFetchError:
    """200 但不是文件：空内容是链接失效；JSON / 网页说明接口行为变了。"""
    body = response.body
    if not body.strip():
        return QrFetchError("link_invalid", "empty response")
    head = body[:64].lstrip().lower()
    if head.startswith(b"{") or head.startswith(b"["):
        message = _safe_platform_message(body)
        return QrFetchError("link_invalid", f"json response {message}".strip())
    if head.startswith(b"<"):
        return QrFetchError("platform_changed", "html response")
    return QrFetchError("bad_content", "unrecognized content")


def _check_xml_bytes(data: bytes, invoice_no: str) -> tuple[bytes, bool]:
    """校验 XML 结构，并用其中的发票号码（EIid）核对二维码里的号码。

    返回 (xml, 是否核对过号码)。XML 里的号码与二维码不一致一律丢弃；XML 里没有该字段时无法核对，
    放行但由调用方给出提示。
    """
    stripped = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    if not stripped.startswith(b"<"):
        raise QrFetchError("bad_content", "xml magic")
    head = stripped[:8192].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise QrFetchError("bad_content", "xml declares doctype or entities")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise QrFetchError("bad_content", "xml not well-formed") from exc
    ids = [(node.text or "").strip() for node in root.iter() if node.tag.split("}")[-1] == "EIid"]
    ids = [value for value in ids if value]
    if ids and invoice_no not in ids:
        raise QrFetchError("bad_content", "invoice number mismatch")
    return data, bool(ids)


def _extract_xml(body: bytes, invoice_no: str) -> tuple[bytes, bool]:
    """平台把 XML 打在 ZIP 里；只在内存里读取其中的 XML，不按文件名写盘。"""
    if body[:2] != b"PK":
        return _check_xml_bytes(body, invoice_no)
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
        infos = [i for i in archive.infolist() if not i.is_dir() and i.filename.lower().endswith(".xml")]
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise QrFetchError("bad_content", "bad zip") from exc
    if len(archive.infolist()) > 20 or not infos:
        raise QrFetchError("bad_content", "zip layout")
    info = infos[0]
    if info.flag_bits & 0x1 or info.file_size > MAX_XML_BYTES:
        raise QrFetchError("bad_content", "zip entry encrypted or oversized")
    try:
        with archive.open(info) as member:
            data = member.read(MAX_XML_BYTES + 1)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError) as exc:
        raise QrFetchError("bad_content", "zip entry unreadable") from exc
    if len(data) > MAX_XML_BYTES:
        raise QrFetchError("bad_content", "zip entry oversized")
    return _check_xml_bytes(data, invoice_no)


def _download(link: InvoiceLink, kind: str) -> tuple[FetchedFile, bool]:
    """下载一种格式；返回文件和“发票号码是否已与二维码核对”。PDF 只检查魔数，不核对号码。"""
    file_format = "XML" if kind == "xml" else "PDF"
    response = _http_request(_export_url(link, file_format))
    if response.status != 200:
        raise _classify_http_failure(response)
    body = response.body
    ctype = _content_type(response)
    if not body.strip() or ctype in {"text/html", "application/json", "text/json", "text/plain"}:
        raise _classify_unexpected_body(response)
    if ctype not in (_XML_CONTENT_TYPES if kind == "xml" else _PDF_CONTENT_TYPES):
        raise QrFetchError("bad_content", "content-type")
    verified = False
    if kind == "pdf":
        if not body.startswith(b"%PDF-"):
            raise _classify_unexpected_body(response)
        data = body
    else:
        if body[:2] != b"PK" and not body.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
            raise _classify_unexpected_body(response)
        data, verified = _extract_xml(body, link.invoice_no)
    # 文件名只用链接里已通过格式校验的 20 位发票号码，不采用平台给的名字（含销售方名称，且不可信）。
    return FetchedFile(kind=kind, filename=f"{link.invoice_no}.{kind}", data=data), verified


_CAPTCHA_WORDS = ("验证码", "滑块", "人机")
_RATE_WORDS = ("频繁", "限流", "过多", "稍后再试")


def _refine_link(link: InvoiceLink) -> tuple[InvoiceLink, bool]:
    """向平台查询这张发票的下载参数（只读），并核对发票号码。返回 (链接, 平台是否确认链接有效)。

    只在直接下载失败时才调用：查询接口能明确告诉我们链接是否有效（code 不是 1 即无效），也能给出
    服务端记录的开票时间和校验码，用来应对二维码令牌格式变化。接口本身不可用（网络错误、非 JSON、
    非 200）时不阻断，返回原链接。遇到验证码或限流的提示时如实报错，不绕过。
    """
    if not link.cs:
        return link, False
    try:
        response = _http_request(
            f"{link.url}{_QUERY_PATH}", body=json.dumps({"Cs": link.cs}).encode("utf-8"), max_bytes=256 * 1024,
        )
    except QrFetchError as exc:
        if exc.code == "untrusted_redirect":
            raise
        return link, False
    if response.status in {401, 403, 412}:
        raise _classify_http_failure(response)
    if response.status == 429:
        raise QrFetchError("rate_limited", "http 429")
    if response.status != 200:
        return link, False
    try:
        payload = json.loads(response.body.decode("utf-8", errors="replace"))
    except ValueError:
        return link, False
    if not isinstance(payload, dict) or "code" not in payload:
        return link, False
    if str(payload.get("code")) != "1":
        message = re.sub(r"[\x00-\x1f\x7f]+", " ", str(payload.get("message") or "")).strip()[:80]
        if any(word in message for word in _CAPTCHA_WORDS):
            raise QrFetchError("blocked", f"captcha: {message}")
        if any(word in message for word in _RATE_WORDS):
            raise QrFetchError("rate_limited", message)
        raise QrFetchError("link_invalid", f"query code {payload.get('code')}: {message}")
    if str(payload.get("fphm") or "") != link.invoice_no:
        raise QrFetchError("platform_changed", "query returned a different invoice number")
    issued = re.sub(r"\D", "", str(payload.get("kprq") or ""))[:14]
    check = str(payload.get("jym") or "")
    refined = replace(
        link,
        issued_at=issued if len(issued) == 14 else link.issued_at,
        check_code=check[-4:] if re.fullmatch(r"[0-9A-Za-z]{4,40}", check) else link.check_code,
    )
    return refined, True


def _download_with_fallback(kind: str, state: dict) -> tuple[FetchedFile, bool]:
    """先按二维码里的参数直接下载（最快）；失败且像是链接或参数问题时，查询平台再核对一次。"""
    try:
        return _download(state["link"], kind)
    except QrFetchError as exc:
        if exc.code not in {"link_invalid", "platform_changed"} or state.get("queried"):
            raise
        state["queried"] = True
        refined, confirmed = _refine_link(state["link"])   # 可能抛出更明确的错误（链接失效、验证码、限流）
        if refined != state["link"]:
            state["link"] = refined
            return _download(refined, kind)
        if confirmed:
            # 平台确认这张发票有效，但下载接口不给文件：多半是下载接口改了。
            raise QrFetchError("platform_changed", "ticket confirmed valid but download returned nothing") from exc
        raise


def fetch_invoice_files(link: InvoiceLink, cancellation: FetchCancellation | None = None) -> QrFetchResult:
    token = _cancellation.set(cancellation)
    try:
        _check_cancelled()
        result = _fetch_invoice_files(link)
        _check_cancelled()
        return result
    finally:
        _cancellation.reset(token)


def _fetch_invoice_files(link: InvoiceLink) -> QrFetchResult:
    """联网取回发票 XML 和 PDF。XML 优先；只拿到其中一个时仍返回，并在 warnings 里说明。"""
    result = QrFetchResult(host=link.host, invoice_no=link.invoice_no)
    state: dict = {"link": link}
    errors: dict[str, QrFetchError] = {}
    for kind in ("xml", "pdf"):
        try:
            fetched, verified = _download_with_fallback(kind, state)
            result.files.append(fetched)
            if kind == "xml" and not verified:
                result.warnings.append("发票 XML 中没有发票号码字段，无法与二维码核对，请导入后确认发票号码。")
        except QrFetchError as exc:
            _check_cancelled()
            errors[kind] = exc
            # 链接无效、被拦截、网络故障时再请求 PDF 只会重复失败；内容类问题则值得再试另一种格式。
            if exc.code not in {"bad_content", "too_large"}:
                break
    if not result.files:
        raise next(iter(errors.values()))
    if "xml" in errors:
        result.warnings.append("未能取得发票 XML，只取回了 PDF；识别会以 PDF 文字为准。")
    elif "pdf" in errors:
        result.warnings.append("未能取得发票 PDF，只取回了 XML；打印票面前请补充发票 PDF。")
    return result


def fetch_invoice_from_image(data: bytes) -> QrFetchResult:
    """图片字节 → 二维码 → 取票链接 → 联网取回文件。任一步失败抛 QrFetchError。"""
    return fetch_invoice_files(find_invoice_link(data))


def read_image_file(path: str | os.PathLike) -> bytes:
    """读取图片文件，超过上限的文件不整体读入内存。"""
    target = Path(path)
    try:
        if not target.is_file():
            raise QrFetchError("image_unreadable", "not a file")
        if target.stat().st_size > MAX_IMAGE_BYTES:
            raise QrFetchError("image_unreadable", "image file too large")
        return target.read_bytes()
    except OSError as exc:
        raise QrFetchError("image_unreadable", type(exc).__name__) from exc

"""文件夹批量导入：扫描一个目录，把一批发票 PDF 快速拆成待创建条目。

设计取向：
- 批量只从发票 PDF 创建条目，可附带匹配到的 XML。
- 用户不需要提前重命名。XML 优先按发票号配对，其次按近似文件名配对。
- PDF 是必需材料；孤立 XML、付款截图、查验单只在预览里提示，不单独创建条目。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..engine.parser import parse_invoice_files

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
_PAYMENT_KEYWORDS = ("付款", "支付", "截图")
_INSPECTION_KEYWORDS = ("查验单", "查验", "验真", "查验平台", "查验次数", "查验时间")
_STEM_JUNK_RE = re.compile(r"[\s_\-+＋（）()\[\]【】{}.,，。]+")
_INVOICE_NO_RE = re.compile(r"\d{8,30}")
_LABELED_INVOICE_NO_RE = re.compile(r"发票号码[:：]?\s*(\d{20})")
_SPACED_INVOICE_NO_RE = re.compile(r"(?<!\d)(?:\d[\s\u3000]*){20}(?!\d)")
_PAYMENT_AMOUNT_RE = re.compile(
    r"(?<![\d.])[-−－—一]\s*(?:[¥￥]\s*)?(\d{1,7}(?:[,，]\d{3})*(?:\.\d{1,2})?)(?![\d.])"
)
_STANDALONE_PAYMENT_AMOUNT_RE = re.compile(
    r"(?<![\w.])[-−－—一]\s*(?:[¥￥]\s*)?(\d{1,7}(?:[,，]\d{3})*(?:\.\d{1,2})?)(?![\d.])"
)
_CONFIRMED_PAYMENT_AMOUNT_RE = re.compile(
    r"[-−－—一]\s*(?:[¥￥]\s*)?(\d{1,7}(?:[,，]\d{3})*(?:\.\d{1,2})?)"
    r"(?=支付成功|付款成功|交易成功|扣款成功)"
)
_PAYMENT_CONTEXT_WORDS = ("支付", "付款", "实付", "交易", "消费", "账单", "扣款")


def _file_info(path: Path, att_type: str, invoice_no: str = "", warning: str = "") -> dict:
    return {
        "path": str(path),
        "name": path.name,
        "type": att_type,
        "type_label": _TYPE_LABEL.get(att_type, att_type),
        "invoice_no": invoice_no,
        "warning": warning,
    }


def _normalize_stem(path: Path) -> str:
    stem = path.stem.lower()
    for token in ("发票", "电子", "数电", "invoice", "pdf", "xml"):
        stem = stem.replace(token, "")
    return _STEM_JUNK_RE.sub("", stem)


def _is_informative_stem(stem: str) -> bool:
    """Only use fuzzy filename matching when the stem carries real identity.

    Short numeric names like "26.pdf" often come from browser downloads or
    screenshots and can accidentally match dates/invoice numbers in XML names.
    """
    if not stem:
        return False
    if stem.isdigit():
        return len(stem) >= 12
    return len(stem) >= 5


def _invoice_no_from_name(path: Path) -> str:
    m = _INVOICE_NO_RE.search(path.stem)
    return m.group(0) if m else ""


def _invoice_no_from_text(text: str) -> str:
    labeled = _LABELED_INVOICE_NO_RE.search(text or "")
    if labeled:
        return labeled.group(1)
    for no in re.findall(r"\d{20}", text or ""):
        return no
    spaced = _SPACED_INVOICE_NO_RE.search(text or "")
    if spaced:
        return re.sub(r"\D", "", spaced.group(0))
    return ""


def _hidden_windows_subprocess_kwargs() -> dict:
    if not sys.platform.startswith("win"):
        return {}
    kwargs: dict = {}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags:
        kwargs["creationflags"] = creationflags
    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    use_show_window = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    if startupinfo_cls and use_show_window:
        startupinfo = startupinfo_cls()
        startupinfo.dwFlags |= use_show_window
        startupinfo.wShowWindow = 0
        kwargs["startupinfo"] = startupinfo
    return kwargs


def _parse_invoice_no(path: Path, att_type: str) -> tuple[str, str]:
    try:
        if att_type == "invoice_pdf":
            parsed = parse_invoice_files(pdf_path=path)
        else:
            parsed = parse_invoice_files(xml_path=path)
        return parsed.invoice_no or "", ""
    except Exception as exc:  # noqa: BLE001 - 扫描阶段只提示，不阻断导入预览
        guessed = _invoice_no_from_name(path)
        return guessed, f"未能预解析：{exc}"


def _looks_like_invoice_pdf_text(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return False
    invoice_markers = (
        "电子发票",
        "数电",
        "发票号码",
        "开票日期",
        "购买方",
        "销售方",
        "价税合计",
        "项目名称",
    )
    return sum(1 for marker in invoice_markers if marker in compact) >= 2


def _pdf_attachment_type(path: Path) -> str:
    probe = _pdf_probe_text(path)
    if any(k in probe for k in _INSPECTION_KEYWORDS) or "inv-veri.chinatax" in probe.lower():
        return "inspection_pdf"
    if _looks_like_invoice_pdf_text(probe):
        return "invoice_pdf"
    if any(k in path.name for k in _INSPECTION_KEYWORDS):
        return "inspection_pdf"
    if any(k in path.name for k in _PAYMENT_KEYWORDS):
        return "payment_screenshot"
    return "invoice_pdf"


def classify_pdf_attachment_type(path: str | Path) -> str:
    return _pdf_attachment_type(Path(path))


def extract_pdf_invoice_no(path: str | Path) -> str:
    path = Path(path)
    probe = _pdf_probe_text(Path(path))
    invoice_no = _invoice_no_from_text(probe)
    if invoice_no:
        return invoice_no
    return _macos_ocr_tax_verification_invoice_no(path) or _windows_ocr_tax_verification_invoice_no(path)


def extract_payment_image_amount(path: str | Path) -> str:
    path = Path(path)
    if path.suffix.lower() not in _IMAGE_EXT:
        return ""
    text = _macos_ocr_image_text(path) or _windows_ocr_image_text(path)
    return _payment_amount_from_text(text)


def suggest_material_bindings(
    infos: list[dict], entries: list[dict], payment_ocr_enabled: bool = True
) -> list[dict]:
    """仅在归属唯一时给出自动绑定建议；其余材料返回明确的手动处理原因。"""
    planned = []
    for raw in infos or []:
        info = dict(raw)
        matches: list[dict] = []
        reason = ""
        if info.get("type") == "inspection_pdf":
            invoice_no = str(info.get("invoice_no") or "").strip()
            if invoice_no:
                matches = [entry for entry in entries if str(entry.get("invoice_no") or "") == invoice_no]
                if not matches:
                    reason = f"发票号 {invoice_no} 没有对应条目"
                elif len(matches) > 1:
                    reason = f"发票号 {invoice_no} 对应 {len(matches)} 个条目"
            else:
                reason = "未识别到查验单发票号码"
        elif info.get("type") == "payment_screenshot":
            if not payment_ocr_enabled:
                reason = ""
            else:
                amount = _normalized_payment_amount(info.get("paid_amount"))
                if amount is not None:
                    matches = [
                        entry for entry in entries
                        if _normalized_payment_amount(entry.get("total")) == amount
                    ]
                    shown = f"{amount:.2f}"
                    if not matches:
                        reason = f"识别到付款 ¥{shown}，没有金额相同的条目"
                    elif len(matches) > 1:
                        reason = f"识别到付款 ¥{shown}，有 {len(matches)} 个金额相同的条目"
                else:
                    reason = "未识别到付款金额"
        else:
            reason = "无法自动判断材料归属"

        if len(matches) == 1:
            info["suggested_entry_id"] = matches[0].get("entry_id") or matches[0].get("id") or ""
            info["binding_reason"] = ""
        else:
            info["suggested_entry_id"] = ""
            info["binding_reason"] = f"{reason}，请手动选择。" if reason else ""
        planned.append(info)
    return planned


def _normalized_payment_amount(value) -> Decimal | None:
    text = str(value or "").replace(",", "").replace("，", "").replace("¥", "").replace("￥", "").strip()
    if not text:
        return None
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _payment_amount_from_text(text: str) -> str:
    def formatted_amount(match: re.Match) -> str:
        raw = match.group(1).replace(",", "").replace("，", "")
        try:
            amount = Decimal(raw).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return ""
        if amount > 0:
            return f"{amount:.2f}"
        return ""

    compact = re.sub(r"\s+", "", text or "")
    compact = re.sub(r"(?<=\d)[·．。](?=\d)", ".", compact)
    # A success label immediately after the amount is the strongest signal and
    # should win even if a product-model line appears earlier in OCR order.
    for match in _CONFIRMED_PAYMENT_AMOUNT_RE.finditer(compact):
        amount = formatted_amount(match)
        if amount:
            return amount

    # Prefer a standalone negative amount line.  This avoids treating product
    # models such as ``ZTPV-25`` as a ¥25 payment.
    for raw_line in (text or "").splitlines():
        normalized = re.sub(r"(?<=\d)\s*[·．。]\s*(?=\d)", ".", raw_line)
        for match in _STANDALONE_PAYMENT_AMOUNT_RE.finditer(normalized):
            amount = formatted_amount(match)
            if amount:
                return amount

        # Some native OCR streams split every character with spaces and read
        # the minus sign as ``一``.  Compact only a line that contains payment
        # context, rather than compacting unrelated product descriptions.
        compact_line = re.sub(r"\s+", "", normalized)
        if any(word in compact_line for word in _PAYMENT_CONTEXT_WORDS):
            for match in _CONFIRMED_PAYMENT_AMOUNT_RE.finditer(compact_line):
                amount = formatted_amount(match)
                if amount:
                    return amount
            for match in _PAYMENT_AMOUNT_RE.finditer(compact_line):
                amount = formatted_amount(match)
                if amount:
                    return amount

    for match in _PAYMENT_AMOUNT_RE.finditer(compact):
        amount = formatted_amount(match)
        if amount:
            return amount
    return ""


def _macos_ocr_image_text(path: Path) -> str:
    if sys.platform != "darwin":
        return ""
    try:
        import objc
        from Foundation import NSDictionary, NSURL

        objc.loadBundle("Vision", globals(), bundle_path="/System/Library/Frameworks/Vision.framework")
        req = VNRecognizeTextRequest.alloc().init()  # type: ignore[name-defined]
        req.setRecognitionLevel_(0)
        if hasattr(req, "setUsesLanguageCorrection_"):
            req.setUsesLanguageCorrection_(False)
        req.setRecognitionLanguages_(["zh-Hans", "en-US"])
        handler = VNImageRequestHandler.alloc().initWithURL_options_(  # type: ignore[name-defined]
            NSURL.fileURLWithPath_(str(path)),
            NSDictionary.dictionary(),
        )
        result = handler.performRequests_error_([req], None)
        if isinstance(result, tuple) and not result[0]:
            return ""
        lines: list[str] = []
        for obs in req.results() or []:
            candidates = obs.topCandidates_(1)
            if candidates:
                lines.append(str(candidates[0].string()))
        return "\n".join(lines)
    except Exception:
        return ""


def _windows_ocr_image_text(path: Path) -> str:
    if not sys.platform.startswith("win"):
        return ""
    script = r"""
param([string]$Path)
[Console]::OutputEncoding = [Text.UTF8Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type -AssemblyName System.Drawing
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStreamWithContentType, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]
function Await-Operation($Operation, [Type]$ResultType) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetParameters().Count -eq 1
    } | Select-Object -First 1
    $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $task.Wait()
    return $task.Result
}
function Recognize-Image($ImagePath) {
    $file = Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
    $stream = Await-Operation ($file.OpenReadAsync()) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType])
    $decoder = Await-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await-Operation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('zh-Hans'))
    if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
    if ($null -eq $engine) { return '' }
    $result = Await-Operation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    return $result.Text
}
function Prepare-PaymentImage($ImagePath, $OutputPath, [double]$CropRatio, [double]$MaxUpscale) {
    $src = [System.Drawing.Bitmap]::new($ImagePath)
    $crop = $null
    $scaled = $null
    $cropGraphics = $null
    $scaleGraphics = $null
    try {
        $cropHeight = [Math]::Max(1, [int]($src.Height * $CropRatio))
        $crop = [System.Drawing.Bitmap]::new(
            $src.Width,
            $cropHeight,
            [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
        )
        $cropGraphics = [System.Drawing.Graphics]::FromImage($crop)
        $cropGraphics.Clear([System.Drawing.Color]::White)
        $cropGraphics.DrawImage($src, 0, 0, $src.Width, $src.Height)

        # Windows.Media.Ocr 拒绝任一边超过 MaxImageDimension 的图片。
        # 手机长截图很容易超过限制，因此先统一缩放到安全范围；顶部区域可适度放大。
        $maxDimension = 2400.0
        $scale = [Math]::Min(
            $MaxUpscale,
            [Math]::Min($maxDimension / $crop.Width, $maxDimension / $crop.Height)
        )
        $targetWidth = [Math]::Max(1, [int]($crop.Width * $scale))
        $targetHeight = [Math]::Max(1, [int]($crop.Height * $scale))
        $scaled = [System.Drawing.Bitmap]::new(
            $targetWidth,
            $targetHeight,
            [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
        )
        $scaleGraphics = [System.Drawing.Graphics]::FromImage($scaled)
        $scaleGraphics.Clear([System.Drawing.Color]::White)
        $scaleGraphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $scaleGraphics.DrawImage($crop, 0, 0, $targetWidth, $targetHeight)
        $scaled.Save($OutputPath, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally {
        if ($null -ne $scaleGraphics) { $scaleGraphics.Dispose() }
        if ($null -ne $cropGraphics) { $cropGraphics.Dispose() }
        if ($null -ne $scaled) { $scaled.Dispose() }
        if ($null -ne $crop) { $crop.Dispose() }
        $src.Dispose()
    }
}
$prepared = @()
try {
    $fullPath = Join-Path ([System.IO.Path]::GetDirectoryName($Path)) 'payment-full.png'
    Prepare-PaymentImage $Path $fullPath 1.0 1.0
    $prepared += $fullPath
    $topPath = Join-Path ([System.IO.Path]::GetDirectoryName($Path)) 'payment-top.png'
    Prepare-PaymentImage $Path $topPath 0.55 2.0
    $prepared += $topPath
} catch {
    # 个别系统不支持的图片编码仍交给 WinRT 解码器直接尝试。
    $prepared = @($Path)
}
foreach ($image in $prepared) {
    try { Recognize-Image $image } catch { }
}
"""
    try:
        with tempfile.TemporaryDirectory(prefix="tidoc-ocr-") as tmp:
            tmp_dir = Path(tmp)
            image_path = tmp_dir / f"input{path.suffix.lower()}"
            script_path = tmp_dir / "ocr-image.ps1"
            shutil.copy2(path, image_path)
            script_path.write_text(script, encoding="utf-8")
            proc = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path),
                    "-Path",
                    str(image_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=20,
                check=False,
                **_hidden_windows_subprocess_kwargs(),
            )
        return (proc.stdout or "") + "\n" + (proc.stderr or "")
    except Exception:
        return ""


def _macos_ocr_tax_verification_invoice_no(path: Path) -> str:
    if sys.platform != "darwin":
        return ""
    try:
        import objc
        from Foundation import NSURL
        from Quartz import (
            CGBitmapContextCreate,
            CGBitmapContextCreateImage,
            CGColorSpaceCreateDeviceRGB,
            CGContextDrawPDFPage,
            CGContextFillRect,
            CGContextScaleCTM,
            CGContextSetRGBFillColor,
            CGContextTranslateCTM,
            CGPDFDocumentCreateWithURL,
            CGPDFDocumentGetPage,
            CGPDFPageGetBoxRect,
            CGRectMake,
            kCGImageAlphaPremultipliedLast,
            kCGPDFMediaBox,
        )

        objc.loadBundle("Vision", globals(), bundle_path="/System/Library/Frameworks/Vision.framework")
        doc = CGPDFDocumentCreateWithURL(NSURL.fileURLWithPath_(str(path)))
        if not doc:
            return ""
        page = CGPDFDocumentGetPage(doc, 1)
        if not page:
            return ""
        rect = CGPDFPageGetBoxRect(page, kCGPDFMediaBox)
        candidates = [
            (rect.size.width * 0.08, rect.size.height * 0.17, rect.size.width * 0.34, rect.size.height * 0.12),
            (rect.size.width * 0.05, rect.size.height * 0.13, rect.size.width * 0.44, rect.size.height * 0.16),
        ]
        for crop_x, crop_top, crop_w, crop_h in candidates:
            text = _macos_ocr_pdf_crop(
                page, rect, crop_x, crop_top, crop_w, crop_h,
                scale=8.0,
                CGBitmapContextCreate=CGBitmapContextCreate,
                CGBitmapContextCreateImage=CGBitmapContextCreateImage,
                CGColorSpaceCreateDeviceRGB=CGColorSpaceCreateDeviceRGB,
                CGContextDrawPDFPage=CGContextDrawPDFPage,
                CGContextFillRect=CGContextFillRect,
                CGContextScaleCTM=CGContextScaleCTM,
                CGContextSetRGBFillColor=CGContextSetRGBFillColor,
                CGContextTranslateCTM=CGContextTranslateCTM,
                CGRectMake=CGRectMake,
                kCGImageAlphaPremultipliedLast=kCGImageAlphaPremultipliedLast,
            )
            invoice_no = _invoice_no_from_text(text)
            if invoice_no:
                return invoice_no
    except Exception:
        return ""
    return ""


def _windows_ocr_tax_verification_invoice_no(path: Path) -> str:
    if not sys.platform.startswith("win"):
        return ""
    script = r"""
param([string]$Path)
[Console]::OutputEncoding = [Text.UTF8Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType = WindowsRuntime]
$null = [Windows.Data.Pdf.PdfPageRenderOptions, Windows.Data.Pdf, ContentType = WindowsRuntime]
function Await-Operation($Operation, [Type]$ResultType) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetParameters().Count -eq 1
    } | Select-Object -First 1
    $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $task.Wait()
    return $task.Result
}
function Await-Action($Action) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and -not $_.IsGenericMethodDefinition -and $_.GetParameters().Count -eq 1
    } | Select-Object -First 1
    $task = $method.Invoke($null, @($Action))
    $task.Wait()
}
$file = Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$pdf = Await-Operation ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
$page = $pdf.GetPage(0)
$size = $page.Size
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('zh-Hans'))
if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
$rects = @(
    @(0.08, 0.17, 0.34, 0.12),
    @(0.02, 0.10, 0.60, 0.22),
    @(0.00, 0.00, 1.00, 0.35)
)
foreach ($r in $rects) {
    $opts = [Windows.Data.Pdf.PdfPageRenderOptions]::new()
    $opts.SourceRect = [Windows.Foundation.Rect]::new($size.Width * $r[0], $size.Height * $r[1], $size.Width * $r[2], $size.Height * $r[3])
    $opts.DestinationWidth = [uint32]($opts.SourceRect.Width * 8)
    $opts.DestinationHeight = [uint32]($opts.SourceRect.Height * 8)
    $stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
    Await-Action ($page.RenderToStreamAsync($stream, $opts))
    $stream.Seek(0) | Out-Null
    $decoder = Await-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await-Operation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Await-Operation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    if ($result -and $result.Text) {
        $result.Text
    }
}
"""
    try:
        with tempfile.TemporaryDirectory(prefix="tidoc-ocr-") as tmp:
            tmp_dir = Path(tmp)
            pdf_path = tmp_dir / "input.pdf"
            script_path = tmp_dir / "ocr.ps1"
            shutil.copy2(path, pdf_path)
            script_path.write_text(script, encoding="utf-8")
            proc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path),
                    "-Path",
                    str(pdf_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=20,
                check=False,
                **_hidden_windows_subprocess_kwargs(),
            )
        return _invoice_no_from_text((proc.stdout or "") + "\n" + (proc.stderr or ""))
    except Exception:
        return ""


def _macos_ocr_pdf_crop(page, rect, crop_x, crop_top, crop_w, crop_h, scale: float, **q) -> str:
    width, height = int(crop_w * scale), int(crop_h * scale)
    ctx = q["CGBitmapContextCreate"](
        None,
        width,
        height,
        8,
        width * 4,
        q["CGColorSpaceCreateDeviceRGB"](),
        q["kCGImageAlphaPremultipliedLast"],
    )
    q["CGContextSetRGBFillColor"](ctx, 1, 1, 1, 1)
    q["CGContextFillRect"](ctx, q["CGRectMake"](0, 0, width, height))
    q["CGContextScaleCTM"](ctx, scale, scale)
    q["CGContextTranslateCTM"](ctx, -crop_x, -(rect.size.height - crop_top - crop_h))
    q["CGContextDrawPDFPage"](ctx, page)
    image = q["CGBitmapContextCreateImage"](ctx)

    req = VNRecognizeTextRequest.alloc().init()  # type: ignore[name-defined]
    req.setRecognitionLevel_(1)
    if hasattr(req, "setUsesLanguageCorrection_"):
        req.setUsesLanguageCorrection_(False)
    req.setRecognitionLanguages_(["en-US", "zh-Hans"])
    handler = VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)  # type: ignore[name-defined]
    handler.performRequests_error_([req], None)
    lines: list[str] = []
    for obs in req.results() or []:
        candidates = obs.topCandidates_(1)
        if candidates:
            lines.append(str(candidates[0].string()))
    return "\n".join(lines)


def _pdf_probe_text(path: Path) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(path)
        chunks: list[str] = []
        if reader.metadata:
            for value in reader.metadata.values():
                if isinstance(value, bytes):
                    for enc in ("utf-8", "gb18030"):
                        try:
                            chunks.append(value.decode(enc, errors="ignore"))
                            break
                        except Exception:
                            continue
                elif value:
                    chunks.append(str(value))
        for page in reader.pages[:1]:
            text = page.extract_text() or ""
            if text:
                chunks.append(text[:2000])
        return "\n".join(chunks)
    except Exception:
        return ""


def _pdf_invoice_numbers(path: Path) -> set[str]:
    """Return distinct invoice numbers represented by a PDF.

    Count numbers tied to the ``发票号码`` metadata field, plus the invoice
    number parsed from each invoice-looking page.  Do not count every 20-digit
    run in the document: bank accounts can also be 20 digits long.
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(path)
        numbers: set[str] = set()
        metadata_chunks: list[str] = []
        if reader.metadata:
            for value in reader.metadata.values():
                if isinstance(value, bytes):
                    for enc in ("utf-8", "gb18030"):
                        try:
                            metadata_chunks.append(value.decode(enc, errors="ignore"))
                            break
                        except Exception:
                            continue
                elif value:
                    metadata_chunks.append(str(value))
        metadata_text = "\n".join(metadata_chunks)
        numbers.update(_LABELED_INVOICE_NO_RE.findall(metadata_text))

        for page in reader.pages:
            text = page.extract_text() or ""
            if not _looks_like_invoice_pdf_text(text):
                continue
            invoice_no = _invoice_no_from_text(text)
            if invoice_no:
                numbers.add(invoice_no)
        return numbers
    except Exception:
        return set()


def _match_xml(pdf: dict, xmls: list[dict], used: set[int]) -> int | None:
    pdf_no = pdf.get("invoice_no") or _invoice_no_from_name(Path(pdf["path"]))
    if pdf_no:
        for idx, xml in enumerate(xmls):
            if idx not in used and xml.get("invoice_no") == pdf_no:
                return idx
    pdf_stem = _normalize_stem(Path(pdf["path"]))
    if not _is_informative_stem(pdf_stem):
        return None
    for idx, xml in enumerate(xmls):
        if idx in used:
            continue
        xml_stem = _normalize_stem(Path(xml["path"]))
        if (
            _is_informative_stem(xml_stem)
            and (pdf_stem == xml_stem or pdf_stem in xml_stem or xml_stem in pdf_stem)
        ):
            return idx
    return None


def _scan_file_paths(file_paths: list[Path]) -> dict:
    """扫描一组文件，返回发票 PDF 导入预览。

    返回结构：
        {
          "groups": [
            {"key": "...", "label": "...", "selected": true,
             "invoice_no": "...",
             "files": [{"path", "name", "type", "type_label", "warning"}...],
             "warnings": [...]},
            ...
          ],
          "ungrouped": [{...file...}],   # 未匹配到 PDF 的 XML
          "ignored": [{...file...}],     # 付款截图 / 查验单不参与批量的文件
          "total_files": int,
        }
    """
    pdfs: list[dict] = []
    xmls: list[dict] = []
    ungrouped: list[dict] = []
    ignored: list[dict] = []
    total = 0

    for entry in sorted((Path(p) for p in file_paths if Path(p).is_file()), key=lambda p: str(p)):
        if not entry.is_file():
            continue
        suffix = entry.suffix.lower()
        if suffix == ".pdf":
            total += 1
            probe_invoice_nos = _pdf_invoice_numbers(entry)
            if len(probe_invoice_nos) > 1:
                ignored.append(_file_info(
                    entry,
                    "other",
                    warning=f"这个 PDF 包含 {len(probe_invoice_nos)} 张发票，请选择拆分后的单张发票 PDF",
                ))
                continue
            att_type = _pdf_attachment_type(entry)
            if att_type != "invoice_pdf":
                ignored.append(_file_info(entry, att_type, warning="这类材料请在条目里添加，或拖到界面后选择绑定条目"))
                continue
            invoice_no, warning = _parse_invoice_no(entry, "invoice_pdf")
            pdfs.append(_file_info(entry, "invoice_pdf", invoice_no, warning))
        elif suffix == ".xml":
            total += 1
            invoice_no, warning = _parse_invoice_no(entry, "invoice_xml")
            xmls.append(_file_info(entry, "invoice_xml", invoice_no, warning))
        elif suffix in _IMAGE_EXT:
            total += 1
            ignored.append(_file_info(entry, "payment_screenshot", warning="付款截图请在条目里添加，或拖到界面后选择绑定条目"))
        else:
            continue

    used_xml: set[int] = set()
    seen_pdf_nos: dict[str, str] = {}
    matched_xml_count = 0
    groups: list[dict] = []
    for idx, pdf in enumerate(pdfs, start=1):
        pdf_no = pdf.get("invoice_no") or ""
        if pdf_no and pdf_no in seen_pdf_nos:
            duplicate_warning = f"与 {seen_pdf_nos[pdf_no]} 的发票号相同，已跳过重复文件"
            ignored.append({**pdf, "warning": duplicate_warning})
            duplicate_xml_idx = _match_xml(pdf, xmls, used_xml)
            if duplicate_xml_idx is not None:
                used_xml.add(duplicate_xml_idx)
                ignored.append({**xmls[duplicate_xml_idx], "warning": duplicate_warning})
            continue
        if pdf_no:
            seen_pdf_nos[pdf_no] = pdf["name"]

        files = [pdf]
        warnings = [pdf["warning"]] if pdf.get("warning") else []
        xml_idx = _match_xml(pdf, xmls, used_xml)
        if xml_idx is not None:
            used_xml.add(xml_idx)
            matched_xml_count += 1
            xml = xmls[xml_idx]
            files.append(xml)
            if xml.get("warning"):
                warnings.append(f"{xml['name']}：{xml['warning']}")
        label = pdf.get("invoice_no") or Path(pdf["path"]).stem
        groups.append({
            "key": f"pdf-{idx}",
            "label": label,
            "invoice_no": pdf.get("invoice_no") or "",
            "selected": True,
            "files": files,
            "warnings": warnings,
        })

    for idx, xml in enumerate(xmls):
        if idx not in used_xml:
            if not pdfs:
                ungrouped.append({**xml, "warning": xml.get("warning") or "没有发票 PDF，不能单独批量导入"})
            else:
                ungrouped.append({**xml, "warning": xml.get("warning") or "未匹配到对应发票 PDF"})

    return {
        "groups": groups,
        "ungrouped": ungrouped,
        "ignored": ignored,
        "total_files": total,
        "invoice_pdf_count": len(groups),
        "matched_xml_count": matched_xml_count,
    }


def scan_folder(folder: str | Path) -> dict:
    """扫描目录，返回发票 PDF 导入预览。"""
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(f"不是有效目录：{folder}")
    return _scan_file_paths([p for p in folder.rglob("*") if p.is_file()])


def scan_files(paths: list[str | Path]) -> dict:
    """扫描用户多选或拖入的发票文件，返回批量导入预览。"""
    file_paths = [Path(p) for p in (paths or [])]
    if not file_paths:
        raise ValueError("没有选择文件")
    return _scan_file_paths(file_paths)


_TYPE_LABEL = {
    "invoice_pdf": "发票 PDF",
    "invoice_xml": "发票 XML",
    "payment_screenshot": "付款截图",
    "inspection_pdf": "查验单 PDF",
    "other": "未导入",
}

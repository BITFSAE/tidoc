"""阿里云增值税发票识别调用与结果解析。

移植自参考仓库 invoice2docx 的 engine.py `parse_aliyun_ocr_invoice`，修正：
- 密钥由调用方显式传入（核心存于本地设置），不再读环境变量；
- 按当前 API 文档优先使用 purchaserName / purchaserTaxNumber / specification
  等字段名，同时保留多候选键兜底；
- 返回原始 data 与规范化结果两层，原始 JSON 由核心落库防止重复计费。

接口：ocr-api 2021-07-07 RecognizeInvoice（文件流 ≤10MB，PDF 默认识别第 1 页）。
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

DEFAULT_ENDPOINT = "ocr-api.cn-hangzhou.aliyuncs.com"
_TIMEOUT_MS = 30_000

# 发票级字段：优先键在前，历史/别名键兜底
_FIELD_CANDIDATES = {
    "invoice_no": ("invoiceNumber", "invoiceNo"),
    "invoice_code": ("invoiceCode",),
    "invoice_date": ("invoiceDate",),
    "seller": ("sellerName", "seller"),
    "buyer_name": ("purchaserName", "buyerName"),
    "buyer_tax_id": (
        "purchaserTaxNumber",
        "buyerTaxNumber",
        "buyerTaxNo",
        "buyerRegisterNum",
        "buyerIdNum",
    ),
    "total": ("totalAmount", "amountWithTax", "totalTaxIncludedAmount"),
}

# 明细行字段候选
_ITEM_CANDIDATES = {
    "name": ("itemName", "name", "goodsName"),
    "spec": ("specification", "specModel", "spec"),
    "unit": ("unit", "unitName"),
    "quantity": ("quantity", "qty"),
    "unit_price": ("unitPrice",),
    "amount": ("amount", "withoutTaxAmount"),
    "tax": ("tax", "taxAmount"),
}


class OcrError(RuntimeError):
    """OCR 调用失败，message 面向操作者（含计费/密钥类提示）。"""


def _pick(mapping, *keys) -> str:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _dec(value) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    text = str(value).replace(",", "").replace("¥", "").replace("￥", "").strip()
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _dec_text(value: Decimal) -> str:
    """去掉尾随零的数量/单价文本（OCR 常返回 2.00000000 之类）。"""
    text = f"{value:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def clean_item_name(raw: str) -> str:
    """去掉数电票货物名称里的「*分类*」前缀。"""
    return re.sub(r"^\*[^*]+\*", "", str(raw or "").strip()).strip()


def _normalize_date(raw: str) -> str:
    text = str(raw or "").strip()
    match = re.match(r"^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?$", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return text


def _build_credential(access_key_id: str, access_key_secret: str):
    """构造静态 AK 凭据客户端，兼容 alibabacloud-credentials 0.x 与 1.x。"""
    from alibabacloud_credentials.client import Client as CredentialClient

    try:
        # 1.x：models.Config，且需要显式 type
        from alibabacloud_credentials.models import Config as CredentialConfig
        return CredentialClient(CredentialConfig(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            type="access_key",
        ))
    except ImportError:
        # 0.x：models.CredentialConfig，type 自动推断
        from alibabacloud_credentials.models import CredentialConfig
        return CredentialClient(CredentialConfig(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
        ))


def recognize_invoice(
    path: str | Path,
    access_key_id: str,
    access_key_secret: str,
    endpoint: str = DEFAULT_ENDPOINT,
) -> dict:
    """调用阿里云 RecognizeInvoice，返回响应内层 data dict（原始识别结果）。

    只做调用与基础校验，不解析字段；密钥错误/未开通服务时给出可操作提示。
    """
    if not access_key_id or not access_key_secret:
        raise OcrError("未配置阿里云 AccessKey，请先在设置的「阿里云 OCR」中填写。")

    file_path = Path(path)
    if not file_path.is_file():
        raise OcrError(f"发票文件不存在：{file_path.name}")

    try:
        from alibabacloud_darabonba_stream.client import Client as StreamClient
        from alibabacloud_ocr_api20210707 import models as ocr_models
        from alibabacloud_ocr_api20210707.client import Client as OcrClient
        from alibabacloud_tea_openapi import models as open_api_models
        from alibabacloud_tea_util import models as util_models
    except ImportError as exc:  # pragma: no cover - 打包产物自带依赖，仅源码环境会走到
        raise OcrError("OCR 识别组件缺少阿里云依赖，请重新安装组件。") from exc

    try:
        credential = _build_credential(access_key_id, access_key_secret)
        config = open_api_models.Config(credential=credential)
        config.endpoint = endpoint or DEFAULT_ENDPOINT
        client = OcrClient(config)
        request = ocr_models.RecognizeInvoiceRequest(
            body=StreamClient.read_from_file_path(str(file_path))
        )
        runtime = util_models.RuntimeOptions(read_timeout=_TIMEOUT_MS, connect_timeout=10_000)
        response = client.recognize_invoice_with_options(request, runtime)
    except Exception as exc:  # noqa: BLE001 — SDK 抛 Tea 系异常，统一转可读提示
        raise OcrError(_friendly_error(exc)) from exc

    if response.status_code != 200:
        raise OcrError(f"阿里云 OCR 返回状态码 {response.status_code}")
    try:
        payload = json.loads(response.body.data)
    except (TypeError, json.JSONDecodeError) as exc:
        raise OcrError("阿里云 OCR 返回内容无法解析。") from exc
    data = payload.get("data") or {}
    if not data:
        raise OcrError("阿里云 OCR 未返回发票数据，请确认文件是清晰的发票 PDF 或图片。")
    return data


def _friendly_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    if any(
        token in lowered
        for token in ("invalidaccesskeyid", "signaturedoesnotmatch", "invalidaccesskeysecret")
    ):
        return "阿里云 AccessKey 无效，请检查设置的「阿里云 OCR」密钥。"
    if any(token in lowered for token in ("forbidden", "nopermission", "unauthorized")):
        return "该 AccessKey 没有文字识别权限，请为 RAM 账号授予 OCR 识别权限。"
    if "throttling" in lowered or "flowcontrol" in lowered:
        return "阿里云 OCR 请求过于频繁，请稍后重试。"
    if any(token in lowered for token in ("timeout", "timed out")):
        return "阿里云 OCR 请求超时，请检查网络后重试。"
    if any(token in lowered for token in ("arrearage", "quota", "out of")):
        return "阿里云 OCR 账户余额或额度不足，请到阿里云控制台处理。"
    detail = str(exc).strip() or text
    return f"阿里云 OCR 调用失败：{detail[:200]}"


def normalize_invoice_data(data: dict) -> dict:
    """把阿里云 data dict 规范化为核心可比较的结构（不依赖 tidoc 核心代码）。

    返回：
    {
      "invoice_no": "2412...", "invoice_date": "2026-01-31", "seller": "...",
      "buyer_name": "...", "buyer_tax_id": "...",
      "total": "123.45",              # 价税合计（含税）
      "item_sum": "123.45",           # 明细含税合计（闭合校验用）
      "closure_pass": true,           # 明细合计与价税合计一致到分
      "items": [{"name","actual_name","unit","quantity","total","spec"}, ...]
    }
    """
    fields = {
        key: _pick(data, *candidates)
        for key, candidates in _FIELD_CANDIDATES.items()
    }
    total = _money(_dec(fields["total"]))
    fields["total"] = f"{total:.2f}"
    if fields["invoice_date"]:
        fields["invoice_date"] = _normalize_date(fields["invoice_date"])

    items: list[dict] = []
    item_sum = Decimal("0")
    last: dict | None = None
    for detail in data.get("invoiceDetails") or []:
        if not isinstance(detail, dict):
            continue
        raw_name = _pick(detail, *_ITEM_CANDIDATES["name"])
        if not raw_name:
            continue
        unit = _pick(detail, *_ITEM_CANDIDATES["unit"])
        quantity_text = _pick(detail, *_ITEM_CANDIDATES["quantity"])
        spec = _pick(detail, *_ITEM_CANDIDATES["spec"])
        line_total = _money(
            _dec(_pick(detail, *_ITEM_CANDIDATES["amount"]))
            + _dec(_pick(detail, *_ITEM_CANDIDATES["tax"]))
        )

        # 价外费用类续行：与上一行同名且无数量的行，金额并入上一行
        if last is not None and not quantity_text and raw_name == last.get("_raw_name"):
            merged = _money(_dec(last["total"]) + line_total)
            last["total"] = f"{merged:.2f}"
            item_sum += line_total
            continue

        item = {
            "_raw_name": raw_name,
            "name": raw_name,
            "actual_name": clean_item_name(raw_name),
            "unit": unit,
            "quantity": _dec_text(_dec(quantity_text)) if quantity_text else "",
            "total": f"{line_total:.2f}",
            "spec": spec,
        }
        items.append(item)
        last = item
        item_sum += line_total

    for item in items:
        item.pop("_raw_name", None)

    item_sum = _money(item_sum)
    closure_pass = bool(items) and item_sum == total
    return {
        "invoice_no": fields["invoice_no"],
        "invoice_code": fields["invoice_code"],
        "invoice_date": fields["invoice_date"],
        "seller": fields["seller"],
        "buyer_name": fields["buyer_name"],
        "buyer_tax_id": fields["buyer_tax_id"],
        "total": fields["total"],
        "item_sum": f"{item_sum:.2f}",
        "closure_pass": closure_pass,
        "items": items,
    }

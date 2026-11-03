"""从查验单正文读取查验次数与时间；不联网、不使用 OCR。"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import DependencyError, PyPdfError

from ..engine.check_lines import INSPECTION_CHECK_PREFIX

INSPECTION_RECOGNITION_VERSION = "inspection-pdf-1"


def parse_inspection_text(text: str) -> dict:
    """用正文标签取值，兼容浏览器打印的偏旁字形与分散空白。"""
    normalized = unicodedata.normalize("NFKC", text or "")
    compact = re.sub(r"\s+", "", normalized)
    counts = {
        (bool(match[1]), int(match[2]))
        for match in re.finditer(r"(当日)?查验次数[:：]?第([0-9]{1,6})次", compact)
        if int(match[2]) > 0
    }
    daily, count = next(iter(counts)) if len(counts) == 1 else (False, None)
    times = set()
    # 保留日期和时间之间的边界，避免「1 日 12 点」压紧后读成「11 日 2 点」。
    time_compact = re.sub(r"(?<=[0-9])\s+(?=[0-9]{1,2}\s*:)", "T", normalized)
    time_compact = re.sub(r"\s+", "", time_compact)
    for match in re.finditer(
        r"查验时间[:：]?([0-9]{4})[-/年]([0-9]{1,2})[-/月]([0-9]{1,2})日?"
        r"T?([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?", time_compact
    ):
        try:
            times.add(datetime(*(int(part or "0") for part in match.groups())).isoformat(sep=" "))
        except ValueError:
            continue
    numbers = set(re.findall(r"发票号码[:：]?([0-9]{8,20})(?![0-9])", compact))
    return {
        "invoice_no": next(iter(numbers)) if len(numbers) == 1 else "",
        "count": count,
        "count_scope": "day" if daily else "unspecified",
        "checked_at": next(iter(times)) if len(times) == 1 else "",
    }


def read_inspection_pdf(path: str | Path) -> dict:
    """只用 PDF 正文，不从标题、文件名或导入时间推断查验次数。"""
    try:
        reader = PdfReader(path)
        return parse_inspection_text("\n".join(page.extract_text() or "" for page in reader.pages))
    except (OSError, ValueError, PyPdfError, DependencyError):
        return parse_inspection_text("")


def cached_inspection_info(recognition_version, recognized_value, sha256) -> dict | None:
    """附件列里缓存的正文信息；规则版本或文件摘要对不上时返回 None。"""
    if recognition_version != INSPECTION_RECOGNITION_VERSION:
        return None
    try:
        cached = json.loads(recognized_value)
    except (TypeError, ValueError):
        return None
    if isinstance(cached, dict) and cached.get("sha256") == sha256:
        return cached
    return None


def inspection_summary(info: dict | None) -> dict:
    """给界面显示用的字段；info 为空表示这份材料没有读到任何信息。"""
    info = info or {}
    count = info.get("count")
    return {
        "count": count if type(count) is int and count > 0 else None,
        "count_scope": info.get("count_scope") or "unspecified",
        "checked_at": info.get("checked_at") or "",
        "invoice_no": info.get("invoice_no") or "",
    }


def _readable_count(result: dict) -> int:
    count = result.get("count")
    return count if type(count) is int and count > 0 else 0


def latest_inspection(results: list[dict]) -> dict | None:
    """按材料里的查验时间选最新的一份；缺少时间时退回添加时间。"""
    if not results:
        return None
    return max(results, key=lambda result: (
        (result.get("checked_at") or result.get("added_at") or "").replace("T", " ", 1),
        _readable_count(result), result.get("added_at") or "", result.get("attachment_id") or "",
    ))


def inspection_overview(invoice_no: str, items: list[tuple[dict, dict | None]]) -> dict | None:
    """卡片上的查验单标记：共几份，以及最新一份是第几次查验。没有查验单时返回 None。

    items 是 (附件行, 正文信息) 的列表。正文里的发票号码与条目不同的不参与判断，
    与「识别提醒」用同一条规则。
    """
    if not items:
        return None
    results = []
    for attachment, info in items:
        info = info or {}
        if info.get("invoice_no") and invoice_no and info["invoice_no"] != invoice_no:
            continue
        results.append({**info, "added_at": attachment["added_at"], "attachment_id": attachment["id"]})
    summary = inspection_summary(latest_inspection(results))
    return {"files": len(items), "count": summary["count"],
            "count_scope": summary["count_scope"], "checked_at": summary["checked_at"]}


def latest_inspection_message(results: list[dict]) -> str:
    """按材料里的查验时间选新结果：第 1 次不提示，第 2 次或以后、读不出次数都提示。"""
    latest = latest_inspection(results)
    if latest is None:
        return ""
    count = _readable_count(latest)
    if not count:
        return (f"{INSPECTION_CHECK_PREFIX} 保存的查验单没有读出查验次数，"
                "无法确认是否为第 1 次查验，请打开核对，必要时重新查验。")
    if count == 1:
        return ""
    scope = "当日" if latest.get("count_scope") == "day" else ""
    when = f"（查验时间：{latest['checked_at']}）" if latest.get("checked_at") else ""
    return f"{INSPECTION_CHECK_PREFIX} 保存的查验结果为{scope}第 {count} 次查验{when}。"

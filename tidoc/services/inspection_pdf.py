"""从查验单正文读取查验次数与时间；不联网、不使用 OCR。"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import DependencyError, PyPdfError

INSPECTION_RECOGNITION_VERSION = "inspection-pdf-1"
INSPECTION_CHECK_PREFIX = "[查验单]"


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


def latest_inspection_message(results: list[dict]) -> str:
    """按材料里的查验时间选新结果；第 1 次与读不到的次数不提示。"""
    readable = [result for result in results if type(result.get("count")) is int and result["count"] > 0]
    if not readable:
        return ""
    latest = max(readable, key=lambda result: (
        (result.get("checked_at") or result.get("added_at") or "").replace("T", " ", 1),
        result["count"], result.get("added_at") or "", result.get("attachment_id") or "",
    ))
    if latest["count"] == 1:
        return ""
    scope = "当日" if latest.get("count_scope") == "day" else ""
    when = f"（查验时间：{latest['checked_at']}）" if latest.get("checked_at") else ""
    return f"{INSPECTION_CHECK_PREFIX} 保存的查验结果为{scope}第 {latest['count']} 次查验{when}。"

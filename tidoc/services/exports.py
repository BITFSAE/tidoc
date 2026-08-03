"""面向用户交付的导出文件。

核心包只用标准库生成两类轻量结果：
- 总览 Excel：给用户 / 负责人快速核对条目。
- 规范命名附件 ZIP：把附件按条目分文件夹整理，方便交材料或归档。
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from ..db.entries import EntryRepo
from .summary import build_entry_summary

_TYPE_PREFIX = {
    "invoice_pdf": "发票",
    "invoice_xml": "发票XML",
    "payment_screenshot": "付款截图",
    "physical_image": "实物图",
    "inspection_pdf": "查验单",
    "other": "附件",
}
_STATUS_LABEL = {"draft": "草稿", "partial": "部分材料", "complete": "完整"}
_CHECK_LABEL = {"pass": "校验通过", "warning": "需确认", "blocked": "问题严重"}
_BAD_NAME_CHARS = re.compile(r"[\\/:*?\"<>|\s]+")


def _safe_name(value: object, fallback: str = "未命名", max_len: int = 64) -> str:
    text = str(value or "").strip()
    text = _BAD_NAME_CHARS.sub("_", text).strip("._")
    if not text:
        text = fallback
    return text[:max_len]


def _money(value: object) -> str:
    return str(value or "")


def _entry_row(entry: dict, profile_lookup: dict[str, dict], idx: int) -> list[object]:
    summary = build_entry_summary(entry)
    profile = profile_lookup.get(entry.get("profile_id") or "", {})
    comp = entry.get("completeness") or {}
    missing = "、".join(comp.get("missing") or [])
    return [
        idx,
        profile.get("name", ""),
        profile.get("reviewer", ""),
        summary["title"],
        _STATUS_LABEL.get(summary["status"], summary["status"]),
        "齐全" if comp.get("ready") else (f"待补：{missing}" if missing else ""),
        _CHECK_LABEL.get(summary["check_status"], summary["check_status"]),
        summary["invoice_no"],
        summary["invoice_date"],
        summary["seller"],
        _money(summary["total"]),
        summary["paid_amount"],
        summary["actual_item_name"],
        summary["notes"],
        len(entry.get("attachments") or []),
    ]


def _xlsx_col(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _cell(value: object, row: int, col: int) -> str:
    ref = f"{_xlsx_col(col)}{row}"
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value or ""))}</t></is></c>'


def _sheet_xml(rows: list[list[object]]) -> str:
    row_xml = []
    for r_idx, row in enumerate(rows, start=1):
        cells = "".join(_cell(value, r_idx, c_idx) for c_idx, value in enumerate(row, start=1))
        row_xml.append(f'<row r="{r_idx}">{cells}</row>')
    widths = "".join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
                     for i, w in enumerate([7, 12, 12, 18, 10, 24, 12, 24, 12, 28, 12, 12, 22, 34, 8], start=1))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<cols>{widths}</cols><sheetData>{"".join(row_xml)}</sheetData></worksheet>'
    )


def export_overview_xlsx(entries_repo: EntryRepo, profile_lookup: dict[str, dict],
                         entry_ids: list[str], out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    headers = ["序号", "报账人", "审核人", "抬头", "状态", "材料状态", "校验", "发票号码", "发票日期",
               "销售方", "价税合计", "实付金额", "实际物资名称", "备注", "附件数"]
    rows: list[list[object]] = [headers]
    for idx, eid in enumerate(entry_ids, start=1):
        entry = entries_repo.get(eid)
        if entry:
            rows.append(_entry_row(entry, profile_lookup, idx))

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>'
        ))
        zf.writestr("_rels/.rels", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'
        ))
        zf.writestr("xl/workbook.xml", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="报账总览" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ))
        zf.writestr("xl/_rels/workbook.xml.rels", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>'
        ))
        zf.writestr("xl/worksheets/sheet1.xml", _sheet_xml(rows))
    return out


def export_attachment_zip(entries_repo: EntryRepo, attachments_root: str | Path,
                          profile_lookup: dict[str, dict], entry_ids: list[str],
                          out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    root = Path(attachments_root)
    manifest: list[str] = ["tidoc 附件整理包", "", "命名规则：序号_发票号_销售方_金额/附件类型_序号.扩展名", ""]
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, eid in enumerate(entry_ids, start=1):
            entry = entries_repo.get(eid)
            if not entry:
                continue
            profile = profile_lookup.get(entry.get("profile_id") or "", {})
            folder = "_".join([
                f"{idx:03d}",
                _safe_name(entry.get("invoice_no"), "无发票号", 28),
                _safe_name(entry.get("seller"), "未识别销售方", 28),
                _safe_name(entry.get("total"), "无金额", 16),
            ])
            manifest.append(f"{folder}  报账人：{profile.get('name', '')}  抬头：{entry.get('title', '')}")
            counts: dict[str, int] = {}
            for att in entry.get("attachments") or []:
                src = root / att["stored_path"]
                if not src.exists():
                    manifest.append(f"  - 缺失：{att.get('original_name', '')}")
                    continue
                prefix = _TYPE_PREFIX.get(att.get("type"), "附件")
                counts[prefix] = counts.get(prefix, 0) + 1
                ext = src.suffix or Path(att.get("original_name") or "").suffix
                arcname = f"{folder}/{prefix}_{counts[prefix]:02d}{ext}"
                zf.write(src, arcname)
                manifest.append(f"  - {arcname} <- {att.get('original_name', '')}")
            manifest.append("")
        zf.writestr("清单.txt", "\n".join(manifest))
    return out

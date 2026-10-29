"""查验单正文读取、按新结果消除旧次数提醒，以及材料生命周期。"""

import json

import pytest
from pypdf import PdfWriter

from tidoc.engine.models import ParsedInvoice
from tidoc.services.inspection_pdf import (
    INSPECTION_RECOGNITION_VERSION,
    latest_inspection_message,
    parse_inspection_text,
    read_inspection_pdf,
)

NUMBER = "26000000000000000001"


@pytest.mark.parametrize("header,scope", [
    ("当日查验次数：第2次", "day"),
    ("当⽇ 查 验 次 数 ： 第 ２ 次", "day"),
    ("查验次数:第2次", "unspecified"),
])
def test_header_normalizes_glyphs_whitespace_and_daily_scope(header, scope):
    info = parse_inspection_text(f"{header}\n查验时间：2026-10-29 13:56:27\n发票号码：{NUMBER}")
    assert info == {"invoice_no": NUMBER, "count": 2, "count_scope": scope,
                    "checked_at": "2026-10-29 13:56:27"}


@pytest.mark.parametrize("header", ["", "查验次数：第0次", "查验次数：第-2次", "第2次", "查验次数：第2次\n查验次数：第3次"])
def test_missing_invalid_or_conflicting_counts_are_unknown(header):
    assert parse_inspection_text(header)["count"] is None


def test_repeated_page_headers_are_not_conflicting():
    header = f"当日查验次数：第3次 查验时间：2026-10-29 13:56:27 发票号码：{NUMBER}"
    info = parse_inspection_text(header + "\n" + header)
    assert info["count"] == 3
    assert info["checked_at"] == "2026-10-29 13:56:27"


def test_bad_time_is_not_used_for_ordering():
    info = parse_inspection_text("当日查验次数：第2次 查验时间：2026-02-30 13:00:00")
    assert info["count"] == 2
    assert info["checked_at"] == ""


@pytest.mark.parametrize("stamp,expected", [
    ("2026-1-1 12:03:04", "2026-01-01 12:03:04"),
    ("2026-1-11 2:03:04", "2026-01-11 02:03:04"),
])
def test_non_padded_date_and_hour_keep_their_boundary(stamp, expected):
    assert parse_inspection_text(f"查验次数：第2次 查验时间：{stamp}")["checked_at"] == expected


def test_pdf_metadata_and_filename_never_supply_the_count(tmp_path):
    path = tmp_path / "当日查验次数：第4次.pdf"
    writer = PdfWriter()
    writer.add_blank_page(842, 595)
    writer.add_metadata({"/Title": f"当日查验次数：第4次 查验时间：2026-10-29 13:56:27 发票号码：{NUMBER}"})
    writer.write(path)
    assert read_inspection_pdf(path)["count"] is None
    path.write_bytes(b"broken pdf")
    assert read_inspection_pdf(path)["count"] is None


def test_latest_check_time_wins_over_maximum_count_and_late_import():
    old = {"count": 4, "count_scope": "day", "checked_at": "2026-10-28 18:00:00", "added_at": "2026-10-30"}
    new = {"count": 1, "count_scope": "day", "checked_at": "2026-10-29 08:00:00"}
    assert latest_inspection_message([old, new]) == ""
    new["count"] = 2
    message = latest_inspection_message([old, new])
    assert "当日第 2 次" in message
    assert "2026-10-29 08:00:00" in message
    assert "第 4 次" not in message
    new["count_scope"] = "unspecified"
    assert "当日" not in latest_inspection_message([new])


def test_missing_check_time_uses_comparable_added_time_format():
    known = {"count": 1, "checked_at": "2026-10-29 09:00:00"}
    undated = {"count": 2, "added_at": "2026-10-29T08:00:00"}
    assert latest_inspection_message([known, undated]) == ""


def make_pdf(path, count=2, when="2026-10-29 13:56:27", number=NUMBER):
    pytest.importorskip("reportlab")
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    pdf = canvas.Canvas(str(path))
    pdf.setFont("STSong-Light", 10)
    for index, line in enumerate([f"当日查验次数：第{count}次", f"查验时间：{when}", f"发票号码：{number}"]):
        pdf.drawString(40, 800 - 18 * index, line)
    pdf.save()
    return path


def new_entry(api):
    profile = api.profiles.create("测试报账人", "测试审核人")
    entry_id = api.entries.create(profile["id"], parsed=ParsedInvoice(invoice_no=NUMBER))
    api.entries.set_check(entry_id, "pass")
    return entry_id


def test_real_pdf_material_lifecycle_and_invoice_rechecks(api, tmp_path):
    entry_id = new_entry(api)
    first = make_pdf(tmp_path / "first.pdf", 1)
    later = make_pdf(tmp_path / "later.pdf", 2)
    next_day = make_pdf(tmp_path / "next.pdf", 1, "2026-10-30 09:00:00")
    attachment = api.add_attachment(entry_id, str(first), "inspection_pdf")["data"]
    assert api.entries.get(entry_id)["check_status"] == "pass"
    before_complete = api.entries.get(entry_id)["completeness"]
    assert api.update_attachment(attachment["id"], {"src_path": str(later)})["ok"]
    entry = api.entries.get(entry_id)
    assert "当日第 2 次" in entry["check_message"]
    assert entry["check_status"] == "warning"
    assert entry["completeness"] == before_complete
    assert entry_id in {entry["id"] for entry in api.entries.list(check_status="warning")}

    api.entries.set_check(entry_id, "warning", "发票字段需要核对")
    assert "当日第 2 次" in api.entries.get(entry_id)["check_message"]
    api.entries.replace_recognized_items(entry_id, [], "pdf", "pass")
    assert "当日第 2 次" in api.entries.get(entry_id)["check_message"]
    newer = api.add_attachment(entry_id, str(next_day), "inspection_pdf")["data"]
    assert api.entries.get(entry_id)["check_status"] == "pass"
    api.delete_attachment(newer["id"])
    assert "当日第 2 次" in api.entries.get(entry_id)["check_message"]
    api.delete_attachment(attachment["id"])
    assert api.entries.get(entry_id)["check_status"] == "pass"


def test_reclassification_clears_only_inspection_reminder(api, tmp_path):
    entry_id = new_entry(api)
    att = api.add_attachment(entry_id, str(make_pdf(tmp_path / "check.pdf")), "inspection_pdf")["data"]
    api.entries.set_check(entry_id, "blocked", "抬头不一致；[付款截图识别] 付款金额待核对")
    api.update_attachment(att["id"], {"type": "other"})
    entry = api.entries.get(entry_id)
    assert "[查验单]" not in entry["check_message"]
    assert "[付款截图识别]" in entry["check_message"]
    assert entry["check_status"] == "blocked"


def test_startup_reads_old_material_once_and_reuses_the_cache(api, tmp_path, monkeypatch):
    from tidoc.api import Api
    from tidoc.services import inspection_pdf

    entry_id = new_entry(api)
    api.attachments.add(entry_id, make_pdf(tmp_path / "old.pdf"), "inspection_pdf")
    api.db.conn.execute("UPDATE entries SET status_engine_version='adapter-policy-1' WHERE id=?", (entry_id,))
    api.db.conn.commit()
    reads = []
    real_read = inspection_pdf.read_inspection_pdf

    def read(path):
        reads.append(path)
        return real_read(path)

    monkeypatch.setattr(inspection_pdf, "read_inspection_pdf", read)
    fresh = Api(api.data_root.root)
    assert "当日第 2 次" in fresh.entries.get(entry_id)["check_message"]
    assert len(reads) == 1
    row = fresh.db.conn.execute("SELECT recognition_version,recognized_value FROM attachments WHERE entry_id=?", (entry_id,)).fetchone()
    assert row["recognition_version"] == INSPECTION_RECOGNITION_VERSION
    assert json.loads(row["recognized_value"])["checked_at"] == "2026-10-29 13:56:27"
    fresh.entries.recompute_status(entry_id)
    assert len(reads) == 1
    fresh.db.close()

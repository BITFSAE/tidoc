"""查验单正文读取、按新结果消除旧次数提醒，以及材料生命周期。"""

import json

import pytest
from pypdf import PdfWriter

from tidoc.engine.models import ParsedInvoice
from tidoc.services.inspection_pdf import (
    INSPECTION_RECOGNITION_VERSION,
    cached_inspection_info,
    inspection_summary,
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


def test_unreadable_count_is_a_reminder_unless_a_newer_first_check_exists():
    unreadable = {"count": None, "added_at": "2026-10-29T08:00:00", "attachment_id": "a"}
    message = latest_inspection_message([unreadable])
    assert message.startswith("[查验单]") and "没有读出查验次数" in message
    # 较新的第 1 次查验单可以确认这是第一次查验，不再提示。
    newer_first = {"count": 1, "checked_at": "2026-10-29 09:00:00"}
    assert latest_inspection_message([unreadable, newer_first]) == ""
    # 更晚添加的读不出次数，就轮到它提示。
    later_unreadable = {"count": None, "added_at": "2026-10-30T08:00:00", "attachment_id": "b"}
    assert "没有读出查验次数" in latest_inspection_message([newer_first, later_unreadable])
    assert latest_inspection_message([]) == ""


def test_second_check_and_unreadable_count_use_different_reminders():
    second = latest_inspection_message([{"count": 2, "count_scope": "day", "checked_at": "2026-10-29 08:00:00"}])
    assert "当日第 2 次" in second and "没有读出" not in second


def test_cached_info_needs_matching_version_and_digest():
    stored = json.dumps({"count": 3, "sha256": "abc"})
    assert cached_inspection_info(INSPECTION_RECOGNITION_VERSION, stored, "abc")["count"] == 3
    assert cached_inspection_info(INSPECTION_RECOGNITION_VERSION, stored, "other") is None
    assert cached_inspection_info("old-version", stored, "abc") is None
    assert cached_inspection_info(INSPECTION_RECOGNITION_VERSION, "not json", "abc") is None
    assert cached_inspection_info(INSPECTION_RECOGNITION_VERSION, "", "abc") is None


def test_summary_keeps_only_what_the_detail_shows():
    assert inspection_summary(None) == {"count": None, "count_scope": "unspecified", "checked_at": "", "invoice_no": ""}
    summary = inspection_summary({"count": 2, "count_scope": "day", "checked_at": "2026-10-29 13:56:27",
                                  "invoice_no": NUMBER, "sha256": "abc"})
    assert summary == {"count": 2, "count_scope": "day", "checked_at": "2026-10-29 13:56:27", "invoice_no": NUMBER}
    assert inspection_summary({"count": 0})["count"] is None


def make_pdf(path, count=2, when="2026-10-29 13:56:27", number=NUMBER, header=None):
    """header 不为空时只写这一行，用来做读不出查验次数的查验单。"""
    pytest.importorskip("reportlab")
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    pdf = canvas.Canvas(str(path))
    pdf.setFont("STSong-Light", 10)
    lines = [header] if header else [
        f"当日查验次数：第{count}次", f"查验时间：{when}", f"发票号码：{number}"]
    for index, line in enumerate(lines):
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
    newer = api.add_attachment(entry_id, str(next_day), "inspection_pdf", options={
        "inspection_action": "keep", "inspection_existing_ids": [attachment["id"]],
    })["data"]
    assert api.entries.get(entry_id)["check_status"] == "warning"
    assert "2 份查验单" in api.entries.get(entry_id)["check_message"]
    assert "当日第 2 次" not in api.entries.get(entry_id)["check_message"]
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


def test_inspection_for_another_invoice_number_is_a_reminder(api, tmp_path):
    entry_id = new_entry(api)
    api.add_attachment(entry_id, str(make_pdf(tmp_path / "check.pdf", 2)), "inspection_pdf")
    assert "当日第 2 次" in api.entries.get(entry_id)["check_message"]

    # 发票号码改过以后，原来的查验单对不上了：给出查验单类提醒，不再用它的查验次数。
    other = "26000000000000000002"
    assert api.correct_locked_field(entry_id, "invoice_no", other)["ok"]
    entry = api.entries.get(entry_id)
    assert entry["check_status"] == "warning"
    assert "当日第 2 次" not in entry["check_message"]
    mismatch = [line for line in entry["check_lines"] if NUMBER in line["text"]]
    assert len(mismatch) == 1 and other in mismatch[0]["text"]
    assert mismatch[0]["category"] == "inspection"
    assert "inspection" in entry["check_open"]

    assert api.correct_locked_field(entry_id, "invoice_no", NUMBER)["ok"]
    entry = api.entries.get(entry_id)
    assert not [line for line in entry["check_lines"] if "不一致" in line["text"]]
    assert "当日第 2 次" in entry["check_message"]


def test_detail_lists_each_inspection_pdf_with_its_check_number(api, tmp_path):
    entry_id = new_entry(api)
    first = api.add_attachment(entry_id, str(make_pdf(tmp_path / "first.pdf", 1)), "inspection_pdf")["data"]
    second = api.add_attachment(entry_id, str(make_pdf(tmp_path / "second.pdf", 3, "2026-10-30 09:00:00")),
                                "inspection_pdf", options={
        "inspection_action": "keep", "inspection_existing_ids": [first["id"]]})["data"]
    by_id = {att["id"]: att for att in api.entries.get(entry_id)["attachments"]}
    assert by_id[first["id"]]["inspection"] == {
        "count": 1, "count_scope": "day", "checked_at": "2026-10-29 13:56:27", "invoice_no": NUMBER}
    assert by_id[second["id"]]["inspection"]["count"] == 3
    assert api.entries.inspection_info(entry_id, second["id"])["checked_at"] == "2026-10-30 09:00:00"
    assert api.entries.inspection_info(entry_id, "missing") is None


def test_detail_reads_old_material_without_cache_and_other_types_have_no_summary(api, tmp_path):
    entry_id = new_entry(api)
    att = api.attachments.add(entry_id, make_pdf(tmp_path / "old.pdf", 2), "inspection_pdf")
    other = api.attachments.add(entry_id, make_pdf(tmp_path / "doc.pdf", 2), "other")
    assert not api.attachments.get(att["id"])["recognized_value"]
    by_id = {a["id"]: a for a in api.entries.get(entry_id)["attachments"]}
    assert by_id[att["id"]]["inspection"]["count"] == 2
    assert "inspection" not in by_id[other["id"]]
    # 只是读取，没有写库。
    assert not api.attachments.get(att["id"])["recognized_value"]


def test_inspection_without_a_readable_count_is_a_reminder_and_keeps_materials_complete(api, tmp_path):
    entry_id = new_entry(api)
    uncounted = make_pdf(tmp_path / "scan.pdf", header=f"全国增值税发票查验平台　发票号码：{NUMBER}")
    complete = api.entries.get(entry_id)["completeness"]
    added = api.add_attachment(entry_id, str(uncounted), "inspection_pdf")
    assert added["ok"], added
    att = added["data"]
    entry = api.entries.get(entry_id)
    assert entry["check_status"] == "warning"
    lines = [line for line in entry["check_lines"] if line["category"] == "inspection"]
    assert len(lines) == 1 and "没有读出查验次数" in lines[0]["text"]
    assert "inspection" in entry["check_open"]
    assert entry["completeness"]["missing"] == complete["missing"]
    [detail] = [a for a in entry["attachments"] if a["id"] == att["id"]]
    assert detail["inspection"]["count"] is None

    # 换成正文写明第 1 次的查验单后，提醒消失。
    assert api.update_attachment(att["id"], {"src_path": str(make_pdf(tmp_path / "ok.pdf", 1))})["ok"]
    entry = api.entries.get(entry_id)
    assert entry["check_status"] == "pass"
    assert not [line for line in entry["check_lines"] if line["category"] == "inspection"]


def test_card_overview_counts_files_and_reports_the_latest_check_in_list_and_detail(api, tmp_path):
    plain = new_entry(api)
    assert api.entries.get(plain)["inspection"] is None
    assert [e for e in api.entries.list() if e["id"] == plain][0]["inspection"] is None

    entry_id = api.entries.create(api.profiles.create("乙", "丙")["id"], parsed=ParsedInvoice(invoice_no=NUMBER))
    first = api.add_attachment(entry_id, str(make_pdf(tmp_path / "a.pdf", 1)), "inspection_pdf")["data"]
    listed = [e for e in api.entries.list() if e["id"] == entry_id][0]["inspection"]
    assert listed == {"files": 1, "count": 1, "count_scope": "day", "checked_at": "2026-10-29 13:56:27"}

    api.add_attachment(entry_id, str(make_pdf(tmp_path / "b.pdf", 4, "2026-10-30 09:00:00")), "inspection_pdf", options={
        "inspection_action": "keep", "inspection_existing_ids": [first["id"]]})
    expected = {"files": 2, "count": 4, "count_scope": "day", "checked_at": "2026-10-30 09:00:00"}
    assert [e for e in api.entries.list() if e["id"] == entry_id][0]["inspection"] == expected
    assert api.entries.get(entry_id)["inspection"] == expected


def test_card_overview_for_unreadable_and_foreign_inspections(api, tmp_path):
    entry_id = new_entry(api)
    api.add_attachment(entry_id, str(make_pdf(tmp_path / "scan.pdf", header=f"全国增值税发票查验平台　发票号码：{NUMBER}")),
                       "inspection_pdf")
    assert api.entries.get(entry_id)["inspection"]["count"] is None
    assert api.entries.get(entry_id)["inspection"]["files"] == 1
    # 号码与条目不同的查验单不参与次数判断，但仍算一份。
    other = api.entries.create(api.profiles.create("丁", "戊")["id"], parsed=ParsedInvoice(invoice_no="26000000000000000002"))
    api.attachments.add(other, make_pdf(tmp_path / "foreign.pdf", 3), "inspection_pdf")
    api.entries.recompute_status(other)
    overview = [e for e in api.entries.list() if e["id"] == other][0]["inspection"]
    assert overview["files"] == 1 and overview["count"] is None


def test_list_does_not_open_pdfs_for_the_card_badge(api, tmp_path, monkeypatch):
    from tidoc.services import inspection_pdf

    entry_id = new_entry(api)
    api.add_attachment(entry_id, str(make_pdf(tmp_path / "a.pdf", 2)), "inspection_pdf")

    def refuse(path):
        raise AssertionError("列表不能为卡片标记逐个读取 PDF")

    monkeypatch.setattr(inspection_pdf, "read_inspection_pdf", refuse)
    assert api.entries.list()[0]["inspection"]["count"] == 2


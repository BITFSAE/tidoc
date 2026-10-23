"""Parser updates prompt without silently changing saved invoice data."""

from decimal import Decimal

from tidoc.db import TYPE_INVOICE_PDF
from tidoc.db import entries as entry_module
from tidoc.engine.models import ParsedInvoice, ParsedItem


def make_invoice(api, tmp_path, suffix="1", *, attachment=True):
    profile = api.profiles.create("测试报账人" + suffix, "测试审核人")
    parsed = ParsedInvoice(
        invoice_no=suffix.zfill(20), total=Decimal("12.00"),
        items=[ParsedItem("*测试*零件", "零件", "个", Decimal("2"), Decimal("12.00"))],
    )
    entry_id = api.entries.create(profile["id"], parsed=parsed)
    if attachment:
        path = tmp_path / (suffix + ".pdf")
        path.write_bytes(b"fictional PDF fixture")
        api.attachments.add(entry_id, path, TYPE_INVOICE_PDF)
        api.entries.mark_invoice_recognized(entry_id)
    return entry_id, parsed, profile["id"]


def test_old_and_unmarked_invoices_prompt_but_current_and_missing_material_do_not(api, tmp_path):
    old, _, profile_id = make_invoice(api, tmp_path)
    current, _, _ = make_invoice(api, tmp_path, "2")
    missing, _, _ = make_invoice(api, tmp_path, "3", attachment=False)
    unmarked, _, _ = make_invoice(api, tmp_path, "4")
    api.db.conn.execute("UPDATE entries SET recognition_version='old-parser' WHERE id=?", (old,))
    api.entries.clear_invoice_recognition(unmarked)
    before = api.entries.get(old)

    rows = {e["id"]: e for e in api.list_entries()["data"]}

    assert rows[old]["invoice_recognition_outdated"] is True
    assert rows[unmarked]["invoice_recognition_outdated"] is True
    assert rows[current]["invoice_recognition_outdated"] is False
    assert rows[missing]["invoice_recognition_outdated"] is False
    assert [e["id"] for e in api.list_entries({"profile_id": profile_id})["data"]] == [old]
    assert api.entries.get(old) == before


def test_dismissal_persists_for_this_data_directory_and_version_only(api, tmp_path, monkeypatch):
    from tidoc.api import Api

    version = entry_module.LOCAL_INVOICE_RECOGNITION_VERSION
    assert api.invoice_recognition_notice()["data"] == {"version": version, "dismissed": False}
    assert api.dismiss_invoice_recognition_notice("older-version")["ok"] is False
    assert api.dismiss_invoice_recognition_notice(version)["data"]["dismissed"] is True
    reopened = Api(api.data_root.root)
    other = Api(tmp_path / "other-data")
    try:
        assert reopened.invoice_recognition_notice()["data"]["dismissed"] is True
        assert other.invoice_recognition_notice()["data"]["dismissed"] is False
        monkeypatch.setattr(entry_module, "LOCAL_INVOICE_RECOGNITION_VERSION", "future-parser")
        assert reopened.invoice_recognition_notice()["data"] == {
            "version": "future-parser", "dismissed": False,
        }
    finally:
        reopened.db.close()
        other.db.close()


def test_notice_clears_after_success_but_failed_recognition_stays_outdated(api, tmp_path, monkeypatch):
    entry_id, parsed, _ = make_invoice(api, tmp_path)
    api.entries.update_field(entry_id, "notes", "保留的备注")
    api.entries.update_field(entry_id, "paid_amount", "10.00")
    api.db.conn.execute("UPDATE entries SET recognition_version='old-parser' WHERE id=?", (entry_id,))
    api.db.conn.commit()

    def failed(*args, **kwargs):
        raise ValueError("测试读取失败")

    monkeypatch.setattr("tidoc.engine.parse_invoice_files", failed)
    result = api.rerecognize_materials([entry_id], ["invoice"])
    assert len(result["data"]["invoice"]["failed"]) == 1
    assert api.list_entries()["data"][0]["invoice_recognition_outdated"] is True

    monkeypatch.setattr("tidoc.engine.parse_invoice_files", lambda *args, **kwargs: parsed)
    result = api.rerecognize_materials([entry_id], ["invoice"])
    assert result["data"]["invoice"]["processed"] == 1
    assert api.list_entries()["data"][0]["invoice_recognition_outdated"] is False
    saved = api.entries.get(entry_id)
    assert saved["fields"]["notes"]["current"] == "保留的备注"
    assert saved["fields"]["paid_amount"]["current"] == "10.00"

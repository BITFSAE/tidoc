"""Only current manual differences enter the modified view; history is preserved."""
import json
from decimal import Decimal

import pytest

from tidoc.db.entries import LOCKED_FIELDS, PAYMENT_AI_RECOGNITION_VERSION, VALUE_SOURCE_PAYMENT_AI, VALUE_SOURCE_PAYMENT_OCR
from tidoc.db.ocr_results import OcrRepo
from tidoc.engine import ParsedInvoice, ParsedItem
from tidoc.services.summary import build_summary
from tests.test_selection_performance import run_selection


def make_entry(repos):
    profile = repos["profiles"].create("测试人员", "")
    return repos["entries"].create(profile["id"], parsed=ParsedInvoice(
        invoice_no="26950000000000000001", total=Decimal("7.00"), source="pdf",
        items=[ParsedItem(name="测试物资", actual_name="测试物资", unit="只", quantity=Decimal("1"), total=Decimal("7.00"))],
    ))


def assert_modified(repos, entry_id, field):
    entries = repos["entries"]
    listed = {entry["id"]: entry for entry in entries.list(modified_only=True)}
    assert field in listed[entry_id]["modified_fields"]
    assert entries.get(entry_id)["modified_fields"] == listed[entry_id]["modified_fields"]


@pytest.mark.parametrize("field", LOCKED_FIELDS)
def test_invoice_corrections_leave_modified_view_when_restored(repos, field):
    entry_id = make_entry(repos)
    entries = repos["entries"]
    original = entries.get(entry_id)[field]
    entries.correct_locked_field(entry_id, field, "8.00" if field == "total" else "人工修正")
    assert_modified(repos, entry_id, field)
    entries.correct_locked_field(entry_id, field, original)
    assert entries.get(entry_id)["modified_fields"] == []
    assert entries.list(modified_only=True) == []
    assert len(entries.history(entry_id)) == 2
    assert build_summary(entries, [entry_id])["entries"][0]["modified_fields"] == []


@pytest.mark.parametrize("field", ["paid_amount", "actual_item_name"])
def test_editable_fields_leave_modified_view_after_reverting(repos, field):
    entry_id = make_entry(repos)
    entries = repos["entries"]
    original = entries.get(entry_id)["fields"][field]["current"]
    entries.update_field(entry_id, field, "8.00" if field == "paid_amount" else "手动填写")
    assert_modified(repos, entry_id, field)
    entries.update_field(entry_id, field, original)
    assert entries.get(entry_id)["modified_fields"] == []
    assert entries.list(modified_only=True) == []
    assert entries.get(entry_id)["fields"][field]["modified"]
    # Old binding packages may carry modified flags without complete history.
    entries.update_field(entry_id, field, "9.00" if field == "paid_amount" else "另一个物资名")
    repos["db"].conn.execute("DELETE FROM field_history WHERE entry_id=?", (entry_id,))
    assert_modified(repos, entry_id, field)


@pytest.mark.parametrize("source", [VALUE_SOURCE_PAYMENT_AI, VALUE_SOURCE_PAYMENT_OCR])
def test_automatic_recognition_and_unchanged_saves_are_not_manual_changes(repos, source):
    entry_id = make_entry(repos)
    entries = repos["entries"]
    entries.update_field(entry_id, "notes", "")
    entries.correct_locked_field(entry_id, "invoice_no", entries.get(entry_id)["invoice_no"])
    entries.update_field(entry_id, "paid_amount", "8.00", value_source=source)
    entries.ocr_update_locked_field(entry_id, "seller", "自动补齐")
    entries.update_recognized_buyer_tax_id(entry_id, "自动补齐")
    assert entries.get(entry_id)["modified_fields"] == []
    assert entries.list(modified_only=True) == []
    entries.update_field(entry_id, "paid_amount", "9.00")
    entries.update_field(entry_id, "paid_amount", "8.00", value_source=source)
    assert entries.get(entry_id)["modified_fields"] == []
    assert entries.list(modified_only=True) == []


def test_equivalent_money_formats_are_not_manual_differences(repos):
    entries = repos["entries"]
    entry_id = make_entry(repos)
    entries.update_field(entry_id, "paid_amount", "7")
    entries.correct_locked_field(entry_id, "total", "7.0")
    assert entries.get(entry_id)["modified_fields"] == []
    entries.update_field(entry_id, "paid_amount", "6.50")
    assert_modified(repos, entry_id, "paid_amount")


def test_new_parser_evidence_can_confirm_a_manual_value_without_erasing_history(repos):
    entries = repos["entries"]
    entry_id = make_entry(repos)
    entries.correct_locked_field(entry_id, "seller", "补全后的销售方")
    entries.update_field(entry_id, "actual_item_name", "补全后的物资")
    assert entries.get(entry_id)["modified_fields"] == ["actual_item_name", "seller"]
    parsed = ParsedInvoice(invoice_no="26950000000000000001", seller="补全后的销售方", total=Decimal("7"),
                           items=[ParsedItem("补全后的物资", "补全后的物资", "只", Decimal("1"), Decimal("7"))])
    before = entries.history(entry_id)
    entries.set_invoice_evidence(entry_id, parsed)
    assert entries.get(entry_id)["modified_fields"] == []
    assert entries.list(modified_only=True) == []
    assert entries.history(entry_id) == before


def test_later_automatic_update_removes_manual_ownership(repos):
    entries = repos["entries"]
    entry_id = make_entry(repos)
    entries.correct_locked_field(entry_id, "seller", "人工填写")
    entries.ocr_update_locked_field(entry_id, "seller", "识别补齐")
    assert entries.get(entry_id)["modified_fields"] == []
    entries.correct_locked_field(entry_id, "seller", "再次修改")
    assert_modified(repos, entry_id, "seller")
    entries.correct_locked_field(entry_id, "seller", "识别补齐")
    assert entries.list(modified_only=True) == []


def test_item_edit_revert_add_delete_and_recognition_replace(repos):
    entries = repos["entries"]
    entry_id = make_entry(repos)
    item = entries.get(entry_id)["items"][0]
    entries.update_item(item["id"], {"spec": "人工规格"})
    assert_modified(repos, entry_id, "items")
    entries.update_item(item["id"], {"spec": item["spec"]})
    assert entries.list(modified_only=True) == []
    added = entries.add_item(entry_id, name="临时补充")
    assert_modified(repos, entry_id, "items")
    entries.delete_item(added["id"])
    assert entries.list(modified_only=True) == []
    entries.update_item(item["id"], {"total": "9.00"})
    recognized = [ParsedItem("识别出来的新名称", "识别出来的新名称", "只", Decimal("1"), Decimal("8"))]
    entries.replace_recognized_items(entry_id, recognized, "pdf", "pass")
    assert entries.get(entry_id)["modified_fields"] == []
    item = entries.get(entry_id)["items"][0]
    entries.update_item(item["id"], {"spec": "再次修改"})
    assert_modified(repos, entry_id, "items")
    entries.update_item(item["id"], {"spec": ""})
    assert entries.list(modified_only=True) == []
    assert any(h["field"] == "[识别结果]items" for h in entries.history(entry_id))


def test_legacy_item_changes_can_be_restored_without_local_parser_evidence(repos):
    entries = repos["entries"]
    entry_id = make_entry(repos)
    repos["db"].conn.execute("UPDATE entries SET recognition_data='' WHERE id=?", (entry_id,))
    original = entries.get(entry_id)["items"][0]
    entries.update_item(original["id"], {"name": "人工修改"})
    assert_modified(repos, entry_id, "items")
    entries.update_item(original["id"], {"name": original["name"]})
    assert entries.list(modified_only=True) == []


def test_saved_ocr_only_confirms_values_for_current_invoice_files(repos, tmp_path):
    entries = repos["entries"]
    entry_id = make_entry(repos)
    entries.correct_locked_field(entry_id, "seller", "核对后的销售方")
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"fictional invoice")
    attachment = repos["attachments"].add(entry_id, path, "invoice_pdf")
    ocr = OcrRepo(repos["db"])
    result = json.dumps({"seller": "核对后的销售方"}, ensure_ascii=False)
    ocr.record(entry_id, file_sha256="different-file", normalized=result)
    assert_modified(repos, entry_id, "seller")
    ocr.record(entry_id, file_sha256=attachment["sha256"], normalized=result)
    assert entries.list(modified_only=True) == []
    repos["db"].conn.execute("UPDATE attachments SET sha256=? WHERE id=?", ("replaced-file", attachment["id"]))
    assert_modified(repos, entry_id, "seller")


def test_only_complete_current_payment_recognition_confirms_manual_amount(repos, tmp_path):
    entries, attachments = repos["entries"], repos["attachments"]
    entry_id = make_entry(repos)
    entries.update_field(entry_id, "paid_amount", "6.50")
    assert_modified(repos, entry_id, "paid_amount")
    path = tmp_path / "payment.jpg"
    path.write_bytes(b"fictional payment")
    first = attachments.add(entry_id, path, "payment_screenshot")
    attachments.set_recognition(first["id"], PAYMENT_AI_RECOGNITION_VERSION, "recognized", "6.5")
    assert entries.list(modified_only=True) == []
    attachments.set_recognition(first["id"], PAYMENT_AI_RECOGNITION_VERSION, "review", "6.5")
    assert_modified(repos, entry_id, "paid_amount")
    attachments.set_recognition(first["id"], "old-version", "recognized", "6.5")
    assert_modified(repos, entry_id, "paid_amount")
    path.write_bytes(b"another fictional payment")
    second = attachments.add(entry_id, path, "payment_screenshot")
    attachments.set_recognition(first["id"], PAYMENT_AI_RECOGNITION_VERSION, "recognized", "3")
    attachments.set_recognition(second["id"], PAYMENT_AI_RECOGNITION_VERSION, "recognized", "3.5")
    assert entries.list(modified_only=True) == []
    attachments.set_recognition(second["id"], PAYMENT_AI_RECOGNITION_VERSION, "failed")
    assert_modified(repos, entry_id, "paid_amount")


def test_organizing_changes_are_excluded_but_item_edits_are_included(repos):
    entries = repos["entries"]
    entry_id = make_entry(repos)
    other = repos["profiles"].create("另一位测试人员", "")
    entries.set_profile(entry_id, other["id"])
    entries.update_field(entry_id, "notes", "整理备注")
    entries.set_meta(entry_id, category="零件", tags=["待核对"])
    entries.add_tag([entry_id], "待交")
    entries.rename_tag("待交", "已交")
    entries.remove_tag([entry_id], "待核对")
    repos["batches"].create("整理批次", entry_ids=[entry_id])
    assert entries.get(entry_id)["modified_fields"] == []
    assert entries.list(modified_only=True) == []
    item = entries.get(entry_id)["items"][0]
    entries.update_item(item["id"], {"spec": "手动规格"})
    assert_modified(repos, entry_id, "items")
    added = entries.add_item(entry_id, name="补充明细")
    entries.delete_item(added["id"])
    history = entries.history(entry_id)
    assert sum(row["field"] == "[人工修正]items" for row in history) == 3


def test_noop_metadata_and_item_saves_are_not_changes(repos):
    entries = repos["entries"]
    entry_id = make_entry(repos)
    entries.set_meta(entry_id, category="", tags=[])
    item = entries.get(entry_id)["items"][0]
    entries.update_item(item["id"], {"name": item["name"]})
    assert entries.list(modified_only=True) == []


def test_modified_view_respects_batch_scope_and_legacy_invoice_history(repos):
    first, second = make_entry(repos), make_entry(repos)
    entries = repos["entries"]
    entries.correct_locked_field(first, "invoice_no", "人工票号")
    entries.update_field(second, "actual_item_name", "人工物资名")
    batch = repos["batches"].create("测试批次", entry_ids=[first])
    assert [entry["id"] for entry in entries.list(modified_only=True, batch_id=batch["id"])] == [first]


def test_manual_marker_survives_frontend_card_refresh_and_reports_invoice_number():
    run_selection(r"""
(async () => {
  const detail = {id: 'changed', total: '7.00', invoice_no: '26950000000000000001',
    modified_fields: ['invoice_no'], fields: {paid_amount: {origin: '7.00', current: '7.00'}}, attachments: []};
  const entry = listEntryFromDetail(detail);
  assert.equal(entryHasPaidDifference(entry), false);
  assert.equal(entryHasManualChanges(entry), true);
  assert.ok(entryModifiedTooltip(entry).includes('发票号码'));
  assert.ok(entryModifiedTooltip(entry).includes(detail.invoice_no));
  const edited = {...entry, modified_fields: ['paid_amount', 'actual_item_name'],
    fields: {paid_amount: {origin: '7.00', current: '6.00'}, actual_item_name: {origin: '测试物资', current: '改过的物资', modified: true}}};
  assert.ok(entryModifiedTooltip(edited).includes('实付金额'));
  assert.ok(entryModifiedTooltip(edited).includes('改过的物资'));
  assert.equal(entryHasManualChanges({...entry, modified_fields: []}), false);
  State.entries = [entry]; State.quickView = 'modified';
  renderEntries = () => { throw new Error('人工修正的条目不应从已修改中移除'); };
  await refreshEntryCard(entry.id, detail);
  assert.equal(State.entries.length, 1);
  updateListSummary();
  assert.ok(document.querySelector('#stats').innerHTML.includes('已改 <b>1</b>'));
  let renders = 0;
  renderEntries = () => renders++;
  await refreshEntryCard(entry.id, {...detail, modified_fields: []});
  assert.equal(State.entries.length, 0);
  assert.equal(renders, 1);
  assert.equal(historyValue('[人工修正]items', '[]'), '无明细');
  assert.ok(historyValue('[人工修正]items', '[{"name":"测试物资","total":"7"}]').includes('金额 7'));
})().catch(e => {console.error(e); process.exitCode = 1;});
""")

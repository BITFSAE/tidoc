"""发票逐条识别版本交接：状态判断、绑定包转发、重复合并与重识别保护。"""

from __future__ import annotations

import json
import zipfile
from decimal import Decimal
from pathlib import Path

from tidoc.db import TYPE_INVOICE_XML
from tidoc.db.entries import (
    LOCAL_INVOICE_RECOGNITION_VERSION,
    compare_invoice_recognition_versions,
    invoice_recognition_version_key,
)
from tidoc.engine.models import ParsedInvoice, ParsedItem
from tidoc.services import bindle as bindle_module

OLDER_VERSION = "invoice-local-2026-11-01-1"
NEWER_VERSION = "invoice-local-2099-01-01-1"
UNKNOWN_VERSION = "old-parser"


def _xml_payload(invoice_no: str, total: str = "12.00", item_name: str = "*零件*螺丝") -> bytes:
    return (
        f'<Invoice><EIid>{invoice_no}</EIid>'
        f'<TotalTax-includedAmount>{total}</TotalTax-includedAmount>'
        f'<SellerName>示例销售方</SellerName>'
        f'<Item><ItemName>{item_name}</ItemName><SpecMod>M3</SpecMod>'
        f'<MeaUnits>个</MeaUnits><Quantity>2</Quantity>'
        f'<Amount>10.00</Amount><ComTaxAm>2.00</ComTaxAm></Item>'
        f'</Invoice>'
    ).encode("utf-8")


def _make_entry(api, tmp_path: Path, suffix: str, *, with_invoice: bool = True,
                content: bytes | None = None, version: str | None = None):
    invoice_no = "26" + suffix.zfill(18)
    profile = api.profiles.create(f"报账人{suffix}", "审核人")
    parsed = ParsedInvoice(
        invoice_no=invoice_no,
        seller="示例销售方",
        total=Decimal("12.00"),
        items=[ParsedItem("*零件*螺丝", "螺丝", "个", Decimal("2"), Decimal("12.00"))],
    )
    entry_id = api.entries.create(profile["id"], parsed=parsed)
    if with_invoice:
        path = tmp_path / f"{suffix}.xml"
        path.write_bytes(content if content is not None else _xml_payload(invoice_no))
        api.attachments.add(entry_id, path, TYPE_INVOICE_XML)
        api.entries.mark_invoice_recognized(entry_id)
        if version is not None:
            api.db.conn.execute(
                "UPDATE entries SET recognition_version = ? WHERE id = ?", (version, entry_id)
            )
            api.db.conn.commit()
    return entry_id, invoice_no


def _state(api, entry_id: str) -> str:
    return next(e for e in api.list_entries()["data"] if e["id"] == entry_id)["invoice_recognition_state"]


def _outdated(api, entry_id: str) -> bool:
    return next(e for e in api.list_entries()["data"] if e["id"] == entry_id)["invoice_recognition_outdated"]


def test_recognition_version_key_compares_date_and_sequence():
    assert invoice_recognition_version_key("invoice-local-2026-09-06") == (2026, 9, 6, 0)
    assert invoice_recognition_version_key("invoice-local-2026-09-06-2") == (2026, 9, 6, 2)
    assert invoice_recognition_version_key("old-parser") is None
    assert invoice_recognition_version_key("invoice-local-2026-13-01") is None
    assert compare_invoice_recognition_versions(OLDER_VERSION) == -1
    assert compare_invoice_recognition_versions(NEWER_VERSION) == 1
    assert compare_invoice_recognition_versions(LOCAL_INVOICE_RECOGNITION_VERSION) == 0
    assert compare_invoice_recognition_versions("invoice-local-2026-11-28") == -1
    assert compare_invoice_recognition_versions(UNKNOWN_VERSION) is None


def test_state_distinguishes_older_newer_unknown_current_changed_and_missing(api, tmp_path):
    older, _ = _make_entry(api, tmp_path, "1", version=OLDER_VERSION)
    newer, _ = _make_entry(api, tmp_path, "2", version=NEWER_VERSION)
    unknown, _ = _make_entry(api, tmp_path, "3", version=UNKNOWN_VERSION)
    current, _ = _make_entry(api, tmp_path, "4")
    changed, _ = _make_entry(api, tmp_path, "5")
    missing, _ = _make_entry(api, tmp_path, "6", with_invoice=False)

    # 原件内容变化后摘要不同，同版也不能当作当前结果。
    api.db.conn.execute(
        "UPDATE attachments SET sha256 = ? WHERE entry_id = ? AND type = 'invoice_xml'",
        ("f" * 64, changed),
    )
    api.db.conn.commit()

    assert _state(api, older) == "older"
    assert _state(api, newer) == "newer"
    assert _state(api, unknown) == "unknown"
    assert _state(api, current) == "current"
    assert _state(api, changed) == "changed"
    assert _state(api, missing) == "missing"

    assert _outdated(api, older) is True
    assert _outdated(api, unknown) is True
    assert _outdated(api, changed) is True
    assert _outdated(api, newer) is False
    assert _outdated(api, current) is False
    assert _outdated(api, missing) is False


def test_preview_lists_pending_ids_and_rerecognition_skips_newer_unless_forced(api, tmp_path):
    older, _ = _make_entry(api, tmp_path, "1", version=OLDER_VERSION)
    newer, _ = _make_entry(api, tmp_path, "2", version=NEWER_VERSION)
    unknown, _ = _make_entry(api, tmp_path, "3", version=UNKNOWN_VERSION)
    current, _ = _make_entry(api, tmp_path, "4")
    missing, _ = _make_entry(api, tmp_path, "5", with_invoice=False)
    ids = [older, newer, unknown, current, missing]

    preview = api.recognition_preview(ids)["data"]["invoice"]
    assert preview["total"] == 4
    assert preview["current"] == 1
    assert preview["older"] == 1
    assert preview["newer"] == 1
    assert preview["unknown"] == 1
    assert preview["changed"] == 0
    assert preview["pending"] == 2
    assert preview["pending_ids"] == [older, unknown]

    result = api.rerecognize_materials(ids, ["invoice"])["data"]["invoice"]
    assert result["processed"] == 2
    assert result["skipped_current"] == 1
    assert result["skipped_newer"] == 1
    assert {r["entry_id"] for r in result["results"]} == {older, unknown}
    assert _state(api, older) == "current"
    assert _state(api, unknown) == "current"
    assert _state(api, newer) == "newer"
    assert _state(api, missing) == "missing"

    forced = api.rerecognize_materials([newer], ["invoice"], force_newer=True)["data"]["invoice"]
    assert forced["processed"] == 1
    assert forced["skipped_newer"] == 0
    assert _state(api, newer) == "current"


def _archive_entries(path: Path) -> dict:
    with zipfile.ZipFile(path) as zf:
        return json.loads(zf.read("entries.json"))


def _rewrite_archive(source: Path, destination: Path, *, replace_member: str | None = None,
                     payload: bytes | None = None, version: int | None = None) -> Path:
    """Copy a signed archive while optionally changing content (invalidates signatures)."""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(destination, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if replace_member and name == replace_member:
                data = payload or b""
            if version is not None and name in {bindle_module.ENTRIES_NAME, bindle_module.MANIFEST_NAME}:
                decoded = json.loads(data)
                decoded["bindle_version"] = version
                data = json.dumps(decoded, ensure_ascii=False).encode("utf-8")
            dst.writestr(name, data)
    return destination


def test_export_writes_real_recognition_version_and_import_restores_it(api, tmp_path):
    from tidoc.api import Api

    entry_id, invoice_no = _make_entry(api, tmp_path, "1", version=OLDER_VERSION)
    fingerprint = api.entries.invoice_recognition_fingerprint(entry_id)
    exported = api.export_bindle([entry_id])
    assert exported["ok"], exported
    payload = _archive_entries(Path(exported["data"]["path"]))
    record = next(r for r in payload["entries"] if r["invoice_no"] == invoice_no)
    assert record["recognition_version"] == OLDER_VERSION
    assert record["recognition_fingerprint"] == fingerprint
    assert record["recognition_version"] != LOCAL_INVOICE_RECOGNITION_VERSION

    scheme = next(s for s in api.adapters.list_schemes()
                  if s["package_id"] == "org.bitfsae.reimbursement")
    target = Api(tmp_path / "receive")
    try:
        target_scheme = next(s for s in target.adapters.list_schemes()
                             if s["package_id"] == scheme["package_id"])
        target.adapters.complete_adapter_setup(target_scheme["id"])
        imported = target.import_bindle(
            exported["data"]["path"], "", options={"scheme_id": target_scheme["id"]}
        )
        assert imported["ok"], imported
        received = target.entries.get(imported["data"]["entry_ids"][0])
        assert received["recognition_version"] == OLDER_VERSION
        assert received["recognition_fingerprint"] == fingerprint
        assert target.entries.invoice_recognition_state(received["id"]) == "older"
    finally:
        target.db.close()


def test_import_does_not_claim_version_when_invoice_file_changed(api, tmp_path):
    from tidoc.api import Api

    entry_id, _ = _make_entry(api, tmp_path, "1", version=OLDER_VERSION)
    exported = Path(api.export_bindle([entry_id])["data"]["path"])
    payload = _archive_entries(exported)
    invoice = next(a for a in payload["entries"][0]["attachments"] if a["type"] == "invoice_xml")
    modified = _rewrite_archive(
        exported, tmp_path / "changed.tidoc",
        replace_member=f"attachments/{invoice['stored_path']}",
        payload=_xml_payload("26999999999999999999", total="99.00"),
    )

    target = Api(tmp_path / "receive")
    try:
        target_scheme = next(s for s in target.adapters.list_schemes()
                             if s["package_id"] == "org.bitfsae.reimbursement")
        target.adapters.complete_adapter_setup(target_scheme["id"])
        inspected = target.inspect_bindle(modified)["data"]
        assert inspected["tampered"]
        imported = target.import_bindle(modified, "", allow_tampered=True)
        assert imported["ok"], imported
        received = target.entries.get(imported["data"]["entry_ids"][0])
        assert received["recognition_version"] == ""
        assert received["recognition_fingerprint"] == ""
        assert target.entries.invoice_recognition_state(received["id"]) == "unknown"
    finally:
        target.db.close()


def test_future_bindle_format_asks_to_upgrade(api, tmp_path):
    entry_id, _ = _make_entry(api, tmp_path, "1")
    exported = Path(api.export_bindle([entry_id])["data"]["path"])
    future = _rewrite_archive(exported, tmp_path / "future.tidoc", version=bindle_module.BINDLE_VERSION + 1)

    result = api.inspect_bindle(future)
    assert result["ok"] is False
    assert "升级" in result["error"]


def _import_into(target, path, *, options=None):
    return target.import_bindle(path, "", options=options or {})


def test_repeated_import_keeps_local_current_result_and_adopts_source_when_unmarked(api, tmp_path):
    from tidoc.api import Api

    entry_id, invoice_no = _make_entry(api, tmp_path, "1", version=OLDER_VERSION)
    exported = Path(api.export_bindle([entry_id])["data"]["path"])
    scheme_id = next(s for s in api.adapters.list_schemes()
                     if s["package_id"] == "org.bitfsae.reimbursement")["id"]

    target = Api(api.data_root.root)  # 同一个库：导出条目就是本机条目
    try:
        # 本机同版结果保持不动，包内较旧版本不能覆盖本机。
        api.db.conn.execute(
            "UPDATE entries SET recognition_version = ? WHERE id = ?",
            (LOCAL_INVOICE_RECOGNITION_VERSION, entry_id),
        )
        api.db.conn.commit()
        api.entries.mark_invoice_recognized(entry_id)
        imported = _import_into(target, exported, options={"scheme_id": scheme_id})
        assert imported["ok"], imported
        assert api.entries.get(entry_id)["recognition_version"] == LOCAL_INVOICE_RECOGNITION_VERSION

        # 本机没有识别结论时，包内同版原件的结果可以沿用来源。
        api.entries.clear_invoice_recognition(entry_id)
        imported = _import_into(target, exported, options={"scheme_id": scheme_id})
        assert imported["ok"], imported
        assert api.entries.get(entry_id)["recognition_version"] == OLDER_VERSION
    finally:
        target.db.close()


def test_duplicate_merge_invalidates_local_result_when_invoice_core_changes(api, tmp_path):
    from tidoc.api import Api

    entry_id, invoice_no = _make_entry(api, tmp_path, "1")
    assert _state(api, entry_id) == "current"
    before = api.entries.get(entry_id)["recognition_fingerprint"]

    profile = api.profiles.create("另一个报账人", "审核人")
    other = Api(tmp_path / "other")
    try:
        other_scheme = next(s for s in other.adapters.list_schemes()
                            if s["package_id"] == "org.bitfsae.reimbursement")
        other.adapters.complete_adapter_setup(other_scheme["id"])
        other_profile = other.profiles.create("另一个报账人", "审核人")
        other_entry = other.entries.create(
            other_profile["id"],
            parsed=ParsedInvoice(invoice_no=invoice_no, seller="示例销售方", total=Decimal("20.00")),
        )
        path = tmp_path / "other.xml"
        path.write_bytes(_xml_payload(invoice_no, total="20.00"))
        other.attachments.add(other_entry, path, TYPE_INVOICE_XML)
        other.entries.mark_invoice_recognized(other_entry)
        exported = Path(other.export_bindle([other_entry])["data"]["path"])
    finally:
        other.db.close()

    scheme_id = next(s for s in api.adapters.list_schemes()
                     if s["package_id"] == "org.bitfsae.reimbursement")["id"]
    imported = api.import_bindle(
        exported, profile["id"],
        options={
            "scheme_id": scheme_id,
            "entry_merge_decisions": {"0": {"base:total": "use_incoming"}},
        },
    )
    assert imported["ok"], imported
    merged = api.entries.get(entry_id)
    assert merged["total"] == "20.00"
    # 发票信息已经变了，旧的本机识别结论不能再挂着。
    assert merged["recognition_version"] == ""
    assert merged["recognition_fingerprint"] != before
    assert _state(api, entry_id) == "unknown"
    assert _outdated(api, entry_id) is True


def test_reparse_protects_manual_fields_and_items(api, tmp_path):
    entry_id, _ = _make_entry(api, tmp_path, "1", version=OLDER_VERSION)
    item = api.entries.get(entry_id)["items"][0]
    api.entries.update_item(item["id"], {"spec": "人工规格"})
    api.entries.update_field(entry_id, "notes", "保留的备注")
    api.entries.update_field(entry_id, "paid_amount", "10.00")
    api.entries.update_field(entry_id, "actual_item_name", "人工物资名")

    result = api.rerecognize_materials([entry_id], ["invoice"])["data"]["invoice"]
    assert result["processed"] == 1
    saved = api.entries.get(entry_id)
    assert saved["fields"]["notes"]["current"] == "保留的备注"
    assert saved["fields"]["paid_amount"]["current"] == "10.00"
    assert saved["fields"]["actual_item_name"]["current"] == "人工物资名"
    assert saved["items"][0]["spec"] == "人工规格"
    assert "items" in saved["modified_fields"]
    assert saved["recognition_version"] == LOCAL_INVOICE_RECOGNITION_VERSION


def test_failed_reparse_leaves_no_partial_update(api, tmp_path, monkeypatch):
    entry_id, _ = _make_entry(api, tmp_path, "1", version=OLDER_VERSION)
    before = api.entries.get(entry_id)
    assert before["recognition_version"] == OLDER_VERSION

    def failed(*args, **kwargs):
        raise ValueError("测试校验失败")

    monkeypatch.setattr("tidoc.engine.check_invoice", failed)
    result = api.rerecognize_materials([entry_id], ["invoice"])["data"]["invoice"]
    assert result["failed"] and result["failed"][0]["entry_id"] == entry_id

    after = api.entries.get(entry_id)
    assert after["recognition_data"] == before["recognition_data"]
    assert after["items"] == before["items"]
    assert after["recognition_version"] == OLDER_VERSION
    assert after["recognition_fingerprint"] == before["recognition_fingerprint"]
    assert _state(api, entry_id) == "older"
    assert _outdated(api, entry_id) is True


def test_import_preview_reports_each_source_version(api, tmp_path):
    expected = ['current', 'older', 'newer', 'unknown']
    ids = [_make_entry(api, tmp_path, str(n), version=version)[0]
           for n, version in enumerate((LOCAL_INVOICE_RECOGNITION_VERSION, OLDER_VERSION,
                                        NEWER_VERSION, UNKNOWN_VERSION), 31)]
    package = api.export_bindle(ids)['data']['path']
    preview = api.inspect_bindle(package)['data']
    assert [entry['invoice_recognition_state'] for entry in preview['entries']] == expected


def test_reexport_preserves_source_and_changed_context_requires_recognition(api, tmp_path, monkeypatch):
    from tidoc.api import Api
    from tidoc.engine.models import PolicyContext

    entry_id, _ = _make_entry(api, tmp_path, '41', version=OLDER_VERSION)
    package = api.export_bindle([entry_id])['data']['path']
    original = _archive_entries(Path(package))['entries'][0]
    receiver = Api(tmp_path / 'forwarder')
    try:
        scheme = next(s for s in receiver.adapters.list_schemes()
                      if s['package_id'] == 'org.bitfsae.reimbursement')
        receiver.adapters.complete_adapter_setup(scheme['id'])
        result = receiver.import_bindle(package, '', options={'scheme_id': scheme['id']})
        assert result['ok'], result
        received_id = result['data']['entry_ids'][0]
        forwarded = receiver.export_bindle([received_id])['data']['path']
        record = _archive_entries(Path(forwarded))['entries'][0]
        assert record['recognition_version'] == original['recognition_version']
        assert record['recognition_fingerprint'] == original['recognition_fingerprint']
        receiver.entries.mark_invoice_recognized(received_id)
        monkeypatch.setattr(receiver.entries, '_policy_context_for_revision', lambda _: PolicyContext(()))
        assert receiver.entries.invoice_recognition_state(received_id) == 'changed'
        assert _state(receiver, received_id) == 'changed'
    finally:
        receiver.db.close()


def test_invalid_fingerprint_is_not_adopted_even_with_newer_version(api, tmp_path):
    from tidoc.api import Api

    entry_id, _ = _make_entry(api, tmp_path, '51', version=NEWER_VERSION)
    api.db.conn.execute('UPDATE entries SET recognition_fingerprint=? WHERE id=?',
                        ('not-an-invoice-fingerprint', entry_id))
    api.db.conn.commit()
    package = api.export_bindle([entry_id])['data']['path']
    assert api.inspect_bindle(package)['data']['entries'][0]['invoice_recognition_state'] == 'unknown'
    receiver = Api(tmp_path / 'invalid-source')
    try:
        result = receiver.import_bindle(package, '')
        assert result['ok'], result
        received = receiver.entries.get(result['data']['entry_ids'][0])
        assert received['recognition_version'] == ''
        assert received['recognition_fingerprint'] == ''
    finally:
        receiver.db.close()


def test_kept_local_invoice_does_not_adopt_conflicting_source_marker(api, tmp_path):
    entry_id, _ = _make_entry(api, tmp_path, '61')
    package = api.export_bindle([entry_id])['data']['path']
    api.entries.clear_invoice_recognition(entry_id)
    api.entries.correct_locked_field(entry_id, 'total', '99.00')
    result = api.import_bindle(package, '')
    assert result['ok'], result
    saved = api.entries.get(entry_id)
    assert saved['total'] == '99.00'
    assert saved['recognition_version'] == ''
    assert saved['recognition_fingerprint'] == ''


def test_unknown_required_package_capability_asks_to_upgrade(api, tmp_path):
    entry_id, _ = _make_entry(api, tmp_path, '71')
    original = api.export_bindle([entry_id])['data']['path']
    package = tmp_path / 'unsupported-capability.tidoc'
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(package, 'w') as target:
        for name in source.namelist():
            data = source.read(name)
            if name == bindle_module.MANIFEST_NAME:
                manifest = json.loads(data)
                manifest['minimum_receiver_capability'] = 'bindle.v999'
                data = json.dumps(manifest).encode()
            target.writestr(name, data)
    result = api.inspect_bindle(package)
    assert result['ok'] is False
    assert '升级' in result['error']


def test_invalid_calendar_version_and_runtime_rule_change(monkeypatch):
    from tidoc.db import entries

    assert invoice_recognition_version_key('invoice-local-2026-02-30-1') is None
    monkeypatch.setattr(entries, 'LOCAL_INVOICE_RECOGNITION_VERSION', NEWER_VERSION)
    assert compare_invoice_recognition_versions(LOCAL_INVOICE_RECOGNITION_VERSION) == -1

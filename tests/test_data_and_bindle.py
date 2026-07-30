"""数据层、修改追踪、绑定包导出/导入/篡改检测的测试。"""

import os
import zipfile
import base64
import json
from decimal import Decimal

from tidoc.db import (
    STATUS_COMPLETE,
    TYPE_INVOICE_XML,
    AttachmentRepo,
    Database,
    DataRoot,
    EntryRepo,
    ProfileRepo,
)
from tidoc.engine import parse_xml
from tidoc.engine.models import ParsedInvoice, ParsedItem
from tidoc.services import export_bindle, import_bindle, inspect_bindle


def test_profile_first_is_default(repos):
    p = repos["profiles"].create("张三", "李老师")
    assert p["is_default"] == 1
    p2 = repos["profiles"].create("王五", "赵老师")
    assert p2["is_default"] == 0
    mode = repos["db"].conn.execute(
        "SELECT value FROM meta WHERE key = 'tidoc.multiClaimantMode'"
    ).fetchone()
    assert mode["value"] == "1"


def test_profile_required_fields(repos):
    import pytest
    with pytest.raises(ValueError):
        repos["profiles"].create("", "李老师")


def test_entry_titles_are_listed_for_dynamic_filter(repos):
    profile = repos["profiles"].create("张三", "李老师")
    repos["entries"].create(profile["id"], title="北京理工大学")
    repos["entries"].create(profile["id"], title="第三方公司")
    repos["entries"].create(profile["id"], title="第三方公司")

    assert repos["entries"].all_titles() == ["北京理工大学", "第三方公司"]


def test_field_modification_marks_permanently(repos, sample_xmls):
    p = repos["profiles"].create("张三", "李老师")
    parsed = parse_xml(sample_xmls[0])
    eid = repos["entries"].create(p["id"], title=parsed.buyer_name, parsed=parsed)

    repos["entries"].update_field(eid, "notes", "改了", p["id"])
    e = repos["entries"].get(eid)
    assert e["fields"]["notes"]["modified"] is True
    assert len(e["history"]) == 1

    # 改回原值，标记仍不擦除（永久）
    repos["entries"].update_field(eid, "notes", "", p["id"])
    e = repos["entries"].get(eid)
    assert e["fields"]["notes"]["modified"] is True
    assert len(e["history"]) == 2


def test_paid_amount_defaults_to_invoice_total(repos, sample_xmls):
    p = repos["profiles"].create("张三", "李老师")
    parsed = parse_xml(sample_xmls[0])
    eid = repos["entries"].create(p["id"], title=parsed.buyer_name, parsed=parsed)

    e = repos["entries"].get(eid)
    paid = e["fields"]["paid_amount"]
    assert paid["current"] == e["total"]
    assert paid["modified"] is False


def test_paid_amount_default_can_be_left_blank(repos):
    profile = repos["profiles"].create("张三", "李老师")
    parsed = ParsedInvoice(total=Decimal("32.29"))

    entry_id = repos["entries"].create(
        profile["id"], parsed=parsed, default_paid_to_total=False
    )

    entry = repos["entries"].get(entry_id)
    assert entry["total"] == "32.29"
    assert entry["fields"]["paid_amount"]["current"] == ""


def test_locked_field_correction_logged(repos, sample_xmls):
    p = repos["profiles"].create("张三", "李老师")
    parsed = parse_xml(sample_xmls[0])
    eid = repos["entries"].create(p["id"], title=parsed.buyer_name, parsed=parsed)
    repos["entries"].correct_locked_field(eid, "invoice_no", "999", p["id"])
    e = repos["entries"].get(eid)
    assert e["invoice_no"] == "999"
    assert any("人工修正" in h["field"] for h in e["history"])


def test_batch_reparse_replaces_items_and_preserves_user_fields(api, tmp_path, monkeypatch):
    from tidoc.db import TYPE_INVOICE_PDF
    from tidoc.engine.validator import TITLE_FOUNDATION

    profile = api.profiles.create("张三", "李老师")
    entry_id = api.entries.create(
        profile["id"],
        title=TITLE_FOUNDATION,
        parsed=ParsedInvoice(
            invoice_no="123",
            buyer_name=TITLE_FOUNDATION,
            total=Decimal("84.00"),
            source="pdf",
        ),
    )
    invoice_path = tmp_path / "invoice.pdf"
    invoice_path.write_bytes(b"placeholder")
    api.attachments.add(entry_id, invoice_path, TYPE_INVOICE_PDF)
    api.entries.update_field(entry_id, "actual_item_name", "用户核对名称", profile["id"])

    reparsed = ParsedInvoice(
        invoice_no="123",
        buyer_name=TITLE_FOUNDATION,
        total=Decimal("84.00"),
        source="pdf",
        items=[ParsedItem(
            name="*橡胶制品*发票原品名",
            actual_name="发票原品名",
            unit="卷",
            quantity=Decimal("2"),
            total=Decimal("84.00"),
        )],
    )
    monkeypatch.setattr("tidoc.engine.parse_invoice_files", lambda *_args: reparsed)

    response = api.reparse_entries([entry_id])

    assert response["ok"] is True
    assert response["data"]["resolved"] == 1
    entry = api.entries.get(entry_id)
    assert entry["check_status"] == "pass"
    assert entry["items"][0]["actual_name"] == "发票原品名"
    assert entry["items"][0]["unit"] == "卷"
    assert entry["fields"]["actual_item_name"]["current"] == "用户核对名称"
    assert entry["fields"]["actual_item_name"]["modified"] is True


def test_entry_profile_can_be_changed_and_logged(repos, sample_xmls):
    p1 = repos["profiles"].create("张三", "李老师")
    p2 = repos["profiles"].create("王五", "赵老师")
    parsed = parse_xml(sample_xmls[0])
    eid = repos["entries"].create(p1["id"], title=parsed.buyer_name, parsed=parsed)

    e = repos["entries"].set_profile(eid, p2["id"], p1["id"])

    assert e["profile_id"] == p2["id"]
    assert any(h["field"] == "报账人" and "王五" in h["new_value"] for h in e["history"])


def test_entry_profiles_can_be_changed_in_batch(repos):
    p1 = repos["profiles"].create("张三", "李老师")
    p2 = repos["profiles"].create("王五", "赵老师")
    ids = [repos["entries"].create(p1["id"]) for _ in range(3)]

    changed = repos["entries"].set_profiles(ids + [ids[0]], p2["id"], p1["id"])

    assert changed == 3
    for entry_id in ids:
        entry = repos["entries"].get(entry_id)
        assert entry["profile_id"] == p2["id"]
        assert any(h["field"] == "报账人" and "王五" in h["new_value"] for h in entry["history"])


def test_print_uses_operator_profile_for_reimburse_doc(repos, sample_xmls, tmp_path, monkeypatch):
    from tidoc.services import printing

    claimant = repos["profiles"].create("张三", "李老师")
    parsed = parse_xml(sample_xmls[0])
    eid = repos["entries"].create(claimant["id"], title=parsed.buyer_name, parsed=parsed)
    captured = {}

    monkeypatch.setattr(
        printing,
        "component_status",
        lambda components_dir=None: {"available": True, "mode": "external", "path": "tidoc-print", "missing": []},
    )

    def fake_external(executable, entries, out_dir, options, profiles):
        captured["entries"] = entries
        captured["profiles"] = profiles
        return {"results": []}

    monkeypatch.setattr(printing, "_build_prints_external", fake_external)
    printing.build_prints(
        repos["entries"],
        repos["profiles"],
        tmp_path,
        [eid],
        tmp_path / "out",
        {"operator_profile": {
            "person_name": "运营同学",
            "student_id": "112233",
            "contact": "13800000000",
            "bank_name": "测试银行",
            "bank_card": "62220000",
        }},
    )

    assert captured["entries"][0]["profile_name"] == "张三"
    assert captured["profiles"][eid]["person_name"] == "运营同学"
    assert captured["profiles"][eid]["bank_card"] == "62220000"


def test_list_filters(repos, sample_xmls):
    p = repos["profiles"].create("张三", "李老师")
    for x in sample_xmls[:3]:
        parsed = parse_xml(x)
        repos["entries"].create(p["id"], title=parsed.buyer_name, parsed=parsed)
    all_e = repos["entries"].list()
    assert len(all_e) == 3
    kw = repos["entries"].list(keyword="立创")
    assert len(kw) >= 0  # 关键字过滤不报错


def test_item_crud(repos, sample_xmls):
    p = repos["profiles"].create("张三", "李老师")
    parsed = parse_xml(sample_xmls[0])
    eid = repos["entries"].create(p["id"], title=parsed.buyer_name, parsed=parsed)
    before = len(repos["entries"].get(eid)["items"])

    # 追加一行
    new_item = repos["entries"].add_item(eid, name="手工加的", actual_name="手工加的",
                                          unit="个", quantity="2", unit_price="5.00", total="10.00")
    items = repos["entries"].get(eid)["items"]
    assert len(items) == before + 1
    assert items[-1]["actual_name"] == "手工加的"
    assert items[-1]["ordinal"] == before  # 排在末尾

    # 更新该行
    updated = repos["entries"].update_item(new_item["id"], {"actual_name": "改名了", "total": "20.00"})
    assert updated["actual_name"] == "改名了"
    assert updated["total"] == "20.00"

    # 删除该行
    ret_eid = repos["entries"].delete_item(new_item["id"])
    assert ret_eid == eid
    assert len(repos["entries"].get(eid)["items"]) == before


def test_item_update_rejects_unknown_field(repos, sample_xmls):
    import pytest
    p = repos["profiles"].create("张三", "李老师")
    parsed = parse_xml(sample_xmls[0])
    eid = repos["entries"].create(p["id"], title=parsed.buyer_name, parsed=parsed)
    item_id = repos["entries"].get(eid)["items"][0]["id"]
    with pytest.raises(ValueError):
        repos["entries"].update_item(item_id, {"nonexistent": "x"})


def test_completeness_and_status_derivation(repos, sample_xmls):
    from tidoc.db import TYPE_INSPECTION, TYPE_INVOICE_PDF, TYPE_PAYMENT
    p = repos["profiles"].create("张三", "李老师")
    parsed = parse_xml(sample_xmls[0])
    eid = repos["entries"].create(p["id"], title=parsed.buyer_name, parsed=parsed)

    # 刚建：无附件 → draft，completeness 未齐
    e = repos["entries"].list(profile_id=p["id"])[0]
    assert e["completeness"]["ready"] is False
    assert e["completeness"]["status"] == "draft"
    assert "发票" in e["completeness"]["missing"]

    # 加三种附件 + 填实付
    repos["attachments"].add(eid, sample_xmls[0], TYPE_INVOICE_PDF)
    repos["attachments"].add(eid, sample_xmls[1], TYPE_PAYMENT)
    repos["attachments"].add(eid, sample_xmls[2], TYPE_INSPECTION)
    repos["entries"].update_field(eid, "paid_amount", "100.00", p["id"])
    repos["entries"].set_check(eid, "pass", "")
    repos["entries"].recompute_status(eid)

    e = [x for x in repos["entries"].list(profile_id=p["id"]) if x["id"] == eid][0]
    assert e["has_invoice"] and e["has_payment"] and e["has_inspection"]
    assert e["completeness"]["ready"] is True
    assert e["completeness"]["status"] == "complete"


def test_attachment_duplicate_rejected(repos, sample_xmls):
    import pytest
    from tidoc.db import TYPE_INVOICE_XML, TYPE_PAYMENT

    p = repos["profiles"].create("张三", "李老师")
    parsed = parse_xml(sample_xmls[0])
    eid = repos["entries"].create(p["id"], title=parsed.buyer_name, parsed=parsed)

    repos["attachments"].add(eid, sample_xmls[0], TYPE_INVOICE_XML)
    with pytest.raises(ValueError, match="已添加过"):
        repos["attachments"].add(eid, sample_xmls[0], TYPE_PAYMENT)


def test_api_applies_payment_ocr_amount(api, sample_xmls, tmp_path, monkeypatch):
    from tidoc.services import folder_import

    p = api.create_profile("张三", "李老师")["data"]
    e = api.create_entry(p["id"], xml_path=sample_xmls[0])["data"]
    payment = tmp_path / "付款截图.jpg"
    payment.write_bytes(b"fake image")

    monkeypatch.setattr(folder_import, "extract_payment_image_amount", lambda path: "27.00")
    res = api.add_attachment(e["id"], str(payment), "payment_screenshot")["data"]

    assert res["payment_ocr"] == {"paid_amount": "27.00", "applied": True}
    updated = api.get_entry(e["id"])["data"]
    assert updated["fields"]["paid_amount"]["current"] == "27.00"
    assert updated["fields"]["paid_amount"]["modified"] is False
    assert updated["fields"]["paid_amount"]["value_source"] == "payment_ocr"


def test_api_can_recognize_payment_ocr_without_applying(api, sample_xmls, tmp_path, monkeypatch):
    from tidoc.services import folder_import

    p = api.create_profile("张三", "李老师")["data"]
    e = api.create_entry(p["id"], xml_path=sample_xmls[0])["data"]
    before = e["fields"]["paid_amount"]["current"]
    payment = tmp_path / "付款截图.jpg"
    payment.write_bytes(b"fake image")

    monkeypatch.setattr(folder_import, "extract_payment_image_amount", lambda path: "27.00")
    res = api.add_attachment(
        e["id"],
        str(payment),
        "payment_screenshot",
        options={"apply_payment_ocr": False},
    )["data"]

    assert res["payment_ocr"] == {"paid_amount": "27.00", "applied": False}
    updated = api.get_entry(e["id"])["data"]
    assert updated["fields"]["paid_amount"]["current"] == before


def test_api_payment_ocr_preference_disables_recognition(api, tmp_path, monkeypatch):
    from tidoc.api import PAYMENT_OCR_PREF_KEY
    from tidoc.services import folder_import

    profile = api.create_profile("张三", "李老师")["data"]
    entry = api.create_entry(profile["id"])["data"]
    payment = tmp_path / "付款截图.png"
    payment.write_bytes(b"fake image")
    api.set_app_preference(PAYMENT_OCR_PREF_KEY, "0")

    def unexpected_ocr(path):
        raise AssertionError(f"OCR should be disabled: {path}")

    monkeypatch.setattr(folder_import, "extract_payment_image_amount", unexpected_ocr)
    classified = api.classify_material_files([str(payment)])["data"]
    attachment = api.add_attachment(entry["id"], str(payment), "payment_screenshot")["data"]

    assert classified[0]["paid_amount"] == ""
    assert "payment_ocr" not in attachment


def test_api_default_paid_preference_is_independent_from_ocr(api):
    from tidoc.api import DEFAULT_PAID_TO_INVOICE_PREF_KEY, PAYMENT_OCR_PREF_KEY

    api.set_app_preference(PAYMENT_OCR_PREF_KEY, "0")
    api.set_app_preference(DEFAULT_PAID_TO_INVOICE_PREF_KEY, "0")

    assert api._payment_ocr_enabled() is False
    assert api._default_paid_to_invoice() is False


def test_api_create_entry_respects_blank_default_paid_preference(api, tmp_path, monkeypatch):
    from tidoc import engine
    from tidoc.api import DEFAULT_PAID_TO_INVOICE_PREF_KEY
    from tidoc.engine.models import CheckResult

    profile = api.create_profile("张三", "李老师")["data"]
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"fake pdf")
    parsed = ParsedInvoice(total=Decimal("32.29"), invoice_no="123")
    monkeypatch.setattr(engine, "parse_invoice_files", lambda *args, **kwargs: parsed)
    monkeypatch.setattr(engine, "check_invoice", lambda *args, **kwargs: CheckResult("pass", ""))
    api.set_app_preference(DEFAULT_PAID_TO_INVOICE_PREF_KEY, "0")

    entry = api.create_entry(profile["id"], pdf_path=str(pdf))["data"]

    assert entry["total"] == "32.29"
    assert entry["fields"]["paid_amount"]["current"] == ""


def test_dropped_file_cleanup(api):
    payload = base64.b64encode(b"temporary").decode()
    saved = api.save_dropped_files([{"name": "a.pdf", "data_url": f"data:application/pdf;base64,{payload}"}])["data"]
    path = saved["paths"][0]
    assert os.path.exists(path)

    res = api.cleanup_dropped_files(saved["paths"])["data"]
    assert res["deleted"] == 1
    assert not os.path.exists(path)


def test_dropped_file_rejects_invalid_base64(api):
    res = api.save_dropped_files([{"name": "bad.pdf", "data_url": "data:application/pdf;base64,not-base64"}])
    assert res["ok"] is False
    assert "文件内容无效" in res["error"]


def test_dropped_file_rejects_oversized_payload(api, monkeypatch):
    monkeypatch.setattr("tidoc.api.MAX_DROPPED_FILE_BYTES", 3)
    payload = base64.b64encode(b"four").decode()
    res = api.save_dropped_files([{"name": "large.pdf", "data_url": payload}])
    assert res["ok"] is False
    assert "文件过大" in res["error"]


def test_api_rejects_unrecognized_invoice_xml(api, sample_xmls, tmp_path):
    p = api.create_profile("张三", "李老师")["data"]
    e = api.create_entry(p["id"], xml_path=sample_xmls[0])["data"]
    bad_xml = tmp_path / "not-invoice.xml"
    bad_xml.write_text("<root><name>not official invoice</name></root>", encoding="utf-8")

    res = api.add_attachment(e["id"], str(bad_xml), "invoice_xml")
    assert res["ok"] is False
    assert "官方电子发票 XML" in res["error"]


def test_api_validates_inspection_pdf_invoice_no(api, sample_xmls, tmp_path):
    from pypdf import PdfWriter

    p = api.create_profile("张三", "李老师")["data"]
    e = api.create_entry(p["id"], xml_path=sample_xmls[0])["data"]

    same = tmp_path / "same-check.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=842, height=595)
    writer.add_metadata({
        "/Title": "国家税务总局全国增值税发票查验平台",
        "/Subject": f"发票号码：{e['invoice_no']}",
    })
    with same.open("wb") as f:
        writer.write(f)
    assert api.add_attachment(e["id"], str(same), "inspection_pdf")["ok"] is True

    wrong = tmp_path / "wrong-check.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=842, height=595)
    writer.add_metadata({
        "/Title": "国家税务总局全国增值税发票查验平台",
        "/Subject": "发票号码：11111111111111111111",
    })
    with wrong.open("wb") as f:
        writer.write(f)
    res = api.add_attachment(e["id"], str(wrong), "inspection_pdf")
    assert res["ok"] is False
    assert "不属于当前条目" in res["error"]


def test_bindle_round_trip_and_tamper(repos, sample_xmls, tmp_path):
    p = repos["profiles"].create("张三", "李老师")
    ids = []
    for x in sample_xmls[:2]:
        parsed = parse_xml(x)
        eid = repos["entries"].create(p["id"], title=parsed.buyer_name, parsed=parsed, status=STATUS_COMPLETE)
        repos["attachments"].add(eid, x, TYPE_INVOICE_XML)
        ids.append(eid)

    out = export_bindle(repos["entries"], repos["attachments"], ids,
                        str(tmp_path / "包.tidoc"), {p["id"]: p})
    assert out.exists()

    insp = inspect_bindle(out)
    assert insp["verified"] is True
    assert len(insp["entries"]) == 2

    # 篡改：改 entries.json
    tampered = tmp_path / "t.tidoc"
    with zipfile.ZipFile(out) as zin, zipfile.ZipFile(tampered, "w") as zout:
        for item in zin.namelist():
            data = zin.read(item)
            if item == "entries.json":
                data = data.replace(b'"total"', b'"ttl"', 1)
            zout.writestr(item, data)
    insp2 = inspect_bindle(tampered)
    assert insp2["verified"] is False
    assert "entries.json" in insp2["tampered"]

    # 拒绝导入篡改包
    res = import_bindle(repos["entries"], repos["attachments"], tampered, p["id"])
    assert res["imported"] == 0

    # 导入到另一份数据库，模拟成员之间交换
    target_root = DataRoot(tmp_path / "target")
    target_db = Database(target_root.db_path)
    target_profiles = ProfileRepo(target_db)
    target_profile = target_profiles.create("王五", "赵老师")
    target_entries = EntryRepo(target_db)
    target_attachments = AttachmentRepo(target_db, target_root)
    res2 = import_bindle(
        target_entries, target_attachments, out, target_profile["id"]
    )
    assert res2["imported"] == 2

    # 同一绑定包再次导入时跳过重复发票
    duplicate = import_bindle(
        target_entries, target_attachments, out, target_profile["id"]
    )
    assert duplicate["imported"] == 0
    assert len(duplicate["skipped"]) == 2
    assert "跳过 2 条重复发票" in duplicate["message"]

    # 用户确认导入完整性异常的包后，条目必须实际标记为严重问题
    suspicious_root = DataRoot(tmp_path / "suspicious")
    suspicious_db = Database(suspicious_root.db_path)
    suspicious_profiles = ProfileRepo(suspicious_db)
    suspicious_profile = suspicious_profiles.create("赵六", "审核人")
    suspicious_entries = EntryRepo(suspicious_db)
    suspicious_attachments = AttachmentRepo(suspicious_db, suspicious_root)
    suspicious = import_bindle(
        suspicious_entries,
        suspicious_attachments,
        tampered,
        suspicious_profile["id"],
        allow_tampered=True,
    )
    assert suspicious["imported"] == 2
    assert all(
        entry["check_status"] == "blocked"
        and "完整性校验未通过" in entry["check_message"]
        and entry["status"] == "partial"
        for entry in suspicious_entries.list()
    )


def test_bindle_restores_claimants_and_respects_optional_notes_and_tags(repos, tmp_path):
    first = repos["profiles"].create("张三", "李老师")
    second = repos["profiles"].create("王五", "赵老师")
    first_entry = repos["entries"].create(first["id"])
    second_entry = repos["entries"].create(second["id"])
    repos["entries"].update_field(first_entry, "notes", "只在本机保留", first["id"])
    repos["entries"].set_meta(first_entry, tags=["待补票"])
    material = tmp_path / "说明.txt"
    material.write_text("material", encoding="utf-8")
    repos["attachments"].add(first_entry, material, "other", "附件私密备注")

    package = export_bindle(
        repos["entries"],
        repos["attachments"],
        [first_entry, second_entry],
        tmp_path / "多人包.tidoc",
        {first["id"]: first, second["id"]: second},
        include_notes=False,
        include_tags=False,
    )
    inspected = inspect_bindle(package)

    assert inspected["options"] == {"include_notes": False, "include_tags": False}
    assert {(p["name"], p["reviewer"]) for p in inspected["profiles"]} == {
        ("张三", "李老师"),
        ("王五", "赵老师"),
    }
    assert {entry["profile_id"] for entry in inspected["entries"]} == {
        first["id"], second["id"],
    }
    exported_first = next(e for e in inspected["entries"] if e["profile_id"] == first["id"])
    assert exported_first["tags"] == []
    assert "notes" not in exported_first["fields"]
    assert all(h["field"] != "notes" for h in exported_first["history"])
    assert exported_first["attachments"][0]["note"] == ""
    assert all("notes" not in entry for entry in inspected["summary"]["entries"])

    target_root = DataRoot(tmp_path / "multi-target")
    target_db = Database(target_root.db_path)
    target_profiles = ProfileRepo(target_db)
    fallback = target_profiles.create("运营同学", "总审核人")
    target_entries = EntryRepo(target_db)
    target_attachments = AttachmentRepo(target_db, target_root)

    result = import_bindle(
        target_entries, target_attachments, package, fallback["id"]
    )

    assert result["imported"] == 2
    assert result["profiles_imported"] == 2
    target_profile_names = {
        profile["id"]: profile["name"] for profile in target_profiles.list()
    }
    imported_owner_names = {
        target_profile_names[entry["profile_id"]] for entry in target_entries.list()
    }
    assert imported_owner_names == {"张三", "王五"}
    assert target_db.conn.execute(
        "SELECT value FROM meta WHERE key = 'tidoc.multiClaimantMode'"
    ).fetchone()["value"] == "1"

    with zipfile.ZipFile(package) as archive:
        payload = json.loads(archive.read("entries.json"))
    assert payload["bindle_version"] == 2

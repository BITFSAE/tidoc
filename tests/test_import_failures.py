"""发票导入失败反馈、重复防护与半成品清理。"""

from decimal import Decimal
from pathlib import Path

import pytest

from tidoc.engine.models import CheckResult, ParsedInvoice, ParsedItem


def _parsed(invoice_no="26957000000168907686"):
    return ParsedInvoice(
        invoice_no=invoice_no,
        seller="深圳市测试有限公司",
        buyer_name="北京理工大学",
        total=Decimal("32.29"),
        source="pdf",
    )


def _patch_invoice_engine(monkeypatch, parsed):
    import tidoc.engine

    monkeypatch.setattr(tidoc.engine, "parse_invoice_files", lambda *_args, **_kwargs: parsed)
    monkeypatch.setattr(tidoc.engine, "check_invoice", lambda *_args, **_kwargs: CheckResult("pass", ""))


def test_batch_duplicate_returns_specific_existing_entry(api, tmp_path, monkeypatch):
    pdf = tmp_path / "重复发票.pdf"
    pdf.write_bytes(b"same invoice")
    parsed = _parsed()
    _patch_invoice_engine(monkeypatch, parsed)
    old_profile = api.create_profile("张三", "李老师")["data"]
    new_profile = api.create_profile("王五", "赵老师")["data"]
    existing_id = api.entries.create(old_profile["id"], parsed=parsed)
    api.attachments.add(existing_id, pdf, "invoice_pdf")

    result = api.batch_create_entries(new_profile["id"], [{
        "key": "pdf-1",
        "label": parsed.invoice_no,
        "files": [{"path": str(pdf), "type": "invoice_pdf"}],
    }])["data"]

    assert result["created"] == 0
    assert len(result["failed"]) == 1
    failure = result["failed"][0]
    assert failure["key"] == "pdf-1"
    assert failure["code"] == "duplicate_invoice"
    assert failure["existing_entry_id"] == existing_id
    assert parsed.invoice_no in failure["error"]
    assert "张三" in failure["error"]
    assert len(api.entries.list()) == 1


def test_drag_paste_batch_path_keeps_missing_tax_id_as_warning(api, tmp_path, monkeypatch):
    import tidoc.engine

    pdf = tmp_path / "拖入发票.pdf"
    pdf.write_bytes(b"invoice")
    parsed = ParsedInvoice(
        invoice_no="26957000000168907687",
        seller="深圳市测试有限公司",
        buyer_name="北京理工大学",
        total=Decimal("32.29"),
        items=[ParsedItem("*材料*线缆", "线缆", "卷", Decimal("1"), Decimal("32.29"))],
        source="pdf",
    )
    monkeypatch.setattr(tidoc.engine, "parse_invoice_files", lambda *_args, **_kwargs: parsed)
    profile = api.create_profile("张三", "李老师")["data"]

    result = api.batch_create_entries(profile["id"], [{
        "key": "dropped-1",
        "label": "拖入发票",
        "files": [{"path": str(pdf), "type": "invoice_pdf"}],
    }])["data"]

    assert result["created"] == 1
    entry = api.entries.get(result["entry_ids"][0])
    assert entry["check_status"] == "warning"
    assert "购买方税号" in entry["check_message"]


def test_single_create_uses_same_global_duplicate_check(api, tmp_path, monkeypatch):
    first_pdf = tmp_path / "first.pdf"
    second_pdf = tmp_path / "second.pdf"
    first_pdf.write_bytes(b"first copy")
    second_pdf.write_bytes(b"second copy")
    parsed = _parsed()
    _patch_invoice_engine(monkeypatch, parsed)
    first_profile = api.create_profile("张三", "李老师")["data"]
    second_profile = api.create_profile("王五", "赵老师")["data"]

    first = api.create_entry(first_profile["id"], pdf_path=str(first_pdf))
    duplicate = api.create_entry(second_profile["id"], pdf_path=str(second_pdf))

    assert first["ok"] is True
    assert duplicate["ok"] is False
    assert parsed.invoice_no in duplicate["error"]
    assert "张三" in duplicate["error"]
    assert len(api.entries.list()) == 1


def test_duplicate_file_hash_is_fallback_when_invoice_number_missing(api, tmp_path, monkeypatch):
    pdf = tmp_path / "无法识别号码.pdf"
    pdf.write_bytes(b"identical invoice bytes")
    parsed = _parsed(invoice_no="")
    _patch_invoice_engine(monkeypatch, parsed)
    profile = api.create_profile("张三", "李老师")["data"]

    first = api.create_entry(profile["id"], pdf_path=str(pdf))
    duplicate = api.create_entry(profile["id"], pdf_path=str(pdf))

    assert first["ok"] is True
    assert duplicate["ok"] is False
    assert "相同发票文件已存在" in duplicate["error"]
    assert len(api.entries.list()) == 1


def test_single_create_removes_partial_entry_and_copied_files(api, tmp_path, monkeypatch):
    pdf = tmp_path / "invoice.pdf"
    payment = tmp_path / "payment.png"
    pdf.write_bytes(b"invoice")
    payment.write_bytes(b"payment")
    _patch_invoice_engine(monkeypatch, _parsed())
    profile = api.create_profile("张三", "李老师")["data"]
    original_add = api.attachments.add

    def fail_on_payment(entry_id, path, att_type, note=""):
        if att_type == "payment_screenshot":
            raise OSError("付款截图复制失败")
        return original_add(entry_id, path, att_type, note)

    monkeypatch.setattr(api.attachments, "add", fail_on_payment)

    result = api.create_entry(
        profile["id"],
        pdf_path=str(pdf),
        payment_paths=[str(payment)],
    )

    assert result == {"ok": False, "error": "付款截图复制失败"}
    assert api.entries.list() == []
    assert list(api.data_root.attachments_dir.iterdir()) == []


def test_batch_failure_removes_partial_entry_and_reports_group_reason(api, tmp_path, monkeypatch):
    pdf = tmp_path / "invoice.pdf"
    xml = tmp_path / "invoice.xml"
    pdf.write_bytes(b"invoice")
    xml.write_bytes(b"xml")
    _patch_invoice_engine(monkeypatch, _parsed())
    profile = api.create_profile("张三", "李老师")["data"]
    original_add = api.attachments.add

    def fail_on_xml(entry_id, path, att_type, note=""):
        if att_type == "invoice_xml":
            raise OSError("XML 复制失败")
        return original_add(entry_id, path, att_type, note)

    monkeypatch.setattr(api.attachments, "add", fail_on_xml)

    result = api.batch_create_entries(profile["id"], [{
        "key": "pdf-7",
        "label": "测试分组",
        "files": [
            {"path": str(pdf), "type": "invoice_pdf"},
            {"path": str(xml), "type": "invoice_xml"},
        ],
    }])["data"]

    assert result["created"] == 0
    assert result["failed"] == [{
        "key": "pdf-7",
        "group": "测试分组",
        "code": "import_failed",
        "error": "XML 复制失败",
    }]
    assert api.entries.list() == []
    assert list(api.data_root.attachments_dir.iterdir()) == []


def test_entry_repo_rolls_back_when_initialization_fails(repos, monkeypatch):
    profile = repos["profiles"].create("张三", "李老师")

    def fail_initialization(*_args, **_kwargs):
        raise OSError("字段初始化失败")

    monkeypatch.setattr(repos["entries"], "_initial_editable", fail_initialization)

    with pytest.raises(OSError, match="字段初始化失败"):
        repos["entries"].create(profile["id"], parsed=_parsed())

    count = repos["db"].conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
    assert count == 0


def test_attachment_copy_failure_removes_partial_file(repos, tmp_path, monkeypatch):
    from tidoc.db import attachments

    profile = repos["profiles"].create("张三", "李老师")
    entry_id = repos["entries"].create(profile["id"])
    source = tmp_path / "付款截图.png"
    source.write_bytes(b"source")

    def partial_copy(_source, destination):
        Path(destination).write_bytes(b"partial")
        raise OSError("磁盘写入失败")

    monkeypatch.setattr(attachments.shutil, "copy2", partial_copy)

    with pytest.raises(OSError, match="磁盘写入失败"):
        repos["attachments"].add(entry_id, source, "payment_screenshot")

    assert repos["attachments"].list(entry_id) == []
    assert not (repos["root"].attachments_dir / entry_id).exists()


def test_attachment_replace_restores_old_file_when_database_update_fails(
    repos, tmp_path, monkeypatch
):
    profile = repos["profiles"].create("张三", "李老师")
    entry_id = repos["entries"].create(profile["id"])
    old_source = tmp_path / "旧截图.png"
    new_source = tmp_path / "新截图.png"
    old_source.write_bytes(b"old")
    new_source.write_bytes(b"new")
    attachment = repos["attachments"].add(
        entry_id, old_source, "payment_screenshot"
    )
    old_stored = Path(attachment["abs_path"])
    real_conn = repos["db"].conn

    class FailingAttachmentUpdate:
        def execute(self, sql, params=()):
            if sql.lstrip().startswith("UPDATE attachments"):
                raise OSError("数据库更新失败")
            return real_conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(real_conn, name)

    monkeypatch.setattr(repos["db"], "conn", FailingAttachmentUpdate())

    with pytest.raises(OSError, match="数据库更新失败"):
        repos["attachments"].update(attachment["id"], src_path=new_source)

    current = repos["attachments"].get(attachment["id"])
    assert current["stored_path"] == attachment["stored_path"]
    assert old_stored.read_bytes() == b"old"
    assert [path for path in old_stored.parent.iterdir() if path.is_file()] == [old_stored]


def test_attachment_replace_copy_failure_keeps_old_file(
    repos, tmp_path, monkeypatch
):
    from tidoc.db import attachments

    profile = repos["profiles"].create("张三", "李老师")
    entry_id = repos["entries"].create(profile["id"])
    old_source = tmp_path / "旧截图.png"
    new_source = tmp_path / "新截图.png"
    old_source.write_bytes(b"old")
    new_source.write_bytes(b"new")
    attachment = repos["attachments"].add(
        entry_id, old_source, "payment_screenshot"
    )
    old_stored = Path(attachment["abs_path"])

    def partial_copy(_source, destination):
        Path(destination).write_bytes(b"partial")
        raise OSError("替换文件写入失败")

    monkeypatch.setattr(attachments.shutil, "copy2", partial_copy)

    with pytest.raises(OSError, match="替换文件写入失败"):
        repos["attachments"].update(attachment["id"], src_path=new_source)

    current = repos["attachments"].get(attachment["id"])
    assert current["stored_path"] == attachment["stored_path"]
    assert old_stored.read_bytes() == b"old"
    assert [path for path in old_stored.parent.iterdir() if path.is_file()] == [old_stored]


def test_entry_delete_removes_attachment_directory(api, tmp_path):
    profile = api.create_profile("张三", "李老师")["data"]
    entry = api.create_entry(profile["id"])["data"]
    payment = tmp_path / "付款截图.png"
    payment.write_bytes(b"payment")
    api.add_attachment(
        entry["id"],
        str(payment),
        "payment_screenshot",
        options={"skip_payment_ocr": True},
    )
    entry_dir = api.data_root.attachments_dir / entry["id"]
    assert entry_dir.is_dir()

    result = api.delete_entry(entry["id"])["data"]

    assert result["deleted"] == entry["id"]
    assert result["cleanup_warning"] == ""
    assert api.entries.get(entry["id"]) is None
    assert not entry_dir.exists()


def test_batch_delete_removes_all_attachment_directories(api, tmp_path):
    profile = api.create_profile("张三", "李老师")["data"]
    entry_ids = []
    entry_dirs = []
    for index in range(2):
        entry = api.create_entry(profile["id"])["data"]
        payment = tmp_path / f"付款截图-{index}.png"
        payment.write_bytes(f"payment-{index}".encode())
        api.add_attachment(
            entry["id"],
            str(payment),
            "payment_screenshot",
            options={"skip_payment_ocr": True},
        )
        entry_ids.append(entry["id"])
        entry_dirs.append(api.data_root.attachments_dir / entry["id"])

    result = api.delete_entries(entry_ids)["data"]

    assert result == {"deleted": 2, "cleanup_warning": ""}
    assert api.entries.list() == []
    assert all(not path.exists() for path in entry_dirs)


def test_delete_batch_can_keep_entries_or_remove_entries_and_files(api, tmp_path):
    profile = api.create_profile("张三", "李老师")["data"]
    kept = api.create_entry(profile["id"])["data"]
    removed = api.create_entry(profile["id"])["data"]
    payment = tmp_path / "付款截图.png"
    payment.write_bytes(b"payment")
    api.add_attachment(
        removed["id"], str(payment), "payment_screenshot",
        options={"skip_payment_ocr": True},
    )
    removed_dir = api.data_root.attachments_dir / removed["id"]

    keep_batch = api.create_batch("保留条目", entry_ids=[kept["id"]])["data"]
    delete_batch = api.create_batch("删除条目", entry_ids=[removed["id"]])["data"]

    kept_result = api.delete_batch(keep_batch["id"], False)["data"]
    removed_result = api.delete_batch(delete_batch["id"], True)["data"]

    assert kept_result == {
        "deleted": keep_batch["id"], "deleted_entries": 0, "cleanup_warning": "",
    }
    assert api.entries.get(kept["id"]) is not None
    assert removed_result == {
        "deleted": delete_batch["id"], "deleted_entries": 1, "cleanup_warning": "",
    }
    assert api.entries.get(removed["id"]) is None
    assert not removed_dir.exists()


def test_delete_batch_with_entries_rolls_back_database_and_files(api, tmp_path, monkeypatch):
    profile = api.create_profile("张三", "李老师")["data"]
    entry = api.create_entry(profile["id"])["data"]
    payment = tmp_path / "付款截图.png"
    payment.write_bytes(b"payment")
    api.add_attachment(
        entry["id"], str(payment), "payment_screenshot",
        options={"skip_payment_ocr": True},
    )
    batch = api.create_batch("待删除", entry_ids=[entry["id"]])["data"]
    entry_dir = api.data_root.attachments_dir / entry["id"]
    stored_files = list(entry_dir.iterdir())
    original_delete = api.batches.delete

    def fail_batch_delete(batch_id, *, commit=True):
        if not commit:
            raise OSError("批次删除失败")
        return original_delete(batch_id, commit=commit)

    monkeypatch.setattr(api.batches, "delete", fail_batch_delete)

    result = api.delete_batch(batch["id"], True)

    assert result == {"ok": False, "error": "批次删除失败"}
    assert api.batches.get(batch["id"])["entry_ids"] == [entry["id"]]
    assert api.entries.get(entry["id"]) is not None
    assert entry_dir.is_dir()
    assert list(entry_dir.iterdir()) == stored_files


def test_entry_delete_restores_attachment_directory_when_database_delete_fails(
    api, tmp_path, monkeypatch
):
    profile = api.create_profile("张三", "李老师")["data"]
    entry = api.create_entry(profile["id"])["data"]
    payment = tmp_path / "付款截图.png"
    payment.write_bytes(b"payment")
    api.add_attachment(
        entry["id"],
        str(payment),
        "payment_screenshot",
        options={"skip_payment_ocr": True},
    )
    entry_dir = api.data_root.attachments_dir / entry["id"]
    stored_files = list(entry_dir.iterdir())

    def fail_delete(_entry_ids):
        raise OSError("数据库删除失败")

    monkeypatch.setattr(api.entries, "delete_many", fail_delete)

    result = api.delete_entry(entry["id"])

    assert result == {"ok": False, "error": "数据库删除失败"}
    assert api.entries.get(entry["id"]) is not None
    assert entry_dir.is_dir()
    assert list(entry_dir.iterdir()) == stored_files


def test_bindle_import_failure_rolls_back_records_and_extracted_files(
    repos, tmp_path, monkeypatch
):
    from tidoc.db import AttachmentRepo, Database, DataRoot, EntryRepo, ProfileRepo
    from tidoc.services.bindle import export_bindle, import_bindle

    source_profile = repos["profiles"].create("张三", "李老师")
    source_entry = repos["entries"].create(source_profile["id"], parsed=_parsed())
    source_file = tmp_path / "发票.pdf"
    source_file.write_bytes(b"invoice")
    repos["attachments"].add(source_entry, source_file, "invoice_pdf")
    package = export_bindle(
        repos["entries"],
        repos["attachments"],
        [source_entry],
        tmp_path / "测试包.tidoc",
        {source_profile["id"]: source_profile},
    )

    target_root = DataRoot(tmp_path / "target")
    target_db = Database(target_root.db_path)
    target_profiles = ProfileRepo(target_db)
    target_profile = target_profiles.create("王五", "赵老师")
    target_entries = EntryRepo(target_db)
    target_attachments = AttachmentRepo(target_db, target_root)
    original_write_bytes = Path.write_bytes

    def fail_target_extract(path, data):
        if target_root.attachments_dir in path.parents:
            original_write_bytes(path, b"partial")
            raise OSError("附件解压失败")
        return original_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_target_extract)

    with pytest.raises(OSError, match="附件解压失败"):
        import_bindle(
            target_entries,
            target_attachments,
            package,
            target_profile["id"],
        )

    assert target_entries.list() == []
    assert list(target_root.attachments_dir.iterdir()) == []


def test_reset_data_root_failure_restores_database_connection(api, tmp_path, monkeypatch):
    import tidoc.db.paths

    profile = api.create_profile("张三", "李老师")["data"]
    monkeypatch.setattr(
        tidoc.db.paths,
        "default_data_root",
        lambda: tmp_path / "different-default",
    )

    def fail_migration(_target):
        raise OSError("目标目录不可写")

    monkeypatch.setattr(api.data_root, "migrate_to", fail_migration)

    result = api.reset_data_root_to_default()

    assert result == {"ok": False, "error": "目标目录不可写"}
    assert api.list_profiles()["data"][0]["id"] == profile["id"]


def test_deleting_last_ocr_payment_restores_invoice_total(api, tmp_path, monkeypatch):
    from tidoc.services import folder_import

    profile = api.create_profile("张三", "李老师")["data"]
    entry_id = api.entries.create(profile["id"], parsed=ParsedInvoice(total=Decimal("100.00")))
    payment = tmp_path / "错误付款截图.png"
    payment.write_bytes(b"payment")
    monkeypatch.setattr(
        folder_import, "extract_payment_image_amount", lambda _path: "27.00"
    )

    attachment = api.add_attachment(
        entry_id, str(payment), "payment_screenshot"
    )["data"]
    recognized = api.get_entry(entry_id)["data"]["fields"]["paid_amount"]
    assert recognized["current"] == "27.00"
    assert recognized["value_source"] == "payment_ocr"

    deleted = api.delete_attachment(attachment["id"])["data"]
    restored = api.get_entry(entry_id)["data"]["fields"]["paid_amount"]

    assert deleted["paid_amount_reset"] == {"reset": True, "value": "100.00"}
    assert restored["current"] == "100.00"
    assert restored["value_source"] == ""


def test_deleting_ocr_payment_preserves_later_manual_amount(api, tmp_path, monkeypatch):
    from tidoc.services import folder_import

    profile = api.create_profile("张三", "李老师")["data"]
    entry_id = api.entries.create(profile["id"], parsed=ParsedInvoice(total=Decimal("100.00")))
    payment = tmp_path / "付款截图.png"
    payment.write_bytes(b"payment")
    monkeypatch.setattr(
        folder_import, "extract_payment_image_amount", lambda _path: "27.00"
    )
    attachment = api.add_attachment(
        entry_id, str(payment), "payment_screenshot"
    )["data"]

    api.update_field(entry_id, "paid_amount", "35.00", profile["id"])
    manual = api.get_entry(entry_id)["data"]["fields"]["paid_amount"]
    assert manual["value_source"] == "manual"

    deleted = api.delete_attachment(attachment["id"])["data"]
    after = api.get_entry(entry_id)["data"]["fields"]["paid_amount"]

    assert deleted["paid_amount_reset"]["reset"] is False
    assert after["current"] == "35.00"
    assert after["value_source"] == "manual"


def test_paid_amount_waits_until_last_payment_is_deleted(api, tmp_path, monkeypatch):
    from tidoc.services import folder_import

    profile = api.create_profile("张三", "李老师")["data"]
    entry_id = api.entries.create(profile["id"], parsed=ParsedInvoice(total=Decimal("100.00")))
    first = tmp_path / "付款截图一.png"
    second = tmp_path / "付款截图二.png"
    first.write_bytes(b"payment-one")
    second.write_bytes(b"payment-two")
    monkeypatch.setattr(
        folder_import, "extract_payment_image_amount", lambda _path: "27.00"
    )
    first_att = api.add_attachment(
        entry_id, str(first), "payment_screenshot"
    )["data"]
    second_att = api.add_attachment(
        entry_id,
        str(second),
        "payment_screenshot",
        options={"skip_payment_ocr": True},
    )["data"]

    first_deleted = api.delete_attachment(first_att["id"])["data"]
    assert first_deleted["paid_amount_reset"]["reset"] is False
    assert api.get_entry(entry_id)["data"]["fields"]["paid_amount"]["current"] == "27.00"

    last_deleted = api.delete_attachment(second_att["id"])["data"]
    assert last_deleted["paid_amount_reset"] == {"reset": True, "value": "100.00"}
    assert api.get_entry(entry_id)["data"]["fields"]["paid_amount"]["current"] == "100.00"


def test_reclassifying_last_payment_restores_invoice_total(api, tmp_path, monkeypatch):
    from tidoc.services import folder_import

    profile = api.create_profile("张三", "李老师")["data"]
    entry_id = api.entries.create(profile["id"], parsed=ParsedInvoice(total=Decimal("100.00")))
    payment = tmp_path / "误分类截图.png"
    payment.write_bytes(b"payment")
    monkeypatch.setattr(
        folder_import, "extract_payment_image_amount", lambda _path: "27.00"
    )
    attachment = api.add_attachment(
        entry_id, str(payment), "payment_screenshot"
    )["data"]

    updated = api.update_attachment(
        attachment["id"], {"type": "other"}
    )["data"]

    assert updated["paid_amount_reset"] == {"reset": True, "value": "100.00"}
    assert api.get_entry(entry_id)["data"]["fields"]["paid_amount"]["current"] == "100.00"


def test_legacy_ocr_value_is_conservatively_restored(api, tmp_path):
    profile = api.create_profile("张三", "李老师")["data"]
    entry_id = api.entries.create(profile["id"], parsed=ParsedInvoice(total=Decimal("100.00")))
    payment = tmp_path / "旧版付款截图.png"
    payment.write_bytes(b"payment")
    attachment = api.add_attachment(
        entry_id,
        str(payment),
        "payment_screenshot",
        options={"skip_payment_ocr": True},
    )["data"]
    api.entries.update_field(
        entry_id,
        "paid_amount",
        "27.00",
        profile["id"],
        value_source="",
    )
    api.db.conn.execute(
        "UPDATE field_history SET changed_at = ? WHERE entry_id = ? AND field = 'paid_amount'",
        (attachment["added_at"], entry_id),
    )
    api.db.conn.commit()

    deleted = api.delete_attachment(attachment["id"])["data"]

    assert deleted["paid_amount_reset"] == {"reset": True, "value": "100.00"}

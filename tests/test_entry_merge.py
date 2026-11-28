"""本地三方/两方合并、材料替换、已归档保护、批注与无 Key 保留 AI 提醒的回归测试。"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import enable_payment_ai, payment_result
from tidoc.api import Api
from tidoc.db import (
    AttachmentRepo,
    BatchRepo,
    Database,
    DataRoot,
    EntryRepo,
    ProfileRepo,
)
from tidoc.db.schema import SCHEMA_VERSION
from tidoc.engine.models import ParsedInvoice, ParsedItem
from tidoc.services.bindle import export_bindle, import_bindle, inspect_bindle
from tidoc.services.inspection_pdf import INSPECTION_RECOGNITION_VERSION


def _make_repo_bundle(tmp_path: Path, name: str):
    root = DataRoot(tmp_path / name)
    db = Database(root.db_path)
    return {
        "root": root,
        "db": db,
        "profiles": ProfileRepo(db),
        "entries": EntryRepo(db),
        "attachments": AttachmentRepo(db, root),
        "batches": BatchRepo(db),
    }


def _write_img(tmp_path: Path, name: str, payload: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def test_three_way_merge_auto_replaces_attachment_and_preserves_reviewer_edits(tmp_path):
    """队员发 v1 -> 汇总人补查验单并改物资名 -> 队员换掉错付款截图发 v2 -> 汇总人三方合并零冲突。"""
    member = _make_repo_bundle(tmp_path, "member")
    reviewer = _make_repo_bundle(tmp_path, "reviewer")
    try:
        mp = member["profiles"].create("队员甲", "审核人")
        rp = reviewer["profiles"].create("汇总人", "审核人")
        eid_m = member["entries"].create(
            mp["id"],
            parsed=ParsedInvoice(
                invoice_no="MERGE-3WAY-001",
                seller="示例供应商",
                total=Decimal("88.00"),
                items=[ParsedItem("*电子*电阻", "电阻", "盒", Decimal("1"), Decimal("88.00"))],
            ),
        )
        bad_shot = _write_img(tmp_path, "bad_order.png", b"bad-order-screenshot-bytes")
        att_bad = member["attachments"].add(eid_m, bad_shot, "payment_screenshot")

        pkg_v1 = export_bindle(
            member["entries"], member["attachments"], [eid_m],
            tmp_path / "v1.tidoc", {mp["id"]: mp},
        )
        r1 = import_bindle(reviewer["entries"], reviewer["attachments"], pkg_v1, rp["id"])
        assert r1["imported"] == 1
        eid_r = r1["entry_ids"][0]
        old_local_path = Path(reviewer["attachments"].list(eid_r)[0]["abs_path"])
        assert old_local_path.exists()

        # 汇总人在本机补了查验单并修改了实际物资名称
        insp_pdf = _write_img(tmp_path, "insp.pdf", b"%PDF-1.4 fake inspection")
        reviewer["attachments"].add(eid_r, insp_pdf, "inspection_pdf")
        reviewer["entries"].update_field(eid_r, "actual_item_name", "贴片电阻0603", rp["id"])

        # 队员在自己电脑上删掉错截图、换上正确付款截图，重新导包 v2
        member["attachments"].delete(att_bad["id"])
        good_shot = _write_img(tmp_path, "good_wechat.png", b"good-wechat-receipt-bytes")
        member["attachments"].add(eid_m, good_shot, "payment_screenshot")
        pkg_v2 = export_bindle(
            member["entries"], member["attachments"], [eid_m],
            tmp_path / "v2.tidoc", {mp["id"]: mp},
        )

        preview = inspect_bindle(pkg_v2, reviewer["entries"])
        e_prev = preview["entries"][0]
        assert e_prev["import_action"] == "merge"
        assert e_prev["merge_preview"]["mode"] == "three_way"
        assert e_prev["merge_preview"]["has_conflicts"] is False
        assert e_prev["merge_preview"]["has_replacements"] is True

        r2 = import_bindle(reviewer["entries"], reviewer["attachments"], pkg_v2, rp["id"], inspected=preview)
        assert r2["updated"] == 1
        final = reviewer["entries"].get(eid_r)
        pay_atts = [a for a in final["attachments"] if a["type"] == "payment_screenshot"]
        insp_atts = [a for a in final["attachments"] if a["type"] == "inspection_pdf"]
        assert len(pay_atts) == 1
        assert pay_atts[0]["original_name"] == "good_wechat.png"
        assert len(insp_atts) == 1  # 汇总人补的查验单完好保留
        assert final["fields"]["actual_item_name"]["current"] == "贴片电阻0603"  # 汇总人的修改完好保留
        assert not old_local_path.exists()  # 旧错图文件已在 commit 后清理
        assert any("[合并更新]付款截图" in h["field"] for h in final["history"])
    finally:
        member["db"].close()
        reviewer["db"].close()


def test_three_way_conflict_and_smart_multi_file_merge(tmp_path):
    """Base=[A], Local=[A,C] 且实付改为 80, Incoming=[B] 且实付改为 85 -> 标为冲突并支持智能合并 [B,C] 与逐项选择。"""
    member = _make_repo_bundle(tmp_path, "member")
    reviewer = _make_repo_bundle(tmp_path, "reviewer")
    try:
        mp = member["profiles"].create("队员乙", "审核人")
        rp = reviewer["profiles"].create("汇总人", "审核人")
        eid_m = member["entries"].create(
            mp["id"], parsed=ParsedInvoice(invoice_no="MERGE-CONFLICT-001", total=Decimal("100.00"))
        )
        shot_a = _write_img(tmp_path, "a.png", b"shot-A")
        att_a = member["attachments"].add(eid_m, shot_a, "payment_screenshot")

        pkg_v1 = export_bindle(member["entries"], member["attachments"], [eid_m], tmp_path / "v1.tidoc", {mp["id"]: mp})
        eid_r = import_bindle(reviewer["entries"], reviewer["attachments"], pkg_v1, rp["id"])["entry_ids"][0]

        # 汇总人本机加了截图 C，并改实付为 80.00
        shot_c = _write_img(tmp_path, "c.png", b"shot-C")
        reviewer["attachments"].add(eid_r, shot_c, "payment_screenshot")
        reviewer["entries"].update_field(eid_r, "paid_amount", "80.00", rp["id"])

        # 队员把 A 换成了 B，并改实付为 85.00
        member["attachments"].delete(att_a["id"])
        shot_b = _write_img(tmp_path, "b.png", b"shot-B")
        member["attachments"].add(eid_m, shot_b, "payment_screenshot")
        member["entries"].update_field(eid_m, "paid_amount", "85.00", mp["id"])
        pkg_v2 = export_bindle(member["entries"], member["attachments"], [eid_m], tmp_path / "v2.tidoc", {mp["id"]: mp})

        preview = inspect_bindle(pkg_v2, reviewer["entries"])
        e_prev = preview["entries"][0]
        assert e_prev["import_action"] == "conflict"
        items_by_key = {item["key"]: item for item in e_prev["merge_preview"]["items"]}
        assert items_by_key["att:payment_screenshot"]["status"] == "conflict"
        assert any(c["value"] == "three_way" for c in items_by_key["att:payment_screenshot"]["choices"])
        assert items_by_key["field:paid_amount"]["status"] == "conflict"

        # 汇总人选择：付款截图用 three_way 智能合并（去 A 留 B+C），实付采用包内 85.00
        result = import_bindle(
            reviewer["entries"], reviewer["attachments"], pkg_v2, rp["id"],
            inspected=preview,
            options={"entry_merge_decisions": {"0": {
                "att:payment_screenshot": "three_way",
                "field:paid_amount": "use_incoming",
            }}},
        )
        assert result["updated"] == 1
        final = reviewer["entries"].get(eid_r)
        names = sorted(a["original_name"] for a in final["attachments"] if a["type"] == "payment_screenshot")
        assert names == ["b.png", "c.png"]
        assert final["fields"]["paid_amount"]["current"] == "85.00"
    finally:
        member["db"].close()
        reviewer["db"].close()


def test_resending_old_bindle_detected_as_ancestor_and_unchanged(tmp_path):
    """对方误发旧版本绑定包（本机已在其基础上更新）时，识别为已包含祖先且无变化。"""
    member = _make_repo_bundle(tmp_path, "member")
    reviewer = _make_repo_bundle(tmp_path, "reviewer")
    try:
        mp = member["profiles"].create("队员丙", "审核人")
        rp = reviewer["profiles"].create("汇总人", "审核人")
        eid_m = member["entries"].create(
            mp["id"], parsed=ParsedInvoice(invoice_no="ANCESTOR-001", total=Decimal("50.00"))
        )
        member["attachments"].add(eid_m, _write_img(tmp_path, "old.png", b"old-pay"), "payment_screenshot")
        pkg_old = export_bindle(member["entries"], member["attachments"], [eid_m], tmp_path / "old.tidoc", {mp["id"]: mp})
        eid_r = import_bindle(reviewer["entries"], reviewer["attachments"], pkg_old, rp["id"])["entry_ids"][0]

        # 汇总人本地做了修正并替换了截图
        reviewer["entries"].update_field(eid_r, "actual_item_name", "汇总人已核对品名", rp["id"])
        old_att = reviewer["entries"].get(eid_r)["attachments"][0]
        reviewer["attachments"].update(old_att["id"], src_path=_write_img(tmp_path, "new.png", b"new-pay"))

        # 再次收到同一个旧包
        preview = inspect_bindle(pkg_old, reviewer["entries"])
        assert preview["entries"][0]["merge_preview"]["mode"] == "ancestor"
        assert preview["entries"][0]["import_action"] == "unchanged"
    finally:
        member["db"].close()
        reviewer["db"].close()


def test_two_way_divergent_inspection_replace_and_archived_protection(tmp_path):
    """无共同基线时不同查验单标为待确认并展示查验次数；已归档条目默认保护不覆盖。"""
    source = _make_repo_bundle(tmp_path, "source")
    target = _make_repo_bundle(tmp_path, "target")
    try:
        sp = source["profiles"].create("发送方", "审核人")
        tp = target["profiles"].create("接收方", "审核人")
        eid_s = source["entries"].create(sp["id"], parsed=ParsedInvoice(invoice_no="INSP-2WAY-001", total=Decimal("20.00")))
        eid_t = target["entries"].create(tp["id"], parsed=ParsedInvoice(invoice_no="INSP-2WAY-001", total=Decimal("20.00")))

        att_s = source["attachments"].add(eid_s, _write_img(tmp_path, "first.pdf", b"%PDF-1.4 first"), "inspection_pdf")
        source["attachments"].set_recognition(
            att_s["id"], INSPECTION_RECOGNITION_VERSION, "recognized",
            json.dumps({"sha256": att_s["sha256"], "count": 1, "count_scope": "day", "checked_at": "2026-11-28 10:00:00", "invoice_no": "INSP-2WAY-001"}),
        )
        att_t = target["attachments"].add(eid_t, _write_img(tmp_path, "second.pdf", b"%PDF-1.4 second"), "inspection_pdf")
        target["attachments"].set_recognition(
            att_t["id"], INSPECTION_RECOGNITION_VERSION, "recognized",
            json.dumps({"sha256": att_t["sha256"], "count": 2, "count_scope": "day", "checked_at": "2026-11-20 10:00:00", "invoice_no": "INSP-2WAY-001"}),
        )

        # 放入已归档批次，检查保护标记
        batch = target["batches"].create("历史已交批次", entry_ids=[eid_t])
        target["batches"].set_archived(batch["id"], True)

        pkg = export_bindle(source["entries"], source["attachments"], [eid_s], tmp_path / "insp.tidoc", {sp["id"]: sp})
        preview = inspect_bindle(pkg, target["entries"])
        e_prev = preview["entries"][0]
        assert e_prev["import_action"] == "conflict"
        assert e_prev["merge_preview"]["archived"] is True
        insp_item = next(i for i in e_prev["merge_preview"]["items"] if i["key"] == "att:inspection_pdf")
        assert insp_item["default_choice"] == "keep_local"
        assert "第 2 次查验" in insp_item["local_files"][0]["meta"]
        assert "第 1 次查验" in insp_item["incoming_files"][0]["meta"]

        # 显式选择替换后，旧查验单被替换
        res = import_bindle(
            target["entries"], target["attachments"], pkg, tp["id"],
            inspected=preview,
            options={"entry_merge_decisions": {"0": {"att:inspection_pdf": "replace"}}},
        )
        assert res["updated"] == 1
        atts = [a for a in target["entries"].get(eid_t)["attachments"] if a["type"] == "inspection_pdf"]
        assert len(atts) == 1 and atts[0]["original_name"] == "first.pdf"
    finally:
        source["db"].close()
        target["db"].close()


def test_replacement_rollback_keeps_original_file_intact(tmp_path, monkeypatch):
    """替换附件中途失败时回滚数据库并清理新文件，本机旧附件磁盘文件完好无损。"""
    from tidoc.services import bindle_import

    member = _make_repo_bundle(tmp_path, "member")
    reviewer = _make_repo_bundle(tmp_path, "reviewer")
    try:
        mp = member["profiles"].create("队员", "审核人")
        rp = reviewer["profiles"].create("汇总人", "审核人")
        eid_m = member["entries"].create(mp["id"], parsed=ParsedInvoice(invoice_no="ROLLBACK-001", total=Decimal("30.00")))
        att_old = member["attachments"].add(eid_m, _write_img(tmp_path, "old.png", b"old-bytes"), "payment_screenshot")
        pkg1 = export_bindle(member["entries"], member["attachments"], [eid_m], tmp_path / "p1.tidoc", {mp["id"]: mp})
        eid_r = import_bindle(reviewer["entries"], reviewer["attachments"], pkg1, rp["id"])["entry_ids"][0]
        local_old_path = Path(reviewer["attachments"].list(eid_r)[0]["abs_path"])

        member["attachments"].delete(att_old["id"])
        member["attachments"].add(eid_m, _write_img(tmp_path, "new.png", b"new-bytes"), "payment_screenshot")
        pkg2 = export_bindle(member["entries"], member["attachments"], [eid_m], tmp_path / "p2.tidoc", {mp["id"]: mp})

        def boom(*args, **kwargs):
            raise RuntimeError("模拟提交前失败")

        monkeypatch.setattr(bindle_import, "_assign_batches", boom)
        with pytest.raises(RuntimeError, match="模拟提交前失败"):
            import_bindle(reviewer["entries"], reviewer["attachments"], pkg2, rp["id"])

        assert local_old_path.exists()
        assert local_old_path.read_bytes() == b"old-bytes"
        atts = reviewer["entries"].get(eid_r)["attachments"]
        assert len(atts) == 1 and atts[0]["original_name"] == "old.png"
    finally:
        member["db"].close()
        reviewer["db"].close()


def test_imported_payment_ai_reminders_and_batch_note_survive_without_ai_key(tmp_path, monkeypatch):
    """汇总人有 AI Key 和批次催办批注 -> 导包给无 AI Key 队员 -> 提醒和批注完整保留；队员换图后提醒自动消失。"""
    reviewer_api = Api(tmp_path / "reviewer_app")
    member_api = Api(tmp_path / "member_app")
    try:
        enable_payment_ai(reviewer_api)
        rp = reviewer_api.create_profile("汇总人", "审核人")["data"]
        mp = member_api.create_profile("队员甲", "审核人")["data"]

        eid_r = reviewer_api.entries.create(
            rp["id"], parsed=ParsedInvoice(invoice_no="AI-REMIND-001", total=Decimal("50.00"))
        )
        order_img = _write_img(tmp_path, "order.png", b"order-page-image")
        monkeypatch.setattr(
            reviewer_api, "_payment_ai_cached",
            lambda _path: payment_result("", kind="order", completed=False),
        )
        reviewer_api.add_attachment(eid_r, str(order_img), "payment_screenshot")
        assert "订单页" in reviewer_api.entries.get(eid_r)["check_message"]

        # 汇总人在批次里写催办批注
        batch = reviewer_api.batches.create("11月报账", entry_ids=[eid_r])
        reviewer_api.set_batch_entry_note(batch["id"], eid_r, "请更换为微信扣款账单截图")

        pkg = reviewer_api.export_bindle([eid_r], "退回修改")["data"]["path"]

        # 队员电脑没有配置 AI Key（同时验证新增导入和重开应用后提醒都保留）
        assert not member_api._payment_ai_available()
        imported = member_api.import_bindle(pkg, mp["id"])["data"]
        assert imported["imported"] == 1
        eid_m = imported["entry_ids"][0]
        entry_m = member_api.entries.get(eid_m)
        assert "[付款截图识别]" in entry_m["check_message"]
        assert "订单页" in entry_m["check_message"]
        assert "请更换为微信扣款账单截图" in entry_m["fields"]["notes"]["current"]
        assert member_api.payment_ai_result(eid_m)["data"]["has_imported"] is True

        # 重启无 Key 客户端，启动检查不会抹掉导入的 AI 提醒
        member_api.db.close()
        member_api = Api(tmp_path / "member_app")
        assert "订单页" in member_api.entries.get(eid_m)["check_message"]

        # 队员替换掉有问题的付款截图后，旧的导入提醒自动消失，且不会提示“尚未识别”
        bad_att_id = member_api.entries.get(eid_m)["attachments"][0]["id"]
        good_img = _write_img(tmp_path, "real_pay.png", b"real-wechat-payment")
        member_api.update_attachment(bad_att_id, {"src_path": str(good_img)})
        after_fix = member_api.entries.get(eid_m)
        assert "[付款截图识别]" not in after_fix["check_message"]
    finally:
        reviewer_api.db.close()
        member_api.db.close()


def test_imported_payment_ai_reminder_merges_onto_existing_entry_without_ai_key(tmp_path, monkeypatch):
    """队员本机已有同一条目和同一张截图（无 AI 结论）-> 收到汇总人带 AI 提醒的回包 -> 无 Key 也能补入提醒。"""
    reviewer_api = Api(tmp_path / "rev2")
    member_api = Api(tmp_path / "mem2")
    try:
        enable_payment_ai(reviewer_api)
        rp = reviewer_api.create_profile("汇总人", "审核人")["data"]
        mp = member_api.create_profile("队员", "审核人")["data"]

        eid_m = member_api.entries.create(mp["id"], parsed=ParsedInvoice(invoice_no="AI-MERGE-002", total=Decimal("50.00")))
        shot = _write_img(tmp_path, "pay4320.png", b"same-screenshot-4320")
        member_api.add_attachment(eid_m, str(shot), "payment_screenshot")
        assert "[付款截图识别]" not in member_api.entries.get(eid_m)["check_message"]

        # 队员发给汇总人，汇总人跑 AI 发现金额不一致（43.20 vs 50.00）再发回给队员
        pkg_to_rev = member_api.export_bindle([eid_m], "交汇总")["data"]["path"]
        eid_r = reviewer_api.import_bindle(pkg_to_rev, rp["id"])["data"]["entry_ids"][0]
        att_r = reviewer_api.entries.get(eid_r)["attachments"][0]
        reviewer_api._save_payment_ai_result(eid_r, att_r, payment_result("43.20"))
        assert "不一致" in reviewer_api.entries.get(eid_r)["check_message"]

        pkg_back = reviewer_api.export_bindle([eid_r], "回传核对")["data"]["path"]
        prev = member_api.inspect_bindle(pkg_back)["data"]
        assert prev["entries"][0]["import_action"] == "merge"
        assert prev["entries"][0]["merge_preview"]["payment_ai_upgrades"] == 1

        res = member_api.import_bindle(pkg_back, mp["id"])["data"]
        assert res["updated"] == 1
        assert "识别合计 43.20 与实付 50.00 不一致" in member_api.entries.get(eid_m)["check_message"]

        # 队员在无 Key 电脑上把实付改成 43.20 后，金额不一致提醒自动消除
        member_api.update_field(eid_m, "paid_amount", "43.20", mp["id"])
        assert "不一致" not in member_api.entries.get(eid_m)["check_message"]
    finally:
        reviewer_api.db.close()
        member_api.db.close()


def test_schema_v14_has_exchange_bases_table(tmp_path):
    db = Database(tmp_path / "schema_check.sqlite")
    try:
        ver = db.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()["value"]
        assert int(ver) == SCHEMA_VERSION == 14
        tbl = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='entry_exchange_bases'"
        ).fetchone()
        assert tbl is not None
    finally:
        db.close()


def test_deliberate_revert_is_not_mistaken_for_stale_ancestor(tmp_path):
    """A -> B -> A（主动改回原值）应以 B 为共同祖先做三方合并，而不是误判为旧包。"""
    member = _make_repo_bundle(tmp_path, "member_rev")
    reviewer = _make_repo_bundle(tmp_path, "reviewer_rev")
    try:
        mp = member["profiles"].create("队员", "审核人")
        rp = reviewer["profiles"].create("汇总人", "审核人")
        eid_m = member["entries"].create(mp["id"], parsed=ParsedInvoice(invoice_no="REVERT-001", total=Decimal("10.00")))
        pkg_a = export_bindle(member["entries"], member["attachments"], [eid_m], tmp_path / "a.tidoc", {mp["id"]: mp})
        eid_r = import_bindle(reviewer["entries"], reviewer["attachments"], pkg_a, rp["id"])["entry_ids"][0]

        # 双方同步到 B（实付 20.00）
        member["entries"].update_field(eid_m, "paid_amount", "20.00", mp["id"])
        pkg_b = export_bindle(member["entries"], member["attachments"], [eid_m], tmp_path / "b.tidoc", {mp["id"]: mp})
        import_bindle(reviewer["entries"], reviewer["attachments"], pkg_b, rp["id"])
        assert reviewer["entries"].get(eid_r)["fields"]["paid_amount"]["current"] == "20.00"

        # 队员主动把实付改回 A（10.00），汇总人也改成了 30.00 -> 应检测到冲突（三方基线是 B=20.00）
        reviewer["entries"].update_field(eid_r, "paid_amount", "30.00", rp["id"])
        member["entries"].update_field(eid_m, "paid_amount", "10.00", mp["id"])
        pkg_revert = export_bindle(member["entries"], member["attachments"], [eid_m], tmp_path / "revert.tidoc", {mp["id"]: mp})

        preview = inspect_bindle(pkg_revert, reviewer["entries"])
        assert preview["entries"][0]["merge_preview"]["mode"] == "three_way"
        assert preview["entries"][0]["import_action"] == "conflict"
    finally:
        member["db"].close()
        reviewer["db"].close()


def test_missing_replacement_member_in_archive_preserves_local_attachment(tmp_path):
    """包内缺少待替换的附件文件时，拒绝删除本机原附件并回滚。"""
    import zipfile

    member = _make_repo_bundle(tmp_path, "m_miss")
    reviewer = _make_repo_bundle(tmp_path, "r_miss")
    try:
        mp = member["profiles"].create("队员", "审核人")
        rp = reviewer["profiles"].create("汇总人", "审核人")
        eid_m = member["entries"].create(mp["id"], parsed=ParsedInvoice(invoice_no="MISS-ATT-001", total=Decimal("10.00")))
        att1 = member["attachments"].add(eid_m, _write_img(tmp_path, "a.png", b"aaa"), "payment_screenshot")
        pkg1 = export_bindle(member["entries"], member["attachments"], [eid_m], tmp_path / "p1.tidoc", {mp["id"]: mp})
        eid_r = import_bindle(reviewer["entries"], reviewer["attachments"], pkg1, rp["id"])["entry_ids"][0]
        local_path = Path(reviewer["attachments"].list(eid_r)[0]["abs_path"])

        # 队员替换了新图，但磁盘上的新图在导出前缺失 -> 导出的包里没有该附件成员
        member["attachments"].delete(att1["id"])
        att2 = member["attachments"].add(eid_m, _write_img(tmp_path, "b.png", b"bbb"), "payment_screenshot")
        Path(att2["abs_path"]).unlink()
        pkg2 = export_bindle(member["entries"], member["attachments"], [eid_m], tmp_path / "p2.tidoc", {mp["id"]: mp})

        with pytest.raises(ValueError, match="缺少附件文件"):
            import_bindle(reviewer["entries"], reviewer["attachments"], pkg2, rp["id"])
        assert local_path.exists()
        assert len(reviewer["attachments"].list(eid_r)) == 1
    finally:
        member["db"].close()
        reviewer["db"].close()


def test_include_notes_false_excludes_merged_notes_history_and_batch_note(tmp_path):
    """include_notes=False 时不导出 [合并更新]notes 历史与批次催办备注。"""
    import zipfile

    src = _make_repo_bundle(tmp_path, "src_nonote")
    try:
        p = src["profiles"].create("队员", "审核人")
        eid = src["entries"].create(p["id"], parsed=ParsedInvoice(invoice_no="NONOTE-001", total=Decimal("10.00")))
        src["entries"].update_field(eid, "notes", "私密备注", p["id"])
        src["db"].conn.execute(
            "INSERT INTO field_history(entry_id, field, old_value, new_value, profile_id, changed_at) VALUES(?,?,?,?,?,?)",
            (eid, "[合并更新]notes", "旧私密", "新私密", "", "2026-11-28T10:00:00"),
        )
        src["batches"].create("某批次", entry_ids=[eid])
        batch_id = src["batches"].list()[0]["id"]
        src["batches"].set_entry_note(batch_id, eid, "私密催办")
        src["db"].conn.commit()

        pkg = export_bindle(
            src["entries"], src["attachments"], [eid],
            tmp_path / "nonote.tidoc", {p["id"]: p}, include_notes=False,
        )
        with zipfile.ZipFile(pkg, "r") as zf:
            payload = json.loads(zf.read("entries.json"))
        raw = json.dumps(payload, ensure_ascii=False)
        assert "私密备注" not in raw
        assert "旧私密" not in raw
        assert "私密催办" not in raw
    finally:
        src["db"].close()


def test_snapshot_hash_normalizes_money_and_absent_batch_note():
    from tidoc.services.entry_merge import build_entry_snapshot, snapshot_digest

    e1 = {"total": "10", "fields": {"paid_amount": {"current": "10.0"}}}
    e2 = {"total": "10.00", "fields": {"paid_amount": {"current": "10.00"}}, "batch_note": ""}
    assert snapshot_digest(build_entry_snapshot(e1)) == snapshot_digest(build_entry_snapshot(e2, batch_note=""))
    assert "batch_note" not in build_entry_snapshot(e1)


def test_partial_invoice_field_accept_keeps_valid_local_amount_warning(tmp_path):
    """仅采纳包内销售方、保留本机总额/明细时，按合并后真实数据重算发票提醒，不误消总额不闭合警告。"""
    src = _make_repo_bundle(tmp_path, "src_warn")
    dst = _make_repo_bundle(tmp_path, "dst_warn")
    try:
        sp = src["profiles"].create("发送", "审核")
        dp = dst["profiles"].create("接收", "审核")
        eid_s = src["entries"].create(
            sp["id"],
            parsed=ParsedInvoice(
                invoice_no="PARTIAL-WARN-001", seller="新销售方", total=Decimal("100.00"),
                items=[ParsedItem("品名", "品名", "个", Decimal("1"), Decimal("100.00"))],
            ),
        )
        src["entries"].set_check(eid_s, "pass", "")
        eid_d = dst["entries"].create(
            dp["id"],
            parsed=ParsedInvoice(
                invoice_no="PARTIAL-WARN-001", seller="旧销售方", total=Decimal("100.00"),
                items=[ParsedItem("品名", "品名", "个", Decimal("1"), Decimal("80.00"))],
            ),
        )
        dst["entries"].set_check(eid_d, "warning", "[发票金额] 明细识别合计与发票总额相差 20.00")

        pkg = export_bindle(src["entries"], src["attachments"], [eid_s], tmp_path / "pw.tidoc", {sp["id"]: sp})
        prev = inspect_bindle(pkg, dst["entries"])
        import_bindle(
            dst["entries"], dst["attachments"], pkg, dp["id"],
            inspected=prev,
            options={"entry_merge_decisions": {"0": {"base:seller": "use_incoming", "items": "keep_local"}}},
        )
        final = dst["entries"].get(eid_d)
        assert final["seller"] == "新销售方"
        assert final["check_status"] == "warning"
        assert "明细" in final["check_message"] or "金额" in final["check_message"]
    finally:
        src["db"].close()
        dst["db"].close()


def test_removing_all_imported_payment_screenshots_clears_stale_payment_warning(tmp_path, monkeypatch):
    """替换/移除最后一张带导入 AI 警告的付款截图后，旧 [付款截图识别] 提醒自动清空。"""
    rev = Api(tmp_path / "rev_clear")
    mem = Api(tmp_path / "mem_clear")
    try:
        enable_payment_ai(rev)
        rp = rev.create_profile("汇总", "审核")["data"]
        mp = mem.create_profile("队员", "审核")["data"]
        eid_r = rev.entries.create(rp["id"], parsed=ParsedInvoice(invoice_no="CLEAR-PAY-001", total=Decimal("50.00")))
        bad = _write_img(tmp_path, "bad.png", b"bad-order")
        monkeypatch.setattr(rev, "_payment_ai_cached", lambda _p: payment_result("", kind="order", completed=False))
        rev.add_attachment(eid_r, str(bad), "payment_screenshot")
        pkg1 = rev.export_bindle([eid_r], "带提醒")["data"]["path"]
        eid_m = mem.import_bindle(pkg1, mp["id"])["data"]["entry_ids"][0]
        assert "[付款截图识别]" in mem.entries.get(eid_m)["check_message"]

        # 发送方把错图删掉换成无 AI 结论的新图再发过来，接收方三方替换后旧提醒应消失
        rev.save_payment_ai_settings({}, clear_key=True)
        rev.delete_attachment(rev.entries.get(eid_r)["attachments"][0]["id"])
        rev.add_attachment(eid_r, str(_write_img(tmp_path, "clean.png", b"clean-shot")), "payment_screenshot")
        pkg2 = rev.export_bindle([eid_r], "已换新图")["data"]["path"]
        mem.import_bindle(pkg2, mp["id"])
        assert "[付款截图识别]" not in mem.entries.get(eid_m)["check_message"]
    finally:
        rev.db.close()
        mem.db.close()


def test_ancestor_resend_does_not_restore_deleted_attachment_note_and_archived_add_is_protected(tmp_path):
    """重发旧包不会把已删除的附件备注写回；已归档条目的纯新增材料也默认保护不静默写入。"""
    from tidoc.services.entry_merge import merge_text

    assert merge_text("请补充", "补充") == "请补充\n补充"
    assert merge_text("第一行\n补充", "补充") == "第一行\n补充"

    member = _make_repo_bundle(tmp_path, "m_anc")
    reviewer = _make_repo_bundle(tmp_path, "r_anc")
    try:
        mp = member["profiles"].create("队员", "审核人")
        rp = reviewer["profiles"].create("汇总人", "审核人")
        eid_m = member["entries"].create(mp["id"], parsed=ParsedInvoice(invoice_no="ANC-NOTE-001", total=Decimal("20.00")))
        member["attachments"].add(eid_m, _write_img(tmp_path, "p.png", b"pay-p"), "payment_screenshot", note="旧附件备注")
        pkg1 = export_bindle(member["entries"], member["attachments"], [eid_m], tmp_path / "p1.tidoc", {mp["id"]: mp})
        eid_r = import_bindle(reviewer["entries"], reviewer["attachments"], pkg1, rp["id"])["entry_ids"][0]

        # 汇总人清空了附件备注
        att_r = reviewer["attachments"].list(eid_r)[0]
        reviewer["attachments"].set_note(att_r["id"], "")
        # 误发旧包 pkg1 -> 应识别为 ancestor，不把“旧附件备注”写回
        prev = inspect_bindle(pkg1, reviewer["entries"])
        assert prev["entries"][0]["merge_preview"]["mode"] == "ancestor"
        assert prev["entries"][0]["import_action"] == "unchanged"
        import_bindle(reviewer["entries"], reviewer["attachments"], pkg1, rp["id"], inspected=prev)
        assert reviewer["attachments"].list(eid_r)[0]["note"] == ""

        # 把汇总人条目归档，队员新增一份查验单发 pkg2 -> 已归档条目默认不静默新增，标为待确认
        b = reviewer["batches"].create("已归档", entry_ids=[eid_r])
        reviewer["batches"].set_archived(b["id"], True)
        member["attachments"].add(eid_m, _write_img(tmp_path, "insp.pdf", b"%PDF-1.4 new insp"), "inspection_pdf")
        pkg2 = export_bindle(member["entries"], member["attachments"], [eid_m], tmp_path / "p2.tidoc", {mp["id"]: mp})
        prev2 = inspect_bindle(pkg2, reviewer["entries"])
        assert prev2["entries"][0]["import_action"] == "conflict"
        insp_item = next(i for i in prev2["entries"][0]["merge_preview"]["items"] if i["key"] == "att:inspection_pdf")
        assert insp_item["default_choice"] == "keep_local"
        assert insp_item["archived_protected"] is True
    finally:
        member["db"].close()
        reviewer["db"].close()

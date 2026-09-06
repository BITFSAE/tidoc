"""批次仓库、标签/备注筛选维度、schema 迁移的测试。"""

import sqlite3
from decimal import Decimal

import pytest

from tidoc.db import Database
from tidoc.db.attachments import TYPE_PAYMENT
from tidoc.db.batches import BatchRepo
from tidoc.db.schema import SCHEMA_VERSION
from tidoc.engine import parse_xml


def _make_entries(repos, sample_xmls, n=3, profile=None):
    p = profile or repos["profiles"].create("张三", "李老师")
    ids = []
    for x in sample_xmls[:n]:
        parsed = parse_xml(x)
        ids.append(repos["entries"].create(p["id"], title=parsed.buyer_name, parsed=parsed))
    return p, ids


# ------------------------------------------------------------------ 批次

def test_batch_create_and_add_entries(repos, sample_xmls):
    _, ids = _make_entries(repos, sample_xmls, 3)
    b = repos["batches"].create("第一批", entry_ids=ids[:2])
    assert b["count"] == 2
    added = repos["batches"].add_entries(b["id"], [ids[2]])
    assert added == 1
    # 重复装入不增加
    assert repos["batches"].add_entries(b["id"], [ids[2]]) == 0
    assert repos["batches"].get(b["id"])["count"] == 3


def test_batch_remove_and_delete_keeps_entries(repos, sample_xmls):
    _, ids = _make_entries(repos, sample_xmls, 2)
    b = repos["batches"].create("批次", entry_ids=ids)
    repos["batches"].remove_entries(b["id"], [ids[0]])
    assert repos["batches"].get(b["id"])["count"] == 1
    repos["batches"].delete(b["id"])
    assert repos["batches"].get(b["id"]) is None
    # 删批次不删条目
    assert len(repos["entries"].list()) == 2


def test_batch_move_entries(repos, sample_xmls):
    _, ids = _make_entries(repos, sample_xmls, 2)
    source = repos["batches"].create("原批次", entry_ids=ids)
    target = repos["batches"].create("新批次", entry_ids=[ids[0]])

    result = repos["batches"].move_entries(source["id"], target["id"], ids)
    assert result == {"added": 1, "removed": 2}
    assert repos["batches"].get(source["id"])["entry_ids"] == []
    assert set(repos["batches"].get(target["id"])["entry_ids"]) == set(ids)


def test_set_single_entry_batch_can_replace_or_clear(repos, sample_xmls):
    _, ids = _make_entries(repos, sample_xmls, 1)
    first = repos["batches"].create("原批次", entry_ids=ids)
    second = repos["batches"].create("新批次")

    replaced = repos["batches"].set_entry_batch(ids[0], second["id"])
    assert replaced["removed"] == 1
    assert repos["batches"].get(first["id"])["entry_ids"] == []
    assert repos["batches"].get(second["id"])["entry_ids"] == ids

    cleared = repos["batches"].set_entry_batch(ids[0])
    assert cleared["removed"] == 1
    assert repos["batches"].get(second["id"])["entry_ids"] == []


def test_batch_entry_note(repos, sample_xmls):
    _, ids = _make_entries(repos, sample_xmls, 1)
    b = repos["batches"].create("批次")
    repos["batches"].set_entry_note(b["id"], ids[0], "缺查验单")
    got = repos["batches"].get(b["id"])
    assert got["entry_notes"][ids[0]] == "缺查验单"
    assert got["count"] == 1  # 设备注会自动装入


def test_batch_note_create_and_update(repos, sample_xmls):
    _, ids = _make_entries(repos, sample_xmls, 1)
    b = repos["batches"].create("批次", note="原始备注", entry_ids=ids)
    assert b["note"] == "原始备注"
    updated = repos["batches"].update(b["id"], note="更新后的批次备注")
    assert updated["note"] == "更新后的批次备注"
    assert repos["batches"].get(b["id"])["note"] == "更新后的批次备注"


def test_batch_delete_cascades_when_entry_deleted(repos, sample_xmls):
    _, ids = _make_entries(repos, sample_xmls, 2)
    b = repos["batches"].create("批次", entry_ids=ids)
    repos["entries"].delete(ids[0])
    # 条目删除后，批次关联应被外键级联清理
    assert repos["batches"].get(b["id"])["count"] == 1


def test_batch_stats_by_person(repos, sample_xmls):
    p1 = repos["profiles"].create("张三", "李老师")
    p2 = repos["profiles"].create("王五", "赵老师")
    _, ids1 = _make_entries(repos, sample_xmls, 2, profile=p1)
    _, ids2 = _make_entries(repos, sample_xmls, 1, profile=p2)
    b = repos["batches"].create("混合批", entry_ids=ids1 + ids2)
    stats = repos["batches"].get(b["id"])["stats"]
    assert stats["count"] == 3
    names = {row["name"] for row in stats["by_person"]}
    assert names == {"张三", "王五"}


def test_batch_archive_excluded_from_list(repos, sample_xmls):
    repos["batches"].create("活跃批")
    b2 = repos["batches"].create("已交批")
    repos["batches"].set_archived(b2["id"], True)
    active = repos["batches"].list()
    assert all(not x["archived"] for x in active)
    assert len(active) == 1
    assert len(repos["batches"].list(include_archived=True)) == 2


def test_archived_batch_entries_leave_working_surface(repos, sample_xmls):
    _, ids = _make_entries(repos, sample_xmls, 3)
    archived = repos["batches"].create("已交批", entry_ids=ids[:2])
    active = repos["batches"].create("在办批", entry_ids=[ids[1]])
    repos["batches"].set_archived(archived["id"], True)

    working = {entry["id"] for entry in repos["entries"].list(active_only=True)}
    shelved = {entry["id"] for entry in repos["entries"].list(archived_only=True)}
    assert working == {ids[1], ids[2]}
    assert shelved == {ids[0]}
    repos["batches"].set_archived(archived["id"], False)
    assert {entry["id"] for entry in repos["entries"].list(active_only=True)} == set(ids)
    assert repos["entries"].list(archived_only=True) == []
    assert repos["batches"].get(active["id"])["entry_ids"] == [ids[1]]


def test_focused_archived_batch_shows_full_membership(repos, sample_xmls):
    _, ids = _make_entries(repos, sample_xmls, 3)
    archived = repos["batches"].create("已交批", entry_ids=ids[:2])
    active = repos["batches"].create("在办批", entry_ids=[ids[1]])
    repos["batches"].set_archived(archived["id"], True)

    focused = {entry["id"] for entry in repos["entries"].list(batch_id=archived["id"])}
    assert focused == {ids[0], ids[1]}
    assert {entry["id"] for entry in repos["entries"].list(archived_only=True)} == {ids[0]}


def test_batches_of_entry(repos, sample_xmls):
    _, ids = _make_entries(repos, sample_xmls, 1)
    b1 = repos["batches"].create("批一", entry_ids=ids)
    b2 = repos["batches"].create("批二", entry_ids=ids)
    names = {x["name"] for x in repos["batches"].batches_of_entry(ids[0])}
    assert names == {"批一", "批二"}
    assert {x["name"] for x in repos["entries"].get(ids[0])["batches"]} == {
        "批一", "批二",
    }
    assert {x["name"] for x in repos["entries"].list()[0]["batches"]} == {
        "批一", "批二",
    }


# ------------------------------------------------------------------ 标签

def test_tag_add_remove_and_filter(repos, sample_xmls):
    _, ids = _make_entries(repos, sample_xmls, 3)
    changed = repos["entries"].add_tag(ids[:2], "待催办")
    assert changed == 2
    # 重复打标不改动
    assert repos["entries"].add_tag(ids[:2], "待催办") == 0
    tagged = repos["entries"].list(tags=["待催办"])
    assert len(tagged) == 2
    repos["entries"].remove_tag([ids[0]], "待催办")
    assert len(repos["entries"].list(tags="待催办")) == 1
    assert "待催办" in repos["entries"].all_tags()


def test_tag_rename_merges_and_delete_is_global(repos, sample_xmls):
    _, ids = _make_entries(repos, sample_xmls, 3)
    repos["entries"].add_tag(ids[:2], "待催办")
    repos["entries"].add_tag([ids[1]], "催办")

    assert repos["entries"].rename_tag("待催办", "催办") == 2
    assert repos["entries"].get(ids[0])["tags"] == ["催办"]
    assert repos["entries"].get(ids[1])["tags"] == ["催办"]

    assert repos["entries"].delete_tag("催办") == 2
    assert "催办" not in repos["entries"].all_tags()


# ------------------------------------------------------------------ 备注筛选

def test_has_notes_filter(repos, sample_xmls):
    p, ids = _make_entries(repos, sample_xmls, 3)
    repos["entries"].update_field(ids[0], "notes", "有备注的一条", p["id"])
    with_notes = repos["entries"].list(has_notes=True)
    without_notes = repos["entries"].list(has_notes=False)
    assert len(with_notes) == 1
    assert len(without_notes) == 2


def test_multiple_payment_screenshot_filter_and_count(repos, tmp_path):
    profile = repos["profiles"].create("张三", "李老师")
    ids = [repos["entries"].create(profile["id"]) for _ in range(3)]
    screenshots = []
    for index in range(3):
        path = tmp_path / f"payment-{index}.png"
        path.write_bytes(f"payment screenshot {index}".encode())
        screenshots.append(path)

    repos["attachments"].add(ids[0], screenshots[0], TYPE_PAYMENT)
    repos["attachments"].add(ids[1], screenshots[1], TYPE_PAYMENT)
    repos["attachments"].add(ids[1], screenshots[2], TYPE_PAYMENT)

    matches = repos["entries"].list(payment_count="multiple")
    assert [entry["id"] for entry in matches] == [ids[1]]
    assert matches[0]["attachment_types"][TYPE_PAYMENT] == 2


def test_batch_filter_on_entries(repos, sample_xmls):
    _, ids = _make_entries(repos, sample_xmls, 3)
    b = repos["batches"].create("批", entry_ids=ids[:1])
    inside = repos["entries"].list(batch_id=b["id"])
    outside = repos["entries"].list(not_in_batch_id=b["id"])
    unbatched = repos["entries"].list(unbatched=True)
    assert len(inside) == 1
    assert len(outside) == 2
    assert {entry["id"] for entry in unbatched} == set(ids[1:])
    assert repos["batches"].unbatched_count() == 2


def test_api_batch_listing_includes_unbatched_count(api):
    profile = api.create_profile("张三", "李老师")["data"]
    entry = api.create_entry(profile["id"])["data"]

    listing = api.list_batches(True)["data"]
    assert listing["batches"] == []
    assert listing["unbatched_count"] == 1

    api.create_batch("第一批", entry_ids=[entry["id"]])
    listing = api.list_batches(True)["data"]
    assert len(listing["batches"]) == 1
    assert listing["unbatched_count"] == 0

    batch_id = listing["batches"][0]["id"]
    api.archive_batch(batch_id, True)
    listing = api.list_batches(True)["data"]
    assert listing["unbatched_count"] == 0
    assert api.list_entries({"active_only": True})["data"] == []
    assert [item["id"] for item in api.list_entries({"archived_only": True})["data"]] == [entry["id"]]


def test_entry_list_uses_bounded_query_count(repos, sample_xmls):
    _make_entries(repos, sample_xmls, 3)
    statements = []
    repos["db"].conn.set_trace_callback(statements.append)
    try:
        entries = repos["entries"].list()
    finally:
        repos["db"].conn.set_trace_callback(None)

    selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(entries) == 3
    # 主查询 + 修改字段 / 附件 / 可编辑字段 / 批次归属，仍为固定查询数。
    assert len(selects) == 5


def test_amount_filter_and_sort_are_numeric(repos):
    profile = repos["profiles"].create("张三", "李老师")
    for total in ("9.00", "80.00", "100.00"):
        entry_id = repos["entries"].create(profile["id"])
        repos["db"].conn.execute("UPDATE entries SET total = ? WHERE id = ?", (total, entry_id))
    repos["db"].conn.commit()

    assert [e["total"] for e in repos["entries"].list(sort="amount")] == ["100.00", "80.00", "9.00"]
    assert [e["total"] for e in repos["entries"].list(amount_min="10", amount_max="90")] == ["80.00"]


def test_keyword_search_includes_invoice_and_paid_amount(repos):
    profile = repos["profiles"].create("张三", "李老师")
    entry_id = repos["entries"].create(profile["id"])
    repos["db"].conn.execute("UPDATE entries SET total = ? WHERE id = ?", ("1234.50", entry_id))
    repos["db"].conn.commit()
    repos["entries"].update_field(entry_id, "paid_amount", "1188.00", profile["id"])

    assert [e["id"] for e in repos["entries"].list(keyword="¥1,234.50")] == [entry_id]
    assert [e["id"] for e in repos["entries"].list(keyword="1188")] == [entry_id]


def test_modified_view_only_tracks_paid_amount_difference(repos, sample_xmls):
    profile = repos["profiles"].create("张三", "李老师")
    parsed = parse_xml(sample_xmls[0])
    entry_id = repos["entries"].create(profile["id"], title=parsed.buyer_name, parsed=parsed)
    total = repos["entries"].get(entry_id)["total"]

    repos["entries"].update_field(entry_id, "actual_item_name", "改过的物资名", profile["id"])
    repos["entries"].update_field(entry_id, "notes", "保留修改记录", profile["id"])
    assert repos["entries"].list(modified_only=True) == []
    assert repos["entries"].list()[0]["modified_fields"] == []

    different = str(Decimal(total) + Decimal("1.00"))
    repos["entries"].update_field(entry_id, "paid_amount", different, profile["id"])
    modified = repos["entries"].list(modified_only=True)
    assert [entry["id"] for entry in modified] == [entry_id]
    assert modified[0]["modified_fields"] == ["paid_amount"]

    repos["entries"].update_field(entry_id, "paid_amount", total, profile["id"])
    assert repos["entries"].get(entry_id)["fields"]["paid_amount"]["modified"] is True
    assert repos["entries"].list(modified_only=True) == []


# ------------------------------------------------------------------ 迁移

def test_v1_db_upgrades_to_latest_schema(tmp_path):
    """模拟一个只有 v1 表的旧库，打开后应补齐批次表并抬升版本号。"""
    db_path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE entries (id TEXT PRIMARY KEY, profile_id TEXT, title TEXT,
            invoice_no TEXT, invoice_date TEXT, seller TEXT, total TEXT,
            buyer_name TEXT, buyer_tax_id TEXT, category TEXT, tags TEXT,
            status TEXT, check_status TEXT, check_message TEXT, source TEXT,
            created_at TEXT, updated_at TEXT);
        INSERT INTO meta(key, value) VALUES('schema_version', '1');
        """
    )
    conn.commit()
    conn.close()

    db = Database(db_path)  # init_db 应升级
    ver = db.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert ver == str(SCHEMA_VERSION)
    # 批次表可用
    repo = BatchRepo(db)
    b = repo.create("迁移后批次")
    assert b["count"] == 0


def test_v3_db_adds_editable_value_source_column(tmp_path):
    db_path = tmp_path / "v3.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE entry_fields (
            entry_id TEXT NOT NULL,
            field TEXT NOT NULL,
            origin TEXT DEFAULT '',
            current TEXT DEFAULT '',
            modified INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (entry_id, field)
        );
        INSERT INTO meta(key, value) VALUES('schema_version', '3');
        INSERT INTO entry_fields(entry_id, field, origin, current, modified)
        VALUES('e1', 'paid_amount', '10.00', '9.00', 1);
        """
    )
    conn.commit()
    conn.close()

    db = Database(db_path)
    row = db.conn.execute(
        "SELECT current, value_source FROM entry_fields WHERE entry_id = 'e1'"
    ).fetchone()

    assert row["current"] == "9.00"
    assert row["value_source"] == ""
    assert db.conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()["value"] == str(SCHEMA_VERSION)


def test_v6_db_adds_local_recognition_cache_columns(tmp_path):
    db_path = tmp_path / "v6.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE entries (
            id TEXT PRIMARY KEY, profile_id TEXT, title TEXT, invoice_no TEXT,
            invoice_date TEXT, seller TEXT, total TEXT, buyer_name TEXT,
            buyer_tax_id TEXT, category TEXT, tags TEXT, status TEXT,
            check_status TEXT, check_message TEXT, source TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE attachments (
            id TEXT PRIMARY KEY, entry_id TEXT, type TEXT, original_name TEXT,
            stored_path TEXT, sha256 TEXT, note TEXT, added_at TEXT
        );
        INSERT INTO meta(key, value) VALUES('schema_version', '6');
        """
    )
    conn.commit()
    conn.close()

    migrated = Database(db_path)
    entry_columns = {
        row["name"] for row in migrated.conn.execute("PRAGMA table_info(entries)").fetchall()
    }
    attachment_columns = {
        row["name"] for row in migrated.conn.execute("PRAGMA table_info(attachments)").fetchall()
    }

    assert {"recognition_version", "recognition_fingerprint"} <= entry_columns
    assert {
        "recognition_version", "recognition_status", "recognized_value", "recognition_message"
    } <= attachment_columns
    assert migrated.conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()["value"] == str(SCHEMA_VERSION)


def test_v2_migration_downgrades_only_item_recognition_mismatch(tmp_path):
    db_path = tmp_path / "old.sqlite"
    db = Database(db_path)
    db.conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    db.conn.execute(
        "INSERT INTO profiles(id, name, reviewer, is_default, created_at) "
        "VALUES('p1', '张三', '李老师', 1, '')"
    )
    common = (
        "INSERT INTO entries("
        "id, profile_id, title, status, check_status, check_message, created_at, updated_at"
        ") VALUES(?, 'p1', '', 'partial', 'blocked', ?, '', '')"
    )
    db.conn.execute(
        common,
        ("item-only", "发票总额与明细合计相差 ¥10.00，请核对明细或总额。"),
    )
    db.conn.execute(
        common,
        (
            "title-conflict",
            "发票总额与明细合计相差 ¥10.00，请核对明细或总额。；"
            "发票抬头为「A」，与当前分区「B」不一致，禁止混入。",
        ),
    )
    db.conn.commit()
    db.close()

    migrated = Database(db_path)
    rows = {
        row["id"]: (row["check_status"], row["check_message"])
        for row in migrated.conn.execute(
            "SELECT id, check_status, check_message FROM entries"
        ).fetchall()
    }

    assert rows["item-only"][0] == "warning"
    assert "明细识别不完整" in rows["item-only"][1]
    assert rows["title-conflict"][0] == "blocked"


def test_v5_migration_corrects_university_tax_id_checks(tmp_path):
    db_path = tmp_path / "v5.sqlite"
    db = Database(db_path)
    db.conn.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
    db.conn.execute(
        "INSERT INTO profiles(id, name, reviewer, is_default, created_at) "
        "VALUES('p1', '张三', '李老师', 1, '')"
    )
    common = (
        "INSERT INTO entries("
        "id, profile_id, title, buyer_name, buyer_tax_id, status, check_status, "
        "check_message, created_at, updated_at"
        ") VALUES(?, 'p1', '北京理工大学', '北京理工大学', ?, 'partial', ?, ?, '', '')"
    )
    old_message = (
        "购买方税号「12100000400009127B」与「北京理工大学」不一致，"
        "应为 12100000400008888X，请核对。"
    )
    db.conn.execute(common, ("correct", "12100000400009127B", "warning", old_message))
    db.conn.execute(common, ("old-pass", "12100000400008888X", "pass", ""))
    db.conn.commit()
    db.close()

    migrated = Database(db_path)
    rows = {
        row["id"]: (row["check_status"], row["check_message"])
        for row in migrated.conn.execute(
            "SELECT id, check_status, check_message FROM entries"
        ).fetchall()
    }

    assert rows["correct"] == ("pass", "")
    assert rows["old-pass"][0] == "warning"
    assert "12100000400009127B" in rows["old-pass"][1]


# ------------------------------------------------------------------ 数据目录迁移

def test_migrate_data_root_moves_files(tmp_path, sample_xmls):
    from tidoc.db import DataRoot, Database, EntryRepo, ProfileRepo

    old = tmp_path / "old"
    root = DataRoot(old)
    db = Database(root.db_path)
    profiles, entries = ProfileRepo(db), EntryRepo(db)
    p = profiles.create("张三", "李老师")
    parsed = parse_xml(sample_xmls[0])
    entries.create(p["id"], title=parsed.buyer_name, parsed=parsed)
    db.close()

    new = tmp_path / "new"
    returned = root.migrate_to(new)
    assert str(returned) == str(new)
    assert (new / "tidoc.sqlite").exists()
    assert not (old / "tidoc.sqlite").exists()

    # 新位置数据完整
    db2 = Database(new / "tidoc.sqlite")
    assert len(ProfileRepo(db2).list()) == 1
    assert len(EntryRepo(db2).list()) == 1


def test_migrate_refuses_nonempty_target(tmp_path):
    from tidoc.db import DataRoot

    root = DataRoot(tmp_path / "old")
    target = tmp_path / "busy"
    target.mkdir()
    (target / "somefile").write_text("x")
    with pytest.raises(ValueError):
        root.migrate_to(target)

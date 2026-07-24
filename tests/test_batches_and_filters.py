"""批次仓库、标签/备注筛选维度、schema 迁移的测试。"""

import sqlite3
from decimal import Decimal

import pytest

from tidoc.db import Database
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


def test_batch_entry_note(repos, sample_xmls):
    _, ids = _make_entries(repos, sample_xmls, 1)
    b = repos["batches"].create("批次")
    repos["batches"].set_entry_note(b["id"], ids[0], "缺查验单")
    got = repos["batches"].get(b["id"])
    assert got["entry_notes"][ids[0]] == "缺查验单"
    assert got["count"] == 1  # 设备注会自动装入


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


def test_batches_of_entry(repos, sample_xmls):
    _, ids = _make_entries(repos, sample_xmls, 1)
    b1 = repos["batches"].create("批一", entry_ids=ids)
    b2 = repos["batches"].create("批二", entry_ids=ids)
    names = {x["name"] for x in repos["batches"].batches_of_entry(ids[0])}
    assert names == {"批一", "批二"}


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


def test_batch_filter_on_entries(repos, sample_xmls):
    _, ids = _make_entries(repos, sample_xmls, 3)
    b = repos["batches"].create("批", entry_ids=ids[:1])
    inside = repos["entries"].list(batch_id=b["id"])
    outside = repos["entries"].list(not_in_batch_id=b["id"])
    assert len(inside) == 1
    assert len(outside) == 2


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
    assert len(selects) == 4


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

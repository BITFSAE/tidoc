"""SQLite 表结构（设计文档第 5、6 节）。

- profiles      身份
- entries       报账条目（一张发票为核心）
- entry_fields  可改字段的 origin/current 双值 + 人工修改标记（第 6.2 节）
- field_history 字段级修改历史，不可擦除（第 6.2 节）
- items         物品明细（识别得到，默认只读）
- attachments   附件
- batches       报账批次（运营组工作单元，可命名、可留存的条目集合）
- batch_entries 批次↔条目归属（一个条目至多一个批次），带批次级催办备注

关键信息（发票号码、总额、抬头、税号）作为 entries 的列，软件内默认只读；
可改字段（实付金额、实际物资名称、备注等）走 entry_fields 以便留痕。
"""

from __future__ import annotations

import sqlite3

# v1：初版；v2：新增 batches / batch_entries（运营组批次）；
# v3：明细识别合计不一致从 blocked 降为 warning；
# v4：可改字段增加 value_source，用于区分付款 OCR 自动值与人工值；
# v5：新增 ocr_results，保存阿里云 OCR 原始响应与解析快照（防重复计费）；
# v6：校正北京理工大学购买方税号，并刷新由旧税号规则产生的提醒；
# v7：记录本地发票 / 付款截图识别规则版本与结果，避免同版重复识别。
# v8：阿里云 OCR 记录保存当次自动修正快照，并区分本机调用与绑定包导入。
# v9：云识别改为软件结果优先，并撤回历史上自动覆盖的销售方。
# v10：OCR 结果记录实际 API 调用页数，多页 PDF 不再按一张误计。
# v11：报账批次改为单一归属，历史重复归属保留最后一次装入的批次。
SCHEMA_VERSION = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS profiles (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,          -- 本人姓名（必填，写入导出）
    reviewer    TEXT NOT NULL,          -- 对应审核人（必填，写入导出）
    is_default  INTEGER NOT NULL DEFAULT 0,
    -- 以下供打印导出组件使用（可选）
    student_id  TEXT DEFAULT '',
    contact     TEXT DEFAULT '',
    bank_name   TEXT DEFAULT '',
    bank_card   TEXT DEFAULT '',
    season      TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id            TEXT PRIMARY KEY,
    profile_id    TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',   -- 抬头，强隔离字段（第 7 节）
    -- 识别得到、默认只读
    invoice_no    TEXT DEFAULT '',
    invoice_date  TEXT DEFAULT '',
    seller        TEXT DEFAULT '',
    total         TEXT DEFAULT '',            -- 价税合计，字符串存 Decimal
    buyer_name    TEXT DEFAULT '',
    buyer_tax_id  TEXT DEFAULT '',
    -- 分类 / 状态
    category      TEXT DEFAULT '',
    tags          TEXT DEFAULT '',            -- JSON 数组字符串
    status        TEXT NOT NULL DEFAULT 'draft',   -- draft/partial/complete
    check_status  TEXT NOT NULL DEFAULT 'warning', -- pass/warning/blocked
    check_message TEXT DEFAULT '',
    source        TEXT DEFAULT '',            -- 数据来源 xml/pdf/xml+pdf/manual
    recognition_version TEXT DEFAULT '',      -- 本地发票解析规则版本（缓存，不随绑定包迁移）
    recognition_fingerprint TEXT DEFAULT '',  -- 本次解析对应的原发票附件摘要
    scheme_id TEXT,
    scheme_revision_id TEXT,
    title_profile_id TEXT DEFAULT '',
    status_engine_version TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    CHECK ((scheme_id IS NULL) = (scheme_revision_id IS NULL)),
    FOREIGN KEY (scheme_id, scheme_revision_id)
        REFERENCES scheme_revision_links(scheme_id, revision_id),
    FOREIGN KEY (profile_id) REFERENCES profiles(id)
);

CREATE INDEX IF NOT EXISTS idx_entries_profile ON entries(profile_id);
CREATE INDEX IF NOT EXISTS idx_entries_title   ON entries(title);
CREATE INDEX IF NOT EXISTS idx_entries_status  ON entries(status);
CREATE INDEX IF NOT EXISTS idx_entries_invoice_no ON entries(invoice_no);

-- 可改字段的 origin/current 双值。current != origin 即永久打上人工修改标记。
CREATE TABLE IF NOT EXISTS entry_fields (
    entry_id    TEXT NOT NULL,
    field       TEXT NOT NULL,      -- paid_amount / actual_item_name / notes / ...
    origin      TEXT DEFAULT '',    -- 识别原值
    current     TEXT DEFAULT '',    -- 当前值
    modified    INTEGER NOT NULL DEFAULT 0,  -- 是否被人工改过（永久，不可擦除）
    value_source TEXT DEFAULT '',   -- payment_ocr / manual / 空（初始或历史数据）
    PRIMARY KEY (entry_id, field),
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
);

-- 字段级修改历史，不可删除，随条目一起导出（第 6.2 节）
CREATE TABLE IF NOT EXISTS field_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id    TEXT NOT NULL,
    field       TEXT NOT NULL,
    old_value   TEXT DEFAULT '',
    new_value   TEXT DEFAULT '',
    profile_id  TEXT DEFAULT '',    -- 操作身份
    changed_at  TEXT NOT NULL,
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_history_entry ON field_history(entry_id);

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id      TEXT NOT NULL,
    name          TEXT DEFAULT '',   -- 发票原始物资名称
    actual_name   TEXT DEFAULT '',
    unit          TEXT DEFAULT '',
    quantity      TEXT DEFAULT '',
    unit_price    TEXT DEFAULT '',
    total         TEXT DEFAULT '',
    spec          TEXT DEFAULT '',
    ordinal       INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_items_entry ON items(entry_id);

CREATE TABLE IF NOT EXISTS attachments (
    id            TEXT PRIMARY KEY,
    entry_id      TEXT NOT NULL,
    type          TEXT NOT NULL,     -- invoice_pdf/invoice_xml/payment_screenshot/physical_image/inspection_pdf/other
    original_name TEXT DEFAULT '',
    stored_path   TEXT DEFAULT '',   -- 相对 attachments/ 的路径
    sha256        TEXT DEFAULT '',
    note          TEXT DEFAULT '',   -- 付款截图可关联实付金额备注
    recognition_version TEXT DEFAULT '', -- 本地付款截图识别规则版本
    recognition_status TEXT DEFAULT '',  -- recognized/unrecognized/error
    recognized_value TEXT DEFAULT '',    -- 本地 OCR 识别出的单张付款金额
    recognition_message TEXT DEFAULT '', -- 未识别或失败原因
    role_id TEXT NOT NULL DEFAULT '',
    role_definition_revision_id TEXT REFERENCES scheme_revisions(revision_id),
    added_at      TEXT NOT NULL,
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_attachments_entry ON attachments(entry_id);

-- 报账批次：运营组把「这次要交的一批」自由圈选、命名、留存的集合（第 8.5、9 节）。
-- 一个批次可跨报账人、跨抬头装入任意条目；一个条目至多属于一个批次。
CREATE TABLE IF NOT EXISTS batches (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    note        TEXT DEFAULT '',            -- 批次说明
    archived    INTEGER NOT NULL DEFAULT 0, -- 归档后批次不占主列表；其条目退出在办
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    default_scheme_id TEXT,
    default_revision_id TEXT,
    CHECK ((default_scheme_id IS NULL) = (default_revision_id IS NULL)),
    FOREIGN KEY (default_scheme_id, default_revision_id)
        REFERENCES scheme_revision_links(scheme_id, revision_id)
);

-- 批次↔条目单一归属。note 是「批次级」催办备注（如「张三缺查验单」），
-- 与条目自身的记账备注（entry_fields.notes）分离，不互相污染。
CREATE TABLE IF NOT EXISTS batch_entries (
    batch_id    TEXT NOT NULL,
    entry_id    TEXT NOT NULL,
    note        TEXT DEFAULT '',
    added_at    TEXT NOT NULL,
    PRIMARY KEY (batch_id, entry_id),
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE,
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_batch_entries_batch ON batch_entries(batch_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_entries_entry ON batch_entries(entry_id);

-- 阿里云 OCR 识别结果（第 10 节）：原始响应永久落库，重复识别追加不覆盖，
-- 既是对账依据（按量计费）也避免同一发票再次计费后丢失历史。
CREATE TABLE IF NOT EXISTS ocr_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id     TEXT NOT NULL,
    provider     TEXT NOT NULL DEFAULT 'aliyun',
    file_sha256  TEXT DEFAULT '',     -- 识别时发票 PDF 的摘要，换文件后旧结果可标记过期
    file_name    TEXT DEFAULT '',
    raw_json     TEXT DEFAULT '',     -- 阿里云返回的完整 data 原文
    normalized   TEXT DEFAULT '',     -- 解析后的字段 + 明细快照 JSON
    closure_pass INTEGER NOT NULL DEFAULT 0,  -- 明细含税合计与价税合计是否闭合
    applied_at   TEXT DEFAULT '',     -- 自动补齐或人工采用发生的时间
    pending      TEXT DEFAULT '',     -- 待人工确认的差异 JSON 列表（字段名 + "items"）
    applied_changes TEXT DEFAULT '', -- 当次自动补齐 / 历史修正的前后快照 JSON
    is_local_call INTEGER NOT NULL DEFAULT 1, -- 0 表示随绑定包导入，不计入本机调用次数
    api_calls    INTEGER NOT NULL DEFAULT 1, -- 本次实际识别页数；单页发票为 1
    status       TEXT NOT NULL DEFAULT 'ok',  -- ok / failed
    error        TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ocr_results_entry ON ocr_results(entry_id);
"""


ADAPTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS adapter_packages (
    content_hash TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    package_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    source TEXT NOT NULL,
    validation_status TEXT NOT NULL DEFAULT 'verified',
    resource_path TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    diagnostics_json TEXT NOT NULL DEFAULT '[]',
    installed_at TEXT NOT NULL,
    UNIQUE(package_id, package_version)
);
CREATE TABLE IF NOT EXISTS scheme_revisions (
    revision_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL REFERENCES adapter_packages(content_hash),
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schemes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(trim(name)) > 0),
    current_revision_id TEXT NOT NULL REFERENCES scheme_revisions(revision_id),
    overrides_json TEXT NOT NULL DEFAULT '{}',
    is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)),
    disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(NOT (is_default = 1 AND disabled = 1)),
    FOREIGN KEY(id,current_revision_id) REFERENCES scheme_revision_links(scheme_id,revision_id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_schemes_default ON schemes(is_default) WHERE is_default=1;
CREATE TABLE IF NOT EXISTS scheme_revision_links (
    scheme_id TEXT NOT NULL REFERENCES schemes(id),
    revision_id TEXT NOT NULL REFERENCES scheme_revisions(revision_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(scheme_id,revision_id)
);
CREATE INDEX IF NOT EXISTS idx_revision_links_revision ON scheme_revision_links(revision_id);
CREATE TABLE IF NOT EXISTS payees (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    personnel_id TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    account_type TEXT NOT NULL DEFAULT 'personal_bank'
        CHECK(account_type IN ('personal_bank','corporate_bank','none')),
    bank_name TEXT NOT NULL DEFAULT '',
    account_number TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheme_payee_links (
    scheme_id TEXT NOT NULL REFERENCES schemes(id),
    profile_id TEXT REFERENCES profiles(id) ON DELETE CASCADE,
    payee_id TEXT NOT NULL REFERENCES payees(id),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payee_scheme_default ON scheme_payee_links(scheme_id) WHERE profile_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_payee_scheme_profile ON scheme_payee_links(scheme_id,profile_id) WHERE profile_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_scheme_payee_id ON scheme_payee_links(payee_id);
CREATE TABLE IF NOT EXISTS batch_payee_links (
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    scheme_id TEXT NOT NULL REFERENCES schemes(id),
    payee_id TEXT NOT NULL REFERENCES payees(id),
    PRIMARY KEY(batch_id,scheme_id)
);
CREATE TABLE IF NOT EXISTS export_jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','running','completed','failed','cancelled')),
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    resources_json TEXT NOT NULL DEFAULT '[]',
    options_json TEXT NOT NULL DEFAULT '{}',
    files_json TEXT NOT NULL DEFAULT '[]',
    diagnostics_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_export_jobs_date ON export_jobs(created_at);
CREATE TABLE IF NOT EXISTS export_job_revisions (
    job_id TEXT NOT NULL REFERENCES export_jobs(id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL REFERENCES scheme_revisions(revision_id),
    PRIMARY KEY(job_id,revision_id)
);
CREATE TABLE IF NOT EXISTS extension_values (
    scope TEXT NOT NULL CHECK(scope IN ('scheme','payee','entry','batch','export')),
    owner_id TEXT NOT NULL,
    scheme_id TEXT NOT NULL REFERENCES schemes(id),
    package_id TEXT NOT NULL,
    field_id TEXT NOT NULL,
    definition_revision_id TEXT NOT NULL REFERENCES scheme_revisions(revision_id),
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(scope,owner_id,scheme_id,package_id,field_id),
    FOREIGN KEY(scheme_id,definition_revision_id) REFERENCES scheme_revision_links(scheme_id,revision_id)
);
CREATE INDEX IF NOT EXISTS idx_extension_revision ON extension_values(definition_revision_id);
CREATE TABLE IF NOT EXISTS extension_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL CHECK(scope IN ('scheme','payee','entry','batch','export')),
    owner_id TEXT NOT NULL,
    scheme_id TEXT NOT NULL REFERENCES schemes(id),
    package_id TEXT NOT NULL DEFAULT '',
    field_id TEXT NOT NULL DEFAULT '',
    definition_revision_id TEXT REFERENCES scheme_revisions(revision_id),
    kind TEXT NOT NULL DEFAULT 'value' CHECK(kind IN ('value','rebind','material')),
    old_value_json TEXT NOT NULL DEFAULT 'null',
    new_value_json TEXT NOT NULL DEFAULT 'null',
    actor_id TEXT NOT NULL DEFAULT '',
    changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extension_history_owner ON extension_history(scope,owner_id,scheme_id,id);
CREATE TABLE IF NOT EXISTS entry_adapter_sources (
    entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    source_digest TEXT NOT NULL,
    revision_id TEXT NOT NULL REFERENCES scheme_revisions(revision_id),
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY(entry_id,source_digest)
);
-- Print-component validation is mutable local cache, deliberately separate from immutable packages.
CREATE TABLE IF NOT EXISTS template_validation_cache (
    content_hash TEXT NOT NULL REFERENCES adapter_packages(content_hash) ON DELETE CASCADE,
    template_path TEXT NOT NULL,
    template_hash TEXT NOT NULL,
    component_fingerprint TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('valid','invalid','pending')),
    diagnostics_json TEXT NOT NULL DEFAULT '[]',
    validated_at TEXT NOT NULL,
    PRIMARY KEY(content_hash,template_path,template_hash,component_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_template_validation_cache_status ON template_validation_cache(status);
CREATE TRIGGER IF NOT EXISTS immutable_adapter_package BEFORE UPDATE ON adapter_packages
BEGIN SELECT RAISE(ABORT,'adapter packages are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_revision BEFORE UPDATE ON scheme_revisions
BEGIN SELECT RAISE(ABORT,'scheme revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_export_snapshot BEFORE UPDATE ON export_jobs
WHEN OLD.status='completed' AND
    (NEW.snapshot_json != OLD.snapshot_json OR NEW.resources_json != OLD.resources_json OR NEW.options_json != OLD.options_json)
BEGIN SELECT RAISE(ABORT,'completed export snapshot is immutable'); END;
"""


def schema_version(conn: sqlite3.Connection) -> int | None:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
        return None
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("数据库版本无效，无法安全升级。") from exc


def _execute_schema(conn: sqlite3.Connection, sql: str) -> None:
    # executescript commits an existing transaction, defeating atomic migration.
    statement = ''
    for line in sql.splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ''
    if statement.strip():
        conn.execute(statement)


def _rebuild_bound_table(conn: sqlite3.Connection, name: str) -> None:
    start = SCHEMA.index('CREATE TABLE IF NOT EXISTS ' + name + ' (')
    end = SCHEMA.index('\n);', start) + 3
    definition = SCHEMA[start:end].replace('IF NOT EXISTS ' + name, name + '_v12')
    conn.execute(definition)
    old_columns = {r[1] for r in conn.execute('PRAGMA table_info(' + name + ')')}
    columns = [r[1] for r in conn.execute('PRAGMA table_info(' + name + '_v12)') if r[1] in old_columns]
    names = ','.join('"' + n + '"' for n in columns)
    conn.execute('INSERT INTO ' + name + '_v12 (' + names + ') SELECT ' + names + ' FROM ' + name)
    conn.execute('DROP TABLE ' + name)
    conn.execute('ALTER TABLE ' + name + '_v12 RENAME TO ' + name)


def _migrate_v12(conn: sqlite3.Connection) -> None:
    for table in ('entries','batches'):
        fks = list(conn.execute('PRAGMA foreign_key_list(' + table + ')'))
        if not any(r[2] == 'scheme_revision_links' for r in fks):
            _rebuild_bound_table(conn, table)
    columns = {r[1] for r in conn.execute('PRAGMA table_info(attachments)')}
    if 'role_id' not in columns:
        conn.execute("ALTER TABLE attachments ADD COLUMN role_id TEXT NOT NULL DEFAULT ''")
    if 'role_definition_revision_id' not in columns:
        conn.execute('ALTER TABLE attachments ADD COLUMN role_definition_revision_id TEXT REFERENCES scheme_revisions(revision_id)')
    conn.execute("UPDATE attachments SET role_id=CASE WHEN type IN ('invoice_pdf','invoice_xml') THEN 'invoice' ELSE type END WHERE role_id=''")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_entries_scheme_revision ON entries(scheme_id,scheme_revision_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_attachments_role ON attachments(entry_id,role_id)')
    # Polymorphic owners: enforce existence even for raw SQL writes and clean up
    # in the same DELETE transaction, including EntryRepo's existing paths.
    owners = {'scheme':'schemes','payee':'payees','entry':'entries','batch':'batches','export':'export_jobs'}
    for scope, table in owners.items():
        for operation in ('INSERT','UPDATE'):
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS extension_owner_{scope}_{operation.lower()}
                BEFORE {operation} ON extension_values WHEN NEW.scope='{scope}'
                AND NOT EXISTS(SELECT 1 FROM {table} WHERE id=NEW.owner_id)
                BEGIN SELECT RAISE(ABORT,'extension owner does not exist'); END""")
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS extension_cleanup_{scope}
            AFTER DELETE ON {table} BEGIN
            DELETE FROM extension_values WHERE scope='{scope}' AND owner_id=OLD.id;
            DELETE FROM extension_history WHERE scope='{scope}' AND owner_id=OLD.id;
            END""")


def init_db(conn: sqlite3.Connection, *, backup_path=None):
    """Refuse future versions; back up with SQLite, then migrate atomically.

    Returns the path of the backup written before an upgrade, or None when none was needed.
    """
    from pathlib import Path
    from datetime import datetime
    previous = schema_version(conn)
    if previous is not None and previous > SCHEMA_VERSION:
        raise ValueError(f"数据库版本 {previous} 高于当前支持的 {SCHEMA_VERSION}，拒绝写入。请更新软件。")
    existing = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='entries'").fetchone())
    previous_version = previous if previous is not None else (1 if existing else SCHEMA_VERSION)
    backup = None
    if conn.in_transaction:
        raise ValueError('数据库迁移需要没有未提交修改的连接。')
    if existing and previous_version < SCHEMA_VERSION:
        filename = conn.execute('PRAGMA database_list').fetchone()[2]
        if filename:
            target = Path(backup_path) if backup_path else Path(filename).parent / 'backups' / (
                'tidoc-before-v12-' + datetime.now().strftime('%Y%m%dT%H%M%S%f') + '.sqlite')
            target.parent.mkdir(parents=True, exist_ok=True)
            from .database import backup_connection
            backup = backup_connection(conn, target)
    foreign_keys = conn.execute('PRAGMA foreign_keys').fetchone()[0]
    conn.execute('PRAGMA foreign_keys=OFF')
    conn.execute('BEGIN IMMEDIATE')
    try:
        _execute_schema(conn, ADAPTER_SCHEMA)
        # The old v10 schema may have duplicate batch membership; create this
        # unique index only after the legacy migration has deduplicated rows.
        _execute_schema(conn, SCHEMA.replace('CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_entries_entry ON batch_entries(entry_id);',''))
        _legacy_migrations(conn, previous_version)
        _migrate_v12(conn)
        _execute_schema(conn, SCHEMA)
        if list(conn.execute('PRAGMA foreign_key_check')):
            raise ValueError('数据库存在失效关联，升级已撤回，请检查迁移前备份。')
        conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(SCHEMA_VERSION),))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute('PRAGMA foreign_keys=' + str(foreign_keys))
    return backup


def _legacy_migrations(conn: sqlite3.Connection, previous_version: int) -> None:
    if previous_version < 3:
        # 历史条目可能仅因明细漏识别而被标成 blocked。抬头分区冲突仍保持
        # blocked；这里只迁移没有分区冲突的纯明细合计问题。
        conn.execute(
            """
            UPDATE entries
               SET check_status = 'warning',
                   check_message = REPLACE(
                       REPLACE(
                           check_message,
                           '发票总额与明细合计相差',
                           '明细识别合计与发票总额相差'
                       ),
                       '请核对明细或总额。',
                       '可能是明细识别不完整，请以发票总额为准。'
                   )
             WHERE check_status = 'blocked'
               AND check_message LIKE '发票总额与明细合计相差%'
               AND check_message NOT LIKE '%与当前分区%'
            """
        )

    if previous_version < 6:
        old_tax_id = "12100000400008888X"
        current_tax_id = "12100000400009127B"
        conn.execute(
            """UPDATE entries
                  SET check_message = REPLACE(check_message, ?, ?)
                WHERE buyer_name = '北京理工大学'
                  AND check_message LIKE '%' || ? || '%'""",
            (old_tax_id, current_tax_id, old_tax_id),
        )
        rows = conn.execute(
            """SELECT id, buyer_tax_id, check_status, check_message
                 FROM entries
                WHERE buyer_name = '北京理工大学'"""
        ).fetchall()
        for entry_id, raw_tax_id, check_status, check_message in rows:
            tax_id = "".join(
                char for char in str(raw_tax_id or "").upper() if char.isalnum()
            )
            problems = [part for part in str(check_message or "").split("；") if part]
            if tax_id == current_tax_id:
                problems = [
                    part for part in problems
                    if not (
                        "购买方税号" in part
                        and "与「北京理工大学」不一致" in part
                    )
                ]
            elif tax_id == old_tax_id and not any("购买方税号" in part for part in problems):
                problems.append(
                    f"购买方税号「{raw_tax_id}」与「北京理工大学」不一致，"
                    f"应为 {current_tax_id}，请核对。"
                )
            status = check_status if check_status == "blocked" else ("warning" if problems else "pass")
            conn.execute(
                "UPDATE entries SET check_status = ?, check_message = ? WHERE id = ?",
                (status, "；".join(problems), entry_id),
            )

    if previous_version < 7:
        entry_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(entries)").fetchall()
        }
        for name in ("recognition_version", "recognition_fingerprint"):
            if name not in entry_columns:
                conn.execute(f"ALTER TABLE entries ADD COLUMN {name} TEXT DEFAULT ''")
        attachment_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(attachments)").fetchall()
        }
        for name in (
            "recognition_version",
            "recognition_status",
            "recognized_value",
            "recognition_message",
        ):
            if name not in attachment_columns:
                conn.execute(f"ALTER TABLE attachments ADD COLUMN {name} TEXT DEFAULT ''")

    if previous_version < 8:
        ocr_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(ocr_results)").fetchall()
        }
        if "applied_changes" not in ocr_columns:
            conn.execute(
                "ALTER TABLE ocr_results ADD COLUMN applied_changes TEXT DEFAULT ''"
            )
        if "is_local_call" not in ocr_columns:
            conn.execute(
                "ALTER TABLE ocr_results ADD COLUMN is_local_call INTEGER NOT NULL DEFAULT 1"
            )

    if previous_version < 9:
        # 销售方文字不再由云识别自动覆盖。只撤回当前值仍等于当次 OCR 新值的
        # 最近一次改动；用户后来亲自改过的值保持不动。
        rows = conn.execute(
            """SELECT h.entry_id, h.field, h.old_value, h.new_value, e.seller,
                      EXISTS(
                          SELECT 1 FROM ocr_results o
                           WHERE o.entry_id = h.entry_id
                             AND o.status = 'ok'
                             AND o.created_at = h.changed_at
                      ) AS from_automatic_run
                 FROM field_history h
                 JOIN entries e ON e.id = h.entry_id
                WHERE h.field IN ('[人工修正]seller', '[阿里云OCR]seller')
                ORDER BY h.id DESC"""
        ).fetchall()
        handled: set[str] = set()
        for entry_id, field, old_value, new_value, current_seller, from_automatic_run in rows:
            if entry_id in handled:
                continue
            handled.add(entry_id)
            # 最近一次销售方变动是用户操作时，不碰用户最终选择。
            if (
                field != "[阿里云OCR]seller"
                or not from_automatic_run
                or str(current_seller or "") != str(new_value or "")
            ):
                continue
            if not str(old_value or "").strip():
                # 修正前为空说明这是补齐而不是覆盖：现行策略仍允许自动补齐
                # 销售方，撤回成空只会丢掉正确值且不留任何待确认提醒。
                continue
            conn.execute(
                "UPDATE entries SET seller = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime') WHERE id = ?",
                (old_value or "", entry_id),
            )
            conn.execute(
                """INSERT INTO field_history(
                       entry_id, field, old_value, new_value, profile_id, changed_at
                   ) VALUES(?, '[阿里云OCR撤回]seller', ?, ?, '',
                            strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))""",
                (entry_id, new_value or "", old_value or ""),
            )

    if previous_version < 10:
        ocr_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(ocr_results)").fetchall()
        }
        if "api_calls" not in ocr_columns:
            conn.execute(
                "ALTER TABLE ocr_results ADD COLUMN api_calls INTEGER NOT NULL DEFAULT 1"
            )

    if previous_version < 11:
        # 旧版允许同一条目同时装入多个批次。升级后保留最后一次装入的归属；
        # added_at 相同时以最后写入的关联为准，再建立数据库级唯一约束。
        conn.execute(
            """DELETE FROM batch_entries
                 WHERE rowid IN (
                       SELECT rowid
                         FROM (
                               SELECT rowid,
                                      ROW_NUMBER() OVER (
                                          PARTITION BY entry_id
                                          ORDER BY added_at DESC, rowid DESC
                                      ) AS position
                                 FROM batch_entries
                              ) ranked
                        WHERE position > 1
                 )"""
        )
        conn.execute("DROP INDEX IF EXISTS idx_batch_entries_entry")
        conn.execute(
            "CREATE UNIQUE INDEX idx_batch_entries_entry ON batch_entries(entry_id)"
        )

    # CREATE TABLE IF NOT EXISTS 不会给历史表补列，因此按真实列结构兜底迁移。
    entry_field_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(entry_fields)").fetchall()
    }
    if "value_source" not in entry_field_columns:
        conn.execute(
            "ALTER TABLE entry_fields ADD COLUMN value_source TEXT DEFAULT ''"
        )

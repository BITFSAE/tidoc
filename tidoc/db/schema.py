"""SQLite 表结构（设计文档第 5、6 节）。

- profiles      身份
- entries       报账条目（一张发票为核心）
- entry_fields  可改字段的 origin/current 双值 + 人工修改标记（第 6.2 节）
- field_history 字段级修改历史，不可擦除（第 6.2 节）
- items         物品明细（识别得到，默认只读）
- attachments   附件
- batches       报账批次（运营组工作单元，可命名、可留存的条目集合）
- batch_entries 批次↔条目关联（多对多），带批次级催办备注

关键信息（发票号码、总额、抬头、税号）作为 entries 的列，软件内默认只读；
可改字段（实付金额、实际物资名称、备注等）走 entry_fields 以便留痕。
"""

from __future__ import annotations

import sqlite3

# v1：初版；v2：新增 batches / batch_entries（运营组批次）；
# v3：明细识别合计不一致从 blocked 降为 warning；
# v4：可改字段增加 value_source，用于区分付款 OCR 自动值与人工值；
# v5：新增 ocr_results，保存阿里云 OCR 原始响应与解析快照（防重复计费）。
SCHEMA_VERSION = 5

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
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
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
    added_at      TEXT NOT NULL,
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_attachments_entry ON attachments(entry_id);

-- 报账批次：运营组把「这次要交的一批」自由圈选、命名、留存的集合（第 8.5、9 节）。
-- 一个批次可跨报账人、跨抬头装入任意条目；一个条目也可同时属于多个批次。
CREATE TABLE IF NOT EXISTS batches (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    note        TEXT DEFAULT '',            -- 批次说明
    archived    INTEGER NOT NULL DEFAULT 0, -- 归档后批次不占主列表；只属于已归档批次的条目退出在办
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- 批次↔条目多对多。note 是「批次级」催办备注（如「张三缺查验单」），
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
CREATE INDEX IF NOT EXISTS idx_batch_entries_entry ON batch_entries(entry_id);

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
    applied_at   TEXT DEFAULT '',     -- 自动补齐 / 修复发生的时间
    pending      TEXT DEFAULT '',     -- 待人工确认的差异 JSON 列表（字段名 + "items"）
    status       TEXT NOT NULL DEFAULT 'ok',  -- ok / failed
    error        TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ocr_results_entry ON ocr_results(entry_id);
"""


def init_db(conn: sqlite3.Connection) -> None:
    """建表并写入 / 升级 schema 版本。幂等，可安全升级历史库。

    所有表用 CREATE TABLE IF NOT EXISTS，历史 v1 库缺的新表会被补齐，
    已有表与数据不动。schema_version 记录当前版本，供后续按需迁移列。
    """
    conn.executescript(SCHEMA)
    version_row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    previous_version = int(version_row[0]) if version_row else SCHEMA_VERSION
    if version_row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

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

    # CREATE TABLE IF NOT EXISTS 不会给历史表补列，因此按真实列结构兜底迁移。
    entry_field_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(entry_fields)").fetchall()
    }
    if "value_source" not in entry_field_columns:
        conn.execute(
            "ALTER TABLE entry_fields ADD COLUMN value_source TEXT DEFAULT ''"
        )

    # 历史库升级：把 schema_version 抬到当前版本（新表已由上面的 executescript 补齐）。
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'schema_version' AND CAST(value AS INTEGER) < ?",
        (str(SCHEMA_VERSION), SCHEMA_VERSION),
    )
    conn.commit()

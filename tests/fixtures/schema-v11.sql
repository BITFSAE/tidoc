-- Frozen empty Tidoc v11 database schema from commit 50de4640780e45e8a9f844ef3f1381e0d6ab00c2.
-- Contains schema and version metadata only; no invoice or personal data.
BEGIN TRANSACTION;
CREATE TABLE attachments (
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
    added_at      TEXT NOT NULL,
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
);
CREATE TABLE batch_entries (
    batch_id    TEXT NOT NULL,
    entry_id    TEXT NOT NULL,
    note        TEXT DEFAULT '',
    added_at    TEXT NOT NULL,
    PRIMARY KEY (batch_id, entry_id),
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE,
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
);
CREATE TABLE batches (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    note        TEXT DEFAULT '',            -- 批次说明
    archived    INTEGER NOT NULL DEFAULT 0, -- 归档后批次不占主列表；其条目退出在办
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE entries (
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
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles(id)
);
CREATE TABLE entry_fields (
    entry_id    TEXT NOT NULL,
    field       TEXT NOT NULL,      -- paid_amount / actual_item_name / notes / ...
    origin      TEXT DEFAULT '',    -- 识别原值
    current     TEXT DEFAULT '',    -- 当前值
    modified    INTEGER NOT NULL DEFAULT 0,  -- 是否被人工改过（永久，不可擦除）
    value_source TEXT DEFAULT '',   -- payment_ocr / manual / 空（初始或历史数据）
    PRIMARY KEY (entry_id, field),
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
);
CREATE TABLE field_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id    TEXT NOT NULL,
    field       TEXT NOT NULL,
    old_value   TEXT DEFAULT '',
    new_value   TEXT DEFAULT '',
    profile_id  TEXT DEFAULT '',    -- 操作身份
    changed_at  TEXT NOT NULL,
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
);
CREATE TABLE items (
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
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
INSERT INTO "meta" VALUES('schema_version','11');
CREATE TABLE ocr_results (
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
CREATE TABLE profiles (
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
CREATE INDEX idx_entries_profile ON entries(profile_id);
CREATE INDEX idx_entries_title   ON entries(title);
CREATE INDEX idx_entries_status  ON entries(status);
CREATE INDEX idx_entries_invoice_no ON entries(invoice_no);
CREATE INDEX idx_history_entry ON field_history(entry_id);
CREATE INDEX idx_items_entry ON items(entry_id);
CREATE INDEX idx_attachments_entry ON attachments(entry_id);
CREATE INDEX idx_batch_entries_batch ON batch_entries(batch_id);
CREATE UNIQUE INDEX idx_batch_entries_entry ON batch_entries(entry_id);
CREATE INDEX idx_ocr_results_entry ON ocr_results(entry_id);
DELETE FROM "sqlite_sequence";
COMMIT;

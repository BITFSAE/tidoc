"""报账条目（Entry）仓库。设计文档第 5、6、8 节。

职责：
- 从解析结果创建条目，写入识别字段（只读）、明细、可改字段的 origin 值。
- 可改字段更新走 update_field：current != origin 即永久打标记 + 写 field_history。
- 关键信息（发票号、总额、抬头、税号）默认只读；确需修正走 correct_locked_field 留痕。
- 列表 / 筛选 / 搜索、状态机、删除。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

from ..engine.models import ParsedInvoice
from ..engine.money import d, money
from .database import Database

# 可自由修改的字段（设计文档 8.5）
EDITABLE_FIELDS = ("paid_amount", "actual_item_name", "notes")
VALUE_SOURCE_PAYMENT_OCR = "payment_ocr"
VALUE_SOURCE_MANUAL = "manual"
# These versions describe recognition rules, not the Tidoc release. Keep them
# unchanged for ordinary app releases and bump only the affected value when its
# local recognition logic or result contract changes.
LOCAL_INVOICE_RECOGNITION_VERSION = "invoice-local-2026-09-09-2"
LOCAL_PAYMENT_RECOGNITION_VERSION = "payment-local-2026-09-06"
PAYMENT_CHECK_PREFIX = "[付款截图识别]"
# 关键信息，软件内默认只读；确需修正走特殊留痕流程（设计文档 8.5、第 6 节）
LOCKED_FIELDS = ("invoice_no", "total", "buyer_name", "buyer_tax_id", "title", "seller", "invoice_date")
# 阿里云 OCR 采用值写入关键信息时的留痕前缀（区别于人工修正）
OCR_HISTORY_LABEL = "[阿里云OCR]"
LOCAL_RECOGNITION_HISTORY_LABEL = "[本地重新识别]"
MANUAL_HISTORY_PREFIX = "[人工修正]"

STATUS_DRAFT = "draft"
STATUS_PARTIAL = "partial"
STATUS_COMPLETE = "complete"
VALID_STATUS = (STATUS_DRAFT, STATUS_PARTIAL, STATUS_COMPLETE)
QUERY_BATCH_SIZE = 900
MATERIAL_REQUIREMENTS_META_KEY = "tidoc.materialRequirements"
MATERIAL_REQUIREMENT_KEYS = (
    "invoice",
    "payment_screenshot",
    "physical_image",
    "inspection_pdf",
    "paid_amount",
)
DEFAULT_MATERIAL_REQUIREMENTS = {
    "invoice": True,
    "payment_screenshot": True,
    "physical_image": False,
    "inspection_pdf": True,
    "paid_amount": True,
}
PAID_AMOUNT_DIFF_SQL = """
TRIM(COALESCE(ef.current, '')) <> ''
AND ROUND(CAST(REPLACE(ef.current, ',', '') AS REAL) * 100)
    <> ROUND(CAST(COALESCE(NULLIF(REPLACE(e.total, ',', ''), ''), '0') AS REAL) * 100)
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()} if row else {}


def paid_amount_differs(total, paid_amount) -> bool:
    """“已修改”只表示实付金额存在且与发票总金额不一致。"""
    paid_text = str(paid_amount or "").strip()
    return bool(paid_text) and money(d(paid_text)) != money(d(total))


class EntryRepo:
    def __init__(self, db: Database):
        self.db = db
        self._material_requirements = self._read_material_requirements()

    # ------------------------------------------------------------------ 创建
    def create(
        self,
        profile_id: str,
        title: str = "",
        parsed: ParsedInvoice | None = None,
        status: str = STATUS_DRAFT,
        default_paid_to_total: bool = True,
    ) -> str:
        entry_id = uuid.uuid4().hex
        now = _now()
        p = parsed or ParsedInvoice()
        try:
            self.db.conn.execute(
                """INSERT INTO entries(id, profile_id, title, invoice_no, invoice_date,
                   seller, total, buyer_name, buyer_tax_id, status, check_status,
                   source, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (entry_id, profile_id, title or p.buyer_name, p.invoice_no, p.invoice_date,
                 p.seller, str(money(p.total)) if p.total else "", p.buyer_name,
                 p.buyer_tax_id, status, "warning", p.source, now, now),
            )
            # 可改字段初始化：origin = current；实付是否默认按发票总额由应用偏好决定。
            for field in EDITABLE_FIELDS:
                origin = self._initial_editable(field, p, default_paid_to_total)
                self.db.conn.execute(
                    "INSERT INTO entry_fields(entry_id, field, origin, current, modified) VALUES(?,?,?,?,0)",
                    (entry_id, field, origin, origin),
                )
            # 明细
            for i, item in enumerate(p.items):
                self.db.conn.execute(
                    """INSERT INTO items(entry_id, name, actual_name, unit, quantity,
                       unit_price, total, spec, ordinal) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (entry_id, item.name, item.actual_name, item.unit,
                     str(item.quantity) if item.quantity is not None else "",
                     str(money(item.unit_price)), str(money(item.total)), item.spec, i),
                )
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise
        return entry_id

    @staticmethod
    def _initial_editable(
        field: str, p: ParsedInvoice, default_paid_to_total: bool = True
    ) -> str:
        if field == "paid_amount":
            return str(money(p.total)) if p.total and default_paid_to_total else ""
        if field == "actual_item_name":
            return p.items[0].actual_name if p.items else ""
        return ""

    def material_requirements(self) -> dict[str, bool]:
        return dict(self._material_requirements)

    def _read_material_requirements(self) -> dict[str, bool]:
        row = self.db.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (MATERIAL_REQUIREMENTS_META_KEY,)
        ).fetchone()
        values = dict(DEFAULT_MATERIAL_REQUIREMENTS)
        if row:
            try:
                raw = json.loads(row["value"] or "{}")
                if isinstance(raw, dict):
                    for key in MATERIAL_REQUIREMENT_KEYS:
                        if key in raw:
                            values[key] = bool(raw[key])
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        # 发票是工作单元的根材料，不允许被设置为可选。
        values["invoice"] = True
        return values

    def set_material_requirements(self, requirements: dict | None = None) -> dict[str, bool]:
        values = dict(DEFAULT_MATERIAL_REQUIREMENTS)
        requirements = requirements if isinstance(requirements, dict) else {}
        for key in MATERIAL_REQUIREMENT_KEYS:
            if key in requirements:
                values[key] = bool(requirements[key])
        values["invoice"] = True
        self.db.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (MATERIAL_REQUIREMENTS_META_KEY, json.dumps(values, ensure_ascii=False, sort_keys=True)),
        )
        self.db.conn.commit()
        self._material_requirements = values
        return values

    # ------------------------------------------------------------------ 读取
    def get(self, entry_id: str) -> dict | None:
        row = self.db.conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if not row:
            return None
        entry = _row_to_dict(row)
        entry["tags"] = json.loads(entry.get("tags") or "[]")
        entry["fields"] = self._fields(entry_id)
        entry["items"] = self._items(entry_id)
        entry["attachments"] = self._attachments(entry_id)
        entry["batches"] = self._batches(entry_id)
        entry["history"] = self.history(entry_id)
        # 按类型汇总附件在场情况，供完整度派生（与 list 保持一致）
        types = {a["type"] for a in entry["attachments"]}
        entry["has_invoice"] = bool({"invoice_pdf", "invoice_xml"} & types)
        entry["has_payment"] = "payment_screenshot" in types
        entry["has_physical"] = "physical_image" in types
        entry["has_inspection"] = "inspection_pdf" in types
        entry["completeness"] = self._completeness(entry, entry["fields"])
        return entry

    def _fields(self, entry_id: str) -> dict:
        rows = self.db.conn.execute(
            "SELECT field, origin, current, modified, value_source "
            "FROM entry_fields WHERE entry_id = ?",
            (entry_id,),
        ).fetchall()
        return {
            r["field"]: {
                "origin": r["origin"],
                "current": r["current"],
                "modified": bool(r["modified"]),
                "value_source": r["value_source"] or "",
            }
            for r in rows
        }

    def _items(self, entry_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM items WHERE entry_id = ? ORDER BY ordinal", (entry_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def _attachments(self, entry_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM attachments WHERE entry_id = ? ORDER BY added_at", (entry_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def _batches(self, entry_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            """SELECT b.id, b.name, b.archived
                 FROM batch_entries be
                 JOIN batches b ON b.id = be.batch_id
                WHERE be.entry_id = ?
                ORDER BY b.archived ASC, b.updated_at DESC""",
            (entry_id,),
        ).fetchall()
        return [
            {"id": row["id"], "name": row["name"], "archived": bool(row["archived"])}
            for row in rows
        ]

    def history(self, entry_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM field_history WHERE entry_id = ? ORDER BY changed_at, id", (entry_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------ 列表 / 筛选
    def list(self, **filters) -> list[dict]:
        """按抬头、报账人、状态、材料数量、关键词、金额、日期和归档状态过滤（设计文档 8.7）。"""
        where, params = [], []
        if filters.get("title"):
            where.append("title = ?"); params.append(filters["title"])
        if filters.get("profile_id"):
            where.append("profile_id = ?"); params.append(filters["profile_id"])
        if filters.get("status"):
            where.append("status = ?"); params.append(filters["status"])
        if filters.get("check_status"):
            where.append("check_status = ?"); params.append(filters["check_status"])
        if filters.get("category"):
            where.append("category = ?"); params.append(filters["category"])
        if filters.get("seller"):
            where.append("e.seller LIKE ?"); params.append(f"%{filters['seller']}%")
        if filters.get("keyword"):
            keyword = str(filters["keyword"]).strip()
            kw = f"%{keyword}%"
            amount_kw = f"%{keyword.replace(',', '').replace('¥', '').replace('￥', '').strip()}%"
            # 关键词检索覆盖：发票号 / 销售方 / 购买方抬头 / 金额 / 实付 / 备注 / 物资 / 明细 / 分类
            where.append("""(
                e.invoice_no LIKE ? OR e.seller LIKE ? OR e.buyer_name LIKE ?
                OR e.category LIKE ? OR REPLACE(e.total, ',', '') LIKE ?
                OR EXISTS (SELECT 1 FROM entry_fields ef
                           WHERE ef.entry_id = e.id AND ef.field IN ('notes','actual_item_name')
                             AND ef.current LIKE ?)
                OR EXISTS (SELECT 1 FROM entry_fields ef
                           WHERE ef.entry_id = e.id AND ef.field = 'paid_amount'
                             AND REPLACE(ef.current, ',', '') LIKE ?)
                OR EXISTS (SELECT 1 FROM items it
                           WHERE it.entry_id = e.id
                             AND (it.name LIKE ? OR it.actual_name LIKE ?))
            )""")
            params += [kw, kw, kw, kw, amount_kw, kw, amount_kw, kw, kw]
        if filters.get("date_from"):
            where.append("e.invoice_date >= ?"); params.append(filters["date_from"])
        if filters.get("date_to"):
            where.append("e.invoice_date <= ?"); params.append(filters["date_to"])
        if filters.get("modified_only"):
            where.append(
                f"EXISTS (SELECT 1 FROM entry_fields ef WHERE ef.entry_id = e.id "
                f"AND ef.field = 'paid_amount' AND {PAID_AMOUNT_DIFF_SQL})"
            )
        # 备注维度：有备注 / 无备注（记账备注即 entry_fields.notes 有非空当前值）
        has_notes = filters.get("has_notes")
        if has_notes is True or has_notes == "yes":
            where.append("EXISTS (SELECT 1 FROM entry_fields ef WHERE ef.entry_id = e.id AND ef.field='notes' AND TRIM(ef.current) <> '')")
        elif has_notes is False or has_notes == "no":
            where.append("NOT EXISTS (SELECT 1 FROM entry_fields ef WHERE ef.entry_id = e.id AND ef.field='notes' AND TRIM(ef.current) <> '')")
        if filters.get("payment_count") == "multiple":
            where.append(
                "(SELECT COUNT(*) FROM attachments a "
                "WHERE a.entry_id = e.id AND a.type = 'payment_screenshot') >= 2"
            )
        # 标签维度：命中任一标签即可（tags 以 JSON 数组字符串存，用 LIKE 粗匹配带引号的标签值）
        tags = filters.get("tags")
        if isinstance(tags, str):
            tags = [tags]
        if tags:
            tag_clauses = []
            for t in tags:
                tag_clauses.append("e.tags LIKE ?")
                params.append(f'%"{t}"%')
            where.append("(" + " OR ".join(tag_clauses) + ")")
        # 批次维度：属于某批次 / 不属于某批次
        if filters.get("batch_id"):
            where.append("EXISTS (SELECT 1 FROM batch_entries be WHERE be.entry_id = e.id AND be.batch_id = ?)")
            params.append(filters["batch_id"])
        if filters.get("not_in_batch_id"):
            where.append("NOT EXISTS (SELECT 1 FROM batch_entries be WHERE be.entry_id = e.id AND be.batch_id = ?)")
            params.append(filters["not_in_batch_id"])
        if filters.get("unbatched"):
            where.append("NOT EXISTS (SELECT 1 FROM batch_entries be WHERE be.entry_id = e.id)")
        # 在办：未进批次，或至少还属于一个未归档批次。
        # 已归档：有批次归属，且全部所属批次都已归档。
        if filters.get("active_only"):
            where.append(
                """(
                    NOT EXISTS (
                        SELECT 1 FROM batch_entries be WHERE be.entry_id = e.id
                    )
                    OR EXISTS (
                        SELECT 1 FROM batch_entries be
                        JOIN batches b ON b.id = be.batch_id
                        WHERE be.entry_id = e.id AND COALESCE(b.archived, 0) = 0
                    )
                )"""
            )
        if filters.get("archived_only"):
            where.append(
                """(
                    EXISTS (
                        SELECT 1 FROM batch_entries be WHERE be.entry_id = e.id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM batch_entries be
                        JOIN batches b ON b.id = be.batch_id
                        WHERE be.entry_id = e.id AND COALESCE(b.archived, 0) = 0
                    )
                )"""
            )
        for key, operator in (("amount_min", ">="), ("amount_max", "<=")):
            value = filters.get(key)
            if value is None or value == "":
                continue
            try:
                normalized = str(money(Decimal(str(value).replace(",", "").strip())))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("金额筛选条件无效") from exc
            where.append(f"CAST(NULLIF(e.total, '') AS REAL) {operator} CAST(? AS REAL)")
            params.append(normalized)

        sql = "SELECT e.* FROM entries e"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # 排序
        sort = filters.get("sort") or "updated"
        order = {
            "updated": "e.updated_at DESC",
            "created": "e.created_at DESC",
            "amount": "CAST(NULLIF(e.total, '') AS REAL) DESC",
            "date": "e.invoice_date DESC",
            "seller": "e.seller ASC",
        }.get(sort, "e.updated_at DESC")
        sql += " ORDER BY " + order
        rows = self.db.conn.execute(sql, params).fetchall()

        result = []
        entry_ids = [r["id"] for r in rows]
        modified_by_entry = {entry_id: [] for entry_id in entry_ids}
        attachments_by_entry = {entry_id: {} for entry_id in entry_ids}
        fields_by_entry = {entry_id: {} for entry_id in entry_ids}
        batches_by_entry = {entry_id: [] for entry_id in entry_ids}
        for offset in range(0, len(entry_ids), QUERY_BATCH_SIZE):
            batch_ids = entry_ids[offset:offset + QUERY_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch_ids)
            modified_rows = self.db.conn.execute(
                f"SELECT ef.entry_id, ef.field FROM entry_fields ef "
                f"JOIN entries e ON e.id = ef.entry_id "
                f"WHERE ef.field = 'paid_amount' AND {PAID_AMOUNT_DIFF_SQL} "
                f"AND ef.entry_id IN ({placeholders})",
                batch_ids,
            ).fetchall()
            for row in modified_rows:
                modified_by_entry[row["entry_id"]].append(row["field"])

            attachment_rows = self.db.conn.execute(
                f"SELECT entry_id, type, COUNT(*) c FROM attachments "
                f"WHERE entry_id IN ({placeholders}) GROUP BY entry_id, type",
                batch_ids,
            ).fetchall()
            for row in attachment_rows:
                attachments_by_entry[row["entry_id"]][row["type"]] = row["c"]

            field_rows = self.db.conn.execute(
                f"SELECT entry_id, field, origin, current, modified, value_source FROM entry_fields "
                f"WHERE entry_id IN ({placeholders})",
                batch_ids,
            ).fetchall()
            for row in field_rows:
                fields_by_entry[row["entry_id"]][row["field"]] = {
                    "origin": row["origin"],
                    "current": row["current"],
                    "modified": bool(row["modified"]),
                    "value_source": row["value_source"] or "",
                }

            batch_rows = self.db.conn.execute(
                f"""SELECT be.entry_id, b.id, b.name, b.archived
                      FROM batch_entries be
                      JOIN batches b ON b.id = be.batch_id
                     WHERE be.entry_id IN ({placeholders})
                     ORDER BY b.archived ASC, b.updated_at DESC""",
                batch_ids,
            ).fetchall()
            for row in batch_rows:
                batches_by_entry[row["entry_id"]].append({
                    "id": row["id"],
                    "name": row["name"],
                    "archived": bool(row["archived"]),
                })

        for r in rows:
            entry = _row_to_dict(r)
            entry["tags"] = json.loads(entry.get("tags") or "[]")
            # “已修改”只表示实付金额与发票总金额不一致；字段留痕仍由 modified/history 独立保存。
            entry["modified_fields"] = modified_by_entry[entry["id"]]
            # 按类型统计附件，供 UI 显示材料状态点
            by_type = attachments_by_entry[entry["id"]]
            entry["attachment_count"] = sum(by_type.values())
            entry["attachment_types"] = by_type
            entry["has_invoice"] = bool(by_type.get("invoice_pdf") or by_type.get("invoice_xml"))
            entry["has_payment"] = bool(by_type.get("payment_screenshot"))
            entry["has_physical"] = bool(by_type.get("physical_image"))
            entry["has_inspection"] = bool(by_type.get("inspection_pdf"))
            entry["batches"] = batches_by_entry[entry["id"]]
            # 列表附上可改字段当前值（备注 / 实付金额 / 实际物资名），供卡片预览
            ef = fields_by_entry[entry["id"]]
            entry["fields"] = ef
            entry["completeness"] = self._completeness(entry, ef)
            result.append(entry)
        return result

    def _completeness(self, entry: dict, fields: dict) -> dict:
        """按设置派生完整度；发票固定必需，其余材料可分别设为必需。

        状态自动推导（不再纯手动）：
        - complete：所有设置为必需的材料齐全 + 校验未 blocked。
        - draft：什么材料都还没有。
        - partial：介于两者之间。
        返回 {ready, status, missing:[中文缺项...]}。
        """
        requirements = self.material_requirements()
        missing = []
        if requirements["invoice"] and not entry.get("has_invoice"):
            missing.append("发票")
        if requirements["payment_screenshot"] and not entry.get("has_payment"):
            missing.append("付款截图")
        if requirements["physical_image"] and not entry.get("has_physical"):
            missing.append("实物图")
        if requirements["inspection_pdf"] and not entry.get("has_inspection"):
            missing.append("查验单")
        paid = (fields.get("paid_amount") or {}).get("current") or ""
        if requirements["paid_amount"] and not str(paid).strip():
            missing.append("实付金额")
        if entry.get("check_status") == "blocked":
            missing.append("校验未通过")
        ready = not missing
        any_material = (
            entry.get("has_invoice") or entry.get("has_payment")
            or entry.get("has_physical") or entry.get("has_inspection")
        )
        status = "complete" if ready else ("partial" if any_material else "draft")
        return {"ready": ready, "status": status, "missing": missing}

    # ------------------------------------------------------------------ 可改字段更新
    def update_field(
        self,
        entry_id: str,
        field: str,
        value: str,
        profile_id: str = "",
        value_source: str | None = None,
    ) -> dict:
        """更新一个可改字段。current != origin 即永久打人工修改标记并写历史。"""
        if field not in EDITABLE_FIELDS:
            raise ValueError(f"字段「{field}」不是可自由修改的字段。")
        row = self.db.conn.execute(
            "SELECT origin, current, modified, value_source FROM entry_fields "
            "WHERE entry_id = ? AND field = ?",
            (entry_id, field),
        ).fetchone()
        if row is None:
            raise ValueError("条目或字段不存在。")
        old_value = row["current"]
        new_value = str(value) if value is not None else ""
        if new_value == old_value:
            if value_source and field == "paid_amount" and row["value_source"] != value_source:
                self.db.conn.execute(
                    "UPDATE entry_fields SET value_source = ? WHERE entry_id = ? AND field = ?",
                    (value_source, entry_id, field),
                )
                self.db.conn.commit()
            return self._fields(entry_id)
        source = ""
        if field == "paid_amount":
            source = VALUE_SOURCE_MANUAL if value_source is None else value_source
        # 一旦 current != origin 就永久标记（即使之后改回，标记不擦除）
        if source == VALUE_SOURCE_PAYMENT_OCR:
            modified = row["modified"]
        else:
            modified = 1 if new_value != row["origin"] else row["modified"]
        self.db.conn.execute(
            "UPDATE entry_fields SET current = ?, modified = ?, value_source = ? "
            "WHERE entry_id = ? AND field = ?",
            (new_value, modified, source, entry_id, field),
        )
        self._log_history(entry_id, field, old_value, new_value, profile_id)
        self._touch(entry_id)
        self.db.conn.commit()
        return self._fields(entry_id)

    def restore_paid_amount_after_last_payment(
        self, entry_id: str, removed_attachment_added_at: str = ""
    ) -> dict:
        """最后一张付款截图移除后，仅撤销仍由付款 OCR 控制的实付金额。"""
        has_payment = self.db.conn.execute(
            "SELECT 1 FROM attachments WHERE entry_id = ? "
            "AND type = 'payment_screenshot' LIMIT 1",
            (entry_id,),
        ).fetchone()
        if has_payment:
            return {"reset": False, "value": ""}
        row = self.db.conn.execute(
            """SELECT e.total, ef.current, ef.value_source
                 FROM entries e
                 JOIN entry_fields ef ON ef.entry_id = e.id AND ef.field = 'paid_amount'
                WHERE e.id = ?""",
            (entry_id,),
        ).fetchone()
        if row is None:
            return {"reset": False, "value": ""}

        source = row["value_source"] or ""
        should_reset = source == VALUE_SOURCE_PAYMENT_OCR
        if not should_reset and not source and removed_attachment_added_at:
            # v4 之前没有来源列。旧版 OCR 只会在附件写入后数秒内从
            # 空值/发票默认值自动改写；用时间与旧值条件保守识别已有自动值。
            latest = self.db.conn.execute(
                """SELECT old_value, new_value, changed_at
                     FROM field_history
                    WHERE entry_id = ? AND field = 'paid_amount'
                    ORDER BY id DESC LIMIT 1""",
                (entry_id,),
            ).fetchone()
            timing_matches = False
            if latest:
                try:
                    timing_matches = abs(
                        (
                            datetime.fromisoformat(latest["changed_at"])
                            - datetime.fromisoformat(removed_attachment_added_at)
                        ).total_seconds()
                    ) <= 5
                except (TypeError, ValueError):
                    timing_matches = latest["changed_at"] == removed_attachment_added_at
            should_reset = bool(
                latest
                and latest["new_value"] == row["current"]
                and timing_matches
                and (not latest["old_value"] or latest["old_value"] == (row["total"] or ""))
            )
        if not should_reset:
            return {"reset": False, "value": row["current"] or ""}

        old_value = row["current"] or ""
        restored = row["total"] or ""
        self.db.conn.execute(
            "UPDATE entry_fields SET current = ?, value_source = '' "
            "WHERE entry_id = ? AND field = 'paid_amount'",
            (restored, entry_id),
        )
        if restored != old_value:
            self._log_history(entry_id, "paid_amount", old_value, restored, "")
            self._touch(entry_id)
        self.db.conn.commit()
        return {"reset": restored != old_value, "value": restored}

    def correct_locked_field(self, entry_id: str, field: str, value: str, profile_id: str = "") -> dict:
        """对关键信息的『标记为人工修正』特殊流程：更新列值 + 强制留痕（第 6 节、8.5）。"""
        if field not in LOCKED_FIELDS:
            raise ValueError(f"字段「{field}」不属于关键信息修正范围。")
        row = self.db.conn.execute(f"SELECT {field} v FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise ValueError("条目不存在。")
        old_value = row["v"] or ""
        new_value = str(value) if value is not None else ""
        if new_value == old_value:
            return self.get(entry_id)
        self.db.conn.execute(f"UPDATE entries SET {field} = ? WHERE id = ?", (new_value, entry_id))
        self._log_history(entry_id, f"[人工修正]{field}", old_value, new_value, profile_id)
        self._touch(entry_id)
        self.db.conn.commit()
        return self.get(entry_id)

    def ocr_update_locked_field(self, entry_id: str, field: str, value: str) -> dict:
        """阿里云 OCR 采用值写入关键信息：更新列值 + 留痕，但不打人工修正标记。

        仅限 OCR 可参与的发票字段；抬头（title）关系到分区隔离，绝不自动改。
        """
        allowed = ("invoice_no", "invoice_date", "seller", "total", "buyer_name", "buyer_tax_id")
        if field not in allowed:
            raise ValueError(f"字段「{field}」不在阿里云 OCR 采用范围内。")
        row = self.db.conn.execute(f"SELECT {field} v FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise ValueError("条目不存在。")
        old_value = row["v"] or ""
        new_value = str(value) if value is not None else ""
        if new_value == old_value:
            return self.get(entry_id)
        self.db.conn.execute(f"UPDATE entries SET {field} = ? WHERE id = ?", (new_value, entry_id))
        self._log_history(entry_id, f"{OCR_HISTORY_LABEL}{field}", old_value, new_value, "")
        self._touch(entry_id)
        self.db.conn.commit()
        return self.get(entry_id)

    def update_recognized_buyer_tax_id(self, entry_id: str, value: str) -> bool:
        """Apply a corrected local parser result unless the user edited this field."""
        if "buyer_tax_id" in self.human_modified_locked_fields(entry_id):
            return False
        row = self.db.conn.execute(
            "SELECT buyer_tax_id FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            raise ValueError("条目不存在。")
        old_value = row["buyer_tax_id"] or ""
        new_value = str(value or "")
        if new_value == old_value:
            return False
        self.db.conn.execute(
            "UPDATE entries SET buyer_tax_id = ? WHERE id = ?", (new_value, entry_id)
        )
        self._log_history(
            entry_id,
            f"{LOCAL_RECOGNITION_HISTORY_LABEL}buyer_tax_id",
            old_value,
            new_value,
            "",
        )
        self._touch(entry_id)
        self.db.conn.commit()
        return True

    def human_modified_locked_fields(self, entry_id: str) -> set[str]:
        """被人工修正过的关键信息字段集合；OCR 不静默覆盖这些值。"""
        rows = self.db.conn.execute(
            "SELECT field FROM field_history WHERE entry_id = ? AND field LIKE ?",
            (entry_id, f"{MANUAL_HISTORY_PREFIX}%"),
        ).fetchall()
        return {row["field"][len(MANUAL_HISTORY_PREFIX):] for row in rows}

    def human_modified_locked_fields_map(self, entry_ids: list[str]) -> dict[str, set[str]]:
        """批量版 human_modified_locked_fields（OCR 徽标批量重算用）。"""
        out: dict[str, set[str]] = {entry_id: set() for entry_id in entry_ids}
        for offset in range(0, len(entry_ids), QUERY_BATCH_SIZE):
            batch = entry_ids[offset:offset + QUERY_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            rows = self.db.conn.execute(
                f"SELECT entry_id, field FROM field_history "
                f"WHERE field LIKE ? AND entry_id IN ({placeholders})",
                [f"{MANUAL_HISTORY_PREFIX}%"] + batch,
            ).fetchall()
            for row in rows:
                out[row["entry_id"]].add(row["field"][len(MANUAL_HISTORY_PREFIX):])
        return out

    def items_by_entry(self, entry_ids: list[str]) -> dict[str, list[dict]]:
        """批量取条目明细（OCR 徽标批量比对用），键为条目 id。"""
        out: dict[str, list[dict]] = {entry_id: [] for entry_id in entry_ids}
        for offset in range(0, len(entry_ids), QUERY_BATCH_SIZE):
            batch = entry_ids[offset:offset + QUERY_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            rows = self.db.conn.execute(
                f"SELECT * FROM items WHERE entry_id IN ({placeholders}) "
                f"ORDER BY entry_id, ordinal",
                batch,
            ).fetchall()
            for row in rows:
                out[row["entry_id"]].append(_row_to_dict(row))
        return out

    def set_profile(self, entry_id: str, new_profile_id: str, operator_profile_id: str = "") -> dict:
        """修改条目归属的报账人。"""
        row = self.db.conn.execute(
            """SELECT e.profile_id, old_p.name AS old_name, old_p.reviewer AS old_reviewer
                 FROM entries e
                 LEFT JOIN profiles old_p ON old_p.id = e.profile_id
                WHERE e.id = ?""",
            (entry_id,),
        ).fetchone()
        if row is None:
            raise ValueError("条目不存在。")
        prof = self.db.conn.execute(
            "SELECT name, reviewer FROM profiles WHERE id = ?", (new_profile_id,)
        ).fetchone()
        if prof is None:
            raise ValueError("报账人不存在。")
        old_profile_id = row["profile_id"] or ""
        if new_profile_id == old_profile_id:
            return self.get(entry_id)
        old_value = " → ".join([x for x in (row["old_name"], row["old_reviewer"]) if x]) or old_profile_id
        new_value = " → ".join([x for x in (prof["name"], prof["reviewer"]) if x]) or new_profile_id
        self.db.conn.execute("UPDATE entries SET profile_id = ? WHERE id = ?", (new_profile_id, entry_id))
        self._log_history(entry_id, "报账人", old_value, new_value, operator_profile_id)
        self._touch(entry_id)
        self.db.conn.commit()
        return self.get(entry_id)

    def set_profiles(
        self,
        entry_ids: list[str],
        new_profile_id: str,
        operator_profile_id: str = "",
    ) -> int:
        """批量修改条目归属，统一提交并为每个实际变化的条目保留历史。"""
        prof = self.db.conn.execute(
            "SELECT name, reviewer FROM profiles WHERE id = ?", (new_profile_id,)
        ).fetchone()
        if prof is None:
            raise ValueError("报账人不存在。")

        changed = 0
        seen: set[str] = set()
        new_value = " → ".join([x for x in (prof["name"], prof["reviewer"]) if x]) or new_profile_id
        for entry_id in (entry_ids or []):
            if not entry_id or entry_id in seen:
                continue
            seen.add(entry_id)
            row = self.db.conn.execute(
                """SELECT e.profile_id, old_p.name AS old_name, old_p.reviewer AS old_reviewer
                     FROM entries e
                     LEFT JOIN profiles old_p ON old_p.id = e.profile_id
                    WHERE e.id = ?""",
                (entry_id,),
            ).fetchone()
            if row is None or row["profile_id"] == new_profile_id:
                continue
            old_value = " → ".join(
                [x for x in (row["old_name"], row["old_reviewer"]) if x]
            ) or (row["profile_id"] or "")
            self.db.conn.execute(
                "UPDATE entries SET profile_id = ? WHERE id = ?",
                (new_profile_id, entry_id),
            )
            self._log_history(entry_id, "报账人", old_value, new_value, operator_profile_id)
            self._touch(entry_id)
            changed += 1
        self.db.conn.commit()
        return changed

    def _log_history(self, entry_id, field, old_value, new_value, profile_id) -> None:
        self.db.conn.execute(
            """INSERT INTO field_history(entry_id, field, old_value, new_value, profile_id, changed_at)
               VALUES(?,?,?,?,?,?)""",
            (entry_id, field, old_value, new_value, profile_id, _now()),
        )

    # ------------------------------------------------------------------ 元数据 / 状态
    def set_status(self, entry_id: str, status: str) -> None:
        if status not in VALID_STATUS:
            raise ValueError(f"非法状态：{status}")
        self.db.conn.execute("UPDATE entries SET status = ? WHERE id = ?", (status, entry_id))
        self._touch(entry_id)
        self.db.conn.commit()

    def recompute_status(self, entry_id: str) -> str:
        """按附件齐全 + 实付已填 + 校验通过自动推导并持久化 status。

        附件或可改字段变化后调用，使列表筛选、导航分区与卡片徽标保持一致。
        """
        entry = self.db.conn.execute(
            "SELECT check_status FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if not entry:
            return ""
        type_rows = self.db.conn.execute(
            "SELECT DISTINCT type FROM attachments WHERE entry_id = ?", (entry_id,)
        ).fetchall()
        types = {r["type"] for r in type_rows}
        stub = {
            "check_status": entry["check_status"],
            "has_invoice": bool({"invoice_pdf", "invoice_xml"} & types),
            "has_payment": "payment_screenshot" in types,
            "has_physical": "physical_image" in types,
            "has_inspection": "inspection_pdf" in types,
        }
        status = self._completeness(stub, self._fields(entry_id))["status"]
        self.db.conn.execute("UPDATE entries SET status = ? WHERE id = ?", (status, entry_id))
        self.db.conn.commit()
        return status

    def set_check(self, entry_id: str, check_status: str, message: str = "") -> None:
        payment_messages = self._payment_check_messages(entry_id)
        combined_message = "；".join(filter(None, [message, *payment_messages]))
        combined_status = check_status
        if payment_messages and check_status != "blocked":
            combined_status = "warning"
        self.db.conn.execute(
            "UPDATE entries SET check_status = ?, check_message = ? WHERE id = ?",
            (combined_status, combined_message, entry_id),
        )
        self._touch(entry_id)
        self.db.conn.commit()

    def replace_recognized_items(
        self,
        entry_id: str,
        items: list,
        source: str,
        check_status: str,
        check_message: str = "",
    ) -> None:
        """用重新识别结果原子替换明细，同时保留用户修改过的条目级字段。"""
        exists = self.db.conn.execute(
            "SELECT check_message FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if not exists:
            raise ValueError("条目不存在。")

        with self.db.conn:
            self.db.conn.execute("DELETE FROM items WHERE entry_id = ?", (entry_id,))
            for ordinal, item in enumerate(items):
                self.db.conn.execute(
                    """INSERT INTO items(entry_id, name, actual_name, unit, quantity,
                       unit_price, total, spec, ordinal) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        entry_id,
                        item.name,
                        item.actual_name,
                        item.unit,
                        str(item.quantity) if item.quantity is not None else "",
                        str(money(item.unit_price)),
                        str(money(item.total)),
                        item.spec,
                        ordinal,
                    ),
                )

            # “实际物资名称”一旦被用户修改就不覆盖；仍为识别默认值时，
            # 跟随新的第一条明细更新 origin/current。
            first_name = items[0].actual_name if items else ""
            self.db.conn.execute(
                """UPDATE entry_fields
                      SET origin = ?, current = ?
                    WHERE entry_id = ? AND field = 'actual_item_name' AND modified = 0""",
                (first_name, first_name, entry_id),
            )
            payment_messages = self._payment_messages_from_text(exists["check_message"])
            combined_message = "；".join(filter(None, [check_message, *payment_messages]))
            combined_status = (
                "warning" if payment_messages and check_status != "blocked" else check_status
            )
            self.db.conn.execute(
                """UPDATE entries
                      SET source = ?, check_status = ?, check_message = ?, updated_at = ?
                    WHERE id = ?""",
                (source, combined_status, combined_message, _now(), entry_id),
            )
        self.recompute_status(entry_id)

    @staticmethod
    def _payment_messages_from_text(message: str) -> list[str]:
        return [
            part for part in str(message or "").split("；")
            if part.startswith(PAYMENT_CHECK_PREFIX)
        ]

    def _payment_check_messages(self, entry_id: str) -> list[str]:
        row = self.db.conn.execute(
            "SELECT check_message FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return self._payment_messages_from_text(row["check_message"] if row else "")

    def invoice_recognition_fingerprint(self, entry_id: str) -> str:
        rows = self.db.conn.execute(
            """SELECT type, sha256 FROM attachments
                WHERE entry_id = ? AND type IN ('invoice_pdf', 'invoice_xml')
                ORDER BY type, sha256""",
            (entry_id,),
        ).fetchall()
        return "|".join(f"{row['type']}:{row['sha256'] or ''}" for row in rows)

    def mark_invoice_recognized(self, entry_id: str) -> None:
        self.db.conn.execute(
            """UPDATE entries SET recognition_version = ?, recognition_fingerprint = ?
                WHERE id = ?""",
            (
                LOCAL_INVOICE_RECOGNITION_VERSION,
                self.invoice_recognition_fingerprint(entry_id),
                entry_id,
            ),
        )
        self.db.conn.commit()

    def clear_invoice_recognition(self, entry_id: str) -> None:
        self.db.conn.execute(
            "UPDATE entries SET recognition_version = '', recognition_fingerprint = '' WHERE id = ?",
            (entry_id,),
        )
        self.db.conn.commit()

    def invoice_recognition_current(self, entry_id: str) -> bool:
        row = self.db.conn.execute(
            "SELECT recognition_version, recognition_fingerprint FROM entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        return bool(
            row
            and row["recognition_version"] == LOCAL_INVOICE_RECOGNITION_VERSION
            and row["recognition_fingerprint"] == self.invoice_recognition_fingerprint(entry_id)
        )

    def refresh_payment_check(self, entry_id: str) -> None:
        """把当前规则的付款截图识别结果合并进条目识别提醒。"""
        entry = self.db.conn.execute(
            "SELECT total, check_status, check_message FROM entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if not entry:
            return
        rows = self.db.conn.execute(
            """SELECT recognition_version, recognition_status, recognized_value
                 FROM attachments
                WHERE entry_id = ? AND type = 'payment_screenshot'""",
            (entry_id,),
        ).fetchall()
        base_messages = [
            part for part in str(entry["check_message"] or "").split("；")
            if part and not part.startswith(PAYMENT_CHECK_PREFIX)
        ]
        payment_messages: list[str] = []
        if rows:
            pending = sum(
                row["recognition_version"] != LOCAL_PAYMENT_RECOGNITION_VERSION
                for row in rows
            )
            failed = sum(
                row["recognition_version"] == LOCAL_PAYMENT_RECOGNITION_VERSION
                and row["recognition_status"] in {"unrecognized", "error"}
                for row in rows
            )
            if pending:
                payment_messages.append(
                    f"{PAYMENT_CHECK_PREFIX} {pending} 张付款截图尚未按当前规则识别，请重新识别。"
                )
            if failed:
                payment_messages.append(
                    f"{PAYMENT_CHECK_PREFIX} {failed} 张付款截图未识别到金额，请核对。"
                )
            if not pending and not failed:
                try:
                    recognized_total = sum(
                        (Decimal(str(row["recognized_value"] or "0")) for row in rows),
                        Decimal("0"),
                    )
                    invoice_total = Decimal(str(entry["total"] or "0"))
                    if money(recognized_total) != money(invoice_total):
                        payment_messages.append(
                            f"{PAYMENT_CHECK_PREFIX} 付款截图识别合计 {money(recognized_total)} "
                            f"与发票总额 {money(invoice_total)} 不一致，请核对。"
                        )
                except (InvalidOperation, ValueError):
                    payment_messages.append(
                        f"{PAYMENT_CHECK_PREFIX} 付款截图金额无法汇总，请核对。"
                    )
        message = "；".join([*base_messages, *payment_messages])
        if entry["check_status"] == "blocked":
            status = "blocked"
        else:
            status = "warning" if message else "pass"
        self.db.conn.execute(
            "UPDATE entries SET check_status = ?, check_message = ? WHERE id = ?",
            (status, message, entry_id),
        )
        self.db.conn.commit()
        self.recompute_status(entry_id)

    def clear_payment_check(self, entry_id: str) -> None:
        """材料集合无法完整识别时移除旧的付款金额结论，保留发票校验结果。"""
        row = self.db.conn.execute(
            "SELECT check_status, check_message FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if not row:
            return
        base_messages = [
            part for part in str(row["check_message"] or "").split("；")
            if part and not part.startswith(PAYMENT_CHECK_PREFIX)
        ]
        if row["check_status"] == "blocked":
            status = "blocked"
        else:
            status = "warning" if base_messages else "pass"
        self.db.conn.execute(
            "UPDATE entries SET check_status = ?, check_message = ? WHERE id = ?",
            (status, "；".join(base_messages), entry_id),
        )
        self.db.conn.commit()
        self.recompute_status(entry_id)

    def set_meta(self, entry_id: str, category: str | None = None, tags: list | None = None) -> None:
        if category is not None:
            self.db.conn.execute("UPDATE entries SET category = ? WHERE id = ?", (category, entry_id))
        if tags is not None:
            self.db.conn.execute(
                "UPDATE entries SET tags = ? WHERE id = ?", (json.dumps(tags, ensure_ascii=False), entry_id)
            )
        self._touch(entry_id)
        self.db.conn.commit()

    def add_tag(self, entry_ids: list[str], tag: str) -> int:
        """给一批条目追加同一个标签（已有则跳过）。返回实际改动条数。"""
        tag = (tag or "").strip()
        if not tag:
            raise ValueError("标签不能为空。")
        changed = 0
        for eid in (entry_ids or []):
            row = self.db.conn.execute("SELECT tags FROM entries WHERE id = ?", (eid,)).fetchone()
            if not row:
                continue
            tags = json.loads(row["tags"] or "[]")
            if tag in tags:
                continue
            tags.append(tag)
            self.db.conn.execute(
                "UPDATE entries SET tags = ? WHERE id = ?", (json.dumps(tags, ensure_ascii=False), eid)
            )
            self._touch(eid)
            changed += 1
        self.db.conn.commit()
        return changed

    def remove_tag(self, entry_ids: list[str], tag: str) -> int:
        """从一批条目移除某标签。返回实际改动条数。"""
        tag = (tag or "").strip()
        changed = 0
        for eid in (entry_ids or []):
            row = self.db.conn.execute("SELECT tags FROM entries WHERE id = ?", (eid,)).fetchone()
            if not row:
                continue
            tags = json.loads(row["tags"] or "[]")
            if tag not in tags:
                continue
            tags = [t for t in tags if t != tag]
            self.db.conn.execute(
                "UPDATE entries SET tags = ? WHERE id = ?", (json.dumps(tags, ensure_ascii=False), eid)
            )
            self._touch(eid)
            changed += 1
        self.db.conn.commit()
        return changed

    def rename_tag(self, old_tag: str, new_tag: str) -> int:
        """全库重命名标签；若目标标签已存在则合并，返回受影响条目数。"""
        old_tag = (old_tag or "").strip()
        new_tag = (new_tag or "").strip()
        if not old_tag or not new_tag:
            raise ValueError("标签名称不能为空。")
        if old_tag == new_tag:
            return 0
        changed = 0
        rows = self.db.conn.execute("SELECT id, tags FROM entries WHERE tags <> '' AND tags <> '[]'").fetchall()
        for row in rows:
            tags = json.loads(row["tags"] or "[]")
            if old_tag not in tags:
                continue
            replaced = [new_tag if tag == old_tag else tag for tag in tags]
            # 保持原顺序，同时处理重命名后与已有标签重复的情况。
            replaced = list(dict.fromkeys(replaced))
            self.db.conn.execute(
                "UPDATE entries SET tags = ? WHERE id = ?",
                (json.dumps(replaced, ensure_ascii=False), row["id"]),
            )
            self._touch(row["id"])
            changed += 1
        self.db.conn.commit()
        return changed

    def delete_tag(self, tag: str) -> int:
        """从全库所有条目删除标签，返回受影响条目数。"""
        tag = (tag or "").strip()
        if not tag:
            raise ValueError("标签名称不能为空。")
        rows = self.db.conn.execute("SELECT id FROM entries").fetchall()
        return self.remove_tag([row["id"] for row in rows], tag)

    def all_tags(self) -> list[str]:
        """当前库里用过的全部标签（去重、按字母序），供筛选下拉与自动补全。"""
        rows = self.db.conn.execute("SELECT tags FROM entries WHERE tags <> '' AND tags <> '[]'").fetchall()
        seen: set[str] = set()
        for r in rows:
            for t in json.loads(r["tags"] or "[]"):
                if t:
                    seen.add(t)
        return sorted(seen)

    def all_titles(self) -> list[str]:
        """当前库实际使用的全部抬头，供主工具条生成可用筛选项。"""
        rows = self.db.conn.execute(
            """SELECT DISTINCT TRIM(title) AS title
                 FROM entries
                WHERE TRIM(COALESCE(title, '')) <> ''
                ORDER BY title"""
        ).fetchall()
        return [row["title"] for row in rows]

    def _touch(self, entry_id: str) -> None:
        self.db.conn.execute("UPDATE entries SET updated_at = ? WHERE id = ?", (_now(), entry_id))

    # ------------------------------------------------------------------ 明细行 CRUD
    def add_item(self, entry_id: str, name: str = "", actual_name: str = "",
                 unit: str = "", quantity: str = "", unit_price: str = "",
                 total: str = "", spec: str = "") -> dict:
        """在条目末尾追加一条明细行，返回新行 dict。"""
        max_ord = self.db.conn.execute(
            "SELECT COALESCE(MAX(ordinal), -1) FROM items WHERE entry_id = ?", (entry_id,)
        ).fetchone()[0]
        self.db.conn.execute(
            """INSERT INTO items(entry_id, name, actual_name, unit, quantity,
               unit_price, total, spec, ordinal) VALUES(?,?,?,?,?,?,?,?,?)""",
            (entry_id, name, actual_name, unit, quantity, unit_price, total, spec, max_ord + 1),
        )
        self._touch(entry_id)
        self.db.conn.commit()
        return self._items(entry_id)[-1]

    def update_item(self, item_id: int, fields: dict) -> dict:
        """更新一条明细行的指定字段，返回更新后的行 dict。"""
        allowed = ("name", "actual_name", "unit", "quantity", "unit_price", "total", "spec")
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                vals.append(str(v) if v is not None else "")
        if not sets:
            raise ValueError("没有可更新的字段。")
        row = self.db.conn.execute("SELECT entry_id FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise ValueError("明细行不存在。")
        entry_id = row["entry_id"]
        vals.append(item_id)
        self.db.conn.execute(f"UPDATE items SET {', '.join(sets)} WHERE id = ?", vals)
        self._touch(entry_id)
        self.db.conn.commit()
        updated = self.db.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_dict(updated)

    def delete_item(self, item_id: int) -> str:
        """删除一条明细行，返回所属 entry_id。"""
        row = self.db.conn.execute("SELECT entry_id FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise ValueError("明细行不存在。")
        entry_id = row["entry_id"]
        self.db.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        self._touch(entry_id)
        self.db.conn.commit()
        return entry_id

    # ------------------------------------------------------------------ 删除
    def delete(self, entry_id: str) -> None:
        self.db.conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        self.db.conn.commit()

    def delete_many(self, entry_ids: list[str]) -> int:
        if not entry_ids:
            return 0
        placeholders = ",".join("?" * len(entry_ids))
        cur = self.db.conn.execute(f"DELETE FROM entries WHERE id IN ({placeholders})", entry_ids)
        self.db.conn.commit()
        return cur.rowcount

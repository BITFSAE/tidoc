"""报账批次（Batch）仓库。设计文档第 8.5、9 节 —— 运营组的核心工作单元。

批次是「一次要交的这批材料」的可命名、可留存集合：
- 跨报账人、跨抬头自由圈选任意条目；一个条目也可同时属于多个批次。
- 每个条目在批次内可带「批次级催办备注」（如「张三缺查验单」），与条目自身
  的记账备注（entry_fields.notes）分离，不互相污染。
- 批次可归档（已交后收档），不再占用主界面的活跃列表。
- 只属于已归档批次的条目退出「在办」，可从「已收档」查看或恢复。

批次自身不持有材料，只引用条目 id；删批次不动条目，删条目由外键级联清理关联。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from .database import Database


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()} if row else {}


class BatchRepo:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ 创建 / 改名 / 删除
    def create(self, name: str, note: str = "", entry_ids: list[str] | None = None) -> dict:
        if not name.strip():
            raise ValueError("批次名称不能为空。")
        batch_id = uuid.uuid4().hex
        now = _now()
        self.db.conn.execute(
            "INSERT INTO batches(id, name, note, archived, created_at, updated_at) VALUES(?,?,?,0,?,?)",
            (batch_id, name.strip(), note or "", now, now),
        )
        for eid in (entry_ids or []):
            self._link(batch_id, eid, now)
        self.db.conn.commit()
        return self.get(batch_id)

    def update(self, batch_id: str, **fields) -> dict:
        allowed = {"name", "note", "archived"}
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "name":
                value = str(value or "").strip()
                if not value:
                    raise ValueError("批次名称不能为空。")
            elif key == "archived":
                value = int(bool(value))
            else:
                value = str(value) if value is not None else ""
            sets.append(f"{key} = ?")
            params.append(value)
        if sets:
            sets.append("updated_at = ?")
            params.append(_now())
            params.append(batch_id)
            self.db.conn.execute(f"UPDATE batches SET {', '.join(sets)} WHERE id = ?", params)
            self.db.conn.commit()
        return self.get(batch_id)

    def set_archived(self, batch_id: str, archived: bool = True) -> dict:
        return self.update(batch_id, archived=archived)

    def delete(self, batch_id: str) -> None:
        # batch_entries 由外键 ON DELETE CASCADE 清理（数据库已开 foreign_keys=ON）。
        self.db.conn.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
        self.db.conn.commit()

    # ------------------------------------------------------------------ 装入 / 移出条目
    def add_entries(self, batch_id: str, entry_ids: list[str]) -> int:
        if not self._exists(batch_id):
            raise ValueError("批次不存在。")
        now = _now()
        added = 0
        for eid in (entry_ids or []):
            added += self._link(batch_id, eid, now)
        self._touch(batch_id)
        self.db.conn.commit()
        return added

    def remove_entries(self, batch_id: str, entry_ids: list[str]) -> int:
        if not entry_ids:
            return 0
        placeholders = ",".join("?" * len(entry_ids))
        cur = self.db.conn.execute(
            f"DELETE FROM batch_entries WHERE batch_id = ? AND entry_id IN ({placeholders})",
            [batch_id, *entry_ids],
        )
        self._touch(batch_id)
        self.db.conn.commit()
        return cur.rowcount

    def move_entries(self, source_batch_id: str, target_batch_id: str, entry_ids: list[str]) -> dict:
        """把条目从一个批次原子移动到另一批次，避免加入成功但移出失败。"""
        if source_batch_id == target_batch_id:
            return {"added": 0, "removed": 0}
        if not self._exists(source_batch_id) or not self._exists(target_batch_id):
            raise ValueError("批次不存在。")
        ids = list(dict.fromkeys(entry_ids or []))
        if not ids:
            return {"added": 0, "removed": 0}
        now = _now()
        try:
            added = sum(self._link(target_batch_id, entry_id, now) for entry_id in ids)
            placeholders = ",".join("?" * len(ids))
            cur = self.db.conn.execute(
                f"DELETE FROM batch_entries WHERE batch_id = ? AND entry_id IN ({placeholders})",
                [source_batch_id, *ids],
            )
            self._touch(source_batch_id)
            self._touch(target_batch_id)
            self.db.conn.commit()
            return {"added": added, "removed": cur.rowcount}
        except Exception:
            self.db.conn.rollback()
            raise

    def set_entry_batch(self, entry_id: str, target_batch_id: str | None = None) -> dict:
        """把单条条目改为一个批次，或明确设为不进任何批次。"""
        if not self.db.conn.execute(
            "SELECT 1 FROM entries WHERE id = ?", (entry_id,)
        ).fetchone():
            raise ValueError("条目不存在。")
        if target_batch_id and not self._exists(target_batch_id):
            raise ValueError("批次不存在。")
        old_rows = self.db.conn.execute(
            "SELECT batch_id FROM batch_entries WHERE entry_id = ?", (entry_id,)
        ).fetchall()
        old_ids = [row["batch_id"] for row in old_rows]
        self.db.conn.execute("DELETE FROM batch_entries WHERE entry_id = ?", (entry_id,))
        now = _now()
        added = 0
        if target_batch_id:
            added = self._link(target_batch_id, entry_id, now)
        for batch_id in set(old_ids) | ({target_batch_id} if target_batch_id else set()):
            self._touch(batch_id)
        self.db.conn.commit()
        return {"added": added, "removed": len(old_ids), "batch_id": target_batch_id or ""}

    def set_entry_note(self, batch_id: str, entry_id: str, note: str) -> dict:
        """设置某条目在该批次内的催办备注。条目若不在批次内则先装入。"""
        row = self.db.conn.execute(
            "SELECT 1 FROM batch_entries WHERE batch_id = ? AND entry_id = ?",
            (batch_id, entry_id),
        ).fetchone()
        if row is None:
            self._link(batch_id, entry_id, _now())
        self.db.conn.execute(
            "UPDATE batch_entries SET note = ? WHERE batch_id = ? AND entry_id = ?",
            (note or "", batch_id, entry_id),
        )
        self._touch(batch_id)
        self.db.conn.commit()
        return self.get(batch_id)

    def _link(self, batch_id: str, entry_id: str, now: str) -> int:
        cur = self.db.conn.execute(
            "INSERT OR IGNORE INTO batch_entries(batch_id, entry_id, note, added_at) VALUES(?,?,'',?)",
            (batch_id, entry_id, now),
        )
        return cur.rowcount

    # ------------------------------------------------------------------ 读取
    def get(self, batch_id: str) -> dict | None:
        row = self.db.conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if not row:
            return None
        batch = _row_to_dict(row)
        batch["archived"] = bool(batch.get("archived"))
        batch["entry_ids"] = self.entry_ids(batch_id)
        batch["entry_notes"] = self._entry_notes(batch_id)
        batch["count"] = len(batch["entry_ids"])
        batch["stats"] = self._stats(batch_id)
        return batch

    def list(self, include_archived: bool = False) -> list[dict]:
        """列出批次，附带条数与每报账人小计。默认不含已归档。"""
        sql = "SELECT * FROM batches"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY archived ASC, updated_at DESC"
        rows = self.db.conn.execute(sql).fetchall()
        result = []
        for r in rows:
            batch = _row_to_dict(r)
            batch["archived"] = bool(batch.get("archived"))
            batch["stats"] = self._stats(batch["id"])
            batch["count"] = batch["stats"]["count"]
            result.append(batch)
        return result

    def unbatched_count(self) -> int:
        """统计尚未归入任何批次的条目，供批次栏的快捷入口显示。"""
        row = self.db.conn.execute(
            """SELECT COUNT(*) AS count
                 FROM entries e
                WHERE NOT EXISTS (
                    SELECT 1 FROM batch_entries be WHERE be.entry_id = e.id
                )"""
        ).fetchone()
        return int(row["count"] or 0)

    def entry_ids(self, batch_id: str) -> list[str]:
        rows = self.db.conn.execute(
            "SELECT entry_id FROM batch_entries WHERE batch_id = ? ORDER BY added_at, entry_id",
            (batch_id,),
        ).fetchall()
        return [r["entry_id"] for r in rows]

    def batches_of_entry(self, entry_id: str) -> list[dict]:
        """某条目所属的批次（用于条目详情/卡片展示归属）。"""
        rows = self.db.conn.execute(
            """SELECT b.id, b.name, b.archived FROM batches b
               JOIN batch_entries be ON be.batch_id = b.id
               WHERE be.entry_id = ? ORDER BY b.updated_at DESC""",
            (entry_id,),
        ).fetchall()
        return [{"id": r["id"], "name": r["name"], "archived": bool(r["archived"])} for r in rows]

    def _entry_notes(self, batch_id: str) -> dict:
        rows = self.db.conn.execute(
            "SELECT entry_id, note FROM batch_entries WHERE batch_id = ?",
            (batch_id,),
        ).fetchall()
        return {r["entry_id"]: r["note"] for r in rows if r["note"]}

    def _stats(self, batch_id: str) -> dict:
        """批次的汇总统计：条数、合计金额、每报账人小计、缺件条数。

        供批次面板一眼看清「这批装了谁、多少钱、还有几条没齐」。
        """
        rows = self.db.conn.execute(
            """SELECT e.id, e.profile_id, e.title, e.total, e.status,
                      p.name AS profile_name
               FROM batch_entries be
               JOIN entries e ON e.id = be.entry_id
               LEFT JOIN profiles p ON p.id = e.profile_id
               WHERE be.batch_id = ?""",
            (batch_id,),
        ).fetchall()
        from decimal import Decimal

        total = Decimal("0")
        by_person: dict[str, dict] = {}
        by_title: dict[str, int] = {}
        incomplete = 0
        for r in rows:
            try:
                amt = Decimal(r["total"] or "0")
            except Exception:
                amt = Decimal("0")
            total += amt
            pname = r["profile_name"] or "未知报账人"
            slot = by_person.setdefault(pname, {"count": 0, "total": Decimal("0"), "incomplete": 0})
            slot["count"] += 1
            slot["total"] += amt
            if r["status"] != "complete":
                slot["incomplete"] += 1
                incomplete += 1
            title = r["title"] or "未标注抬头"
            by_title[title] = by_title.get(title, 0) + 1
        return {
            "count": len(rows),
            "total": str(total),
            "incomplete": incomplete,
            "by_person": [
                {"name": k, "count": v["count"], "total": str(v["total"]), "incomplete": v["incomplete"]}
                for k, v in sorted(by_person.items(), key=lambda kv: -kv[1]["count"])
            ],
            "by_title": by_title,
        }

    # ------------------------------------------------------------------ 辅助
    def _exists(self, batch_id: str) -> bool:
        return self.db.conn.execute(
            "SELECT 1 FROM batches WHERE id = ?", (batch_id,)
        ).fetchone() is not None

    def _touch(self, batch_id: str) -> None:
        self.db.conn.execute(
            "UPDATE batches SET updated_at = ? WHERE id = ?", (_now(), batch_id)
        )

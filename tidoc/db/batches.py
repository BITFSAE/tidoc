"""报账批次（Batch）仓库。设计文档第 8.5、9 节 —— 运营组的核心工作单元。

批次是「一次要交的这批材料」的可命名、可留存集合：
- 跨报账人、跨抬头自由圈选任意条目；一个条目至多属于一个批次。
- 每个条目在批次内可带「批次级催办备注」（如「张三缺查验单」），与条目自身
  的记账备注（entry_fields.notes）分离，不互相污染。
- 批次可归档（已提交后归档），不再占用主界面的活跃列表。
- 已归档批次的条目退出「在办」，可从「已归档」查看或恢复。

批次自身不持有材料，只引用条目 id；删批次不动条目，删条目由外键级联清理关联。
"""

from __future__ import annotations

import uuid
import json
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
    def create(self, name: str, note: str = "", entry_ids: list[str] | None = None,
               *, default_scheme_id: str | None = None,
               default_revision_id: str | None = None) -> dict:
        if not name.strip():
            raise ValueError("批次名称不能为空。")
        batch_id = uuid.uuid4().hex
        now = _now()
        if default_scheme_id and not default_revision_id:
            from .adapters import AdapterRepo
            default_revision_id = AdapterRepo(self.db).get_scheme(default_scheme_id)["current_revision_id"]
        if default_revision_id and not default_scheme_id:
            raise ValueError("批次默认方案和修订必须成对指定。")
        if not default_scheme_id and getattr(self.db, "adapter_service", None):
            default_scheme_id, default_revision_id = self.db.adapter_service.default_binding()
        self.db.conn.execute(
            "INSERT INTO batches(id, name, note, archived, created_at, updated_at,default_scheme_id,default_revision_id) VALUES(?,?,?,0,?,?,?,?)",
            (batch_id, name.strip(), note or "", now, now, default_scheme_id, default_revision_id),
        )
        try:
            self._set_entries_batch(entry_ids or [], batch_id, now)
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise
        return self.get(batch_id)

    def set_default_binding(self, batch_id: str, scheme_id: str, revision_id: str | None = None) -> dict:
        if not self._exists(batch_id):
            raise ValueError("批次不存在。")
        from .adapters import AdapterRepo
        revision_id = revision_id or AdapterRepo(self.db).get_scheme(scheme_id)["current_revision_id"]
        with self.db.transaction():
            self.db.conn.execute("UPDATE batches SET default_scheme_id=?,default_revision_id=?,updated_at=? WHERE id=?",
                                 (scheme_id, revision_id, _now(), batch_id))
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

    def delete(self, batch_id: str, *, commit: bool = True) -> None:
        # batch_entries 由外键 ON DELETE CASCADE 清理（数据库已开 foreign_keys=ON）。
        self.db.conn.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
        self.db.conn.execute("DELETE FROM meta WHERE key=?",(self._output_settings_key(batch_id),))
        if commit:
            self.db.conn.commit()

    # ------------------------------------------------------------------ 归入 / 移出条目
    def add_entries(self, batch_id: str, entry_ids: list[str]) -> int:
        """把条目归入目标批次；已有归属时直接移动。"""
        if not self._exists(batch_id):
            raise ValueError("批次不存在。")
        try:
            changed = self._set_entries_batch(entry_ids or [], batch_id, _now())
            self.db.conn.commit()
            return changed
        except Exception:
            self.db.conn.rollback()
            raise

    def remove_entries(self, batch_id: str, entry_ids: list[str]) -> int:
        if not entry_ids:
            return 0
        if not self._exists(batch_id):
            raise ValueError("批次不存在。")
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
        placeholders = ",".join("?" * len(ids))
        source_ids = [
            row["entry_id"]
            for row in self.db.conn.execute(
                f"SELECT entry_id FROM batch_entries WHERE batch_id = ? AND entry_id IN ({placeholders})",
                [source_batch_id, *ids],
            ).fetchall()
        ]
        if not source_ids:
            return {"added": 0, "removed": 0}
        try:
            moved = self._set_entries_batch(source_ids, target_batch_id, _now())
            self.db.conn.commit()
            return {"added": moved, "removed": moved}
        except Exception:
            self.db.conn.rollback()
            raise

    def set_entries_batch(
        self, entry_ids: list[str], target_batch_id: str | None = None
    ) -> dict:
        """批量设置唯一批次归属；target_batch_id 为空时移到“未进批次”。"""
        if target_batch_id and not self._exists(target_batch_id):
            raise ValueError("批次不存在。")
        try:
            changed = self._set_entries_batch(entry_ids or [], target_batch_id, _now())
            self.db.conn.commit()
            return {"changed": changed, "batch_id": target_batch_id or ""}
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
        old_row = self.db.conn.execute(
            "SELECT batch_id FROM batch_entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        old_batch_id = old_row["batch_id"] if old_row else ""
        try:
            changed = self._set_entries_batch([entry_id], target_batch_id, _now())
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise
        return {
            "added": int(bool(changed and target_batch_id)),
            "removed": int(bool(changed and old_batch_id)),
            "batch_id": target_batch_id or "",
        }

    def set_entry_note(self, batch_id: str, entry_id: str, note: str) -> dict:
        """设置某条目在该批次内的催办备注。条目若不在批次内则先移入。"""
        if not self._exists(batch_id):
            raise ValueError("批次不存在。")
        row = self.db.conn.execute(
            "SELECT 1 FROM batch_entries WHERE batch_id = ? AND entry_id = ?",
            (batch_id, entry_id),
        ).fetchone()
        try:
            if row is None:
                self._set_entries_batch([entry_id], batch_id, _now())
            self.db.conn.execute(
                "UPDATE batch_entries SET note = ? WHERE batch_id = ? AND entry_id = ?",
                (note or "", batch_id, entry_id),
            )
            self._touch(batch_id)
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise
        return self.get(batch_id)

    def _link(self, batch_id: str, entry_id: str, now: str) -> int:
        cur = self.db.conn.execute(
            "INSERT OR IGNORE INTO batch_entries(batch_id, entry_id, note, added_at) VALUES(?,?,'',?)",
            (batch_id, entry_id, now),
        )
        return cur.rowcount

    def _set_entries_batch(
        self, entry_ids: list[str], target_batch_id: str | None, now: str
    ) -> int:
        """在当前事务中设置唯一归属，并刷新受影响批次的更新时间。"""
        changed = 0
        touched: set[str] = set()
        for entry_id in list(dict.fromkeys(entry_ids or [])):
            if not self.db.conn.execute(
                "SELECT 1 FROM entries WHERE id = ?", (entry_id,)
            ).fetchone():
                raise ValueError("条目不存在。")
            old_row = self.db.conn.execute(
                "SELECT batch_id FROM batch_entries WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            old_batch_id = old_row["batch_id"] if old_row else ""
            if old_batch_id == (target_batch_id or ""):
                continue
            if old_batch_id:
                self.db.conn.execute(
                    "DELETE FROM batch_entries WHERE entry_id = ?", (entry_id,)
                )
                touched.add(old_batch_id)
            if target_batch_id:
                self._link(target_batch_id, entry_id, now)
                touched.add(target_batch_id)
            changed += 1
        for batch_id in touched:
            self._touch(batch_id)
        return changed

    # ------------------------------------------------------------------ 读取
    def get(self, batch_id: str) -> dict | None:
        row = self.db.conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if not row:
            return None
        batch = _row_to_dict(row)
        batch["archived"] = bool(batch.get("archived"))
        batch["output_settings"] = self._output_settings(batch_id)
        batch["payee_mappings"] = {
            item['scheme_id']:{key:item[key] for key in ('id','name','personnel_id','contact','account_type','bank_name','account_number')}
            for item in self.db.conn.execute('''SELECT l.scheme_id,p.* FROM batch_payee_links l
                JOIN payees p ON p.id=l.payee_id WHERE l.batch_id=? ORDER BY l.scheme_id''',(batch_id,)).fetchall()
        }
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
            batch["output_settings"] = self._output_settings(batch["id"])
            batch["stats"] = self._stats(batch["id"])
            batch["count"] = batch["stats"]["count"]
            result.append(batch)
        return result

    @staticmethod
    def _output_settings_key(batch_id: str) -> str:
        return "tidoc.batch_output_settings." + batch_id

    def _output_settings(self, batch_id: str) -> dict:
        row=self.db.conn.execute("SELECT value FROM meta WHERE key=?",(self._output_settings_key(batch_id),)).fetchone()
        if not row: return {}
        try:
            value=json.loads(row[0])
        except (TypeError,ValueError):
            raise ValueError("批次输出设置存储数据无效。")
        if not isinstance(value,dict): raise ValueError("批次输出设置存储数据无效。")
        return value

    def set_output_settings(self, batch_id: str, settings: dict, *, expected_updated_at: str | None = None) -> dict:
        """Persist batch-scoped registered output-setting overrides atomically."""
        from ..adapters.registry import SETTINGS
        from ..adapters.resolver import _valid_setting
        if not self._exists(batch_id): raise ValueError("批次不存在。")
        if not isinstance(settings,dict): raise ValueError("批次输出设置必须是对象。")
        allowed={key for key,spec in SETTINGS.items() if 'batch' in spec.get('scopes',[])}
        unknown=set(settings)-allowed
        if unknown: raise ValueError("设置不适用于批次："+', '.join(sorted(unknown)))
        for key,value in settings.items():
            if not _valid_setting(key,value): raise ValueError("设置值无效："+key)
        with self.db.transaction():
            row=self.db.conn.execute("SELECT updated_at FROM batches WHERE id=?",(batch_id,)).fetchone()
            if not row: raise ValueError("批次不存在。")
            if expected_updated_at is not None and row['updated_at']!=expected_updated_at:
                raise ValueError("批次已被其他操作修改，请刷新后保存。")
            stamp=datetime.now().isoformat(timespec="microseconds")
            self.db.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self._output_settings_key(batch_id),json.dumps(settings,ensure_ascii=False,sort_keys=True,separators=(',',':'))))
            self.db.conn.execute("UPDATE batches SET updated_at=? WHERE id=?",(stamp,batch_id))
        return self.get(batch_id)

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

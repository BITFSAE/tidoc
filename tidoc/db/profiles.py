"""身份（Profile）仓库。设计文档第 5、8.1 节。"""

from __future__ import annotations

import uuid
from datetime import datetime

from .database import Database

# 供打印导出组件使用的可选字段
OPTIONAL_FIELDS = ("student_id", "contact", "bank_name", "bank_card", "season")
MULTI_CLAIMANT_PREF_KEY = "tidoc.multiClaimantMode"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()} if row else {}


class ProfileRepo:
    def __init__(self, db: Database):
        self.db = db

    def create(self, name: str, reviewer: str, is_default: bool = False, **optional) -> dict:
        if not name.strip() or not reviewer.strip():
            raise ValueError("本人姓名与审核人均为必填。")
        profile_id = uuid.uuid4().hex
        previous_count = self._count()
        if is_default:
            self.db.conn.execute("UPDATE profiles SET is_default = 0")
        elif previous_count == 0:
            is_default = True  # 第一个身份自动设为默认
        cols = {k: optional.get(k, "") for k in OPTIONAL_FIELDS}
        self.db.conn.execute(
            """INSERT INTO profiles(id, name, reviewer, is_default, student_id,
               contact, bank_name, bank_card, season, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (profile_id, name.strip(), reviewer.strip(), int(is_default),
             cols["student_id"], cols["contact"], cols["bank_name"],
             cols["bank_card"], cols["season"], _now()),
        )
        if previous_count == 1:
            self.db.conn.execute(
                """INSERT INTO meta(key, value) VALUES(?, '1')
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (MULTI_CLAIMANT_PREF_KEY,),
            )
        self.db.conn.commit()
        return self.get(profile_id)

    def get(self, profile_id: str) -> dict:
        row = self.db.conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        return _row_to_dict(row)

    def list(self) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM profiles ORDER BY is_default DESC, created_at ASC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_default(self) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM profiles ORDER BY is_default DESC, created_at ASC LIMIT 1"
        ).fetchone()
        return _row_to_dict(row) if row else None

    def update(self, profile_id: str, **fields) -> dict:
        allowed = {"name", "reviewer", *OPTIONAL_FIELDS}
        sets, params = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key} = ?")
                params.append(str(value).strip() if value is not None else "")
        if sets:
            params.append(profile_id)
            self.db.conn.execute(f"UPDATE profiles SET {', '.join(sets)} WHERE id = ?", params)
            self.db.conn.commit()
        return self.get(profile_id)

    def set_default(self, profile_id: str) -> None:
        self.db.conn.execute("UPDATE profiles SET is_default = 0")
        self.db.conn.execute("UPDATE profiles SET is_default = 1 WHERE id = ?", (profile_id,))
        self.db.conn.commit()

    def delete(self, profile_id: str) -> None:
        in_use = self.db.conn.execute(
            "SELECT COUNT(*) c FROM entries WHERE profile_id = ?", (profile_id,)
        ).fetchone()["c"]
        if in_use:
            raise ValueError(f"该身份下还有 {in_use} 条报账条目，无法删除。")
        self.db.conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        self.db.conn.commit()

    def _count(self) -> int:
        return self.db.conn.execute("SELECT COUNT(*) c FROM profiles").fetchone()["c"]

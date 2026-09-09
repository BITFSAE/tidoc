"""阿里云 OCR 结果仓库（设计文档第 10 节）。

每次调用都追加一行（含失败），原始响应永久保存：
- 失败行也是对账依据（按量计费）。
- 同一条目再次识别时，旧行保留、其待确认差异清空，以最新一行为准。
- pending 记录「待人工确认的差异」，条目列表据此显示 OCR 徽标。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Iterable

from .database import Database

PROVIDER_ALIYUN = "aliyun"

# SQLite 变量数上限远高于此，分块只为稳妥（与 entries.QUERY_BATCH_SIZE 同思路）
_QUERY_BATCH_SIZE = 500


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class OcrRepo:
    def __init__(self, db: Database):
        self.db = db

    def record(
        self,
        entry_id: str,
        *,
        provider: str = PROVIDER_ALIYUN,
        file_sha256: str = "",
        file_name: str = "",
        raw_json: str = "",
        normalized: str = "",
        closure_pass: bool = False,
        status: str = "ok",
        error: str = "",
        pending: Iterable[str] = (),
        applied_changes: dict | None = None,
        is_local_call: bool = True,
        api_calls: int = 1,
    ) -> dict:
        """记录一次识别（成功或失败），并让同条目的旧行退出待确认状态。"""
        now = _now()
        pending_json = json.dumps(list(pending), ensure_ascii=False)
        cur = self.db.conn.execute(
            """INSERT INTO ocr_results(entry_id, provider, file_sha256, file_name,
               raw_json, normalized, closure_pass, applied_at, pending, applied_changes,
               is_local_call, api_calls, status, error, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                entry_id, provider, file_sha256, file_name,
                raw_json, normalized, 1 if closure_pass else 0,
                now if applied_changes else "",
                pending_json if status == "ok" else "[]",
                json.dumps(applied_changes or {}, ensure_ascii=False),
                1 if is_local_call else 0,
                max(1, int(api_calls or 1)),
                status, error, now,
            ),
        )
        # 新结果成为唯一权威：旧成功行的待确认差异不再驱动徽标
        self.db.conn.execute(
            """UPDATE ocr_results SET pending = '[]'
                WHERE entry_id = ? AND id <> ?""",
            (entry_id, cur.lastrowid),
        )
        self.db.conn.commit()
        return self.get(cur.lastrowid)

    def get(self, result_id: int) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM ocr_results WHERE id = ?", (result_id,)
        ).fetchone()
        return self._to_dict(row)

    def latest(self, entry_id: str, ok_only: bool = True) -> dict | None:
        sql = "SELECT * FROM ocr_results WHERE entry_id = ?"
        if ok_only:
            sql += " AND status = 'ok'"
        sql += " ORDER BY id DESC LIMIT 1"
        row = self.db.conn.execute(sql, (entry_id,)).fetchone()
        return self._to_dict(row)

    def latest_ok_rows(self, entry_ids: list[str]) -> dict[str, dict]:
        """每个条目最新一次成功结果（列表徽标批量重算用，避免逐条查询）。"""
        latest: dict = {}
        ids = [i for i in entry_ids if i]
        for offset in range(0, len(ids), _QUERY_BATCH_SIZE):
            batch = ids[offset:offset + _QUERY_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            rows = self.db.conn.execute(
                f"SELECT * FROM ocr_results WHERE status = 'ok' "
                f"AND entry_id IN ({placeholders}) ORDER BY id",
                batch,
            ).fetchall()
            for row in rows:
                latest[row["entry_id"]] = row  # 按 id 升序遍历，后行覆盖前行即最新
        return {entry_id: self._to_dict(row) for entry_id, row in latest.items()}

    def latest_failed(self, entry_id: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM ocr_results WHERE entry_id = ? AND status = 'failed' "
            "ORDER BY id DESC LIMIT 1",
            (entry_id,),
        ).fetchone()
        return self._to_dict(row)

    def history(self, entry_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM ocr_results WHERE entry_id = ? ORDER BY id DESC",
            (entry_id,),
        ).fetchall()
        return [self._to_dict(r) for r in rows]

    def count_calls(self) -> int:
        """累计调用次数（含失败），供设置里对账计费。"""
        row = self.db.conn.execute(
            "SELECT COALESCE(SUM(api_calls), 0) FROM ocr_results WHERE is_local_call = 1"
        ).fetchone()
        return int(row[0]) if row else 0

    def mark_applied(self, result_id: int, pending: list[str]) -> None:
        """自动补齐 / 用户采用后更新待确认差异；全清时记录应用时间。

        列表刷新会高频重算差异，与已存值一致时跳过写入，不产生磁盘 commit。
        """
        pending = list(pending)
        row = self.db.conn.execute(
            "SELECT pending FROM ocr_results WHERE id = ?", (result_id,)
        ).fetchone()
        if row is not None:
            try:
                current = json.loads(row["pending"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                current = None
            if isinstance(current, list) and current == pending:
                return
        applied_at = "" if pending else _now()
        if pending:
            self.db.conn.execute(
                "UPDATE ocr_results SET pending = ? WHERE id = ?",
                (json.dumps(pending, ensure_ascii=False), result_id),
            )
        else:
            self.db.conn.execute(
                "UPDATE ocr_results SET pending = '[]', "
                "applied_at = COALESCE(NULLIF(applied_at, ''), ?) WHERE id = ?",
                (applied_at, result_id),
            )
        self.db.conn.commit()

    def pending_fields(self, entry_id: str) -> list[str]:
        row = self.db.conn.execute(
            "SELECT pending FROM ocr_results WHERE entry_id = ? AND status = 'ok' "
            "ORDER BY id DESC LIMIT 1",
            (entry_id,),
        ).fetchone()
        if not row:
            return []
        try:
            pending = json.loads(row["pending"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return pending if isinstance(pending, list) else []

    def pending_entry_ids(self) -> set[str]:
        """存在未确认 OCR 差异的条目集合，供列表徽标。"""
        rows = self.db.conn.execute(
            "SELECT entry_id, pending FROM ocr_results WHERE status = 'ok' "
            "ORDER BY entry_id, id"
        ).fetchall()
        latest_pending: dict[str, str] = {}
        for row in rows:
            latest_pending[row["entry_id"]] = row["pending"] or "[]"
        pending_ids = set()
        for entry_id, pending in latest_pending.items():
            try:
                values = json.loads(pending)
            except (TypeError, ValueError, json.JSONDecodeError):
                values = None
            if isinstance(values, list) and values:
                pending_ids.add(entry_id)
        return pending_ids

    def result_is_stale(self, entry_id: str, current_sha256: str) -> bool:
        """发票附件被替换后，旧识别结果不再对应现行文件。"""
        latest = self.latest(entry_id)
        if not latest:
            return False
        recorded = latest.get("file_sha256") or ""
        return bool(recorded and current_sha256 and recorded != current_sha256)

    @staticmethod
    def _to_dict(row) -> dict | None:
        if not row:
            return None
        item = {k: row[k] for k in row.keys()}
        item["closure_pass"] = bool(item.get("closure_pass"))
        try:
            item["pending_list"] = json.loads(item.get("pending") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            item["pending_list"] = []
        try:
            item["applied_changes_data"] = json.loads(
                item.get("applied_changes") or "{}"
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            item["applied_changes_data"] = {}
        item["is_local_call"] = bool(item.get("is_local_call", 1))
        item["api_calls"] = max(1, int(item.get("api_calls") or 1))
        return item

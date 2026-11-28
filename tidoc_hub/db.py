"""SQLite WAL metadata store and RBAC/workflow logic for tidoc_hub."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .storage import validate_sha256

VALID_ROLES = ("admin", "member")
VALID_SPACE_STATUS = ("active", "closed", "archived")
VALID_REVIEW_STATES = ("submitted", "needs_fix", "updated", "locked", "withdrawn")
MAX_ANCESTORS = 100
MAX_ENTRY_BLOBS = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    reviewer     TEXT NOT NULL DEFAULT '',
    role         TEXT NOT NULL CHECK(role IN ('admin','member')),
    token_hash   TEXT NOT NULL UNIQUE,
    device_name  TEXT NOT NULL DEFAULT '',
    disabled     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS invites (
    code       TEXT PRIMARY KEY,
    role       TEXT NOT NULL CHECK(role IN ('admin','member')),
    note       TEXT NOT NULL DEFAULT '',
    max_uses   INTEGER NOT NULL DEFAULT 1,
    uses       INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL DEFAULT '',
    revoked    INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spaces (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closed','archived')),
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blobs (
    owner_user_id TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    content_type  TEXT NOT NULL DEFAULT 'application/octet-stream',
    original_name TEXT NOT NULL DEFAULT '',
    storage_key   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','ready')),
    created_at    TEXT NOT NULL,
    confirmed_at  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (owner_user_id, sha256)
);

CREATE TABLE IF NOT EXISTS space_entries (
    id                 TEXT PRIMARY KEY,
    space_id           TEXT NOT NULL REFERENCES spaces(id),
    owner_user_id      TEXT NOT NULL REFERENCES users(id),
    owner_name         TEXT NOT NULL DEFAULT '',
    invoice_no         TEXT NOT NULL DEFAULT '',
    invoice_sha256     TEXT NOT NULL DEFAULT '',
    revision           INTEGER NOT NULL DEFAULT 1,
    snapshot_digest    TEXT NOT NULL,
    ancestors_json     TEXT NOT NULL DEFAULT '[]',
    profile_json       TEXT NOT NULL DEFAULT '{}',
    payload_json       TEXT NOT NULL,
    blob_shas_json     TEXT NOT NULL DEFAULT '[]',
    blob_keys_json     TEXT NOT NULL DEFAULT '{}',
    review_state       TEXT NOT NULL DEFAULT 'submitted'
                       CHECK(review_state IN ('submitted','needs_fix','updated','locked','withdrawn')),
    review_note        TEXT NOT NULL DEFAULT '',
    updated_by_user_id TEXT NOT NULL,
    updated_by_name    TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_space_entries_space ON space_entries(space_id);
CREATE INDEX IF NOT EXISTS idx_space_entries_owner ON space_entries(space_id, owner_user_id);

CREATE TABLE IF NOT EXISTS entry_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id        TEXT NOT NULL REFERENCES space_entries(id) ON DELETE CASCADE,
    revision        INTEGER NOT NULL,
    snapshot_digest TEXT NOT NULL,
    actor_user_id   TEXT NOT NULL,
    actor_name      TEXT NOT NULL DEFAULT '',
    action          TEXT NOT NULL,
    review_state    TEXT NOT NULL,
    review_note     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entry_revisions_entry ON entry_revisions(entry_id);

CREATE TABLE IF NOT EXISTS push_receipts (
    actor_user_id   TEXT NOT NULL REFERENCES users(id),
    space_id        TEXT NOT NULL REFERENCES spaces(id),
    submission_id   TEXT NOT NULL,
    source_entry_id TEXT NOT NULL,
    entry_id        TEXT NOT NULL REFERENCES space_entries(id) ON DELETE CASCADE,
    result_json     TEXT NOT NULL,
    created_entry   INTEGER NOT NULL,
    PRIMARY KEY (actor_user_id, space_id, submission_id)
);
CREATE INDEX IF NOT EXISTS idx_push_receipts_source
    ON push_receipts(actor_user_id, space_id, source_entry_id);

CREATE TABLE IF NOT EXISTS invoice_registry (
    dedup_key     TEXT PRIMARY KEY,
    space_id      TEXT NOT NULL REFERENCES spaces(id),
    entry_id      TEXT NOT NULL UNIQUE REFERENCES space_entries(id) ON DELETE CASCADE,
    owner_user_id TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
"""


class HubDbError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400, code: str = "", extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.extra = extra or {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _dedup_key(invoice_no: str, invoice_sha256: str) -> str:
    no = str(invoice_no or "").strip()
    if no:
        return f"no:{no}"
    sha = str(invoice_sha256 or "").strip().lower()
    if sha:
        return f"sha:{sha}"
    return ""


def _sanitize_payload(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise HubDbError("条目数据格式无效。", status=400, code="invalid_payload")
    cleaned = dict(payload)
    cleaned.pop("payees", None)
    return cleaned


def _content_fingerprint(profile: dict, payload: dict, blob_shas: list[str]) -> str:
    content = {
        "profile": profile,
        "payload": {key: value for key, value in payload.items() if key != "updated_at"},
        "blob_shas": blob_shas,
    }
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class HubDB:
    """Thread-safe SQLite repository for tidoc_hub."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------------ Users & Auth
    def create_user(
        self,
        name: str,
        *,
        role: str = "member",
        reviewer: str = "",
        device_name: str = "",
    ) -> tuple[dict, str]:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise HubDbError("姓名不能为空。", status=400, code="invalid_name")
        if role not in VALID_ROLES:
            raise HubDbError("无效的角色类型。", status=400, code="invalid_role")
        token = f"th_{secrets.token_urlsafe(32)}"
        uid = uuid.uuid4().hex
        now = _now_iso()
        with self._lock:
            self.conn.execute(
                """INSERT INTO users(id, name, reviewer, role, token_hash, device_name, disabled, created_at, last_seen_at)
                   VALUES(?,?,?,?,?,?,0,?,?)""",
                (uid, clean_name, str(reviewer or "").strip(), role, _token_hash(token),
                 str(device_name or "").strip()[:120], now, now),
            )
            self.conn.commit()
            user = self._public_user(self.conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
        return user, token

    def authenticate(self, token: str) -> dict | None:
        raw = str(token or "").strip()
        if not raw:
            return None
        digest = _token_hash(raw)
        with self._lock:
            row = self.conn.execute("SELECT * FROM users WHERE token_hash = ?", (digest,)).fetchone()
            if not row:
                return None
            if not hmac.compare_digest(str(row["token_hash"]), digest):
                return None
            if int(row["disabled"] or 0):
                return None
            now = _now_iso()
            self.conn.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (now, row["id"]))
            self.conn.commit()
            return self._public_user(row)

    @staticmethod
    def _public_user(row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "reviewer": row["reviewer"] or "",
            "role": row["role"],
            "device_name": row["device_name"] or "",
            "disabled": bool(row["disabled"]),
            "created_at": row["created_at"],
            "last_seen_at": row["last_seen_at"] or "",
        }

    def list_users(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
            return [self._public_user(r) for r in rows]

    def update_user(self, actor: dict, user_id: str, *, disabled: bool | None = None, role: str | None = None) -> dict:
        with self._lock:
            row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise HubDbError("成员不存在。", status=404, code="not_found")
            new_disabled = int(bool(disabled)) if disabled is not None else int(row["disabled"])
            new_role = str(role) if role is not None else str(row["role"])
            if new_role not in VALID_ROLES:
                raise HubDbError("角色类型无效。", status=400, code="invalid_role")
            if user_id == actor["id"] and new_disabled:
                raise HubDbError("不能停用当前正在使用的管理员账号。", status=400, code="cannot_disable_self")
            # Prevent leaving zero active admins
            if row["role"] == "admin" and not int(row["disabled"]) and (new_disabled or new_role != "admin"):
                other_admins = self.conn.execute(
                    "SELECT COUNT(*) c FROM users WHERE role = 'admin' AND disabled = 0 AND id <> ?",
                    (user_id,),
                ).fetchone()["c"]
                if other_admins < 1:
                    raise HubDbError("至少需要保留一名启用中的管理员。", status=400, code="last_admin")
            self.conn.execute(
                "UPDATE users SET disabled = ?, role = ? WHERE id = ?",
                (new_disabled, new_role, user_id),
            )
            self.conn.commit()
            updated = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return self._public_user(updated)

    # ------------------------------------------------------------------ Invites
    def create_invite(
        self,
        *,
        role: str = "member",
        max_uses: int = 20,
        expires_days: int = 30,
        note: str = "",
        created_by: str = "",
    ) -> dict:
        if role not in VALID_ROLES:
            raise HubDbError("无效的角色类型。", status=400, code="invalid_role")
        uses_limit = max(1, min(1000, int(max_uses or 1)))
        days = max(0, min(3650, int(expires_days or 0)))
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
        expires_at = (
            (now_dt + timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")
            if days > 0 else ""
        )
        code = secrets.token_hex(4).upper() + "-" + secrets.token_hex(4).upper()
        with self._lock:
            self.conn.execute(
                """INSERT INTO invites(code, role, note, max_uses, uses, expires_at, revoked, created_by, created_at)
                   VALUES(?,?,?,?,0,?,0,?,?)""",
                (code, role, str(note or "").strip()[:200], uses_limit, expires_at, created_by, now),
            )
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM invites WHERE code = ?", (code,)).fetchone()
            return self._public_invite(row)

    @staticmethod
    def _public_invite(row) -> dict:
        return {
            "code": row["code"],
            "role": row["role"],
            "note": row["note"] or "",
            "max_uses": int(row["max_uses"]),
            "uses": int(row["uses"]),
            "expires_at": row["expires_at"] or "",
            "revoked": bool(row["revoked"]),
            "created_at": row["created_at"],
        }

    def list_invites(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM invites ORDER BY created_at DESC").fetchall()
            return [self._public_invite(r) for r in rows]

    def revoke_invite(self, code: str) -> None:
        with self._lock:
            cur = self.conn.execute("UPDATE invites SET revoked = 1 WHERE code = ?", (str(code or "").strip(),))
            if cur.rowcount == 0:
                raise HubDbError("邀请码不存在。", status=404, code="not_found")
            self.conn.commit()

    def join_with_invite(self, invite_code: str, name: str, reviewer: str = "", device_name: str = "") -> tuple[dict, str]:
        code = str(invite_code or "").strip().upper()
        clean_name = str(name or "").strip()
        if not code:
            raise HubDbError("请填写邀请码。", status=400, code="invalid_invite")
        if not clean_name:
            raise HubDbError("请填写本人姓名。", status=400, code="invalid_name")
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
        with self._lock:
            row = self.conn.execute("SELECT * FROM invites WHERE UPPER(code) = ?", (code,)).fetchone()
            if not row:
                raise HubDbError("邀请码无效。", status=400, code="invalid_invite")
            if int(row["revoked"] or 0):
                raise HubDbError("邀请码已被撤销。", status=400, code="invite_revoked")
            if row["expires_at"]:
                exp = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
                if now_dt > exp:
                    raise HubDbError("邀请码已过期。", status=400, code="invite_expired")
            cur = self.conn.execute(
                "UPDATE invites SET uses = uses + 1 WHERE code = ? AND uses < max_uses AND revoked = 0",
                (row["code"],),
            )
            if cur.rowcount == 0:
                raise HubDbError("邀请码使用次数已用尽。", status=400, code="invite_exhausted")
            token = f"th_{secrets.token_urlsafe(32)}"
            uid = uuid.uuid4().hex
            self.conn.execute(
                """INSERT INTO users(id, name, reviewer, role, token_hash, device_name, disabled, created_at, last_seen_at)
                   VALUES(?,?,?,?,?,?,0,?,?)""",
                (uid, clean_name, str(reviewer or "").strip(), row["role"], _token_hash(token),
                 str(device_name or "").strip()[:120], now, now),
            )
            self.conn.commit()
            user = self._public_user(self.conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())
        return user, token

    # ------------------------------------------------------------------ Spaces
    def create_space(self, name: str, description: str = "", *, created_by: str = "") -> dict:
        clean = str(name or "").strip()
        if not clean:
            raise HubDbError("空间名称不能为空。", status=400, code="invalid_space")
        sid = uuid.uuid4().hex
        now = _now_iso()
        with self._lock:
            self.conn.execute(
                """INSERT INTO spaces(id, name, description, status, created_by, created_at, updated_at)
                   VALUES(?,?,?,'active',?,?,?)""",
                (sid, clean[:120], str(description or "").strip()[:500], created_by, now, now),
            )
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM spaces WHERE id = ?", (sid,)).fetchone()
            return self._space_dict(row, None)

    def update_space(self, space_id: str, *, name: str | None = None, description: str | None = None, status: str | None = None) -> dict:
        with self._lock:
            row = self.conn.execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
            if not row:
                raise HubDbError("共享空间不存在。", status=404, code="not_found")
            next_name = str(name).strip()[:120] if name is not None else row["name"]
            if not next_name:
                raise HubDbError("空间名称不能为空。", status=400, code="invalid_space")
            next_desc = str(description).strip()[:500] if description is not None else row["description"]
            next_status = str(status) if status is not None else row["status"]
            if next_status not in VALID_SPACE_STATUS:
                raise HubDbError("无效的空间状态。", status=400, code="invalid_status")
            now = _now_iso()
            self.conn.execute(
                "UPDATE spaces SET name = ?, description = ?, status = ?, updated_at = ? WHERE id = ?",
                (next_name, next_desc, next_status, now, space_id),
            )
            self.conn.commit()
            updated = self.conn.execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
            return self._space_dict(updated, None)

    def get_space(self, space_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
            return self._space_dict(row, None) if row else None

    def list_spaces(self, user: dict, *, include_archived: bool = False) -> list[dict]:
        with self._lock:
            if include_archived:
                rows = self.conn.execute("SELECT * FROM spaces ORDER BY updated_at DESC").fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM spaces WHERE status <> 'archived' ORDER BY updated_at DESC"
                ).fetchall()
            counts_by_space: dict[str, dict[str, int]] = {}
            if user["role"] == "admin":
                stat_rows = self.conn.execute(
                    """SELECT space_id, review_state, COUNT(*) c
                         FROM space_entries
                        WHERE review_state <> 'withdrawn'
                        GROUP BY space_id, review_state"""
                ).fetchall()
            else:
                # Strictly isolate member counts to their own entries only.
                stat_rows = self.conn.execute(
                    """SELECT space_id, review_state, COUNT(*) c
                         FROM space_entries
                        WHERE review_state <> 'withdrawn' AND owner_user_id = ?
                        GROUP BY space_id, review_state""",
                    (user["id"],),
                ).fetchall()
            for r in stat_rows:
                bucket = counts_by_space.setdefault(r["space_id"], {})
                bucket[r["review_state"]] = int(r["c"])
            return [self._space_dict(r, counts_by_space.get(r["id"])) for r in rows]

    @staticmethod
    def _space_dict(row, counts: dict[str, int] | None) -> dict:
        c = counts or {}
        total = sum(c.values())
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"] or "",
            "status": row["status"],
            "created_by": row["created_by"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "entry_count": total,
            "needs_fix_count": c.get("needs_fix", 0),
            "updated_count": c.get("updated", 0),
            "locked_count": c.get("locked", 0),
        }

    # ------------------------------------------------------------------ Blobs
    def get_blob(self, owner_user_id: str, sha256: str) -> dict | None:
        digest = validate_sha256(sha256)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM blobs WHERE owner_user_id = ? AND sha256 = ?",
                (owner_user_id, digest),
            ).fetchone()
            return dict(row) if row else None

    def upsert_pending_blob(
        self,
        owner_user_id: str,
        sha256: str,
        size_bytes: int,
        content_type: str,
        original_name: str,
        storage_key: str,
    ) -> dict:
        digest = validate_sha256(sha256)
        now = _now_iso()
        with self._lock:
            self.conn.execute(
                """INSERT INTO blobs(owner_user_id, sha256, size_bytes, content_type, original_name, storage_key, status, created_at)
                   VALUES(?,?,?,?,?,?,'pending',?)
                   ON CONFLICT(owner_user_id, sha256) DO UPDATE SET
                       size_bytes = CASE WHEN blobs.status='ready' THEN blobs.size_bytes ELSE excluded.size_bytes END,
                       content_type = CASE WHEN blobs.status='ready' THEN blobs.content_type ELSE excluded.content_type END,
                       original_name = CASE WHEN blobs.status='ready' THEN blobs.original_name ELSE excluded.original_name END,
                       storage_key = CASE WHEN blobs.status='ready' THEN blobs.storage_key ELSE excluded.storage_key END""",
                (owner_user_id, digest, int(size_bytes), content_type or "application/octet-stream",
                 str(original_name or "")[:255], storage_key, now),
            )
            self.conn.commit()
            return dict(self.conn.execute(
                "SELECT * FROM blobs WHERE owner_user_id = ? AND sha256 = ?",
                (owner_user_id, digest),
            ).fetchone())

    def mark_blob_ready(self, owner_user_id: str, sha256: str, actual_size: int) -> dict:
        digest = validate_sha256(sha256)
        now = _now_iso()
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM blobs WHERE owner_user_id = ? AND sha256 = ?",
                (owner_user_id, digest),
            ).fetchone()
            if not row:
                raise HubDbError("未找到待确认的附件上传记录。", status=404, code="blob_not_found")
            if int(row["size_bytes"]) != int(actual_size):
                raise HubDbError("上传文件大小与声明不一致，已拒绝确认。", status=400, code="blob_size_mismatch")
            self.conn.execute(
                "UPDATE blobs SET status = 'ready', confirmed_at = ? WHERE owner_user_id = ? AND sha256 = ?",
                (now, owner_user_id, digest),
            )
            self.conn.commit()
            return dict(self.conn.execute(
                "SELECT * FROM blobs WHERE owner_user_id = ? AND sha256 = ?",
                (owner_user_id, digest),
            ).fetchone())

    def authorize_blob_download(self, user: dict, space_id: str, entry_id: str, sha256: str) -> dict:
        """Strictly verify RBAC: user must be admin or own entry_id, and sha256 must belong to entry_id."""
        digest = validate_sha256(sha256)
        with self._lock:
            entry = self.conn.execute(
                "SELECT * FROM space_entries WHERE id = ? AND space_id = ?",
                (entry_id, space_id),
            ).fetchone()
            if not entry:
                raise HubDbError("条目不存在或无权访问。", status=404, code="not_found")
            if user["role"] != "admin" and entry["owner_user_id"] != user["id"]:
                raise HubDbError("无权下载其他成员的报账材料。", status=403, code="forbidden")
            blob_shas = set(json.loads(entry["blob_shas_json"] or "[]"))
            if digest not in blob_shas:
                raise HubDbError("该文件不属于指定条目。", status=403, code="blob_not_in_entry")
            blob_keys = json.loads(entry["blob_keys_json"] or "{}")
            storage_key = str(blob_keys.get(digest) or "")
            if not storage_key:
                raise HubDbError("附件索引缺失或尚未就绪。", status=404, code="blob_not_ready")
            blob = self.conn.execute(
                "SELECT * FROM blobs WHERE storage_key = ? AND sha256 = ? AND status = 'ready'",
                (storage_key, digest),
            ).fetchone()
            if not blob:
                raise HubDbError("附件尚未就绪或不存在。", status=404, code="blob_not_ready")
            return dict(blob)

    # ------------------------------------------------------------------ Entries
    @staticmethod
    def _entry_dict(row) -> dict:
        return {
            "id": row["id"],
            "space_id": row["space_id"],
            "owner_user_id": row["owner_user_id"],
            "owner_name": row["owner_name"] or "",
            "invoice_no": row["invoice_no"] or "",
            "invoice_sha256": row["invoice_sha256"] or "",
            "revision": int(row["revision"]),
            "snapshot_digest": row["snapshot_digest"],
            "ancestors": json.loads(row["ancestors_json"] or "[]"),
            "profile": json.loads(row["profile_json"] or "{}"),
            "payload": json.loads(row["payload_json"] or "{}"),
            "blob_shas": json.loads(row["blob_shas_json"] or "[]"),
            "review_state": row["review_state"],
            "review_note": row["review_note"] or "",
            "updated_by_user_id": row["updated_by_user_id"],
            "updated_by_name": row["updated_by_name"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _entry_summary(row) -> dict:
        payload = json.loads(row["payload_json"])
        return {
            "invoice_no": row["invoice_no"] or payload.get("invoice_no") or "",
            "seller": payload.get("seller") or "",
            "title": payload.get("title") or "",
            "total": payload.get("total", ""),
            "owner_name": row["owner_name"] or "",
            "review_state": row["review_state"] or "",
            "review_note": row["review_note"] or "",
        }

    def list_space_entries(
        self, user: dict, space_id: str, *, include_withdrawn: bool = True, summary: bool = False,
    ) -> tuple[dict, list[dict]]:
        with self._lock:
            space = self.conn.execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
            if not space:
                raise HubDbError("共享空间不存在。", status=404, code="not_found")
            where = ["space_id = ?"]
            params: list[Any] = [space_id]
            if user["role"] != "admin":
                where.append("owner_user_id = ?")
                params.append(user["id"])
            if not include_withdrawn:
                where.append("review_state <> 'withdrawn'")
            columns = "invoice_no, owner_name, review_state, review_note, payload_json" if summary else "*"
            rows = self.conn.execute(
                f"SELECT {columns} FROM space_entries WHERE {' AND '.join(where)} ORDER BY updated_at DESC",
                params,
            ).fetchall()
            serialize = self._entry_summary if summary else self._entry_dict
            return self._space_dict(space, None), [serialize(r) for r in rows]

    def get_space_entry(self, user: dict, space_id: str, entry_id: str) -> dict:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM space_entries WHERE id = ? AND space_id = ?",
                (entry_id, space_id),
            ).fetchone()
            if not row:
                raise HubDbError("条目不存在。", status=404, code="not_found")
            if user["role"] != "admin" and row["owner_user_id"] != user["id"]:
                raise HubDbError("无权查看其他成员的条目。", status=403, code="forbidden")
            return self._entry_dict(row)

    def sync_state(self, user: dict, space_id: str) -> dict:
        """Return lightweight per-entry revision/digest metadata scoped by RBAC."""
        with self._lock:
            space = self.conn.execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
            if not space:
                raise HubDbError("共享空间不存在。", status=404, code="space_not_found")
            where = ["space_id = ?"]
            params: list[Any] = [space_id]
            if user["role"] != "admin":
                where.append("owner_user_id = ?")
                params.append(user["id"])
            rows = self.conn.execute(
                f"""SELECT id, revision, review_state, review_note, snapshot_digest, updated_at
                      FROM space_entries
                     WHERE {' AND '.join(where)}
                     ORDER BY id""",
                params,
            ).fetchall()
            entries = [
                {
                    "id": r["id"],
                    "revision": int(r["revision"]),
                    "review_state": r["review_state"],
                    "review_note": r["review_note"] or "",
                    "snapshot_digest": r["snapshot_digest"],
                    "updated_at": r["updated_at"],
                }
                for r in rows
            ]
            space_dict = self._space_dict(space, None)
            version_material = {
                "space_status": space_dict["status"],
                "space_name": space_dict["name"],
                "space_updated_at": space_dict["updated_at"],
                "entries": [
                    [
                        e["id"],
                        e["revision"],
                        e["review_state"],
                        e["review_note"],
                        e["snapshot_digest"],
                    ]
                    for e in entries
                ],
            }
            encoded = json.dumps(
                version_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            version = hashlib.sha256(encoded).hexdigest()
            return {
                "space": space_dict,
                "version": version,
                "max_revision": max((e["revision"] for e in entries), default=0),
                "entry_count": len(entries),
                "entries": entries,
            }

    def upload_state(self, user: dict, space_id: str, entry_ids: list[str]) -> dict:
        with self._lock:
            space = self.conn.execute("SELECT status FROM spaces WHERE id = ?", (space_id,)).fetchone()
            if not space:
                raise HubDbError("共享空间不存在。", status=404, code="space_not_found")
            admin = user["role"] == "admin"
            if not admin and space["status"] != "active":
                raise HubDbError("该共享空间已截止或归档，无法继续上传。", status=423, code="space_closed")
            order = ",".join("?" for _ in entry_ids)
            owner = "" if admin else " AND owner_user_id = ?"
            params = [space_id, *entry_ids]
            if not admin:
                params.append(user["id"])
            rows = self.conn.execute(
                f"""SELECT id, revision, review_state, profile_json, payload_json, blob_shas_json
                      FROM space_entries WHERE space_id = ? AND id IN ({order}){owner}""",
                params,
            ).fetchall()
        states = {}
        for row in rows:
            payload = json.loads(row["payload_json"])
            shas = json.loads(row["blob_shas_json"])
            states[row["id"]] = {
                "revision": row["revision"], "review_state": row["review_state"],
                "fingerprint": _content_fingerprint(json.loads(row["profile_json"]), payload, shas),
                "blob_shas": shas,
            }
        return {"entries": states}

    def push_entry(self, user: dict, space_id: str, body: dict) -> tuple[dict, bool]:
        submission_id = str(body.get("submission_id") or "")
        if submission_id:
            request = {key: value for key, value in body.items() if key != "submission_id"}
            encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if submission_id != hashlib.sha256(encoded).hexdigest():
                raise HubDbError("提交标识与条目内容不匹配。", status=400, code="invalid_submission")
        payload = _sanitize_payload(body.get("payload"))
        source_entry_id = str(payload.get("id") or "") if submission_id else ""
        if not re.fullmatch(r"[0-9a-f]{32}", source_entry_id):
            source_entry_id = ""
        snapshot_digest = str(body.get("snapshot_digest") or "").strip().lower()
        if not snapshot_digest or len(snapshot_digest) > 128:
            raise HubDbError("缺少有效的条目快照摘要。", status=400, code="invalid_digest")
        invoice_no = str(body.get("invoice_no") or payload.get("invoice_no") or "").strip()
        invoice_sha256 = str(body.get("invoice_sha256") or "").strip().lower()
        remote_entry_id = str(body.get("remote_entry_id") or "").strip()
        base_revision = int(body.get("base_revision") or 0)
        client_ancestors = [str(a).strip().lower() for a in (body.get("ancestors") or []) if str(a).strip()]
        profile = body.get("profile") if isinstance(body.get("profile"), dict) else {}
        raw_blob_shas = [validate_sha256(s) for s in (body.get("blob_shas") or []) if str(s).strip()]
        blob_shas = list(dict.fromkeys(raw_blob_shas))
        if len(blob_shas) > MAX_ENTRY_BLOBS:
            raise HubDbError(f"单条目的附件数量不能超过 {MAX_ENTRY_BLOBS} 个。", status=400, code="too_many_blobs")
        req_review_state = body.get("review_state")
        req_review_note = body.get("review_note")

        if req_review_state is not None:
            if user["role"] != "admin":
                raise HubDbError("仅管理员可以设置审核状态。", status=403, code="forbidden")
            if req_review_state not in ("submitted", "needs_fix", "updated", "locked"):
                raise HubDbError("无效的审核状态。", status=400, code="invalid_review_state")

        now = _now_iso()
        dedup = _dedup_key(invoice_no, invoice_sha256)

        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            space = self.conn.execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
            if not space:
                raise HubDbError("共享空间不存在。", status=404, code="not_found")
            if submission_id:
                receipt = self.conn.execute(
                    """SELECT result_json, created_entry FROM push_receipts
                        WHERE actor_user_id = ? AND space_id = ? AND submission_id = ?""",
                    (user["id"], space_id, submission_id),
                ).fetchone()
                if receipt:
                    entry = json.loads(receipt["result_json"])
                    if user["role"] != "admin" and entry["owner_user_id"] != user["id"]:
                        raise HubDbError("无权查看其他成员的条目。", status=403, code="forbidden")
                    live = self.conn.execute(
                        "SELECT * FROM space_entries WHERE id = ? AND space_id = ?", (entry["id"], space_id)
                    ).fetchone()
                    if live is None:
                        raise HubDbError("共享区中未找到对应条目。", status=404, code="not_found")
                    if user["role"] != "admin" and live["owner_user_id"] != user["id"]:
                        raise HubDbError("无权查看其他成员的条目。", status=403, code="forbidden")
                    if any(live[key] != entry[key] for key in ("revision", "review_state", "review_note")):
                        raise HubDbError(
                            "共享区在上次提交后已有更新，请先拉取合并后再上传。",
                            status=409, code="revision_conflict",
                            extra={"server_revision": live["revision"], "server_entry": self._entry_dict(live)},
                        )
                    return entry, bool(receipt["created_entry"])
            if user["role"] != "admin" and space["status"] != "active":
                raise HubDbError("该共享空间已截止或归档，无法继续上传。", status=423, code="space_closed")

            existing = None
            if remote_entry_id:
                existing = self.conn.execute(
                    "SELECT * FROM space_entries WHERE id = ? AND space_id = ?",
                    (remote_entry_id, space_id),
                ).fetchone()
                if not existing:
                    raise HubDbError("共享区中未找到对应条目。", status=404, code="not_found")
            elif source_entry_id:
                existing = self.conn.execute(
                    """SELECT entry.* FROM push_receipts receipt
                         JOIN space_entries entry ON entry.id = receipt.entry_id
                        WHERE receipt.actor_user_id = ? AND receipt.space_id = ? AND receipt.source_entry_id = ?
                        ORDER BY receipt.rowid DESC LIMIT 1""",
                    (user["id"], space_id, source_entry_id),
                ).fetchone()

            def record_result(entry_id: str, created: bool, *, unchanged: bool = False) -> tuple[dict, bool]:
                if not submission_id:
                    row = self.conn.execute("SELECT * FROM space_entries WHERE id = ?", (entry_id,)).fetchone()
                    return self._entry_dict(row), created
                row = self.conn.execute(
                    """SELECT id, revision, owner_user_id, owner_name, review_state, review_note
                         FROM space_entries WHERE id = ?""",
                    (entry_id,),
                ).fetchone()
                result = dict(row)
                if unchanged:
                    result["_unchanged"] = True
                self.conn.execute(
                    """INSERT INTO push_receipts(actor_user_id, space_id, submission_id, source_entry_id,
                                                  entry_id, result_json, created_entry) VALUES(?,?,?,?,?,?,?)""",
                    (user["id"], space_id, submission_id, source_entry_id,
                     entry_id, json.dumps(result, ensure_ascii=False), int(created)),
                )
                return result, created

            # Check global invoice registry for duplicates across all spaces / members
            if dedup:
                reg = self.conn.execute(
                    """SELECT r.*, s.name AS space_name
                         FROM invoice_registry r
                         JOIN spaces s ON s.id = r.space_id
                        WHERE r.dedup_key = ?""",
                    (dedup,),
                ).fetchone()
                if reg:
                    if existing is None:
                        if reg["space_id"] == space_id and (
                            user["role"] == "admin" or reg["owner_user_id"] == user["id"]
                        ):
                            existing = self.conn.execute(
                                "SELECT * FROM space_entries WHERE id = ?",
                                (reg["entry_id"],),
                            ).fetchone()
                        else:
                            msg = (
                                f"该发票（{invoice_no or '同文件'}）已在空间「{reg['space_name']}」中提交，禁止重复报账。"
                                if user["role"] == "admin"
                                else "该发票已在共享区中提交过，请勿重复报账。"
                            )
                            raise HubDbError(msg, status=409, code="duplicate_invoice")
                    elif reg["entry_id"] != existing["id"]:
                        msg = (
                            f"该发票号码已归属于空间「{reg['space_name']}」的另一条记录，禁止重复报账。"
                            if user["role"] == "admin"
                            else "该发票已在共享区中存在，请勿重复报账。"
                        )
                        raise HubDbError(msg, status=409, code="duplicate_invoice")

            if existing is not None:
                if user["role"] != "admin":
                    if existing["owner_user_id"] != user["id"]:
                        raise HubDbError("无权修改其他成员的条目。", status=403, code="forbidden")
                    if existing["review_state"] == "locked":
                        raise HubDbError("该条目已被审核锁定，无法修改；如需更改请联系审核人解锁。", status=423, code="entry_locked")

                current_rev = int(existing["revision"])
                if base_revision != current_rev:
                    raise HubDbError(
                        f"共享区已有更新（当前版本 v{current_rev}，你的基线为 v{base_revision}），请先拉取合并后再上传。",
                        status=409,
                        code="revision_conflict",
                        extra={"server_revision": current_rev, "server_entry": self._entry_dict(existing)},
                    )


            # Verify caller possesses every referenced blob (either uploaded by caller OR already bound to this entry)
            existing_blob_keys: dict[str, str] = (
                json.loads(existing["blob_keys_json"] or "{}") if existing is not None else {}
            )
            resolved_blob_keys: dict[str, str] = {}
            if blob_shas:
                placeholders = ",".join("?" for _ in blob_shas)
                caller_rows = self.conn.execute(
                    f"""SELECT sha256, storage_key FROM blobs
                         WHERE owner_user_id = ? AND status = 'ready' AND sha256 IN ({placeholders})""",
                    [user["id"], *blob_shas],
                ).fetchall()
                caller_keys = {r["sha256"]: r["storage_key"] for r in caller_rows}
                missing = []
                for s in blob_shas:
                    if s in caller_keys:
                        resolved_blob_keys[s] = caller_keys[s]
                    elif s in existing_blob_keys:
                        resolved_blob_keys[s] = existing_blob_keys[s]
                    else:
                        missing.append(s)
                if missing:
                    raise HubDbError(
                        f"有 {len(missing)} 份附件尚未完成上传确认，请重试。",
                        status=400,
                        code="missing_blobs",
                        extra={"missing": missing},
                    )

            if existing is not None:
                # Prepared upload state is only a hint. Decide no-op under the same
                # write lock as revision checks so remote review/content changes stay visible.
                if (submission_id and body.get("skip_unchanged") is True
                        and req_review_state is None and req_review_note is None
                        and existing["review_state"] not in ("needs_fix", "withdrawn")
                        and invoice_no == existing["invoice_no"]
                        and invoice_sha256 == existing["invoice_sha256"]
                        and snapshot_digest == existing["snapshot_digest"]
                        and _content_fingerprint(profile, payload, blob_shas) == _content_fingerprint(
                            json.loads(existing["profile_json"]), json.loads(existing["payload_json"]),
                            json.loads(existing["blob_shas_json"]))):
                    return record_result(existing["id"], False, unchanged=True)

                # Store ancestors NEWEST-FIRST so entry_merge.find_common_base picks the latest common base.
                old_ancestors = json.loads(existing["ancestors_json"] or "[]")
                merged_ancestors = list(dict.fromkeys([
                    str(existing["snapshot_digest"]),
                    *old_ancestors,
                    *client_ancestors,
                ]))[:MAX_ANCESTORS]

                next_state = existing["review_state"]
                next_note = existing["review_note"] or ""
                if user["role"] == "admin":
                    if req_review_state is not None:
                        next_state = req_review_state
                    elif next_state == "withdrawn":
                        next_state = "submitted"
                    if req_review_note is not None:
                        next_note = str(req_review_note).strip()[:1000]
                else:
                    if existing["review_state"] == "needs_fix":
                        next_state = "updated"
                    elif existing["review_state"] == "withdrawn":
                        next_state = "submitted"

                next_rev = current_rev + 1
                self.conn.execute(
                    """UPDATE space_entries
                          SET invoice_no = ?, invoice_sha256 = ?, revision = ?,
                              snapshot_digest = ?, ancestors_json = ?, profile_json = ?,
                              payload_json = ?, blob_shas_json = ?, blob_keys_json = ?,
                              review_state = ?, review_note = ?, updated_by_user_id = ?,
                              updated_by_name = ?, updated_at = ?
                        WHERE id = ?""",
                    (
                        invoice_no, invoice_sha256, next_rev, snapshot_digest,
                        json.dumps(merged_ancestors, ensure_ascii=False),
                        json.dumps(profile, ensure_ascii=False),
                        json.dumps(payload, ensure_ascii=False),
                        json.dumps(blob_shas, ensure_ascii=False),
                        json.dumps(resolved_blob_keys, ensure_ascii=False),
                        next_state, next_note, user["id"], user["name"], now, existing["id"],
                    ),
                )
                self.conn.execute("DELETE FROM invoice_registry WHERE entry_id = ?", (existing["id"],))
                if dedup and next_state != "withdrawn":
                    self.conn.execute(
                        """INSERT INTO invoice_registry(dedup_key, space_id, entry_id, owner_user_id, created_at)
                           VALUES(?,?,?,?,?)""",
                        (dedup, space_id, existing["id"], existing["owner_user_id"], now),
                    )
                self.conn.execute(
                    """INSERT INTO entry_revisions(entry_id, revision, snapshot_digest, actor_user_id,
                                                   actor_name, action, review_state, review_note, created_at)
                       VALUES(?,?,?,?,?,'push',?,?,?)""",
                    (existing["id"], next_rev, snapshot_digest, user["id"], user["name"],
                     next_state, next_note, now),
                )
                self.conn.execute("UPDATE spaces SET updated_at = ? WHERE id = ?", (now, space_id))
                return record_result(existing["id"], False)

            # Create new entry (ancestors newest-first)
            eid = uuid.uuid4().hex
            init_ancestors = list(dict.fromkeys([snapshot_digest, *client_ancestors]))[:MAX_ANCESTORS]
            init_state = req_review_state if (user["role"] == "admin" and req_review_state) else "submitted"
            init_note = str(req_review_note or "").strip()[:1000] if user["role"] == "admin" else ""
            owner_name = str(profile.get("name") or user["name"] or "").strip()
            self.conn.execute(
                """INSERT INTO space_entries(
                       id, space_id, owner_user_id, owner_name, invoice_no, invoice_sha256,
                       revision, snapshot_digest, ancestors_json, profile_json, payload_json,
                       blob_shas_json, blob_keys_json, review_state, review_note, updated_by_user_id,
                       updated_by_name, created_at, updated_at
                   ) VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    eid, space_id, user["id"], owner_name, invoice_no, invoice_sha256,
                    snapshot_digest, json.dumps(init_ancestors, ensure_ascii=False),
                    json.dumps(profile, ensure_ascii=False), json.dumps(payload, ensure_ascii=False),
                    json.dumps(blob_shas, ensure_ascii=False),
                    json.dumps(resolved_blob_keys, ensure_ascii=False),
                    init_state, init_note, user["id"], user["name"], now, now,
                ),
            )
            if dedup:
                self.conn.execute(
                    """INSERT INTO invoice_registry(dedup_key, space_id, entry_id, owner_user_id, created_at)
                       VALUES(?,?,?,?,?)""",
                    (dedup, space_id, eid, user["id"], now),
                )
            self.conn.execute(
                """INSERT INTO entry_revisions(entry_id, revision, snapshot_digest, actor_user_id,
                                               actor_name, action, review_state, review_note, created_at)
                   VALUES(?,1,?,?,?,'create',?,?,?)""",
                (eid, snapshot_digest, user["id"], user["name"], init_state, init_note, now),
            )
            self.conn.execute("UPDATE spaces SET updated_at = ? WHERE id = ?", (now, space_id))
            return record_result(eid, True)

    def review_entry(
        self,
        actor: dict,
        space_id: str,
        entry_id: str,
        *,
        review_state: str,
        review_note: str = "",
        base_revision: int = 0,
    ) -> dict:
        if review_state not in ("submitted", "needs_fix", "updated", "locked"):
            raise HubDbError("无效的审核状态。", status=400, code="invalid_review_state")
        now = _now_iso()
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM space_entries WHERE id = ? AND space_id = ?",
                (entry_id, space_id),
            ).fetchone()
            if not row:
                raise HubDbError("条目不存在。", status=404, code="not_found")
            if row["review_state"] == "withdrawn":
                raise HubDbError("该条目已被撤回，无法修改审核状态。", status=409, code="entry_withdrawn")
            cur_rev = int(row["revision"])
            if base_revision and base_revision != cur_rev:
                raise HubDbError(
                    f"条目版本已更新至 v{cur_rev}，请先刷新后再提交审核。",
                    status=409,
                    code="revision_conflict",
                    extra={"server_revision": cur_rev, "server_entry": self._entry_dict(row)},
                )
            next_rev = cur_rev + 1
            note = str(review_note or "").strip()[:1000]
            self.conn.execute(
                """UPDATE space_entries
                      SET revision = ?, review_state = ?, review_note = ?,
                          updated_by_user_id = ?, updated_by_name = ?, updated_at = ?
                    WHERE id = ?""",
                (next_rev, review_state, note, actor["id"], actor["name"], now, entry_id),
            )
            self.conn.execute(
                """INSERT INTO entry_revisions(entry_id, revision, snapshot_digest, actor_user_id,
                                               actor_name, action, review_state, review_note, created_at)
                   VALUES(?,?,?,?,?,'review',?,?,?)""",
                (entry_id, next_rev, row["snapshot_digest"], actor["id"], actor["name"],
                 review_state, note, now),
            )
            self.conn.execute("UPDATE spaces SET updated_at = ? WHERE id = ?", (now, space_id))
            self.conn.commit()
            updated = self.conn.execute("SELECT * FROM space_entries WHERE id = ?", (entry_id,)).fetchone()
            return self._entry_dict(updated)

    def withdraw_entry(self, actor: dict, space_id: str, entry_id: str, *, base_revision: int = 0) -> dict:
        now = _now_iso()
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM space_entries WHERE id = ? AND space_id = ?",
                (entry_id, space_id),
            ).fetchone()
            if not row:
                raise HubDbError("条目不存在。", status=404, code="not_found")
            if actor["role"] != "admin":
                space = self.conn.execute("SELECT status FROM spaces WHERE id = ?", (space_id,)).fetchone()
                if not space or space["status"] != "active":
                    raise HubDbError("该共享空间已截止或归档，无法撤回条目。", status=423, code="space_closed")
                if row["owner_user_id"] != actor["id"]:
                    raise HubDbError("无权撤回其他成员的条目。", status=403, code="forbidden")
                if row["review_state"] == "locked":
                    raise HubDbError("该条目已被审核锁定，无法撤回。", status=423, code="entry_locked")
            cur_rev = int(row["revision"])
            if base_revision and base_revision != cur_rev:
                raise HubDbError(
                    f"条目版本已更新至 v{cur_rev}，请先刷新后再撤回。",
                    status=409,
                    code="revision_conflict",
                    extra={"server_revision": cur_rev, "server_entry": self._entry_dict(row)},
                )
            next_rev = cur_rev + 1
            self.conn.execute(
                """UPDATE space_entries
                      SET revision = ?, review_state = 'withdrawn',
                          updated_by_user_id = ?, updated_by_name = ?, updated_at = ?
                    WHERE id = ?""",
                (next_rev, actor["id"], actor["name"], now, entry_id),
            )
            self.conn.execute("DELETE FROM invoice_registry WHERE entry_id = ?", (entry_id,))
            self.conn.execute(
                """INSERT INTO entry_revisions(entry_id, revision, snapshot_digest, actor_user_id,
                                               actor_name, action, review_state, review_note, created_at)
                   VALUES(?,?,?,?,?,'withdraw','withdrawn','',?)""",
                (entry_id, next_rev, row["snapshot_digest"], actor["id"], actor["name"], now),
            )
            self.conn.execute("UPDATE spaces SET updated_at = ? WHERE id = ?", (now, space_id))
            self.conn.commit()
            updated = self.conn.execute("SELECT * FROM space_entries WHERE id = ?", (entry_id,)).fetchone()
            return self._entry_dict(updated)

"""Shared-batch association and near-realtime background synchronization service."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..adapters.service import AdapterService
from ..db.attachments import AttachmentRepo
from ..db.batches import BatchRepo
from ..db.entries import EntryRepo
from ..db.paths import DataRoot
from . import bindle as storage
from . import hub_sync
from .entry_merge import build_entry_snapshot, record_exchange_base, snapshot_digest

HUB_CONFIG_META_KEY = "tidoc.hub.config"
HUB_TOKEN_META_KEY = "tidoc.hub.token"
HUB_BATCH_LINKS_META_KEY = "tidoc.hub.batchLinks"

AUTO_SYNC_INTERVAL_SECONDS = 5.0
MAX_AUTO_SYNC_BACKOFF_SECONDS = 60.0
SELECTIVE_FETCH_LIMIT = 8


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _resolve_pull_scheme_id(adapters: AdapterService, batch_scheme_id: str = "") -> str:
    """Resolve the local scheme bound to newly pulled entries in a shared batch."""
    candidate = str(batch_scheme_id or "").strip()
    if candidate:
        try:
            scheme = adapters.get_scheme(candidate)
            if not scheme.get("disabled"):
                return str(scheme.get("id") or "")
        except ValueError:
            pass
    try:
        scheme_id, _revision_id = adapters.default_binding()
        return str(scheme_id or "")
    except ValueError:
        return ""


class _WorkerDatabase:
    """Short-lived SQLite connection wrapper for background sync steps."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.adapter_service = None
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")

    @contextmanager
    def transaction(self):
        with self._lock:
            nested = self.conn.in_transaction
            name = "tidoc_w_" + uuid.uuid4().hex
            self.conn.execute("SAVEPOINT " + name if nested else "BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("RELEASE SAVEPOINT " + name) if nested else self.conn.commit()
            except BaseException:
                if nested:
                    self.conn.execute("ROLLBACK TO SAVEPOINT " + name)
                    self.conn.execute("RELEASE SAVEPOINT " + name)
                else:
                    self.conn.rollback()
                raise

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


def _meta_get(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"] or "") if row else ""


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _read_hub_credentials(conn: sqlite3.Connection) -> tuple[bool, str, str, str, str]:
    raw_cfg = _meta_get(conn, HUB_CONFIG_META_KEY)
    try:
        cfg = json.loads(raw_cfg) if raw_cfg else {}
    except ValueError:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    token = _meta_get(conn, HUB_TOKEN_META_KEY).strip()
    server_url = str(cfg.get("server_url") or "").strip()
    user_id = str(cfg.get("user_id") or "").strip()
    role = str(cfg.get("role") or "member").strip() or "member"
    connected = bool(server_url and token and user_id)
    return connected, server_url, user_id, token, role


def _existing_batches_map(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT id, name, archived FROM batches").fetchall()
    return {
        str(r["id"]): {
            "id": str(r["id"]),
            "name": str(r["name"] or ""),
            "archived": bool(r["archived"]),
        }
        for r in rows
    }


def _read_batch_links(conn: sqlite3.Connection) -> tuple[dict[str, dict], bool]:
    raw = _meta_get(conn, HUB_BATCH_LINKS_META_KEY)
    try:
        doc = json.loads(raw) if raw else {}
    except ValueError:
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    raw_batches = doc.get("batches")
    if not isinstance(raw_batches, dict):
        return {}, False
    existing = _existing_batches_map(conn)
    cleaned: dict[str, dict] = {}
    pruned = False
    for batch_id, rec in raw_batches.items():
        bid = str(batch_id or "")
        if bid not in existing or not isinstance(rec, dict):
            pruned = True
            continue
        item = dict(rec)
        if item.get("last_state") == "syncing":
            item["last_state"] = "synced" if item.get("last_sync") else "manual"
        cleaned[bid] = item
    return cleaned, pruned


def _write_batch_links(conn: sqlite3.Connection, batches: dict[str, dict], *, commit: bool = True) -> None:
    payload = {"version": 1, "batches": batches}
    _meta_set(conn, HUB_BATCH_LINKS_META_KEY, json.dumps(payload, ensure_ascii=False))
    if commit:
        conn.commit()


def _has_hub_entry_links_table(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_entry_links'"
        ).fetchone()
        is not None
    )


def linked_batches_for_entries(conn: sqlite3.Connection, entry_ids: list[str]) -> list[str]:
    """Return linked local batch IDs whose remote space matches any of the given entries."""
    ids = list(dict.fromkeys(str(e or "") for e in (entry_ids or []) if e))
    if not ids or not _has_hub_entry_links_table(conn):
        return []
    links, _ = _read_batch_links(conn)
    if not links:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""SELECT server_url, space_id, remote_entry_id
              FROM hub_entry_links
             WHERE entry_id IN ({placeholders}) AND remote_entry_id <> ''""",
        ids,
    ).fetchall()
    if not rows:
        return []
    targets = {(str(r["server_url"] or ""), str(r["space_id"] or "")) for r in rows}
    return [
        bid
        for bid, rec in links.items()
        if (str(rec.get("server_url") or ""), str(rec.get("space_id") or "")) in targets
    ]


def record_deleted_remote_entries(conn: sqlite3.Connection, entry_ids: list[str]) -> list[str]:
    """Persist tombstones in linked-batch metadata before entries are deleted locally.

    Called inside the caller's DB transaction before ``DELETE FROM entries`` so
    subsequent sync ticks do not silently resurrect locally deleted shared entries.
    """
    ids = list(dict.fromkeys(str(e or "") for e in (entry_ids or []) if e))
    if not ids or not _has_hub_entry_links_table(conn):
        return []
    links, _ = _read_batch_links(conn)
    if not links:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""SELECT entry_id, server_url, space_id, remote_entry_id, remote_revision
              FROM hub_entry_links
             WHERE entry_id IN ({placeholders}) AND remote_entry_id <> ''""",
        ids,
    ).fetchall()
    if not rows:
        return []
    deleted_id_set = set(ids)
    now = _now_iso()
    affected: list[str] = []
    for bid, rec in links.items():
        rec_url = str(rec.get("server_url") or "")
        rec_space = str(rec.get("space_id") or "")
        changed = False
        deleted_map = dict(rec.get("deleted_remote_entries") or {})
        entry_states = dict(rec.get("entry_sync_states") or {})
        pull_conflicts = dict(rec.get("pull_conflicts") or {})
        suppressed = dict(rec.get("suppressed_conflicts") or {})
        for r in rows:
            if str(r["server_url"] or "") != rec_url or str(r["space_id"] or "") != rec_space:
                continue
            eid = str(r["entry_id"] or "")
            rid = str(r["remote_entry_id"] or "")
            rev = int(r["remote_revision"] or 0)
            if rid:
                prev_tomb = deleted_map.get(rid) or {}
                prev_rev = int(prev_tomb.get("revision") or 0)
                deleted_map[rid] = {"revision": max(prev_rev, rev), "deleted_at": now}
                pull_conflicts.pop(rid, None)
                changed = True
            if eid:
                entry_states.pop(eid, None)
                suppressed.pop(eid, None)
        for eid in deleted_id_set:
            if eid in entry_states:
                entry_states.pop(eid, None)
                changed = True
            if eid in suppressed:
                suppressed.pop(eid, None)
                changed = True
        for rid, pc in list(pull_conflicts.items()):
            if str((pc or {}).get("local_entry_id") or "") in deleted_id_set:
                pull_conflicts.pop(rid, None)
                changed = True
        if changed:
            rec["deleted_remote_entries"] = deleted_map
            rec["entry_sync_states"] = entry_states
            rec["pull_conflicts"] = pull_conflicts
            rec["suppressed_conflicts"] = suppressed
            other_sup = [s for s in suppressed.values() if (s or {}).get("reason") == "other_space"]
            real_sup = [s for s in suppressed.values() if (s or {}).get("reason") != "other_space"]
            rec["conflict_count"] = len(pull_conflicts) + len(real_sup)
            rec["other_space_count"] = len(other_sup)
            rec["other_space_names"] = sorted(
                {str(s.get("other_space_name") or "") for s in other_sup if s.get("other_space_name")}
            )
            affected.append(bid)
    if affected:
        _write_batch_links(conn, links, commit=False)
    return affected


def _count_other_space_entries(
    conn: sqlite3.Connection, batch_id: str, server_url: str, space_id: str
) -> tuple[int, list[str]]:
    if not batch_id or not server_url or not _has_hub_entry_links_table(conn):
        return 0, []
    rows = conn.execute(
        """SELECT h.space_name
             FROM batch_entries be
             JOIN hub_entry_links h ON h.entry_id = be.entry_id
            WHERE be.batch_id = ?
              AND h.server_url = ?
              AND h.space_id <> ''
              AND h.space_id <> ?
              AND COALESCE(h.review_state, '') <> 'withdrawn'""",
        (batch_id, server_url, space_id),
    ).fetchall()
    names = sorted({str(r["space_name"] or "").strip() for r in rows if str(r["space_name"] or "").strip()})
    return len(rows), names


def _format_other_space_message(count: int, names: list[str]) -> str:
    if count <= 0:
        return ""
    if names:
        shown = "、".join(names[:2])
        suffix = "等" if len(names) > 2 else ""
        return f"{count} 条仍关联「{shown}」{suffix}，未上传到当前批次"
    return f"{count} 条仍关联其他共享批次，未上传到当前批次"


def _bundle_sync_digest(
    payload: dict,
    snap: dict,
    profile_dict: dict,
    invoice_no: str,
    invoice_sha256: str,
) -> str:
    att_meta = [
        [
            str(att.get("sha256") or ""),
            str(att.get("type") or ""),
            str(att.get("role_id") or ""),
            str(att.get("recognition_version") or ""),
            str(att.get("recognition_status") or ""),
            str(att.get("recognized_value") or ""),
            str(att.get("recognition_message") or ""),
        ]
        for att in (payload.get("attachments") or [])
    ]
    material = {
        "snapshot": snap,
        "profile": {
            "id": str(profile_dict.get("id") or ""),
            "name": str(profile_dict.get("name") or ""),
            "reviewer": str(profile_dict.get("reviewer") or ""),
        },
        "invoice_no": str(invoice_no or ""),
        "invoice_sha256": str(invoice_sha256 or ""),
        "att_meta": att_meta,
        "check_message": str(payload.get("check_message") or ""),
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_REPORT_STATUS_PRIORITY = {
    "error": 0,
    "pull_conflict": 1,
    "conflict": 2,
    "locked": 3,
    "duplicate": 4,
    "other_space": 5,
    "space_closed": 6,
    "stopped": 7,
    "pending": 8,
    "uploading": 9,
    "pushed": 10,
    "pulled_new": 11,
    "pulled_merged": 12,
    "status_updated": 13,
    "withdrawn": 14,
}
_ATTENTION_REPORT_STATUSES = {
    "error",
    "pull_conflict",
    "conflict",
    "locked",
    "duplicate",
    "other_space",
    "space_closed",
    "stopped",
    "pending",
}


def _entry_display_fields(source: dict | None) -> dict[str, str]:
    data = source or {}
    seller = str(data.get("seller") or "").strip()
    invoice_no = str(data.get("invoice_no") or "").strip()
    title = str(data.get("title") or "").strip()
    total_raw = data.get("total")
    total = "" if total_raw is None else str(total_raw).strip()
    head = seller or title
    label = " · ".join(p for p in (head, invoice_no) if p) or "未命名条目"
    return {
        "seller": seller,
        "invoice_no": invoice_no,
        "title": title,
        "total": total,
        "label": label,
    }


def _load_entry_meta_map(conn: sqlite3.Connection, entry_ids: list[str]) -> dict[str, dict[str, str]]:
    ids = list(dict.fromkeys(str(eid or "").strip() for eid in entry_ids if str(eid or "").strip()))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, seller, invoice_no, title, total FROM entries WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    return {str(r["id"]): _entry_display_fields(dict(r)) for r in rows}


def _map_suppressed_reason(reason: str) -> str:
    r = str(reason or "").strip()
    if r in ("locked", "entry_locked"):
        return "locked"
    if r == "duplicate_invoice":
        return "duplicate"
    if r == "other_space":
        return "other_space"
    return "conflict"


def _build_report_payload(
    items: list[dict[str, Any]],
    unchanged_count: int,
    *,
    updated_at: str = "",
) -> dict[str, Any]:
    counts: dict[str, int] = {
        "pushed": 0,
        "pulled": 0,
        "status_updated": 0,
        "unchanged": max(0, int(unchanged_count or 0)),
        "conflict": 0,
        "locked": 0,
        "duplicate": 0,
        "other_space": 0,
        "space_closed": 0,
        "stopped": 0,
        "pending": 0,
        "error": 0,
        "withdrawn": 0,
    }
    deduped: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for raw in items:
        item = dict(raw)
        st = str(item.get("status") or "error")
        key = (str(item.get("entry_id") or ""), str(item.get("remote_entry_id") or ""), st)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(item)
        if st == "pushed":
            counts["pushed"] += 1
        elif st in ("pulled_new", "pulled_merged"):
            counts["pulled"] += 1
        elif st == "status_updated":
            counts["status_updated"] += 1
        elif st in ("pull_conflict", "conflict"):
            counts["conflict"] += 1
        elif st in counts:
            counts[st] += 1
    deduped.sort(key=lambda it: _REPORT_STATUS_PRIORITY.get(str(it.get("status") or ""), 99))
    issue_count = sum(1 for it in deduped if str(it.get("status") or "") in _ATTENTION_REPORT_STATUSES)
    return {
        "counts": counts,
        "issue_count": issue_count,
        "unchanged_count": counts["unchanged"],
        "items": deduped,
        "updated_at": updated_at or _now_iso(),
    }


def _build_shared_info(
    record: dict,
    *,
    archived: bool,
    connected: bool,
    cur_server_url: str,
    cur_user_id: str,
    runtime: dict | None = None,
) -> dict:
    rt = runtime or {}
    rec_url = str(record.get("server_url") or "")
    rec_uid = str(record.get("user_id") or "")
    account_active = bool(connected and rec_url == cur_server_url and rec_uid == cur_user_id)
    auto_sync = bool(record.get("auto_sync"))
    last_sync = str(record.get("last_sync") or "")
    conflict_count = int(rt.get("conflict_count", record.get("conflict_count") or 0))
    other_space_count = int(rt.get("other_space_count", record.get("other_space_count") or 0))
    other_space_names = list(rt.get("other_space_names", record.get("other_space_names") or []))
    needs_upgrade = bool(rt.get("needs_upgrade", record.get("needs_upgrade") or False))
    checking = bool(rt.get("queued") or rt.get("running"))
    stopping = bool(rt.get("stopping"))

    if not connected:
        state = "paused"
        auto_sync = False
        message = str(record.get("last_message") or "未连接共享区，同步已暂停")
    elif not account_active:
        state = "paused"
        auto_sync = False
        message = str(record.get("last_message") or "共享区账号或服务器已变更，原关联已停用")
    elif archived:
        state = "paused"
        message = "批次已归档，同步已暂停"
    elif checking and rt.get("visible_syncing"):
        state = "syncing"
        message = str(rt.get("message") or "正在同步…")
    elif rt.get("user_stopped"):
        state = "stopped"
        message = str(rt.get("message") or "已停止同步")
    else:
        state = str(rt.get("state") or record.get("last_state") or ("synced" if last_sync else "manual"))
        if state == "syncing":
            state = "synced" if last_sync else "manual"
        message = str(rt.get("message") if "message" in rt else (record.get("last_message") or ""))

    report_src = rt.get("report") if isinstance(rt.get("report"), dict) else (record.get("last_report") or {})
    report_counts = dict((report_src or {}).get("counts") or {})
    issue_count = int(
        (report_src or {}).get("issue_count")
        if (report_src or {}).get("issue_count") is not None
        else (conflict_count + other_space_count)
    )
    can_stop = bool(connected and account_active and not archived and checking and rt.get("visible_syncing"))
    can_resume = bool(
        connected
        and account_active
        and not archived
        and not checking
        and (state == "stopped" or rt.get("user_stopped") or (state == "error" and report_counts.get("pending", 0) > 0))
    )

    return {
        "space_id": str(record.get("space_id") or ""),
        "space_name": str(record.get("space_name") or ""),
        "server_url": rec_url,
        "user_id": rec_uid,
        "auto_sync": auto_sync,
        "state": state,
        "message": message,
        "last_sync": last_sync,
        "conflict_count": conflict_count,
        "other_space_count": other_space_count,
        "other_space_names": other_space_names,
        "needs_upgrade": needs_upgrade,
        "checking": checking,
        "stopping": stopping,
        "can_stop": can_stop,
        "can_resume": can_resume,
        "progress": copy.deepcopy(rt.get("progress")) if (checking and rt.get("visible_syncing") and rt.get("progress")) else None,
        "issue_count": issue_count,
        "report_counts": report_counts,
    }


def _is_unsupported_sync_state_error(client: hub_sync.HubClient, exc: hub_sync.HubError) -> bool:
    if exc.status not in (404, 405) or exc.code not in ("", "not_found", "method_not_allowed"):
        return False
    if exc.code == "space_not_found":
        return False
    try:
        info = client.info()
        features = info.get("features") or []
        return "space_sync_state" not in features
    except hub_sync.HubError:
        return exc.status == 405


def _server_supports_sync_state(client: hub_sync.HubClient) -> bool | None:
    try:
        info = client.info()
        return "space_sync_state" in (info.get("features") or [])
    except hub_sync.HubError:
        return None


def _compute_entry_sync_state(
    entries_repo: EntryRepo,
    attachments_repo: AttachmentRepo,
    adapter_service,
    entry_id: str,
    remote_revision: int,
) -> dict | None:
    try:
        payload, snap, _blobs, profile_dict, _anc, invoice_sha256 = hub_sync._build_hub_entry_bundle(
            entries_repo, attachments_repo, entry_id, adapter_service=adapter_service
        )
    except hub_sync.HubError:
        return None
    return {
        "bundle_digest": _bundle_sync_digest(
            payload, snap, profile_dict, str(payload.get("invoice_no") or ""), invoice_sha256
        ),
        "snapshot_digest": snapshot_digest(snap),
        "remote_revision": int(remote_revision or 0),
    }


class HubBatchSyncManager:
    """Coordinates 1-to-1 batch<->space links and non-blocking near-realtime sync."""

    def __init__(
        self,
        db_path: str | Path,
        data_root: DataRoot,
        *,
        interval_seconds: float = AUTO_SYNC_INTERVAL_SECONDS,
        max_backoff_seconds: float = MAX_AUTO_SYNC_BACKOFF_SECONDS,
        payment_ai_flags_getter: Callable[[], tuple[bool, bool]] | None = None,
    ):
        self._db_path = Path(db_path)
        self._data_root = data_root
        self._interval_seconds = float(interval_seconds)
        self._max_backoff_seconds = float(max_backoff_seconds)
        self._payment_ai_flags_getter = payment_ai_flags_getter

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._worker_client: hub_sync.HubClient | None = None

        self._generation = 0
        self._account_epoch = 0
        self._batch_epochs: dict[str, int] = {}
        self._queue: deque[tuple[str, bool]] = deque()
        self._queued_manual: dict[str, bool] = {}
        self._running_batch: str = ""
        self._rerun_manual: dict[str, bool] = {}
        self._user_stop_requested: set[str] = set()
        self._runtime: dict[str, dict[str, Any]] = {}

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def bump_generation(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation

    def stop(self, *, timeout: float = 5.0) -> None:
        with self._lock:
            self._stop_event.set()
            self._account_epoch += 1
            self._queue.clear()
            self._queued_manual.clear()
            self._rerun_manual.clear()
            worker = self._worker
            self._cond.notify_all()
        if worker is not None and worker is not threading.current_thread() and worker.is_alive():
            worker.join(timeout=timeout)
        with self._lock:
            client = self._worker_client
            self._worker_client = None
            if worker is None or not worker.is_alive():
                self._worker = None
        if client is not None:
            client.close()

    def _close_worker_client(self) -> None:
        with self._lock:
            client = self._worker_client
            self._worker_client = None
        if client is not None:
            client.close()

    def _get_worker_client(self, server_url: str, token: str) -> hub_sync.HubClient:
        with self._lock:
            client = self._worker_client
            if client is not None and client.base_url == server_url and client.token == token:
                return client
            old = self._worker_client
            client = hub_sync.HubClient(server_url, token)
            self._worker_client = client
        if old is not None:
            old.close()
        return client

    def _ensure_worker_locked(self) -> None:
        if self._stop_event.is_set():
            return
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="tidoc-hub-batch-sync",
        )
        self._worker.start()

    def _enqueue_locked(self, batch_id: str, *, manual: bool) -> None:
        self._user_stop_requested.discard(batch_id)
        if self._running_batch == batch_id:
            prev = self._rerun_manual.get(batch_id, False)
            self._rerun_manual[batch_id] = prev or manual
        elif batch_id in self._queued_manual:
            self._queued_manual[batch_id] = self._queued_manual[batch_id] or manual
        else:
            self._queued_manual[batch_id] = manual
            self._queue.append((batch_id, manual))
        rt = self._runtime.setdefault(batch_id, {})
        rt["queued"] = True
        rt["user_stopped"] = False
        rt["stopping"] = False
        if manual:
            rt["visible_syncing"] = True
            rt["state"] = "syncing"
            rt["message"] = "正在同步…"
            rt["error_count"] = 0
            rt["next_due_at"] = 0.0
        self._ensure_worker_locked()
        self._cond.notify_all()

    def _batch_epoch(self, batch_id: str) -> int:
        with self._lock:
            return self._batch_epochs.get(batch_id, 0)

    def _bump_batch_epoch_locked(self, batch_id: str) -> int:
        nxt = self._batch_epochs.get(batch_id, 0) + 1
        self._batch_epochs[batch_id] = nxt
        return nxt

    # ------------------------------------------------------------------ Account & manual hooks
    def on_disconnected(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            self._account_epoch += 1
            for bid in list(self._batch_epochs) + list(self._runtime):
                self._bump_batch_epoch_locked(bid)
            self._queue.clear()
            self._queued_manual.clear()
            self._rerun_manual.clear()
            self._user_stop_requested.clear()
            self._runtime.clear()
            old_client = self._worker_client
            self._worker_client = None
        if old_client is not None:
            old_client.close()

        links, _ = _read_batch_links(conn)
        if links:
            for rec in links.values():
                rec["auto_sync"] = False
                rec["last_state"] = "paused"
                rec["last_message"] = "已断开共享区连接"
            _write_batch_links(conn, links, commit=True)

    def on_account_changed(self, conn: sqlite3.Connection, new_server_url: str, new_user_id: str) -> None:
        with self._lock:
            self._account_epoch += 1
            for bid in list(self._batch_epochs) + list(self._runtime):
                self._bump_batch_epoch_locked(bid)
            self._queue.clear()
            self._queued_manual.clear()
            self._rerun_manual.clear()
            self._user_stop_requested.clear()
            self._runtime.clear()
            old_client = self._worker_client
            self._worker_client = None
        if old_client is not None:
            old_client.close()

        links, pruned = _read_batch_links(conn)
        changed = pruned
        for rec in links.values():
            if rec.get("server_url") != new_server_url or rec.get("user_id") != new_user_id:
                rec["auto_sync"] = False
                rec["last_state"] = "paused"
                rec["last_message"] = "已切换共享区账号，原关联已停用"
                changed = True
            else:
                if rec.get("last_state") == "paused":
                    rec["last_state"] = "synced" if rec.get("last_sync") else "manual"
                    rec["last_message"] = ""
                    changed = True
        if changed:
            _write_batch_links(conn, links, commit=True)

    def on_entries_deleted(self, batch_ids: list[str]) -> None:
        if not batch_ids:
            return
        with self._lock:
            for bid in batch_ids:
                self._bump_batch_epoch_locked(str(bid))

    def on_manual_pull_applied(
        self,
        entries_repo: EntryRepo,
        attachments_repo: AttachmentRepo,
        adapter_service,
        server_url: str,
        user_id: str,
        space_id: str,
        result: dict,
    ) -> None:
        changed_entries = (
            int(result.get("imported") or 0)
            + int(result.get("updated") or 0)
            + int(result.get("status_updated") or 0)
            + int(result.get("withdrawn_updated") or 0)
        )
        if changed_entries > 0:
            self.bump_generation()

        conn = entries_repo.db.conn
        links, pruned = _read_batch_links(conn)
        updated_meta = pruned
        imported_set = set(result.get("entry_ids") or [])
        updated_set = set(result.get("updated_entry_ids") or [])
        remote_snap_digests = dict(result.get("remote_snapshot_digests") or {})
        affected_ids = list(
            dict.fromkeys(
                (result.get("entry_ids") or [])
                + (result.get("updated_entry_ids") or [])
                + (result.get("status_updated_entry_ids") or [])
            )
        )
        link_rows = {
            eid: hub_sync.get_hub_link(conn, eid)
            for eid in affected_ids
        }
        now = _now_iso()
        for bid, rec in links.items():
            if rec.get("server_url") != server_url or rec.get("user_id") != user_id or rec.get("space_id") != space_id:
                continue
            entry_states = dict(rec.get("entry_sync_states") or {})
            pull_conflicts = dict(rec.get("pull_conflicts") or {})
            suppressed = dict(rec.get("suppressed_conflicts") or {})
            deleted_remote = dict(rec.get("deleted_remote_entries") or {})
            for eid in affected_ids:
                lnk = link_rows.get(eid)
                rev = int(lnk["remote_revision"] or 0) if lnk else 0
                if lnk and lnk.get("remote_entry_id"):
                    rid = str(lnk["remote_entry_id"])
                    pull_conflicts.pop(rid, None)
                    deleted_remote.pop(rid, None)
                suppressed.pop(eid, None)
                if eid in imported_set:
                    st = _compute_entry_sync_state(entries_repo, attachments_repo, adapter_service, eid, rev)
                    if st is not None:
                        entry_states[eid] = {
                            "bundle_digest": st["bundle_digest"],
                            "remote_revision": rev,
                            "imported_only": True,
                        }
                elif eid in updated_set:
                    st = _compute_entry_sync_state(entries_repo, attachments_repo, adapter_service, eid, rev)
                    r_snap = str(remote_snap_digests.get(eid) or "")
                    if st is not None and r_snap and st["snapshot_digest"] == r_snap:
                        entry_states[eid] = {
                            "bundle_digest": st["bundle_digest"],
                            "remote_revision": rev,
                        }
                    else:
                        # Merged entry still holds local-only edits not yet on remote; allow push.
                        entry_states.pop(eid, None)
            for w in result.get("withdrawn_local") or []:
                rid = str(w.get("remote_entry_id") or "")
                lid = str(w.get("local_entry_id") or "")
                if rid:
                    pull_conflicts.pop(rid, None)
                    deleted_remote.pop(rid, None)
                if lid:
                    suppressed.pop(lid, None)
            rec["entry_sync_states"] = entry_states
            rec["pull_conflicts"] = pull_conflicts
            rec["suppressed_conflicts"] = suppressed
            rec["deleted_remote_entries"] = deleted_remote
            rec["last_remote_version"] = ""
            rec["last_sync"] = now
            rec.pop("last_report", None)
            other_sup = [s for s in suppressed.values() if (s or {}).get("reason") == "other_space"]
            real_sup = [s for s in suppressed.values() if (s or {}).get("reason") != "other_space"]
            remaining = len(pull_conflicts) + len(real_sup)
            other_cnt = len(other_sup)
            other_names = sorted({str(s.get("other_space_name") or "") for s in other_sup if s.get("other_space_name")})
            rec["conflict_count"] = remaining
            rec["other_space_count"] = other_cnt
            rec["other_space_names"] = other_names
            if remaining == 0:
                if other_cnt > 0:
                    rec["last_state"] = "partial"
                    rec["last_message"] = _format_other_space_message(other_cnt, other_names)
                else:
                    rec["last_state"] = "synced"
                    rec["last_message"] = str(result.get("message") or "已同步")
            updated_meta = True
            with self._lock:
                rt = self._runtime.setdefault(bid, {})
                rt.pop("report", None)
                if not (rt.get("queued") or rt.get("running")):
                    rt["state"] = rec["last_state"]
                    rt["message"] = rec["last_message"]
                    rt["conflict_count"] = remaining
                    rt["other_space_count"] = other_cnt
                    rt["other_space_names"] = other_names
                    rt["next_due_at"] = time.monotonic() + self._interval_seconds
        if updated_meta:
            _write_batch_links(conn, links, commit=True)

    def on_manual_push_applied(
        self,
        entries_repo: EntryRepo,
        attachments_repo: AttachmentRepo,
        adapter_service,
        server_url: str,
        user_id: str,
        space_id: str,
        pushed_items: list[dict],
    ) -> None:
        if not pushed_items:
            return
        conn = entries_repo.db.conn
        links, pruned = _read_batch_links(conn)
        updated_meta = pruned
        for bid, rec in links.items():
            if rec.get("server_url") != server_url or rec.get("user_id") != user_id or rec.get("space_id") != space_id:
                continue
            entry_states = dict(rec.get("entry_sync_states") or {})
            pull_conflicts = dict(rec.get("pull_conflicts") or {})
            suppressed = dict(rec.get("suppressed_conflicts") or {})
            for item in pushed_items:
                eid = str(item.get("entry_id") or "")
                rid = str(item.get("remote_entry_id") or "")
                rev = int(item.get("revision") or 0)
                if not eid:
                    continue
                if rid:
                    pull_conflicts.pop(rid, None)
                suppressed.pop(eid, None)
                st = _compute_entry_sync_state(entries_repo, attachments_repo, adapter_service, eid, rev)
                if st is not None:
                    entry_states[eid] = st
            rec["entry_sync_states"] = entry_states
            rec["pull_conflicts"] = pull_conflicts
            rec["suppressed_conflicts"] = suppressed
            rec["last_remote_version"] = ""
            rec.pop("last_report", None)
            other_sup = [s for s in suppressed.values() if (s or {}).get("reason") == "other_space"]
            real_sup = [s for s in suppressed.values() if (s or {}).get("reason") != "other_space"]
            remaining = len(pull_conflicts) + len(real_sup)
            other_cnt = len(other_sup)
            other_names = sorted({str(s.get("other_space_name") or "") for s in other_sup if s.get("other_space_name")})
            rec["conflict_count"] = remaining
            rec["other_space_count"] = other_cnt
            rec["other_space_names"] = other_names
            if remaining == 0 and rec.get("last_state") == "conflict":
                rec["last_state"] = "partial" if other_cnt > 0 else "synced"
                rec["last_message"] = _format_other_space_message(other_cnt, other_names) if other_cnt > 0 else "已同步"
            updated_meta = True
            with self._lock:
                rt = self._runtime.setdefault(bid, {})
                rt.pop("report", None)
                if not (rt.get("queued") or rt.get("running")):
                    rt["conflict_count"] = remaining
                    rt["other_space_count"] = other_cnt
                    rt["other_space_names"] = other_names
                    if remaining == 0 and rt.get("state") == "conflict":
                        rt["state"] = rec["last_state"]
                        rt["message"] = rec["last_message"]
        if updated_meta:
            _write_batch_links(conn, links, commit=True)

    # ------------------------------------------------------------------ Public / Bridge operations
    def batch_links(self, conn: sqlite3.Connection) -> dict:
        connected, cur_url, cur_uid, _tok, _role = _read_hub_credentials(conn)
        existing = _existing_batches_map(conn)
        links, pruned = _read_batch_links(conn)
        if pruned:
            _write_batch_links(conn, links, commit=True)
        with self._lock:
            out: dict[str, dict] = {}
            for bid, rec in links.items():
                batch_info = existing.get(bid)
                if not batch_info:
                    continue
                out[bid] = _build_shared_info(
                    rec,
                    archived=batch_info["archived"],
                    connected=connected,
                    cur_server_url=cur_url,
                    cur_user_id=cur_uid,
                    runtime=self._runtime.get(bid),
                )
        return {"batches": out}

    def attach_to_batches(self, conn: sqlite3.Connection, batches: list[dict]) -> list[dict]:
        if not batches:
            return batches
        links_map = self.batch_links(conn)["batches"]
        for batch in batches:
            if isinstance(batch, dict) and batch.get("id"):
                batch["shared_batch"] = links_map.get(batch["id"])
        return batches

    def unlink_batch(self, conn: sqlite3.Connection, batch_id: str) -> dict:
        bid = str(batch_id or "").strip()
        if not bid:
            raise ValueError("请指定要解除关联的批次。")
        links, pruned = _read_batch_links(conn)
        removed = links.pop(bid, None)
        if removed is not None or pruned:
            _write_batch_links(conn, links, commit=True)
        with self._lock:
            self._bump_batch_epoch_locked(bid)
            self._user_stop_requested.discard(bid)
            if bid in self._queued_manual:
                del self._queued_manual[bid]
                self._queue = deque((b, m) for b, m in self._queue if b != bid)
            self._rerun_manual.pop(bid, None)
            self._runtime.pop(bid, None)
        return {"batch_id": bid, "unlinked": True}

    def set_batch_auto_sync(self, conn: sqlite3.Connection, batch_id: str, enabled: bool) -> dict:
        bid = str(batch_id or "").strip()
        if not bid:
            raise ValueError("请指定要设置的批次。")
        existing = _existing_batches_map(conn)
        batch_info = existing.get(bid)
        if not batch_info:
            raise ValueError("报账批次不存在。")
        links, pruned = _read_batch_links(conn)
        rec = links.get(bid)
        if not rec:
            if pruned:
                _write_batch_links(conn, links, commit=True)
            raise ValueError("该批次尚未关联共享批次。")
        connected, cur_url, cur_uid, _tok, _role = _read_hub_credentials(conn)
        flag = bool(enabled)
        if flag:
            if not connected or rec.get("server_url") != cur_url or rec.get("user_id") != cur_uid:
                raise ValueError("当前未连接到该共享批次对应的账号，无法开启自动同步。")
            if batch_info["archived"]:
                raise ValueError("已归档的批次已暂停同步，请先恢复批次后再开启自动同步。")
            rec["auto_sync"] = True
            rec["needs_upgrade"] = False
            if rec.get("last_state") in ("error", "stopped"):
                rec["last_state"] = "synced" if rec.get("last_sync") else "manual"
                rec["last_message"] = ""
            _write_batch_links(conn, links, commit=True)
            with self._lock:
                self._user_stop_requested.discard(bid)
                rt = self._runtime.setdefault(bid, {})
                rt["error_count"] = 0
                rt["needs_upgrade"] = False
                rt["user_stopped"] = False
                rt["stopping"] = False
                rt["next_due_at"] = 0.0
                if rt.get("state") in ("error", "stopped"):
                    rt["state"] = rec["last_state"]
                    rt["message"] = ""
        else:
            rec["auto_sync"] = False
            if rec.get("last_state") in ("syncing", "error"):
                rec["last_state"] = "synced" if rec.get("last_sync") else "manual"
                rec["last_message"] = ""
            _write_batch_links(conn, links, commit=True)
            with self._lock:
                self._bump_batch_epoch_locked(bid)
                self._user_stop_requested.discard(bid)
                if bid in self._queued_manual and not self._queued_manual[bid]:
                    del self._queued_manual[bid]
                    self._queue = deque((b, m) for b, m in self._queue if b != bid)
                if self._rerun_manual.get(bid) is False:
                    self._rerun_manual.pop(bid, None)
                rt = self._runtime.get(bid)
                if rt is not None:
                    if not self._queued_manual.get(bid) and self._running_batch != bid:
                        rt["queued"] = False
                        rt["visible_syncing"] = False
                        rt["stopping"] = False
                        rt["progress"] = None
                        if rt.get("state") in ("syncing", "error"):
                            rt["state"] = rec["last_state"]
                            rt["message"] = rec["last_message"]
        with self._lock:
            info = _build_shared_info(
                rec,
                archived=batch_info["archived"],
                connected=connected,
                cur_server_url=cur_url,
                cur_user_id=cur_uid,
                runtime=self._runtime.get(bid),
            )
        return {"batch_id": bid, "shared": info}

    def sync_batch(self, conn: sqlite3.Connection, batch_id: str) -> dict:
        bid = str(batch_id or "").strip()
        if not bid:
            raise ValueError("请指定要同步的批次。")
        existing = _existing_batches_map(conn)
        batch_info = existing.get(bid)
        if not batch_info:
            raise ValueError("报账批次不存在。")
        if batch_info["archived"]:
            raise ValueError("该批次已归档，请先取消归档后再同步。")
        connected, cur_url, cur_uid, _tok, _role = _read_hub_credentials(conn)
        if not connected:
            raise ValueError("尚未连接到团队共享区，请先连接后再同步。")
        links, pruned = _read_batch_links(conn)
        rec = links.get(bid)
        if not rec:
            if pruned:
                _write_batch_links(conn, links, commit=True)
            raise ValueError("该批次尚未关联共享批次。")
        if rec.get("server_url") != cur_url or rec.get("user_id") != cur_uid:
            raise ValueError("该批次关联的是其他共享区账号，当前已停用，请重新关联。")

        if rec.get("needs_upgrade"):
            rec["needs_upgrade"] = False
            _write_batch_links(conn, links, commit=True)

        with self._lock:
            rt = self._runtime.setdefault(bid, {})
            rt["needs_upgrade"] = False
            rt["error_count"] = 0
            rt["user_stopped"] = False
            self._enqueue_locked(bid, manual=True)
            info = _build_shared_info(
                rec,
                archived=False,
                connected=True,
                cur_server_url=cur_url,
                cur_user_id=cur_uid,
                runtime=self._runtime.get(bid),
            )
        return {"batch_id": bid, "shared": info}

    def stop_batch_sync(self, conn: sqlite3.Connection, batch_id: str) -> dict:
        bid = str(batch_id or "").strip()
        if not bid:
            raise ValueError("请指定要停止的批次。")
        existing = _existing_batches_map(conn)
        batch_info = existing.get(bid)
        if not batch_info:
            raise ValueError("报账批次不存在。")
        connected, cur_url, cur_uid, _tok, _role = _read_hub_credentials(conn)
        links, pruned = _read_batch_links(conn)
        rec = links.get(bid)
        if not rec:
            if pruned:
                _write_batch_links(conn, links, commit=True)
            raise ValueError("该批次尚未关联共享批次。")

        persist_stopped = False
        with self._lock:
            rt = self._runtime.setdefault(bid, {})
            if self._running_batch == bid:
                self._user_stop_requested.add(bid)
                self._rerun_manual.pop(bid, None)
                rt["stopping"] = True
                rt["message"] = "正在完成当前条目，随后停止…"
            elif bid in self._queued_manual or rt.get("queued"):
                self._queued_manual.pop(bid, None)
                self._queue = deque((b, m) for b, m in self._queue if b != bid)
                self._rerun_manual.pop(bid, None)
                self._user_stop_requested.discard(bid)
                rt["queued"] = False
                rt["visible_syncing"] = False
                rt["stopping"] = False
                rt["user_stopped"] = True
                rt["progress"] = None
                rt["state"] = "stopped"
                rt["message"] = "已停止同步"
                persist_stopped = True
            info = _build_shared_info(
                rec,
                archived=batch_info["archived"],
                connected=connected,
                cur_server_url=cur_url,
                cur_user_id=cur_uid,
                runtime=rt,
            )
        if persist_stopped:
            rec["last_state"] = "stopped"
            rec["last_message"] = "已停止同步"
            links[bid] = rec
            _write_batch_links(conn, links, commit=True)
        return {"batch_id": bid, "shared": info}

    def batch_sync_report(self, conn: sqlite3.Connection, batch_id: str) -> dict:
        bid = str(batch_id or "").strip()
        if not bid:
            raise ValueError("请指定要查看的批次。")
        existing = _existing_batches_map(conn)
        batch_info = existing.get(bid)
        if not batch_info:
            raise ValueError("报账批次不存在。")
        connected, cur_url, cur_uid, _tok, _role = _read_hub_credentials(conn)
        links, pruned = _read_batch_links(conn)
        rec = links.get(bid)
        if not rec:
            if pruned:
                _write_batch_links(conn, links, commit=True)
            raise ValueError("该批次尚未关联共享批次。")

        total_row = conn.execute(
            "SELECT COUNT(*) AS c FROM batch_entries WHERE batch_id = ?", (bid,)
        ).fetchone()
        total_entries = int(total_row["c"] or 0) if total_row else 0

        with self._lock:
            rt = self._runtime.get(bid)
            rt_report = copy.deepcopy(rt.get("report")) if (rt and isinstance(rt.get("report"), dict)) else None
            shared = _build_shared_info(
                rec,
                archived=batch_info["archived"],
                connected=connected,
                cur_server_url=cur_url,
                cur_user_id=cur_uid,
                runtime=rt,
            )

        if rt_report is not None:
            report = rt_report
        else:
            stored = rec.get("last_report") if isinstance(rec.get("last_report"), dict) else None
            items: list[dict[str, Any]] = []
            seen_eids: set[str] = set()
            pull_conflicts = dict(rec.get("pull_conflicts") or {})
            suppressed = dict(rec.get("suppressed_conflicts") or {})
            conflict_eids = [str(pc.get("local_entry_id") or "") for pc in pull_conflicts.values() if pc]
            sup_eids = list(suppressed.keys())
            stored_items = list((stored or {}).get("items") or [])
            stored_eids = [str(it.get("entry_id") or "") for it in stored_items if it.get("entry_id")]
            meta_map = _load_entry_meta_map(conn, conflict_eids + sup_eids + stored_eids)

            if stored_items:
                for it in stored_items:
                    eid = str(it.get("entry_id") or "")
                    if eid and eid not in meta_map:
                        continue
                    item = dict(it)
                    if eid and eid in meta_map:
                        item.update(meta_map[eid])
                    items.append(item)
                    if eid:
                        seen_eids.add(eid)
            for rid, pc in pull_conflicts.items():
                eid = str((pc or {}).get("local_entry_id") or "")
                if eid and eid in seen_eids:
                    continue
                if eid and eid not in meta_map:
                    continue
                disp = meta_map.get(eid) or _entry_display_fields({})
                items.append({
                    "entry_id": eid,
                    "remote_entry_id": str(rid),
                    **disp,
                    "status": "pull_conflict",
                    "message": "双方均有修改，需核对共享变化",
                })
                if eid:
                    seen_eids.add(eid)
            for eid, sup in suppressed.items():
                if eid in seen_eids or eid not in meta_map:
                    continue
                sup_dict = sup or {}
                items.append({
                    "entry_id": eid,
                    "remote_entry_id": "",
                    **meta_map[eid],
                    "status": _map_suppressed_reason(str(sup_dict.get("reason") or "")),
                    "message": str(sup_dict.get("message") or "未上传"),
                })
                seen_eids.add(eid)

            if not items and _has_hub_entry_links_table(conn):
                space_id = str(rec.get("space_id") or "")
                other_rows = conn.execute(
                    """SELECT h.entry_id, h.space_name
                         FROM batch_entries be
                         JOIN hub_entry_links h ON h.entry_id = be.entry_id
                        WHERE be.batch_id = ?
                          AND h.server_url = ?
                          AND h.space_id <> ''
                          AND h.space_id <> ?
                          AND COALESCE(h.review_state, '') <> 'withdrawn'""",
                    (bid, cur_url, space_id),
                ).fetchall()
                if other_rows:
                    o_meta = _load_entry_meta_map(conn, [str(r["entry_id"]) for r in other_rows])
                    for r in other_rows:
                        eid = str(r["entry_id"])
                        if eid not in o_meta:
                            continue
                        sname = str(r["space_name"] or "").strip()
                        items.append({
                            "entry_id": eid,
                            "remote_entry_id": "",
                            **o_meta[eid],
                            "status": "other_space",
                            "message": f"该条目仍关联到其他共享批次「{sname}」" if sname else "该条目仍关联到其他共享批次",
                        })

            unchanged_cnt = int((stored or {}).get("unchanged_count") or 0)
            if stored is None and shared["last_sync"] and total_entries >= len(items):
                unchanged_cnt = total_entries - len(items)
            report = _build_report_payload(
                items,
                unchanged_cnt,
                updated_at=str((stored or {}).get("updated_at") or shared["last_sync"] or ""),
            )

        report["total_entries"] = total_entries
        return {"batch_id": bid, "shared": shared, "report": report}

    def detach_other_space_entries(self, conn: sqlite3.Connection, batch_id: str) -> dict:
        """Remove stale hub_entry_links pointing to other shared batches for entries in this batch."""
        bid = str(batch_id or "").strip()
        if not bid:
            raise ValueError("请指定要处理的批次。")
        existing = _existing_batches_map(conn)
        batch_info = existing.get(bid)
        if not batch_info:
            raise ValueError("报账批次不存在。")
        connected, cur_url, cur_uid, _tok, _role = _read_hub_credentials(conn)
        if not connected:
            raise ValueError("尚未连接到团队共享区，请先连接后再操作。")
        links, pruned = _read_batch_links(conn)
        rec = links.get(bid)
        if not rec:
            if pruned:
                _write_batch_links(conn, links, commit=True)
            raise ValueError("该批次尚未关联共享批次。")
        if rec.get("server_url") != cur_url or rec.get("user_id") != cur_uid:
            raise ValueError("该批次关联的是其他共享区账号，当前已停用，请重新关联。")
        space_id = str(rec.get("space_id") or "")

        detached_ids: list[str] = []
        if _has_hub_entry_links_table(conn):
            rows = conn.execute(
                """SELECT h.entry_id
                     FROM batch_entries be
                     JOIN hub_entry_links h ON h.entry_id = be.entry_id
                    WHERE be.batch_id = ?
                      AND h.server_url = ?
                      AND h.space_id <> ''
                      AND h.space_id <> ?""",
                (bid, cur_url, space_id),
            ).fetchall()
            detached_ids = [str(r["entry_id"]) for r in rows]
            if detached_ids:
                placeholders = ",".join("?" for _ in detached_ids)
                conn.execute(
                    f"DELETE FROM hub_entry_links WHERE entry_id IN ({placeholders}) AND server_url = ? AND space_id <> ?",
                    [*detached_ids, cur_url, space_id],
                )

        suppressed = dict(rec.get("suppressed_conflicts") or {})
        entry_states = dict(rec.get("entry_sync_states") or {})
        for eid in detached_ids:
            suppressed.pop(eid, None)
            entry_states.pop(eid, None)
        for eid, item in list(suppressed.items()):
            if (item or {}).get("reason") == "other_space":
                suppressed.pop(eid, None)
        rec["suppressed_conflicts"] = suppressed
        rec["entry_sync_states"] = entry_states
        rec["other_space_count"] = 0
        rec["other_space_names"] = []
        rec.pop("last_report", None)
        pull_conflicts = dict(rec.get("pull_conflicts") or {})
        remaining_conflicts = len(pull_conflicts) + len(suppressed)
        rec["conflict_count"] = remaining_conflicts
        if remaining_conflicts == 0 and rec.get("last_state") in ("partial", "conflict"):
            rec["last_state"] = "synced" if rec.get("last_sync") else "manual"
            rec["last_message"] = ""
        links[bid] = rec
        _write_batch_links(conn, links, commit=True)

        with self._lock:
            self._bump_batch_epoch_locked(bid)
            rt = self._runtime.setdefault(bid, {})
            rt.pop("report", None)
            rt["other_space_count"] = 0
            rt["other_space_names"] = []
            rt["conflict_count"] = remaining_conflicts
            if remaining_conflicts == 0 and rt.get("state") in ("partial", "conflict"):
                rt["state"] = rec["last_state"]
                rt["message"] = rec["last_message"]
            rt["next_due_at"] = 0.0
            info = _build_shared_info(
                rec,
                archived=batch_info["archived"],
                connected=True,
                cur_server_url=cur_url,
                cur_user_id=cur_uid,
                runtime=rt,
            )
        return {"batch_id": bid, "detached": len(detached_ids), "shared": info}

    def tick(self, conn: sqlite3.Connection) -> dict:
        connected, cur_url, cur_uid, _tok, _role = _read_hub_credentials(conn)
        existing = _existing_batches_map(conn)
        links, pruned = _read_batch_links(conn)
        if pruned:
            _write_batch_links(conn, links, commit=True)

        now_mono = time.monotonic()
        with self._lock:
            stale_rt = [bid for bid in self._runtime if bid not in links]
            for bid in stale_rt:
                self._runtime.pop(bid, None)

            if connected:
                for bid, rec in links.items():
                    batch_info = existing.get(bid)
                    if not batch_info or batch_info["archived"]:
                        continue
                    if rec.get("server_url") != cur_url or rec.get("user_id") != cur_uid:
                        continue
                    if not rec.get("auto_sync") or rec.get("needs_upgrade"):
                        continue
                    if bid in self._queued_manual or self._running_batch == bid:
                        continue
                    rt = self._runtime.get(bid) or {}
                    if rt.get("needs_upgrade") or rt.get("user_stopped") or rec.get("last_state") == "stopped":
                        continue
                    if now_mono >= float(rt.get("next_due_at") or 0.0):
                        self._enqueue_locked(bid, manual=False)

            batches_out: dict[str, dict] = {}
            for bid, rec in links.items():
                batch_info = existing.get(bid)
                if not batch_info:
                    continue
                batches_out[bid] = _build_shared_info(
                    rec,
                    archived=batch_info["archived"],
                    connected=connected,
                    cur_server_url=cur_url,
                    cur_user_id=cur_uid,
                    runtime=self._runtime.get(bid),
                )
            gen = self._generation
        return {"batches": batches_out, "generation": gen}

    def link_batch(
        self,
        api,
        batch_id: str = "",
        space_id: str = "",
        auto_sync: bool = False,
    ) -> dict:
        bid = str(batch_id or "").strip()
        sid = str(space_id or "").strip()
        if not bid and not sid:
            raise ValueError("请选择要关联的本地批次或共享批次。")

        # Step 1: Validate local state & capture account epoch without holding _api_lock over network.
        with api._api_lock:
            conn = api.db.conn
            connected, cur_url, cur_uid, token, _role = _read_hub_credentials(conn)
            if not connected:
                raise ValueError("尚未连接到团队共享区，请先在「设置 → 协同共享区」中连接。")
            existing = _existing_batches_map(conn)
            links, _ = _read_batch_links(conn)
            local_batch = None
            if bid:
                local_batch = existing.get(bid)
                if not local_batch:
                    raise ValueError("所选本地批次不存在。")
                cur_link = links.get(bid)
                if (
                    cur_link
                    and cur_link.get("server_url") == cur_url
                    and cur_link.get("user_id") == cur_uid
                    and sid
                    and cur_link.get("space_id") != sid
                ):
                    raise ValueError("该本地批次已关联其他共享批次，请先解除关联。")
                if (
                    cur_link
                    and cur_link.get("server_url") == cur_url
                    and cur_link.get("user_id") == cur_uid
                    and not sid
                ):
                    raise ValueError("该本地批次已关联共享批次，请先解除关联。")
            if sid:
                for other_bid, rec in links.items():
                    if (
                        other_bid != bid
                        and rec.get("server_url") == cur_url
                        and rec.get("user_id") == cur_uid
                        and rec.get("space_id") == sid
                    ):
                        other_name = (existing.get(other_bid) or {}).get("name") or other_bid
                        raise ValueError(
                            f"该共享批次已关联到本地批次「{other_name}」，每个共享批次在本机最多关联一个批次。"
                        )
            with self._lock:
                epoch_snapshot = self._account_epoch

        # Step 2: Explicit network request outside _api_lock.
        client = hub_sync.HubClient(cur_url, token)
        try:
            if bid and not sid:
                space = client.create_space(local_batch["name"])
            else:
                spaces = client.list_spaces(include_archived=True)
                space = next((s for s in spaces if str(s.get("id") or "") == sid), None)
                if not space:
                    raise ValueError("所选共享批次不存在或已被移除。")
                if str(space.get("status") or "") == "archived":
                    raise ValueError("该共享批次已归档，无法关联。")
        finally:
            client.close()

        resolved_sid = str(space.get("id") or "")
        resolved_sname = str(space.get("name") or "")

        # Step 3: Re-validate & commit atomically under _api_lock.
        with api._api_lock:
            with self._lock:
                if self._account_epoch != epoch_snapshot:
                    raise ValueError("共享区账号状态已变更，请重试。")
            conn = api.db.conn
            connected2, cur_url2, cur_uid2, _tok2, _role2 = _read_hub_credentials(conn)
            if not connected2 or cur_url2 != cur_url or cur_uid2 != cur_uid:
                raise ValueError("共享区账号状态已变更，请重试。")

            existing = _existing_batches_map(conn)
            links, _ = _read_batch_links(conn)
            for other_bid, rec in links.items():
                if (
                    other_bid != bid
                    and rec.get("server_url") == cur_url
                    and rec.get("user_id") == cur_uid
                    and rec.get("space_id") == resolved_sid
                ):
                    other_name = (existing.get(other_bid) or {}).get("name") or other_bid
                    raise ValueError(
                        f"该共享批次已关联到本地批次「{other_name}」，每个共享批次在本机最多关联一个批次。"
                    )

            if bid:
                local_batch = existing.get(bid)
                if not local_batch:
                    raise ValueError("所选本地批次不存在。")
                cur_link = links.get(bid)
                if (
                    cur_link
                    and cur_link.get("server_url") == cur_url
                    and cur_link.get("user_id") == cur_uid
                    and cur_link.get("space_id") != resolved_sid
                ):
                    raise ValueError("该本地批次已关联其他共享批次，请先解除关联。")
                target_bid = bid
            else:
                created = api.batches.create(resolved_sname)
                target_bid = str(created["id"])
                local_batch = {"id": target_bid, "name": created["name"], "archived": False}

            enable_auto = bool(auto_sync) and not bool(local_batch["archived"])
            existing_rec = links.get(target_bid)
            same_binding = (
                existing_rec is not None
                and existing_rec.get("server_url") == cur_url
                and existing_rec.get("user_id") == cur_uid
                and existing_rec.get("space_id") == resolved_sid
            )
            other_cnt, other_names = _count_other_space_entries(conn, target_bid, cur_url, resolved_sid)
            record = dict(existing_rec) if same_binding else {
                "space_id": resolved_sid,
                "space_name": resolved_sname,
                "server_url": cur_url,
                "user_id": cur_uid,
                "auto_sync": enable_auto,
                "last_sync": "",
                "last_state": "partial" if other_cnt > 0 else "manual",
                "last_message": _format_other_space_message(other_cnt, other_names) if other_cnt > 0 else "",
                "conflict_count": 0,
                "other_space_count": other_cnt,
                "other_space_names": other_names,
                "needs_upgrade": False,
                "last_remote_version": "",
                "entry_sync_states": {},
                "pull_conflicts": {},
                "suppressed_conflicts": {},
                "deleted_remote_entries": {},
            }
            record["space_name"] = resolved_sname
            record["auto_sync"] = enable_auto
            record["other_space_count"] = other_cnt
            record["other_space_names"] = other_names
            links[target_bid] = record
            _write_batch_links(conn, links, commit=True)

        with self._lock:
            self._bump_batch_epoch_locked(target_bid)
            rt = self._runtime.setdefault(target_bid, {})
            rt["next_due_at"] = 0.0 if enable_auto else time.monotonic() + self._interval_seconds
            rt["error_count"] = 0
            rt["needs_upgrade"] = False
            rt["state"] = record.get("last_state") or "manual"
            rt["message"] = record.get("last_message") or ""
            rt["conflict_count"] = int(record.get("conflict_count") or 0)
            rt["other_space_count"] = int(record.get("other_space_count") or 0)
            rt["other_space_names"] = list(record.get("other_space_names") or [])
            info = _build_shared_info(
                record,
                archived=bool(local_batch["archived"]),
                connected=True,
                cur_server_url=cur_url,
                cur_user_id=cur_uid,
                runtime=rt,
            )
        return {"batch_id": target_bid, "shared": info}

    # ------------------------------------------------------------------ Background worker
    def _worker_loop(self) -> None:
        while True:
            with self._lock:
                while not self._queue and not self._stop_event.is_set():
                    self._cond.wait(timeout=1.0)
                if self._stop_event.is_set():
                    break
                batch_id, force_manual = self._queue.popleft()
                self._queued_manual.pop(batch_id, None)
                self._running_batch = batch_id
                rt = self._runtime.setdefault(batch_id, {})
                rt["queued"] = False
                rt["running"] = True
                if force_manual:
                    rt["visible_syncing"] = True
                    rt["state"] = "syncing"
                    rt["message"] = "正在同步…"

            try:
                self._run_sync_task(batch_id, force_manual=force_manual)
            except Exception as exc:  # noqa: BLE001
                self._record_worker_error(batch_id, f"同步异常：{exc}")
            finally:
                with self._lock:
                    self._running_batch = ""
                    self._user_stop_requested.discard(batch_id)
                    rerun = self._rerun_manual.pop(batch_id, None)
                    rt = self._runtime.get(batch_id)
                    if rt is not None:
                        rt["running"] = False
                        rt["stopping"] = False
                        rt["progress"] = None
                        if rerun is None:
                            rt["visible_syncing"] = False
                    if rerun is not None and not self._stop_event.is_set():
                        self._enqueue_locked(batch_id, manual=rerun)

    def _open_worker_repos(self) -> tuple[_WorkerDatabase, AdapterService, EntryRepo, AttachmentRepo, BatchRepo]:
        worker_db = _WorkerDatabase(self._db_path)
        adapters = AdapterService(worker_db, self._data_root)
        worker_db.adapter_service = adapters
        entries = EntryRepo(worker_db)
        attachments = AttachmentRepo(worker_db, self._data_root)
        batches = BatchRepo(worker_db)
        return worker_db, adapters, entries, attachments, batches

    def _record_worker_error(self, batch_id: str, message: str, *, needs_upgrade: bool = False) -> None:
        with self._lock:
            self._user_stop_requested.discard(batch_id)
            rt = self._runtime.get(batch_id)
            if rt is None:
                return
            err_count = int(rt.get("error_count") or 0) + 1
            backoff = min(
                self._max_backoff_seconds,
                self._interval_seconds * (2 ** min(err_count - 1, 4)),
            )
            rt["error_count"] = err_count
            rt["next_due_at"] = time.monotonic() + backoff
            rt["visible_syncing"] = False
            rt["stopping"] = False
            rt["user_stopped"] = False
            rt["progress"] = None
            rt["state"] = "error"
            rt["message"] = message
            rt["needs_upgrade"] = needs_upgrade

    def _is_cancelled(self, batch_id: str, account_epoch: int, batch_epoch: int, *, force_manual: bool) -> bool:
        if self._stop_event.is_set():
            return True
        with self._lock:
            if self._account_epoch != account_epoch:
                return True
            if self._batch_epochs.get(batch_id, 0) != batch_epoch:
                return True
            if not force_manual and self._runtime.get(batch_id) is None:
                return True
        return False

    def _run_sync_task(self, batch_id: str, *, force_manual: bool) -> None:
        # Phase 1: Short DB read for initial state & credentials (no network).
        worker_db, adapters, entries_repo, attachments_repo, _batches_repo = self._open_worker_repos()
        try:
            conn = worker_db.conn
            connected, server_url, user_id, token, user_role = _read_hub_credentials(conn)
            if not connected:
                return
            batch_row = conn.execute("SELECT id, name, archived FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if not batch_row or int(batch_row["archived"] or 0):
                return
            links, _ = _read_batch_links(conn)
            rec = links.get(batch_id)
            if not rec or rec.get("server_url") != server_url or rec.get("user_id") != user_id:
                return
            if not force_manual and not rec.get("auto_sync"):
                return
            space_id = str(rec.get("space_id") or "")
            if not space_id:
                return

            with self._lock:
                account_epoch = self._account_epoch
                batch_epoch = self._batch_epochs.get(batch_id, 0)
            if self._is_cancelled(batch_id, account_epoch, batch_epoch, force_manual=force_manual):
                return

            links_by_remote = hub_sync._hub_link_by_remote(conn, server_url, space_id)
            default_prof = conn.execute(
                "SELECT id FROM profiles ORDER BY is_default DESC, created_at ASC LIMIT 1"
            ).fetchone()
            default_pid = str(default_prof["id"]) if default_prof else ""
        finally:
            worker_db.close()

        def cancel_check() -> bool:
            return self._is_cancelled(batch_id, account_epoch, batch_epoch, force_manual=force_manual)

        def user_stop_check() -> bool:
            with self._lock:
                return batch_id in self._user_stop_requested

        def enter_active_sync(msg: str, progress: dict[str, Any] | None = None) -> None:
            with self._lock:
                if cancel_check():
                    return
                rt = self._runtime.setdefault(batch_id, {})
                rt["visible_syncing"] = True
                rt["state"] = "syncing"
                if not rt.get("stopping"):
                    rt["message"] = msg
                rt["progress"] = progress

        report_items: list[dict[str, Any]] = []
        unchanged_count = 0
        stopped_early = False

        def publish_live_report() -> None:
            payload_rep = _build_report_payload(report_items, unchanged_count)
            with self._lock:
                if not cancel_check():
                    rt = self._runtime.setdefault(batch_id, {})
                    rt["report"] = payload_rep

        def make_commit_guard(*, require_auto_sync: bool):
            def guard(tx_conn: sqlite3.Connection) -> None:
                if cancel_check():
                    raise hub_sync.HubError("同步已取消。", code="cancelled")
                c_ok, c_url, c_uid, _c_tok, _c_role = _read_hub_credentials(tx_conn)
                if not c_ok or c_url != server_url or c_uid != user_id:
                    raise hub_sync.HubError("共享区账号已变更。", code="cancelled")
                b_row = tx_conn.execute("SELECT archived FROM batches WHERE id = ?", (batch_id,)).fetchone()
                if not b_row or int(b_row["archived"] or 0):
                    raise hub_sync.HubError("批次不存在或已归档。", code="cancelled")
                cur_links, _ = _read_batch_links(tx_conn)
                cur_rec = cur_links.get(batch_id)
                if (
                    not cur_rec
                    or cur_rec.get("server_url") != server_url
                    or cur_rec.get("user_id") != user_id
                    or cur_rec.get("space_id") != space_id
                ):
                    raise hub_sync.HubError("批次关联已解除或变更。", code="cancelled")
                if require_auto_sync and not cur_rec.get("auto_sync"):
                    raise hub_sync.HubError("自动同步已关闭。", code="cancelled")
            return guard

        client = self._get_worker_client(server_url, token)

        # Phase 2: Lightweight remote sync-state probe (1 GET).
        if cancel_check():
            return
        try:
            remote_state = client.sync_state(space_id)
        except hub_sync.HubError as exc:
            if cancel_check():
                return
            if _is_unsupported_sync_state_error(client, exc):
                msg = "共享区服务端版本较旧，不支持自动同步，请升级服务端后重试（仍可手动上传/拉取）。"
                self._commit_batch_meta(
                    batch_id, server_url, user_id, space_id,
                    account_epoch=account_epoch, batch_epoch=batch_epoch,
                    force_manual=force_manual,
                    updates={"auto_sync": False, "needs_upgrade": True, "last_state": "error", "last_message": msg},
                )
                self._record_worker_error(batch_id, msg, needs_upgrade=True)
                return
            if exc.status == 404 and exc.code == "space_not_found":
                msg = "共享区中找不到该批次，请核对或取消关联。"
                self._commit_batch_meta(
                    batch_id, server_url, user_id, space_id,
                    account_epoch=account_epoch, batch_epoch=batch_epoch,
                    force_manual=force_manual,
                    updates={"last_state": "error", "last_message": msg},
                )
                self._record_worker_error(batch_id, msg)
                return
            if exc.status == 401:
                msg = "共享区登录凭证已失效或被停用，请重新连接。"
                self._commit_batch_meta(
                    batch_id, server_url, user_id, space_id,
                    account_epoch=account_epoch, batch_epoch=batch_epoch,
                    force_manual=force_manual,
                    updates={"auto_sync": False, "last_state": "error", "last_message": msg},
                )
                self._record_worker_error(batch_id, msg)
                return
            self._record_worker_error(batch_id, str(exc))
            return

        if cancel_check():
            return

        remote_space = remote_state.get("space") or {"id": space_id, "name": rec.get("space_name") or ""}
        remote_space_name = str(remote_space.get("name") or rec.get("space_name") or "")
        remote_space_status = str(remote_space.get("status") or "active")
        remote_version = str(remote_state.get("version") or "")
        remote_entries_meta = list(remote_state.get("entries") or [])
        remote_meta_by_id = {str(r["id"]): r for r in remote_entries_meta if r.get("id")}

        if remote_space_status == "archived":
            msg = "该共享批次已归档，同步已暂停。"
            self._commit_batch_meta(
                batch_id, server_url, user_id, space_id,
                account_epoch=account_epoch, batch_epoch=batch_epoch,
                force_manual=force_manual,
                updates={
                    "space_name": remote_space_name,
                    "auto_sync": False,
                    "last_state": "paused",
                    "last_message": msg,
                },
            )
            with self._lock:
                if not cancel_check():
                    rt = self._runtime.setdefault(batch_id, {})
                    rt["state"] = "paused"
                    rt["message"] = msg
                    rt["next_due_at"] = time.monotonic() + self._interval_seconds
            return

        # Phase 3: Determine which remote entries actually need fetching for Pull.
        pull_conflicts_cache = dict(rec.get("pull_conflicts") or {})
        deleted_remote_entries = dict(rec.get("deleted_remote_entries") or {})
        cleared_tombstones: dict[str, int] = {}
        for tomb_rid, tomb_info in deleted_remote_entries.items():
            rmeta_now = remote_meta_by_id.get(tomb_rid)
            if rmeta_now is None or str(rmeta_now.get("review_state") or "") == "withdrawn":
                cleared_tombstones[tomb_rid] = int((tomb_info or {}).get("revision") or 0)
        changed_remote_ids: list[str] = []
        suppressed_pull_conflicts: dict[str, dict] = {}
        local_digests_for_links: dict[str, str] = {}

        if links_by_remote or pull_conflicts_cache:
            worker_db, adapters, entries_repo, attachments_repo, _ = self._open_worker_repos()
            try:
                for rid, cached_pc in pull_conflicts_cache.items():
                    lnk = links_by_remote.get(rid)
                    target_eid = lnk["entry_id"] if lnk else str(cached_pc.get("local_entry_id") or "")
                    target_rev = lnk["remote_revision"] if lnk else int(cached_pc.get("remote_revision") or 0)
                    if target_eid:
                        st = _compute_entry_sync_state(
                            entries_repo, attachments_repo, adapters, target_eid, target_rev
                        )
                        if st:
                            local_digests_for_links[rid] = st["bundle_digest"]
            finally:
                worker_db.close()

        for rmeta in remote_entries_meta:
            rid = str(rmeta["id"])
            r_rev = int(rmeta.get("revision") or 0)
            r_state = str(rmeta.get("review_state") or "")
            r_note = str(rmeta.get("review_note") or "")
            lnk = links_by_remote.get(rid)
            if lnk is None:
                if r_state != "withdrawn":
                    tomb = deleted_remote_entries.get(rid)
                    if tomb is not None and r_rev <= int((tomb or {}).get("revision") or 0):
                        # Locally deleted at this revision; do not resurrect on sync.
                        continue
                    cached_pc = pull_conflicts_cache.get(rid)
                    cur_ldigest = local_digests_for_links.get(rid, "")
                    if (
                        not force_manual
                        and cached_pc
                        and int(cached_pc.get("remote_revision") or 0) == r_rev
                        and cached_pc.get("local_digest") == cur_ldigest
                    ):
                        suppressed_pull_conflicts[rid] = cached_pc
                    else:
                        changed_remote_ids.append(rid)
                continue
            needs_check = (
                r_rev != int(lnk.get("remote_revision") or 0)
                or r_state != str(lnk.get("review_state") or "")
                or r_note != str(lnk.get("review_note") or "")
            )
            if not needs_check:
                continue
            if r_state == "withdrawn":
                # Still need to acknowledge withdrawal locally unless already acked.
                changed_remote_ids.append(rid)
                continue
            cached_pc = pull_conflicts_cache.get(rid)
            cur_ldigest = local_digests_for_links.get(rid, "")
            if (
                not force_manual
                and cached_pc
                and int(cached_pc.get("remote_revision") or 0) == r_rev
                and cur_ldigest
                and cached_pc.get("local_digest") == cur_ldigest
            ):
                suppressed_pull_conflicts[rid] = cached_pc
                continue
            changed_remote_ids.append(rid)

        pulled_summary = {"imported": 0, "updated": 0, "status_updated": 0, "withdrawn_updated": 0}
        new_pull_conflicts: dict[str, dict] = dict(suppressed_pull_conflicts)
        applied_imported_entry_ids: list[str] = []
        applied_updated_entry_ids: list[str] = []
        applied_status_entry_ids: list[str] = []
        applied_pull_revisions: dict[str, int] = {}
        applied_remote_snap_digests: dict[str, str] = {}
        local_changed_retry = False

        if changed_remote_ids:
            if cancel_check():
                return
            try:
                if len(changed_remote_ids) <= SELECTIVE_FETCH_LIMIT:
                    fetched_entries = []
                    for rid in changed_remote_ids:
                        if cancel_check():
                            return
                        fetched_entries.append(client.get_space_entry(space_id, rid))
                    listing = {"space": remote_space, "entries": fetched_entries}
                else:
                    listing = client.list_space_entries(space_id, include_withdrawn=True)
            except hub_sync.HubError as exc:
                if not cancel_check():
                    self._record_worker_error(batch_id, str(exc))
                return

            if cancel_check():
                return

            # Plan pull using short worker DB read.
            auto_apply_indexes: list[int] = []
            pull_scheme_id = ""
            worker_db, adapters, entries_repo, attachments_repo, _ = self._open_worker_repos()
            try:
                batch_scheme_row = worker_db.conn.execute(
                    "SELECT default_scheme_id FROM batches WHERE id = ?", (batch_id,)
                ).fetchone()
                pull_scheme_id = _resolve_pull_scheme_id(
                    adapters,
                    str(batch_scheme_row["default_scheme_id"] or "") if batch_scheme_row else "",
                )
                preview = hub_sync.preview_pull(
                    client,
                    entries_repo,
                    space_id,
                    adapter_service=adapters,
                    target_scheme_id=pull_scheme_id or None,
                    listing=listing,
                )
                conn = worker_db.conn
                preview_meta_by_rid: dict[str, dict[str, str]] = {}
                for idx, p_entry in enumerate(preview.get("entries") or []):
                    action = p_entry.get("import_action")
                    matched_id = str(p_entry.get("existing_entry_id") or "")
                    rid = str((p_entry.get("hub_meta") or {}).get("remote_entry_id") or "")
                    r_rev = int((p_entry.get("hub_meta") or {}).get("revision") or 0)
                    if rid:
                        preview_meta_by_rid[rid] = _entry_display_fields(p_entry)
                    if action == "unchanged" and not p_entry.get("hub_review_changed"):
                        continue
                    if action == "conflict":
                        ldigest = ""
                        if matched_id:
                            st = _compute_entry_sync_state(entries_repo, attachments_repo, adapters, matched_id, r_rev)
                            ldigest = st["bundle_digest"] if st else ""
                        new_pull_conflicts[rid] = {
                            "remote_revision": r_rev,
                            "local_digest": ldigest,
                            "local_entry_id": matched_id,
                        }
                        continue
                    if matched_id:
                        existing_link = hub_sync.get_hub_link(conn, matched_id)
                        if (
                            existing_link
                            and existing_link["server_url"] == server_url
                            and existing_link["space_id"] != space_id
                            and existing_link["review_state"] != "withdrawn"
                        ):
                            st = _compute_entry_sync_state(entries_repo, attachments_repo, adapters, matched_id, r_rev)
                            new_pull_conflicts[rid] = {
                                "remote_revision": r_rev,
                                "local_digest": st["bundle_digest"] if st else "",
                                "local_entry_id": matched_id,
                            }
                            continue
                    auto_apply_indexes.append(idx)
                has_withdrawn = bool(preview.get("withdrawn_local"))
            finally:
                worker_db.close()

            if auto_apply_indexes or has_withdrawn:
                if cancel_check():
                    return
                if user_stop_check():
                    stopped_early = True
                else:
                    enter_active_sync(
                        f"正在拉取 {len(auto_apply_indexes)} 条共享区更新…" if auto_apply_indexes else "正在同步共享区状态…",
                        {"phase": "pull", "done": 0, "total": len(auto_apply_indexes), "current_label": ""},
                    )
                    worker_db, adapters, entries_repo, attachments_repo, _ = self._open_worker_repos()
                    try:
                        apply_opts = {
                            "batch_id": batch_id,
                            "move_existing_entries": False,
                            "selected_entry_indexes": auto_apply_indexes,
                        }
                        if pull_scheme_id:
                            apply_opts["scheme_id"] = pull_scheme_id
                        pull_res = hub_sync.apply_pull(
                            client,
                            entries_repo,
                            attachments_repo,
                            space_id,
                            default_pid,
                            options=apply_opts,
                            preview=preview,
                            adapter_service=adapters,
                            cancel_check=cancel_check,
                            on_before_commit=make_commit_guard(require_auto_sync=not force_manual),
                        )
                        pulled_summary["imported"] = int(pull_res.get("imported") or 0)
                        pulled_summary["updated"] = int(pull_res.get("updated") or 0)
                        pulled_summary["status_updated"] = int(pull_res.get("status_updated") or 0)
                        pulled_summary["withdrawn_updated"] = int(pull_res.get("withdrawn_updated") or 0)
                        applied_imported_entry_ids = list(dict.fromkeys(pull_res.get("entry_ids") or []))
                        applied_updated_entry_ids = list(dict.fromkeys(pull_res.get("updated_entry_ids") or []))
                        applied_status_entry_ids = list(
                            dict.fromkeys(pull_res.get("status_updated_entry_ids") or [])
                        )
                        applied_remote_snap_digests = dict(pull_res.get("remote_snapshot_digests") or {})
                        applied_all = list(
                            dict.fromkeys(
                                applied_imported_entry_ids + applied_updated_entry_ids + applied_status_entry_ids
                            )
                        )
                        if self._payment_ai_flags_getter is not None and applied_all:
                            avail, enabled = self._payment_ai_flags_getter()
                            for eid in applied_all:
                                if avail:
                                    entries_repo.refresh_payment_check(eid, remind_pending=enabled)
                                elif entries_repo.has_imported_payment_recognition(eid):
                                    entries_repo.refresh_payment_check(eid, remind_pending=False, imported_only=True)
                                else:
                                    entries_repo.clear_payment_check(eid)
                        for eid in applied_all:
                            lnk = hub_sync.get_hub_link(worker_db.conn, eid)
                            rev = int(lnk["remote_revision"] or 0) if lnk else 0
                            applied_pull_revisions[eid] = rev
                            if lnk and lnk.get("remote_entry_id"):
                                rid = str(lnk["remote_entry_id"])
                                if rid in deleted_remote_entries:
                                    cleared_tombstones[rid] = int((deleted_remote_entries[rid] or {}).get("revision") or 0)
                        pull_meta_map = _load_entry_meta_map(worker_db.conn, applied_all)
                        for eid in applied_imported_entry_ids:
                            report_items.append({
                                "entry_id": eid,
                                "remote_entry_id": "",
                                **(pull_meta_map.get(eid) or _entry_display_fields({})),
                                "status": "pulled_new",
                                "message": "从共享区拉取新增",
                            })
                        for eid in applied_updated_entry_ids:
                            report_items.append({
                                "entry_id": eid,
                                "remote_entry_id": "",
                                **(pull_meta_map.get(eid) or _entry_display_fields({})),
                                "status": "pulled_merged",
                                "message": "已从共享区合并更新",
                            })
                        for eid in applied_status_entry_ids:
                            if eid in applied_imported_entry_ids or eid in applied_updated_entry_ids:
                                continue
                            report_items.append({
                                "entry_id": eid,
                                "remote_entry_id": "",
                                **(pull_meta_map.get(eid) or _entry_display_fields({})),
                                "status": "status_updated",
                                "message": "已同步审核状态",
                            })
                        for w in pull_res.get("withdrawn_local") or []:
                            lid = str(w.get("local_entry_id") or "")
                            report_items.append({
                                "entry_id": lid,
                                "remote_entry_id": str(w.get("remote_entry_id") or ""),
                                **_entry_display_fields(w),
                                "status": "withdrawn",
                                "message": "共享区已撤回（本机保留）",
                            })
                    except hub_sync.HubError as exc:
                        if exc.code == "cancelled" or cancel_check():
                            return
                        if exc.code == "local_changed":
                            local_changed_retry = True
                        else:
                            self._record_worker_error(batch_id, str(exc))
                            return
                    finally:
                        worker_db.close()

                    if any(pulled_summary.values()):
                        self.bump_generation()

        if cancel_check():
            return

        # Phase 4: Read local batch entries & plan Push (no network while reading DB).
        entry_sync_states: dict[str, dict] = dict(rec.get("entry_sync_states") or {})
        suppressed_conflicts: dict[str, dict] = dict(rec.get("suppressed_conflicts") or {})
        pull_conflict_local_ids: set[str] = set()

        worker_db, adapters, entries_repo, attachments_repo, _ = self._open_worker_repos()
        try:
            conn = worker_db.conn
            # Refresh post-pull entry_sync_states ONLY when local content matches what remote holds.
            # Fresh imports record an imported_only baseline; merged entries that still retain
            # local-only edits (e.g. unpushed notes) must NOT be marked synced so Phase 4 pushes them.
            for eid in applied_imported_entry_ids:
                rev = applied_pull_revisions.get(eid, 0)
                st = _compute_entry_sync_state(entries_repo, attachments_repo, adapters, eid, rev)
                if st is not None:
                    entry_sync_states[eid] = {
                        "bundle_digest": st["bundle_digest"],
                        "remote_revision": rev,
                        "imported_only": True,
                    }
                suppressed_conflicts.pop(eid, None)
            for eid in applied_updated_entry_ids:
                rev = applied_pull_revisions.get(eid, 0)
                st = _compute_entry_sync_state(entries_repo, attachments_repo, adapters, eid, rev)
                lnk_now = hub_sync.get_hub_link(conn, eid)
                rid_now = str(lnk_now["remote_entry_id"]) if lnk_now else ""
                rmeta_now = remote_meta_by_id.get(rid_now) if rid_now else None
                expected_remote_snap = str(
                    applied_remote_snap_digests.get(eid)
                    or (rmeta_now or {}).get("snapshot_digest")
                    or ""
                )
                if st is not None and expected_remote_snap and st["snapshot_digest"] == expected_remote_snap:
                    entry_sync_states[eid] = {
                        "bundle_digest": st["bundle_digest"],
                        "remote_revision": rev,
                    }
                else:
                    entry_sync_states.pop(eid, None)
                suppressed_conflicts.pop(eid, None)
            for eid in applied_status_entry_ids:
                suppressed_conflicts.pop(eid, None)

            links_by_remote_now = hub_sync._hub_link_by_remote(conn, server_url, space_id)
            pull_conflict_meta = _load_entry_meta_map(
                conn,
                [str((pc or {}).get("local_entry_id") or "") for pc in new_pull_conflicts.values()]
                + [str((links_by_remote_now.get(rid) or {}).get("entry_id") or "") for rid in new_pull_conflicts],
            )
            for conflict_rid, pc_info in new_pull_conflicts.items():
                lnk = links_by_remote_now.get(conflict_rid)
                pc_eid = str(lnk["entry_id"]) if lnk else str((pc_info or {}).get("local_entry_id") or "")
                if pc_eid:
                    pull_conflict_local_ids.add(pc_eid)
                disp = pull_conflict_meta.get(pc_eid) or _entry_display_fields({})
                report_items.append({
                    "entry_id": pc_eid,
                    "remote_entry_id": str(conflict_rid),
                    **disp,
                    "status": "pull_conflict",
                    "message": "双方均有修改，需核对共享变化",
                })

            batch_entry_rows = conn.execute(
                "SELECT entry_id FROM batch_entries WHERE batch_id = ? ORDER BY added_at, entry_id",
                (batch_id,),
            ).fetchall()
            batch_entry_ids = [str(r["entry_id"]) for r in batch_entry_rows]

            to_push: list[dict] = []
            locked_or_conflict_msgs: list[str] = []
            next_suppressed: dict[str, dict] = {}
            closed_space_pending = False

            for eid in batch_entry_ids:
                if eid in pull_conflict_local_ids:
                    if eid in suppressed_conflicts:
                        next_suppressed[eid] = suppressed_conflicts[eid]
                    continue
                try:
                    payload, snap, blob_files, profile_dict, ancestors, invoice_sha256 = hub_sync._build_hub_entry_bundle(
                        entries_repo, attachments_repo, eid, adapter_service=adapters
                    )
                except hub_sync.HubError as exc:
                    disp_map = _load_entry_meta_map(conn, [eid])
                    report_items.append({
                        "entry_id": eid,
                        "remote_entry_id": "",
                        **(disp_map.get(eid) or _entry_display_fields({})),
                        "status": "error",
                        "message": str(exc),
                    })
                    continue
                disp = _entry_display_fields(payload)
                local_digest = snapshot_digest(snap)
                b_digest = _bundle_sync_digest(
                    payload, snap, profile_dict, str(payload.get("invoice_no") or ""), invoice_sha256
                )
                lnk = hub_sync.get_hub_link(conn, eid)
                if lnk and lnk["server_url"] == server_url and lnk["space_id"] != space_id:
                    if lnk["review_state"] != "withdrawn":
                        other_name = str(lnk.get("space_name") or "").strip()
                        other_msg = (
                            f"该条目仍关联到其他共享批次「{other_name}」"
                            if other_name
                            else "该条目仍关联到其他共享批次"
                        )
                        next_suppressed[eid] = {
                            "bundle_digest": b_digest,
                            "remote_revision": 0,
                            "reason": "other_space",
                            "other_space_id": str(lnk.get("space_id") or ""),
                            "other_space_name": other_name,
                            "message": other_msg,
                        }
                        report_items.append({
                            "entry_id": eid,
                            "remote_entry_id": "",
                            **disp,
                            "status": "other_space",
                            "message": other_msg,
                        })
                        continue
                is_linked_here = bool(
                    lnk and lnk["server_url"] == server_url and lnk["space_id"] == space_id and lnk["remote_entry_id"]
                )
                if is_linked_here:
                    if lnk["review_state"] == "withdrawn":
                        # Remote withdrawal preserves local entry and never auto-repushes.
                        report_items.append({
                            "entry_id": eid,
                            "remote_entry_id": str(lnk.get("remote_entry_id") or ""),
                            **disp,
                            "status": "withdrawn",
                            "message": "共享区已撤回（本机保留）",
                        })
                        continue
                    rid = str(lnk["remote_entry_id"])
                    rmeta = remote_meta_by_id.get(rid)
                    if not rmeta:
                        unchanged_count += 1
                        continue
                    r_rev = int(rmeta.get("revision") or 0)
                    remote_snap_digest = str(rmeta.get("snapshot_digest") or "")
                    prev = entry_sync_states.get(eid)
                    if local_digest == remote_snap_digest and (
                        prev is None or prev.get("bundle_digest") == b_digest
                    ):
                        unchanged_count += 1
                        continue
                    if (
                        prev is not None
                        and prev.get("bundle_digest") == b_digest
                        and r_rev == int(lnk.get("remote_revision") or 0)
                        and (not remote_snap_digest or local_digest == remote_snap_digest or prev.get("imported_only"))
                    ):
                        unchanged_count += 1
                        continue
                    if rmeta.get("review_state") == "locked" and user_role != "admin":
                        next_suppressed[eid] = {
                            "bundle_digest": b_digest,
                            "remote_revision": r_rev,
                            "reason": "locked",
                            "message": "条目已被审核锁定，本地修改未上传",
                        }
                        locked_or_conflict_msgs.append("条目已被审核锁定")
                        report_items.append({
                            "entry_id": eid,
                            "remote_entry_id": rid,
                            **disp,
                            "status": "locked",
                            "message": "条目已被审核锁定，本地修改未上传",
                        })
                        continue
                    prev_sup = suppressed_conflicts.get(eid)
                    if (
                        not force_manual
                        and prev_sup
                        and prev_sup.get("bundle_digest") == b_digest
                        and int(prev_sup.get("remote_revision") or 0) == r_rev
                    ):
                        next_suppressed[eid] = prev_sup
                        msg = prev_sup.get("message") or "存在冲突"
                        locked_or_conflict_msgs.append(msg)
                        report_items.append({
                            "entry_id": eid,
                            "remote_entry_id": rid,
                            **disp,
                            "status": _map_suppressed_reason(str(prev_sup.get("reason") or "")),
                            "message": msg,
                        })
                        continue
                    if r_rev != int(lnk.get("remote_revision") or 0):
                        # Remote moved since our link; don't push blindly over unseen revision.
                        next_suppressed[eid] = {
                            "bundle_digest": b_digest,
                            "remote_revision": r_rev,
                            "reason": "conflict",
                            "message": "共享区已有更新，请先拉取合并",
                        }
                        locked_or_conflict_msgs.append("共享区已有更新")
                        report_items.append({
                            "entry_id": eid,
                            "remote_entry_id": rid,
                            **disp,
                            "status": "conflict",
                            "message": "共享区已有更新，请先拉取合并",
                        })
                        continue
                    if remote_space_status != "active" and user_role != "admin":
                        closed_space_pending = True
                        report_items.append({
                            "entry_id": eid,
                            "remote_entry_id": rid,
                            **disp,
                            "status": "space_closed",
                            "message": "共享批次已截止，无法上传新修改",
                        })
                        continue
                    to_push.append({
                        "entry_id": eid,
                        "remote_entry_id": rid,
                        "base_revision": int(lnk["remote_revision"] or 0),
                        "payload": payload,
                        "snap": snap,
                        "blob_files": blob_files,
                        "profile_dict": profile_dict,
                        "ancestors": ancestors,
                        "invoice_sha256": invoice_sha256,
                        "bundle_digest": b_digest,
                        "local_digest": local_digest,
                        "display": disp,
                    })
                else:
                    prev_sup = suppressed_conflicts.get(eid)
                    if (
                        not force_manual
                        and prev_sup
                        and prev_sup.get("bundle_digest") == b_digest
                        and prev_sup.get("remote_version") == remote_version
                    ):
                        next_suppressed[eid] = prev_sup
                        msg = prev_sup.get("message") or "存在冲突"
                        locked_or_conflict_msgs.append(msg)
                        report_items.append({
                            "entry_id": eid,
                            "remote_entry_id": "",
                            **disp,
                            "status": _map_suppressed_reason(str(prev_sup.get("reason") or "")),
                            "message": msg,
                        })
                        continue
                    if remote_space_status != "active" and user_role != "admin":
                        closed_space_pending = True
                        report_items.append({
                            "entry_id": eid,
                            "remote_entry_id": "",
                            **disp,
                            "status": "space_closed",
                            "message": "共享批次已截止，无法上传新修改",
                        })
                        continue
                    to_push.append({
                        "entry_id": eid,
                        "remote_entry_id": "",
                        "base_revision": 0,
                        "payload": payload,
                        "snap": snap,
                        "blob_files": blob_files,
                        "profile_dict": profile_dict,
                        "ancestors": ancestors,
                        "invoice_sha256": invoice_sha256,
                        "bundle_digest": b_digest,
                        "local_digest": local_digest,
                        "display": disp,
                    })
        finally:
            worker_db.close()

        # Phase 5: Upload changed entries (each entry commits with its original sent snapshot).
        pushed_count = 0
        if to_push and not stopped_early:
            if user_stop_check():
                stopped_early = True
                for rem in to_push:
                    report_items.append({
                        "entry_id": rem["entry_id"],
                        "remote_entry_id": rem["remote_entry_id"],
                        **rem["display"],
                        "status": "stopped",
                        "message": "已停止，等待继续同步",
                    })
            else:
                enter_active_sync(
                    f"正在准备上传 {len(to_push)} 条…",
                    {"phase": "push", "done": 0, "total": len(to_push), "current_label": ""},
                )
                publish_live_report()
                remote_ids_to_check = sorted({item["remote_entry_id"] for item in to_push if item["remote_entry_id"]})
                try:
                    upload_states = (
                        client.upload_state(space_id, remote_ids_to_check)
                        if remote_ids_to_check
                        else None
                    )
                except hub_sync.HubError as exc:
                    if not cancel_check():
                        err_items = list(report_items)
                        for rem in to_push:
                            err_items.append({
                                "entry_id": rem["entry_id"],
                                "remote_entry_id": rem["remote_entry_id"],
                                **rem["display"],
                                "status": "pending",
                                "message": f"准备上传失败（{exc}），可重试",
                            })
                        err_report = _build_report_payload(err_items, unchanged_count)
                        with self._lock:
                            rt = self._runtime.get(batch_id)
                            if rt is not None:
                                rt["report"] = err_report
                        self._record_worker_error(batch_id, str(exc))
                    return

                confirmed_shas: set[str] = set()
                for idx, item in enumerate(to_push):
                    if cancel_check():
                        return
                    if user_stop_check():
                        stopped_early = True
                        for rem in to_push[idx:]:
                            report_items.append({
                                "entry_id": rem["entry_id"],
                                "remote_entry_id": rem["remote_entry_id"],
                                **rem["display"],
                                "status": "stopped",
                                "message": "已停止，等待继续同步",
                            })
                        break
                    eid = item["entry_id"]
                    rid = item["remote_entry_id"]
                    disp = item["display"]
                    enter_active_sync(
                        f"正在上传第 {idx + 1} / {len(to_push)} 条 · {disp['label']}",
                        {"phase": "push", "done": idx, "total": len(to_push), "current_label": disp["label"]},
                    )
                    bound_shas: set[str] = set()
                    if upload_states is not None and rid:
                        r_state_item = upload_states.get(rid)
                        if r_state_item:
                            bound_shas = set(r_state_item.get("blob_shas") or [])
                    try:
                        blob_shas = hub_sync._upload_entry_blobs(
                            client, item["blob_files"], confirmed_shas, bound_shas=bound_shas
                        )
                        if cancel_check():
                            return
                        payload = item["payload"]
                        push_body = {
                            "remote_entry_id": rid,
                            "base_revision": item["base_revision"],
                            "invoice_no": str(payload.get("invoice_no") or "").strip(),
                            "invoice_sha256": item["invoice_sha256"],
                            "snapshot_digest": item["local_digest"],
                            "ancestors": item["ancestors"],
                            "profile": item["profile_dict"],
                            "payload": payload,
                            "blob_shas": blob_shas,
                        }
                        if upload_states is not None and rid:
                            push_body["skip_unchanged"] = True
                        push_body["submission_id"] = snapshot_digest(push_body)
                        res = client.push_entry(space_id, push_body)
                    except hub_sync.HubError as exc:
                        if cancel_check():
                            return
                        if exc.code in ("revision_conflict", "duplicate_invoice", "entry_locked"):
                            srv_rev = int((exc.payload or {}).get("server_revision") or item["base_revision"] or 0)
                            next_suppressed[eid] = {
                                "bundle_digest": item["bundle_digest"],
                                "remote_revision": srv_rev,
                                "remote_version": remote_version,
                                "reason": exc.code,
                                "message": str(exc),
                            }
                            locked_or_conflict_msgs.append(str(exc))
                            report_items.append({
                                "entry_id": eid,
                                "remote_entry_id": rid,
                                **disp,
                                "status": _map_suppressed_reason(exc.code),
                                "message": str(exc),
                            })
                            publish_live_report()
                            continue
                        err_items = list(report_items)
                        err_items.append({
                            "entry_id": eid,
                            "remote_entry_id": rid,
                            **disp,
                            "status": "error",
                            "message": str(exc),
                        })
                        for rem in to_push[idx + 1:]:
                            err_items.append({
                                "entry_id": rem["entry_id"],
                                "remote_entry_id": rem["remote_entry_id"],
                                **rem["display"],
                                "status": "pending",
                                "message": "因前面条目上传失败而中止，可重试",
                            })
                        err_report = _build_report_payload(err_items, unchanged_count)
                        rem_count = len(to_push) - idx - 1
                        summary_parts = []
                        if pushed_count:
                            summary_parts.append(f"已上传 {pushed_count} 条")
                        summary_parts.append(f"1 条失败（{exc}）")
                        if rem_count:
                            summary_parts.append(f"{rem_count} 条未处理")
                        err_msg = "；".join(summary_parts) if (pushed_count or rem_count) else str(exc)
                        self._commit_batch_meta(
                            batch_id,
                            server_url,
                            user_id,
                            space_id,
                            account_epoch=account_epoch,
                            batch_epoch=batch_epoch,
                            force_manual=force_manual,
                            clear_deleted_remote=cleared_tombstones,
                            updates={
                                "space_name": remote_space_name,
                                "last_state": "error",
                                "last_message": err_msg,
                                "last_remote_version": "",
                                "entry_sync_states": entry_sync_states,
                                "pull_conflicts": new_pull_conflicts,
                                "suppressed_conflicts": next_suppressed,
                                "last_report": err_report,
                            },
                        )
                        with self._lock:
                            rt = self._runtime.get(batch_id)
                            if rt is not None:
                                rt["report"] = err_report
                        self._record_worker_error(batch_id, err_msg)
                        return

                    remote_entry = res["entry"]
                    now = _now_iso()
                    if res.get("unchanged"):
                        unchanged_count += 1
                    else:
                        pushed_count += 1
                        report_items.append({
                            "entry_id": eid,
                            "remote_entry_id": str(remote_entry.get("id") or rid),
                            **disp,
                            "status": "pushed",
                            "message": "已上传到共享区",
                        })
                    new_rev = int(remote_entry.get("revision") or 0)

                    # Short BEGIN IMMEDIATE transaction recording original sent snapshot & link.
                    worker_db, _adapters, _entries, _att, _batches = self._open_worker_repos()
                    try:
                        conn = worker_db.conn
                        conn.execute("BEGIN IMMEDIATE")
                        make_commit_guard(require_auto_sync=not force_manual)(conn)
                        record_exchange_base(conn, eid, item["snap"], "export", now)
                        hub_sync.upsert_hub_link(
                            conn,
                            eid,
                            server_url=server_url,
                            space_id=space_id,
                            space_name=remote_space_name,
                            remote_entry=remote_entry,
                            synced_at=now,
                        )
                        conn.commit()
                    except hub_sync.HubError as exc:
                        worker_db.conn.rollback()
                        if exc.code == "cancelled" or cancel_check():
                            return
                        raise
                    finally:
                        worker_db.close()

                    entry_sync_states[eid] = {
                        "bundle_digest": item["bundle_digest"],
                        "remote_revision": new_rev,
                    }
                    next_suppressed.pop(eid, None)
                    publish_live_report()

        if cancel_check():
            return

        # Phase 6: Finalize batch metadata & runtime status.
        other_space_suppressed = {
            eid: item for eid, item in next_suppressed.items() if (item or {}).get("reason") == "other_space"
        }
        real_suppressed = {
            eid: item for eid, item in next_suppressed.items() if (item or {}).get("reason") != "other_space"
        }
        conflict_count = len(new_pull_conflicts) + len(real_suppressed)
        other_space_count = len(other_space_suppressed)
        other_space_names = sorted(
            {
                str(item.get("other_space_name") or "").strip()
                for item in other_space_suppressed.values()
                if str(item.get("other_space_name") or "").strip()
            }
        )
        now = _now_iso()
        report_payload = _build_report_payload(report_items, unchanged_count, updated_at=now)
        parts: list[str] = []
        if pushed_count:
            parts.append(f"上传 {pushed_count} 条")
        if pulled_summary["imported"]:
            parts.append(f"新增 {pulled_summary['imported']} 条")
        if pulled_summary["updated"]:
            parts.append(f"合并 {pulled_summary['updated']} 条")
        if pulled_summary["status_updated"]:
            parts.append(f"审核状态 {pulled_summary['status_updated']} 条")
        if pulled_summary["withdrawn_updated"]:
            parts.append(f"撤回状态 {pulled_summary['withdrawn_updated']} 条")

        if stopped_early:
            final_state = "stopped"
            stopped_pending = report_payload["counts"].get("stopped", 0)
            stop_parts = list(parts)
            if stopped_pending:
                stop_parts.append(f"剩余 {stopped_pending} 条未处理")
            if new_pull_conflicts:
                stop_parts.append(f"{len(new_pull_conflicts)} 条待拉取核对")
            if real_suppressed:
                stop_parts.append(f"{len(real_suppressed)} 条未上传（锁定或冲突）")
            final_msg = f"已停止（{'；'.join(stop_parts)}）" if stop_parts else "已停止同步"
        elif conflict_count > 0:
            final_state = "conflict"
            conflict_parts = []
            if new_pull_conflicts:
                conflict_parts.append(f"{len(new_pull_conflicts)} 条待拉取核对")
            if real_suppressed:
                conflict_parts.append(f"{len(real_suppressed)} 条未上传（锁定或冲突）")
            if other_space_count:
                conflict_parts.append(_format_other_space_message(other_space_count, other_space_names))
            final_msg = "；".join(parts + conflict_parts)
        elif other_space_count > 0:
            final_state = "partial"
            final_msg = "；".join(parts + [_format_other_space_message(other_space_count, other_space_names)])
        elif closed_space_pending:
            final_state = "paused"
            final_msg = "共享批次已截止，无法上传新修改（已更新审核状态）"
        else:
            final_state = "synced"
            final_msg = "；".join(parts) if parts else "已是最新"

        # If local_changed interrupted pull, don't cache remote_version so next tick retries pull cleanly.
        cached_version = "" if (local_changed_retry or pushed_count or stopped_early) else remote_version

        self._commit_batch_meta(
            batch_id,
            server_url,
            user_id,
            space_id,
            account_epoch=account_epoch,
            batch_epoch=batch_epoch,
            force_manual=force_manual,
            clear_deleted_remote=cleared_tombstones,
            updates={
                "space_name": remote_space_name,
                "last_sync": now,
                "last_state": final_state,
                "last_message": final_msg,
                "conflict_count": conflict_count,
                "other_space_count": other_space_count,
                "other_space_names": other_space_names,
                "needs_upgrade": False,
                "last_remote_version": cached_version,
                "entry_sync_states": entry_sync_states,
                "pull_conflicts": new_pull_conflicts,
                "suppressed_conflicts": next_suppressed,
                "last_report": report_payload,
            },
        )
        with self._lock:
            self._user_stop_requested.discard(batch_id)
            if not cancel_check():
                rt = self._runtime.get(batch_id)
                if rt is not None:
                    rt["visible_syncing"] = False
                    rt["stopping"] = False
                    rt["user_stopped"] = stopped_early
                    rt["progress"] = None
                    rt["state"] = final_state
                    rt["message"] = final_msg
                    rt["conflict_count"] = conflict_count
                    rt["other_space_count"] = other_space_count
                    rt["other_space_names"] = other_space_names
                    rt["needs_upgrade"] = False
                    rt["error_count"] = 0
                    rt["report"] = report_payload
                    rt["next_due_at"] = (
                        time.monotonic() + 0.2
                        if local_changed_retry
                        else time.monotonic() + self._interval_seconds
                    )

    def _commit_batch_meta(
        self,
        batch_id: str,
        server_url: str,
        user_id: str,
        space_id: str,
        *,
        account_epoch: int,
        batch_epoch: int,
        force_manual: bool,
        updates: dict[str, Any],
        clear_deleted_remote: dict[str, int] | None = None,
    ) -> bool:
        worker_db, _adapters, _entries, _att, _batches = self._open_worker_repos()
        try:
            conn = worker_db.conn
            conn.execute("BEGIN IMMEDIATE")
            if self._is_cancelled(batch_id, account_epoch, batch_epoch, force_manual=force_manual):
                conn.rollback()
                return False
            c_ok, c_url, c_uid, _c_tok, _c_role = _read_hub_credentials(conn)
            if not c_ok or c_url != server_url or c_uid != user_id:
                conn.rollback()
                return False
            b_row = conn.execute("SELECT archived FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if not b_row or int(b_row["archived"] or 0):
                conn.rollback()
                return False
            links, _ = _read_batch_links(conn)
            cur = links.get(batch_id)
            if (
                not cur
                or cur.get("server_url") != server_url
                or cur.get("user_id") != user_id
                or cur.get("space_id") != space_id
            ):
                conn.rollback()
                return False
            if not force_manual and not cur.get("auto_sync"):
                conn.rollback()
                return False
            # Preserve latest auto_sync unless caller explicitly overrides it (e.g. archived/401/needs_upgrade).
            preserved_auto = cur.get("auto_sync")
            preserved_deleted = dict(cur.get("deleted_remote_entries") or {})
            if clear_deleted_remote:
                for rid, cleared_rev in clear_deleted_remote.items():
                    existing_tomb = preserved_deleted.get(rid)
                    if existing_tomb is not None and int((existing_tomb or {}).get("revision") or 0) <= int(cleared_rev or 0):
                        preserved_deleted.pop(rid, None)
            cur.update(updates)
            if "auto_sync" not in updates:
                cur["auto_sync"] = preserved_auto
            cur["deleted_remote_entries"] = preserved_deleted
            links[batch_id] = cur
            _write_batch_links(conn, links, commit=False)
            conn.commit()
            return True
        except Exception:
            try:
                worker_db.conn.rollback()
            except Exception:
                pass
            raise
        finally:
            worker_db.close()

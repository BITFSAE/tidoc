"""Prefix-aware zero-dependency HTTP server for tidoc_hub."""

from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .cos_sign import CosSigner
from .db import HubDB, HubDbError
from .storage import BlobStore, CosBlobStore, LocalBlobStore, validate_sha256

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_BLOB_BYTES = 50 * 1024 * 1024
MAX_BLOB_BATCH = 8
JOIN_RATE_WINDOW = 600
JOIN_RATE_LIMIT = 20

RE_SPACE = re.compile(r"^/api/v1/spaces/([a-zA-Z0-9_-]+)$")
RE_SPACE_ENTRIES = re.compile(r"^/api/v1/spaces/([a-zA-Z0-9_-]+)/entries$")
RE_SPACE_ENTRY = re.compile(r"^/api/v1/spaces/([a-zA-Z0-9_-]+)/entries/([a-zA-Z0-9_-]+)$")
RE_SPACE_PUSH = re.compile(r"^/api/v1/spaces/([a-zA-Z0-9_-]+)/entries/push$")
RE_SPACE_UPLOAD_STATE = re.compile(r"^/api/v1/spaces/([a-zA-Z0-9_-]+)/entries/upload-state$")
RE_SPACE_REVIEW = re.compile(r"^/api/v1/spaces/([a-zA-Z0-9_-]+)/entries/([a-zA-Z0-9_-]+)/review$")
RE_SPACE_WITHDRAW = re.compile(r"^/api/v1/spaces/([a-zA-Z0-9_-]+)/entries/([a-zA-Z0-9_-]+)/withdraw$")
RE_INVITE_REVOKE = re.compile(r"^/api/v1/admin/invites/([a-zA-Z0-9_-]+)/revoke$")
RE_ADMIN_USER = re.compile(r"^/api/v1/admin/users/([a-zA-Z0-9_-]+)$")
RE_LOCAL_BLOB = re.compile(r"^/_local_blobs/([a-zA-Z0-9_-]+)/([0-9a-f]{64})$")


@dataclass
class HubConfig:
    host: str = "127.0.0.1"
    port: int = 8767
    prefix: str = "/tidoc-hub"
    data_dir: str = "./data"
    storage: str = "local"  # "cos" | "local"
    public_base_url: str = ""
    cos_secret_id: str = ""
    cos_secret_key: str = ""
    cos_bucket: str = ""
    cos_region: str = "ap-beijing"
    local_secret: str = ""
    trust_proxy: bool = True
    max_concurrency: int = 16
    join_rate_limit: int = JOIN_RATE_LIMIT
    max_blob_bytes: int = MAX_BLOB_BYTES

    def normalized_prefix(self) -> str:
        p = str(self.prefix or "").strip()
        if not p or p == "/":
            return ""
        return "/" + p.strip("/")


class _RateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = max(1, int(limit))
        self.window = max(1, int(window_seconds))
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            if len(self._hits) > 1000:
                stale = [k for k, v in self._hits.items() if not v or v[-1] < cutoff]
                for k in stale:
                    del self._hits[k]
                if len(self._hits) > 4000:
                    self._hits.clear()
            q = self._hits[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: HubConfig, db: HubDB, store: BlobStore):
        self.config = config
        self.prefix = config.normalized_prefix()
        self.db = db
        self.store = store
        self.semaphore = threading.BoundedSemaphore(max(1, int(config.max_concurrency)))
        self.join_limiter = _RateLimiter(config.join_rate_limit, JOIN_RATE_WINDOW)
        super().__init__((config.host, int(config.port)), _HubHandler)

    def close_resources(self) -> None:
        self.server_close()
        self.db.close()


class _HubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    timeout = 120

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _client_ip(self) -> str:
        peer_ip = self.client_address[0] if self.client_address else "unknown"
        # Only honor proxy-injected client IP headers when the TCP peer is a local reverse proxy.
        if self.server.config.trust_proxy and peer_ip in ("127.0.0.1", "::1"):
            real_ip = self.headers.get("X-Real-IP", "").strip()
            if real_ip:
                return real_ip
            xff = self.headers.get("X-Forwarded-For", "")
            if xff:
                parts = [p.strip() for p in xff.split(",") if p.strip()]
                if parts:
                    return parts[-1]
        return peer_ip

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_error(self, status: int, message: str, *, code: str = "", extra: dict | None = None) -> None:
        # Rejected POSTs may still have an unread body; do not reuse that connection.
        self.close_connection = True
        payload: dict[str, Any] = {"ok": False, "error": message}
        if code:
            payload["code"] = code
        if extra:
            payload.update(extra)
        self._send_json(status, payload)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HubDbError("无效的请求长度。", status=400, code="bad_request")
        if length > MAX_JSON_BYTES:
            raise HubDbError("请求体过大。", status=413, code="payload_too_large")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise HubDbError("请求 JSON 格式无效。", status=400, code="invalid_json") from exc
        if not isinstance(data, dict):
            raise HubDbError("请求 JSON 必须是对象。", status=400, code="invalid_json")
        return data

    def _route_path(self) -> tuple[str | None, dict[str, list[str]]]:
        parsed = urlsplit(self.path)
        raw_path = parsed.path or "/"
        prefix = self.server.prefix
        if prefix:
            if raw_path == prefix:
                rel = "/"
            elif raw_path.startswith(prefix + "/"):
                rel = raw_path[len(prefix):]
            else:
                return None, {}
        else:
            rel = raw_path
        return rel, parse_qs(parsed.query)

    def _require_auth(self, *, admin: bool = False) -> dict:
        auth = str(self.headers.get("Authorization") or "")
        if not auth.lower().startswith("bearer "):
            raise HubDbError("缺少访问令牌。", status=401, code="unauthorized")
        token = auth[7:].strip()
        user = self.server.db.authenticate(token)
        if not user:
            raise HubDbError("访问令牌无效或账号已停用。", status=401, code="unauthorized")
        if admin and user["role"] != "admin":
            raise HubDbError("需要管理员权限。", status=403, code="forbidden")
        return user

    def _presign_upload(self, user: dict, item: dict) -> dict:
        sha = validate_sha256(item.get("sha256", ""))
        size_bytes = int(item.get("size_bytes") or 0)
        if size_bytes <= 0 or size_bytes > self.server.config.max_blob_bytes:
            raise HubDbError("附件大小超出限制。", status=413, code="blob_too_large")
        ctype = str(item.get("content_type") or "application/octet-stream").strip()
        name = str(item.get("original_name") or "").strip()
        db, store = self.server.db, self.server.store
        storage_key = store.storage_key(user["id"], sha)
        existing = db.get_blob(user["id"], sha)
        if existing and int(existing["size_bytes"]) == size_bytes:
            ready = existing["status"] == "ready"
            if ready and store.mode == "cos":
                return {"exists": True, "sha256": sha}
            exists, actual_size, _etag = store.head_object(existing["storage_key"])
            if exists and actual_size == size_bytes:
                if not ready:
                    db.mark_blob_ready(user["id"], sha, actual_size)
                return {"exists": True, "sha256": sha}
        db.upsert_pending_blob(user["id"], sha, size_bytes, ctype, name, storage_key)
        signed = store.presign_put(storage_key, ctype, size_bytes=size_bytes, expires=300)
        return {"exists": False, "sha256": sha, **signed}

    def _confirm_upload(self, user: dict, item: dict) -> dict:
        sha = validate_sha256(item.get("sha256", ""))
        db = self.server.db
        pending = db.get_blob(user["id"], sha)
        if not pending:
            raise HubDbError("未找到待确认的附件上传记录。", status=404, code="blob_not_found")
        exists, size_bytes, _etag = self.server.store.head_object(pending["storage_key"])
        if not exists:
            raise HubDbError("存储中未找到已上传的文件，请重试上传。", status=400, code="blob_not_uploaded")
        if size_bytes != pending["size_bytes"] or not 0 < size_bytes <= self.server.config.max_blob_bytes:
            raise HubDbError("上传的文件大小与申请时不符。", status=400, code="blob_invalid_size")
        rec = db.mark_blob_ready(user["id"], sha, size_bytes)
        return {"sha256": sha, "size_bytes": rec["size_bytes"]}

    def _upload_batch(self, user: dict, handler) -> None:
        items = self._read_json().get("blobs")
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_BLOB_BATCH:
            raise HubDbError("本次处理的附件数量超出限制。", status=400, code="invalid_blob_batch")
        if not all(isinstance(item, dict) for item in items):
            raise HubDbError("附件信息格式无效。", status=400, code="invalid_blob_batch")
        self._send_json(200, {"ok": True, "blobs": [handler(user, item) for item in items]})

    def _dispatch(self, method: str) -> None:
        acquired = self.server.semaphore.acquire(blocking=True, timeout=15)
        if not acquired:
            self._send_error(503, "服务器繁忙，请稍后重试。", code="server_busy")
            return
        try:
            rel_path, qs = self._route_path()
            if rel_path is None:
                self._send_error(404, "路径不存在。", code="not_found")
                return

            # Local signed blob storage endpoints (only active in storage == "local")
            m_blob = RE_LOCAL_BLOB.match(rel_path)
            if m_blob:
                self._handle_local_blob(method, m_blob.group(1), m_blob.group(2), qs)
                return

            if method == "GET" and rel_path == "/health":
                self._send_json(200, {"ok": True, "service": "tidoc-hub", "storage": self.server.store.mode})
                return

            if method == "GET" and rel_path == "/api/v1/info":
                self._send_json(200, {
                    "ok": True,
                    "service": "tidoc-hub",
                    "api_version": 1,
                    "storage": self.server.store.mode,
                    "max_blob_bytes": self.server.config.max_blob_bytes,
                    "features": ["batch_blob_upload", "incremental_push"],
                })
                return

            if method == "POST" and rel_path == "/api/v1/join":
                if not self.server.join_limiter.allow(self._client_ip()):
                    self._send_error(429, "加入请求过于频繁，请稍后再试。", code="rate_limited")
                    return
                body = self._read_json()
                user, token = self.server.db.join_with_invite(
                    body.get("invite_code", ""),
                    body.get("name", ""),
                    body.get("reviewer", ""),
                    body.get("device_name", ""),
                )
                self._send_json(200, {"ok": True, "token": token, "user": user})
                return

            if method == "GET" and rel_path == "/api/v1/me":
                user = self._require_auth()
                self._send_json(200, {"ok": True, "user": user})
                return

            # Spaces
            if method == "GET" and rel_path == "/api/v1/spaces":
                user = self._require_auth()
                include_archived = (qs.get("include_archived", ["0"])[0] == "1")
                spaces = self.server.db.list_spaces(user, include_archived=include_archived)
                self._send_json(200, {"ok": True, "spaces": spaces, "features": ["batch_blob_upload", "incremental_push"]})
                return

            if method == "POST" and rel_path == "/api/v1/spaces":
                admin = self._require_auth(admin=True)
                body = self._read_json()
                space = self.server.db.create_space(
                    body.get("name", ""),
                    body.get("description", ""),
                    created_by=admin["id"],
                )
                self._send_json(200, {"ok": True, "space": space})
                return

            m = RE_SPACE.match(rel_path)
            if m and method == "PATCH":
                self._require_auth(admin=True)
                body = self._read_json()
                space = self.server.db.update_space(
                    m.group(1),
                    name=body.get("name"),
                    description=body.get("description"),
                    status=body.get("status"),
                )
                self._send_json(200, {"ok": True, "space": space})
                return

            # Admin invites & users
            if method == "GET" and rel_path == "/api/v1/admin/invites":
                self._require_auth(admin=True)
                self._send_json(200, {"ok": True, "invites": self.server.db.list_invites()})
                return

            if method == "POST" and rel_path == "/api/v1/admin/invites":
                admin = self._require_auth(admin=True)
                body = self._read_json()
                invite = self.server.db.create_invite(
                    role=body.get("role", "member"),
                    max_uses=body.get("max_uses", 20),
                    expires_days=body.get("expires_days", 30),
                    note=body.get("note", ""),
                    created_by=admin["id"],
                )
                self._send_json(200, {"ok": True, "invite": invite})
                return

            m = RE_INVITE_REVOKE.match(rel_path)
            if m and method == "POST":
                self._require_auth(admin=True)
                self.server.db.revoke_invite(m.group(1))
                self._send_json(200, {"ok": True})
                return

            if method == "GET" and rel_path == "/api/v1/admin/users":
                self._require_auth(admin=True)
                self._send_json(200, {"ok": True, "users": self.server.db.list_users()})
                return

            m = RE_ADMIN_USER.match(rel_path)
            if m and method == "PATCH":
                admin = self._require_auth(admin=True)
                body = self._read_json()
                updated = self.server.db.update_user(
                    admin,
                    m.group(1),
                    disabled=body.get("disabled"),
                    role=body.get("role"),
                )
                self._send_json(200, {"ok": True, "user": updated})
                return

            # Blobs (scoped per-user under blobs/<user_id>/<sha256>)
            if method == "POST" and rel_path == "/api/v1/blobs/presign-upload":
                result = self._presign_upload(self._require_auth(), self._read_json())
                self._send_json(200, {"ok": True, **result})
                return

            if method == "POST" and rel_path == "/api/v1/blobs/confirm":
                result = self._confirm_upload(self._require_auth(), self._read_json())
                self._send_json(200, {"ok": True, **result})
                return

            if method == "POST" and rel_path == "/api/v1/blobs/presign-upload-batch":
                self._upload_batch(self._require_auth(), self._presign_upload)
                return

            if method == "POST" and rel_path == "/api/v1/blobs/confirm-batch":
                self._upload_batch(self._require_auth(), self._confirm_upload)
                return

            if method == "POST" and rel_path == "/api/v1/blobs/presign-download":
                user = self._require_auth()
                body = self._read_json()
                sha = validate_sha256(body.get("sha256", ""))
                space_id = str(body.get("space_id") or "").strip()
                entry_id = str(body.get("entry_id") or "").strip()
                blob = self.server.db.authorize_blob_download(user, space_id, entry_id, sha)
                presigned = self.server.store.presign_get(blob["storage_key"], expires=300)
                self._send_json(200, {
                    "ok": True,
                    "sha256": sha,
                    "size_bytes": blob["size_bytes"],
                    **presigned,
                })
                return

            # Space entries
            m = RE_SPACE_ENTRIES.match(rel_path)
            if m and method == "GET":
                user = self._require_auth()
                include_withdrawn = (qs.get("include_withdrawn", ["1"])[0] != "0")
                summary = (qs.get("view", [""])[0] == "summary")
                space, entries = self.server.db.list_space_entries(
                    user, m.group(1), include_withdrawn=include_withdrawn, summary=summary,
                )
                self._send_json(200, {"ok": True, "space": space, "entries": entries})
                return

            m = RE_SPACE_UPLOAD_STATE.match(rel_path)
            if m and method == "POST":
                user = self._require_auth()
                ids = self._read_json().get("entry_ids")
                if not isinstance(ids, list) or not 1 <= len(ids) <= 500 or not all(
                    isinstance(eid, str) and re.fullmatch(r"[0-9a-f]{32}", eid) for eid in ids
                ):
                    raise HubDbError("待检查的条目标识无效。", status=400, code="invalid_entry_ids")
                self._send_json(200, {"ok": True, **self.server.db.upload_state(user, m.group(1), ids)})
                return

            m = RE_SPACE_PUSH.match(rel_path)
            if m and method == "POST":
                user = self._require_auth()
                body = self._read_json()
                entry, created = self.server.db.push_entry(user, m.group(1), body)
                unchanged = bool(entry.pop("_unchanged", False))
                self._send_json(200, {"ok": True, "entry": entry, "created": created, "unchanged": unchanged})
                return

            m = RE_SPACE_REVIEW.match(rel_path)
            if m and method == "POST":
                admin = self._require_auth(admin=True)
                body = self._read_json()
                entry = self.server.db.review_entry(
                    admin,
                    m.group(1),
                    m.group(2),
                    review_state=str(body.get("review_state") or ""),
                    review_note=str(body.get("review_note") or ""),
                    base_revision=int(body.get("base_revision") or 0),
                )
                self._send_json(200, {"ok": True, "entry": entry})
                return

            m = RE_SPACE_WITHDRAW.match(rel_path)
            if m and method == "POST":
                user = self._require_auth()
                body = self._read_json()
                entry = self.server.db.withdraw_entry(
                    user,
                    m.group(1),
                    m.group(2),
                    base_revision=int(body.get("base_revision") or 0),
                )
                self._send_json(200, {"ok": True, "entry": entry})
                return

            m = RE_SPACE_ENTRY.match(rel_path)
            if m and method == "GET":
                user = self._require_auth()
                entry = self.server.db.get_space_entry(user, m.group(1), m.group(2))
                self._send_json(200, {"ok": True, "entry": entry})
                return

            self._send_error(404, "接口不存在。", code="not_found")
        except (BrokenPipeError, ConnectionResetError):
            return
        except HubDbError as exc:
            self._send_error(exc.status, str(exc), code=exc.code, extra=exc.extra)
        except ValueError as exc:
            self._send_error(400, str(exc), code="bad_request")
        except Exception as exc:  # noqa: BLE001
            try:
                self._send_error(500, f"服务器内部错误：{exc}", code="internal_error")
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            self.server.semaphore.release()

    def _handle_local_blob(self, method: str, owner_id: str, sha256: str, qs: dict[str, list[str]]) -> None:
        if not isinstance(self.server.store, LocalBlobStore):
            self._send_error(404, "未启用本地存储。", code="not_found")
            return
        store: LocalBlobStore = self.server.store
        exp = qs.get("exp", [""])[0]
        sig = qs.get("sig", [""])[0]
        sz_raw = qs.get("sz", ["0"])[0]
        if method == "PUT":
            ok_sig, expected_size = store.verify_signature("put", owner_id, sha256, exp, sig, sz_raw)
            if not ok_sig:
                self._send_error(403, "上传签名无效或已过期。", code="invalid_signature")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send_error(400, "缺少 Content-Length。", code="bad_request")
                return
            if length <= 0 or length > self.server.config.max_blob_bytes:
                self._send_error(413, "附件大小超出限制。", code="blob_too_large")
                return
            if expected_size > 0 and length != expected_size:
                self._send_error(400, "上传大小与预签名声明不符。", code="size_mismatch")
                return
            data = self.rfile.read(length)
            try:
                size = store.write_blob(owner_id, sha256, data)
            except ValueError as exc:
                self._send_error(400, str(exc), code="sha256_mismatch")
                return
            self._send_json(200, {"ok": True, "sha256": sha256, "size_bytes": size})
            return

        if method == "GET":
            ok_sig, _ = store.verify_signature("get", owner_id, sha256, exp, sig)
            if not ok_sig:
                self._send_error(403, "下载签名无效或已过期。", code="invalid_signature")
                return
            storage_key = store.storage_key(owner_id, sha256)
            path = store.path_for_key(storage_key)
            if not path.is_file():
                self._send_error(404, "文件不存在。", code="not_found")
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return

        self._send_error(405, "不支持的请求方法。", code="method_not_allowed")

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_PUT(self) -> None:
        self._dispatch("PUT")


def create_server(config: HubConfig) -> HubServer:
    data_dir = Path(config.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    db = HubDB(data_dir / "tidoc_hub.sqlite")
    if config.storage == "cos":
        signer = CosSigner(
            config.cos_secret_id,
            config.cos_secret_key,
            config.cos_bucket,
            config.cos_region,
        )
        store: BlobStore = CosBlobStore(signer)
    else:
        prefix = config.normalized_prefix()
        public_base = config.public_base_url or f"http://{config.host}:{config.port}{prefix}"
        store = LocalBlobStore(data_dir / "blobs", public_base, secret=config.local_secret)

    server = HubServer(config, db, store)
    if isinstance(store, LocalBlobStore) and not config.public_base_url:
        bound_host, bound_port = server.server_address[:2]
        store.public_base_url = f"http://{bound_host}:{bound_port}{server.prefix}"
    return server

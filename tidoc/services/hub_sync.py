"""团队协同共享区客户端同步服务（显式用户触发，不产生后台自动联网）。

复用 bindle.py / bindle_import.py / entry_merge.py 的规范条目序列化、三方合并规划与原子落库，
把原本需要反复互传 .tidoc 绑定包的流程变成基于共享区 + 对象存储的按需同步。
"""

from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
import re
import shutil
import socket
import tempfile
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPDefaultErrorHandler, HTTPErrorProcessor, HTTPHandler, HTTPSHandler, OpenerDirector, Request

from ..db.attachments import AttachmentRepo
from ..db.entries import EntryRepo
from . import bindle as storage
from . import bindle_import
from .entry_merge import (
    BATCH_NOTE_PREFIX,
    build_entry_snapshot,
    known_ancestors,
    load_exchange_bases,
    merge_text,
    plan_entry_merge,
    record_exchange_base,
    snapshot_digest,
)
from .payment_ai import NoRedirect, ssl_context

MAX_HUB_BLOB_BYTES = 50 * 1024 * 1024
DEFAULT_TIMEOUT = 25
BLOB_TIMEOUT = 120
HUB_BLOB_BATCH_SIZE = 8
HUB_UPLOAD_WORKERS = 3
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _http_only_opener() -> OpenerDirector:
    """Build an opener that ONLY speaks HTTP/HTTPS (never file:// or ftp://) and never follows redirects."""
    opener = OpenerDirector()
    opener.add_handler(HTTPHandler())
    opener.add_handler(HTTPSHandler(context=ssl_context()))
    opener.add_handler(NoRedirect())
    opener.add_handler(HTTPErrorProcessor())
    opener.add_handler(HTTPDefaultErrorHandler())
    return opener


def _validate_http_url(url: str, label: str = "链接") -> str:
    value = str(url or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HubError(f"无效的{label}地址。")
    return value


def _validate_sha256(sha: str) -> str:
    cleaned = str(sha or "").strip().lower()
    if not _SHA256_RE.match(cleaned):
        raise HubError("附件文件摘要格式无效。")
    return cleaned


class HubError(RuntimeError):
    """共享区服务端返回的明确错误或网络错误。"""

    def __init__(self, message: str, *, status: int = 0, code: str = "", payload: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload or {}


def normalize_server_url(url: str) -> str:
    value = str(url or "").strip().rstrip("/")
    if not value:
        raise HubError("请填写共享区服务器地址。")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HubError("服务器地址格式不正确，请以 https:// 或 http:// 开头。")
    return value


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class HubClient:
    """面向 tidoc_hub REST API 的轻量纯标准库客户端。"""

    def __init__(self, base_url: str, token: str = ""):
        self.base_url = normalize_server_url(base_url)
        self.token = str(token or "").strip()
        self._opener = _http_only_opener()
        self._supports_blob_batch = True
        endpoint = urlsplit(self.base_url)
        if endpoint.scheme == "https":
            self._hub_connection = HTTPSConnection(endpoint.hostname, endpoint.port, context=ssl_context())
        else:
            self._hub_connection = HTTPConnection(endpoint.hostname, endpoint.port)
        self._hub_connection_lock = threading.Lock()
        self._active_sock: socket.socket | None = None
        self._closed = threading.Event()

    def close(self) -> None:
        # Disconnect is a UI action: do not wait behind a stalled HTTP response. Interrupt
        # the active socket now (including after HTTPConnection hands it off to HTTPResponse
        # for HTTP/1.0 or Connection: close); the request owner closes its connection on exit.
        self._closed.set()
        connection = self._hub_connection
        seen: set[int] = set()
        for sock in (self._active_sock, connection.sock):
            if sock is not None and id(sock) not in seen:
                seen.add(id(sock))
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if self._hub_connection_lock.acquire(blocking=False):
            try:
                connection.close()
            finally:
                self._hub_connection_lock.release()

    def _hub_exchange(self, method: str, route: str, data: bytes | None, headers: dict, timeout: int):
        with self._hub_connection_lock:
            connection = self._hub_connection
            reuse = connection.sock is not None
            for attempt in range(2):
                if self._closed.is_set():
                    connection.close()
                    raise OSError("共享区连接已关闭")
                try:
                    connection.timeout = timeout
                    if connection.sock is not None:
                        connection.sock.settimeout(timeout)
                    connection.request(method, route, body=data, headers=headers)
                    self._active_sock = connection.sock
                    if self._closed.is_set():
                        raise OSError("共享区连接已关闭")
                    with connection.getresponse() as response:
                        body = response.read()
                        if self._closed.is_set():
                            raise OSError("共享区连接已关闭")
                        return response.status, body
                except (HTTPException, OSError):
                    connection.close()
                    if attempt or not reuse or method != "GET":
                        raise
                finally:
                    self._active_sock = None
                    if self._closed.is_set():
                        connection.close()

    def _request_json(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        query: dict[str, Any] | None = None,
        auth: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> dict:
        url = f"{self.base_url}{path}"
        if query:
            qs = urlencode({k: v for k, v in query.items() if v is not None and v != ""})
            if qs:
                url = f"{url}?{qs}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "Tidoc-HubClient/1",
        }
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        endpoint = urlsplit(url)
        route = endpoint.path + ("?" + endpoint.query if endpoint.query else "")
        try:
            status, raw = self._hub_exchange(method.upper(), route, data, headers, timeout)
        except (HTTPException, OSError) as exc:
            raise HubError(f"无法连接共享区服务器：{exc}") from exc
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError as exc:
            if 200 <= status < 300:
                raise HubError("共享区服务器返回了无法解析的响应。") from exc
            payload = {}
        if not 200 <= status < 300:
            if not isinstance(payload, dict):
                payload = {}
            msg = str(payload.get("error") or "").strip()
            code = str(payload.get("code") or "").strip()
            if not msg:
                if status == 401:
                    msg = "共享区登录凭证已失效或被停用，请重新加入。"
                elif status == 403:
                    msg = "当前账号没有执行该操作的权限。"
                elif status == 404:
                    msg = "共享区中未找到对应记录或接口。"
                elif status == 429:
                    msg = "请求过于频繁，请稍后再试。"
                else:
                    msg = f"共享区服务返回错误（HTTP {status}）。"
            raise HubError(msg, status=status, code=code, payload=payload)

        if not isinstance(payload, dict):
            raise HubError("共享区服务器返回格式异常。")
        if payload.get("ok") is False:
            raise HubError(
                str(payload.get("error") or "共享区请求失败。"),
                code=str(payload.get("code") or ""),
                payload=payload,
            )
        return payload

    def upload_blob(
        self,
        upload_url: str,
        file_path: Path,
        headers: dict[str, str] | None = None,
        *,
        timeout: int = BLOB_TIMEOUT,
    ) -> None:
        safe_url = _validate_http_url(upload_url, "上传")
        size = file_path.stat().st_size
        if size <= 0 or size > MAX_HUB_BLOB_BYTES:
            raise HubError(f"附件 {file_path.name} 大小超出限制（上限 {MAX_HUB_BLOB_BYTES // (1024 * 1024)} MB）。")
        req_headers = dict(headers or {})
        req_headers.setdefault("Content-Type", mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
        req_headers["Content-Length"] = str(size)
        # Never attach Hub Bearer token to COS/storage presigned PUT requests.
        try:
            with file_path.open("rb") as data:
                req = Request(safe_url, data=data, headers=req_headers, method="PUT")
                with self._opener.open(req, timeout=timeout) as resp:
                    resp.read()
        except HTTPError as exc:
            raise HubError(f"上传附件 {file_path.name} 失败（HTTP {exc.code}）。", status=exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise HubError(f"上传附件 {file_path.name} 失败：{exc}") from exc

    def download_blob(
        self,
        download_url: str,
        dest_path: Path,
        *,
        expected_sha256: str = "",
        timeout: int = BLOB_TIMEOUT,
    ) -> str:
        safe_url = _validate_http_url(download_url, "下载")
        req = Request(safe_url, headers={"User-Agent": "Tidoc-HubClient/1"}, method="GET")
        digest = hashlib.sha256()
        total = 0
        try:
            with self._opener.open(req, timeout=timeout) as resp, dest_path.open("wb") as out:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_HUB_BLOB_BYTES:
                        raise HubError("下载的附件超过大小上限。")
                    digest.update(chunk)
                    out.write(chunk)
        except HubError:
            dest_path.unlink(missing_ok=True)
            raise
        except HTTPError as exc:
            dest_path.unlink(missing_ok=True)
            raise HubError(f"下载附件失败（HTTP {exc.code}）。", status=exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            dest_path.unlink(missing_ok=True)
            raise HubError(f"下载附件失败：{exc}") from exc

        actual = digest.hexdigest()
        if expected_sha256 and actual != expected_sha256.lower():
            dest_path.unlink(missing_ok=True)
            raise HubError("下载的附件完整性校验失败，已中止同步。")
        return actual

    # --- API endpoints ---
    def info(self) -> dict:
        return self._request_json("GET", "/api/v1/info", auth=False)

    def join(self, invite_code: str, name: str, reviewer: str = "", device_name: str = "") -> dict:
        return self._request_json(
            "POST",
            "/api/v1/join",
            {
                "invite_code": str(invite_code or "").strip(),
                "name": str(name or "").strip(),
                "reviewer": str(reviewer or "").strip(),
                "device_name": str(device_name or "").strip(),
            },
            auth=False,
        )

    def me(self) -> dict:
        return self._request_json("GET", "/api/v1/me")

    def list_spaces(self, include_archived: bool = False) -> list[dict]:
        res = self._request_json(
            "GET",
            "/api/v1/spaces",
            query={"include_archived": "1"} if include_archived else None,
        )
        self._supports_blob_batch = "batch_blob_upload" in (res.get("features") or [])
        return list(res.get("spaces") or [])

    def create_space(self, name: str, description: str = "") -> dict:
        res = self._request_json(
            "POST",
            "/api/v1/spaces",
            {"name": str(name or "").strip(), "description": str(description or "").strip()},
        )
        return res["space"]

    def update_space(self, space_id: str, **fields) -> dict:
        res = self._request_json("PATCH", f"/api/v1/spaces/{space_id}", fields)
        return res["space"]

    def list_invites(self) -> list[dict]:
        res = self._request_json("GET", "/api/v1/admin/invites")
        return list(res.get("invites") or [])

    def create_invite(
        self,
        *,
        role: str = "member",
        max_uses: int = 20,
        expires_days: int = 30,
        note: str = "",
    ) -> dict:
        res = self._request_json(
            "POST",
            "/api/v1/admin/invites",
            {
                "role": role,
                "max_uses": int(max_uses),
                "expires_days": int(expires_days),
                "note": str(note or "").strip(),
            },
        )
        return res["invite"]

    def revoke_invite(self, code: str) -> None:
        self._request_json("POST", f"/api/v1/admin/invites/{code}/revoke", {})

    def list_users(self) -> list[dict]:
        res = self._request_json("GET", "/api/v1/admin/users")
        return list(res.get("users") or [])

    def update_user(self, user_id: str, **fields) -> dict:
        res = self._request_json("PATCH", f"/api/v1/admin/users/{user_id}", fields)
        return res["user"]

    def presign_upload(self, sha256: str, size_bytes: int, content_type: str, original_name: str = "") -> dict:
        return self._request_json(
            "POST",
            "/api/v1/blobs/presign-upload",
            {
                "sha256": sha256.lower(),
                "size_bytes": int(size_bytes),
                "content_type": content_type or "application/octet-stream",
                "original_name": original_name,
            },
        )

    def confirm_blob(self, sha256: str) -> dict:
        return self._request_json("POST", "/api/v1/blobs/confirm", {"sha256": sha256.lower()})

    def presign_upload_batch(self, blobs: list[dict]) -> dict[str, dict] | None:
        if not self._supports_blob_batch:
            return None
        try:
            res = self._request_json(
                "POST", "/api/v1/blobs/presign-upload-batch", {"blobs": blobs}, timeout=BLOB_TIMEOUT,
            )
        except HubError as exc:
            if exc.status not in (404, 405) or exc.code not in ("", "not_found", "method_not_allowed"):
                raise
            self._supports_blob_batch = False
            return None
        signed = {str(item.get("sha256")): item for item in (res.get("blobs") or []) if isinstance(item, dict)}
        if signed.keys() != {blob["sha256"] for blob in blobs}:
            raise HubError("共享区未返回完整的附件上传信息，请重试。")
        return signed

    def confirm_blob_batch(self, sha256s: list[str]) -> None:
        blobs = [{"sha256": sha} for sha in sha256s]
        res = self._request_json("POST", "/api/v1/blobs/confirm-batch", {"blobs": blobs}, timeout=BLOB_TIMEOUT)
        confirmed = {item.get("sha256") for item in (res.get("blobs") or []) if isinstance(item, dict)}
        if confirmed != set(sha256s):
            raise HubError("共享区未返回完整的附件确认结果，请重试。")

    def presign_download(self, sha256: str, space_id: str, entry_id: str) -> dict:
        return self._request_json(
            "POST",
            "/api/v1/blobs/presign-download",
            {"sha256": sha256.lower(), "space_id": space_id, "entry_id": entry_id},
        )

    def list_space_entries(self, space_id: str, *, include_withdrawn: bool = True, summary: bool = False) -> dict:
        return self._request_json(
            "GET",
            f"/api/v1/spaces/{space_id}/entries",
            query={"include_withdrawn": "1" if include_withdrawn else "0", "view": "summary" if summary else ""},
        )

    def sync_state(self, space_id: str) -> dict:
        return self._request_json("GET", f"/api/v1/spaces/{space_id}/sync-state")

    def get_space_entry(self, space_id: str, entry_id: str) -> dict:
        res = self._request_json("GET", f"/api/v1/spaces/{space_id}/entries/{entry_id}")
        return res["entry"]

    def upload_state(self, space_id: str, remote_ids: list[str]) -> dict | None:
        states: dict = {}
        for start in range(0, len(remote_ids), 500):
            try:
                result = self._request_json(
                    "POST", f"/api/v1/spaces/{space_id}/entries/upload-state",
                    {"entry_ids": remote_ids[start:start + 500]},
                )
            except HubError as exc:
                if exc.status in (404, 405) and exc.code in ("", "not_found", "method_not_allowed"):
                    return None
                raise
            states.update(result["entries"])
        return states

    def push_entry(self, space_id: str, body: dict) -> dict:
        return self._request_json("POST", f"/api/v1/spaces/{space_id}/entries/push", body)

    def review_entry(
        self,
        space_id: str,
        entry_id: str,
        *,
        review_state: str,
        review_note: str = "",
        base_revision: int = 0,
    ) -> dict:
        res = self._request_json(
            "POST",
            f"/api/v1/spaces/{space_id}/entries/{entry_id}/review",
            {
                "review_state": review_state,
                "review_note": str(review_note or "").strip(),
                "base_revision": int(base_revision or 0),
            },
        )
        return res["entry"]

    def withdraw_entry(self, space_id: str, entry_id: str, *, base_revision: int = 0) -> dict:
        res = self._request_json(
            "POST",
            f"/api/v1/spaces/{space_id}/entries/{entry_id}/withdraw",
            {"base_revision": int(base_revision or 0)},
        )
        return res["entry"]


# ---------------------------------------------------------------------------
# Local DB hub_entry_links helpers
# ---------------------------------------------------------------------------


def get_hub_link(conn, entry_id: str) -> dict | None:
    row = conn.execute(
        """SELECT entry_id, server_url, space_id, space_name, remote_entry_id,
                  remote_revision, owner_user_id, owner_name, review_state,
                  review_note, last_synced_at
             FROM hub_entry_links
            WHERE entry_id = ?""",
        (entry_id,),
    ).fetchone()
    return dict(row) if row else None


def upsert_hub_link(
    conn,
    entry_id: str,
    *,
    server_url: str,
    space_id: str,
    space_name: str,
    remote_entry: dict,
    synced_at: str,
) -> dict:
    record = {
        "entry_id": entry_id,
        "server_url": server_url,
        "space_id": space_id,
        "space_name": space_name,
        "remote_entry_id": str(remote_entry.get("id") or ""),
        "remote_revision": int(remote_entry.get("revision") or 0),
        "owner_user_id": str(remote_entry.get("owner_user_id") or ""),
        "owner_name": str(remote_entry.get("owner_name") or ""),
        "review_state": str(remote_entry.get("review_state") or ""),
        "review_note": str(remote_entry.get("review_note") or ""),
        "last_synced_at": synced_at,
    }
    conn.execute(
        """INSERT INTO hub_entry_links(
               entry_id, server_url, space_id, space_name, remote_entry_id,
               remote_revision, owner_user_id, owner_name, review_state,
               review_note, last_synced_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(entry_id) DO UPDATE SET
               server_url = excluded.server_url,
               space_id = excluded.space_id,
               space_name = excluded.space_name,
               remote_entry_id = excluded.remote_entry_id,
               remote_revision = excluded.remote_revision,
               owner_user_id = excluded.owner_user_id,
               owner_name = excluded.owner_name,
               review_state = excluded.review_state,
               review_note = excluded.review_note,
               last_synced_at = excluded.last_synced_at""",
        (
            record["entry_id"], record["server_url"], record["space_id"],
            record["space_name"], record["remote_entry_id"], record["remote_revision"],
            record["owner_user_id"], record["owner_name"], record["review_state"],
            record["review_note"], record["last_synced_at"],
        ),
    )
    return record


# ---------------------------------------------------------------------------
# Virtual blob archive adapter for bindle_import / bindle._merge_existing_entry
# ---------------------------------------------------------------------------


class _DownloadedBlobArchive:
    """Duck-typed zipfile-compatible view backed by downloaded temp blob files."""

    def __init__(self, member_paths: dict[str, Path]):
        self._member_paths = member_paths

    def getinfo(self, name: str):
        if name not in self._member_paths:
            raise KeyError(name)
        return self._member_paths[name]

    def open(self, name: str):
        if name not in self._member_paths:
            raise KeyError(name)
        return self._member_paths[name].open("rb")


# ---------------------------------------------------------------------------
# Entry canonical serialization for Hub push
# ---------------------------------------------------------------------------


def _build_hub_entry_bundle(
    entries_repo: EntryRepo,
    attachments_repo: AttachmentRepo,
    entry_id: str,
    *,
    adapter_service=None,
) -> tuple[dict, dict, list[tuple[str, Path, str, int, str]], dict, list[str], str]:
    """Serialize one local entry into the canonical shared-space representation.

    Always includes canonical notes/tags and excludes payees / local secrets.
    Returns (payload, snapshot, blob_files, profile_dict, ancestors, invoice_sha256).
    """
    entry = entries_repo.get(entry_id)
    if not entry:
        raise HubError("条目不存在或已被删除。")
    conn = entries_repo.db.conn
    service = storage._adapter_service(entries_repo, adapter_service)

    profile_row = conn.execute(
        "SELECT id, name, reviewer, is_default FROM profiles WHERE id = ?",
        (entry.get("profile_id") or "",),
    ).fetchone()
    profile_dict = storage._serialize_profile(dict(profile_row)) if profile_row else {
        "id": entry.get("profile_id") or "",
        "name": "",
        "reviewer": "",
        "is_default": False,
    }
    entry["_profile_name"] = profile_dict["name"]
    entry["_reviewer"] = profile_dict["reviewer"]

    ocr_rows = conn.execute(
        """SELECT provider, file_sha256, file_name, raw_json, normalized,
                  closure_pass, applied_at, pending, applied_changes,
                  api_calls, status, error, created_at
             FROM ocr_results WHERE entry_id = ? ORDER BY id""",
        (entry_id,),
    ).fetchall()
    entry["_ocr_results"] = [dict(r) for r in ocr_rows]

    batch_note_row = conn.execute(
        "SELECT note FROM batch_entries WHERE entry_id = ? LIMIT 1",
        (entry_id,),
    ).fetchone()
    entry["_batch_note"] = str(batch_note_row["note"] or "").strip() if batch_note_row else ""

    # Adapter public projection (same boundary as bindle export; ask/sensitive fields excluded)
    def _clean_adapter_source(src: dict) -> dict | None:
        if not isinstance(src, dict) or not isinstance(src.get("definition"), dict):
            return None
        from ..adapters.transfer import project_extension_data, public_rule_projection
        clean_def = public_rule_projection(src["definition"], include_ask_fields=set())
        clean_vals, clean_hist = project_extension_data(
            clean_def,
            src.get("extension_values") or [],
            src.get("extension_history") or [],
            include_ask_fields=set(),
        )
        return {
            "source_revision_id": str(src.get("source_revision_id") or ""),
            "source_revision_digest": str(src.get("source_revision_digest") or ""),
            "definition": clean_def,
            "extension_values": clean_vals,
            "extension_history": clean_hist,
            "sources": [],
        }

    sanitized_sources = [
        cleaned for cleaned in (_clean_adapter_source(s) for s in (entry.get("adapter_sources") or []))
        if cleaned is not None
    ]
    adapter_payload = None
    revision_id = entry.get("scheme_revision_id") or ""
    if revision_id and service:
        definition, values, history = storage._extension_records(service, entry_id, revision_id)
        if definition:
            from ..adapters.transfer import project_extension_data, public_rule_projection
            clean_def = public_rule_projection(definition, include_ask_fields=set())
            clean_values, clean_history = project_extension_data(
                clean_def, values, history, include_ask_fields=set()
            )
            adapter_payload = {
                "source_revision_id": revision_id,
                "source_revision_digest": str(definition.get("digest") or ""),
                "definition": clean_def,
                "extension_values": clean_values,
                "extension_history": clean_history,
                "sources": sanitized_sources,
            }
    entry["_adapter"] = adapter_payload

    # Collect local attachment files and make sure sha256 matches actual file bytes.
    blob_files: list[tuple[str, Path, str, int, str]] = []
    seen_shas: set[str] = set()
    invoice_sha256 = ""
    valid_attachments: list[dict] = []
    for att in entry.get("attachments") or []:
        stored_path = str(att.get("stored_path") or "")
        if not stored_path:
            continue
        file_path = attachments_repo.data_root.attachments_dir / stored_path
        if not file_path.is_file():
            raise HubError(f"附件文件缺失：{att.get('original_name') or stored_path}，请先补齐后再上传。")
        sha = str(att.get("sha256") or "").strip().lower()
        if not sha or len(sha) != 64:
            sha = _file_sha256(file_path)
            att = {**att, "sha256": sha}
        size = file_path.stat().st_size
        if size <= 0 or size > MAX_HUB_BLOB_BYTES:
            raise HubError(f"附件 {att.get('original_name') or file_path.name} 大小超出限制。")
        if not invoice_sha256 and att.get("type") in {"invoice_pdf", "invoice_xml"}:
            invoice_sha256 = sha
        if sha not in seen_shas:
            seen_shas.add(sha)
            ctype = mimetypes.guess_type(att.get("original_name") or file_path.name)[0] or "application/octet-stream"
            blob_files.append((sha, file_path, str(att.get("original_name") or file_path.name), size, ctype))
        valid_attachments.append(att)
    entry["attachments"] = valid_attachments

    payload = storage._serialize_entry(entry, include_notes=True, include_tags=True)
    payload.pop("payees", None)
    snap = build_entry_snapshot(payload)
    ancestors = known_ancestors(conn, entry_id)
    return payload, snap, blob_files, profile_dict, ancestors, invoice_sha256


# ---------------------------------------------------------------------------
# Push to shared space
# ---------------------------------------------------------------------------


def _upload_entry_blobs(
    client: HubClient, blob_files: list[tuple[str, Path, str, int, str]], confirmed_shas: set[str],
    *, bound_shas: set[str],
) -> list[str]:
    pending = [blob for blob in blob_files if blob[0] not in confirmed_shas and blob[0] not in bound_shas]

    def upload(blob, signed):
        _sha, path, _name, _size, ctype = blob
        client.upload_blob(signed["upload_url"], path, headers=signed.get("headers") or {"Content-Type": ctype})

    for start in range(0, len(pending), HUB_BLOB_BATCH_SIZE):
        group = pending[start:start + HUB_BLOB_BATCH_SIZE]
        signed_batch = None
        if len(group) > 1:
            signed_batch = client.presign_upload_batch([
                {"sha256": sha, "size_bytes": size, "content_type": ctype, "original_name": name}
                for sha, _path, name, size, ctype in group
            ])
        if signed_batch is None:
            for blob in group:
                sha, _path, name, size, ctype = blob
                signed = client.presign_upload(sha, size, ctype, name)
                if not signed.get("exists"):
                    upload(blob, signed)
                    client.confirm_blob(sha)
                confirmed_shas.add(sha)
            continue

        missing = []
        for blob in group:
            sha = blob[0]
            if signed_batch[sha].get("exists"):
                confirmed_shas.add(sha)
            else:
                missing.append(blob)
        if not missing:
            continue

        uploaded = []
        first_error = None
        with ThreadPoolExecutor(max_workers=min(HUB_UPLOAD_WORKERS, len(missing))) as executor:
            futures = [(blob[0], executor.submit(upload, blob, signed_batch[blob[0]])) for blob in missing]
            for sha, future in futures:
                try:
                    future.result()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                else:
                    uploaded.append(sha)
        if uploaded:
            try:
                if len(uploaded) == 1:
                    client.confirm_blob(uploaded[0])
                else:
                    client.confirm_blob_batch(uploaded)
                confirmed_shas.update(uploaded)
            except HubError:
                if first_error is None:
                    raise
        if first_error is not None:
            raise first_error
    return [blob[0] for blob in blob_files]


def push_entries(
    client: HubClient,
    entries_repo: EntryRepo,
    attachments_repo: AttachmentRepo,
    space_id: str,
    entry_ids: list[str],
    *,
    space_name: str = "",
    adapter_service=None,
    review_state: str | None = None,
    review_note: str | None = None,
    upload_state: dict | None = None,
) -> dict:
    if not entry_ids:
        raise HubError("请先选择要上传到共享区的条目。")
    conn = entries_repo.db.conn
    now = datetime.now().isoformat(timespec="seconds")
    confirmed_shas: set[str] = set()

    pushed: list[dict] = []
    unchanged: list[dict] = []
    conflicts: list[dict] = []
    duplicates: list[dict] = []
    locked: list[dict] = []
    failed: list[dict] = []

    for entry_id in dict.fromkeys(str(eid) for eid in entry_ids if str(eid)):
        try:
            payload, snap, blob_files, profile_dict, ancestors, invoice_sha256 = _build_hub_entry_bundle(
                entries_repo, attachments_repo, entry_id, adapter_service=adapter_service
            )
            existing_link = get_hub_link(conn, entry_id)
            remote_entry_id = ""
            base_revision = 0
            if (
                existing_link
                and existing_link["server_url"] == client.base_url
                and existing_link["space_id"] == space_id
            ):
                remote_entry_id = existing_link["remote_entry_id"]
                base_revision = int(existing_link["remote_revision"] or 0)

            bound_shas: set[str] = set()
            if upload_state is not None and remote_entry_id:
                remote = upload_state.get(remote_entry_id)
                if not remote:
                    raise HubError("共享区中未找到对应条目，请先核对当前空间。", status=404, code="not_found")
                bound_shas = set(remote["blob_shas"])

            blob_shas = _upload_entry_blobs(client, blob_files, confirmed_shas, bound_shas=bound_shas)
            digest = snapshot_digest(snap)
            push_body = {
                "remote_entry_id": remote_entry_id,
                "base_revision": base_revision,
                "invoice_no": str(payload.get("invoice_no") or "").strip(),
                "invoice_sha256": invoice_sha256,
                "snapshot_digest": digest,
                "ancestors": ancestors,
                "profile": profile_dict,
                "payload": payload,
                "blob_shas": blob_shas,
            }
            if review_state is not None:
                push_body["review_state"] = review_state
            if review_note is not None:
                push_body["review_note"] = str(review_note)
            if upload_state is not None:
                push_body["skip_unchanged"] = True
            push_body["submission_id"] = snapshot_digest(push_body)

            res = client.push_entry(space_id, push_body)
            remote_entry = res["entry"]
            record_exchange_base(conn, entry_id, snap, "export", now)
            link = upsert_hub_link(
                conn,
                entry_id,
                server_url=client.base_url,
                space_id=space_id,
                space_name=space_name or (existing_link["space_name"] if existing_link else ""),
                remote_entry=remote_entry,
                synced_at=now,
            )
            conn.commit()
            outcome = unchanged if res.get("unchanged") else pushed
            outcome.append({
                "entry_id": entry_id,
                "remote_entry_id": remote_entry["id"],
                "revision": remote_entry["revision"],
                "created": bool(res.get("created")),
                "invoice_no": payload.get("invoice_no") or "",
                "seller": payload.get("seller") or "",
                "review_state": remote_entry.get("review_state") or "",
                "link": link,
            })
        except HubError as exc:
            item = {
                "entry_id": entry_id,
                "error": str(exc),
                "code": exc.code,
                "payload": exc.payload,
            }
            if exc.code == "revision_conflict":
                conflicts.append(item)
            elif exc.code == "duplicate_invoice":
                duplicates.append(item)
            elif exc.code == "entry_locked":
                locked.append(item)
            else:
                failed.append(item)
        except Exception as exc:  # noqa: BLE001
            failed.append({"entry_id": entry_id, "error": str(exc), "code": "local_error", "payload": {}})

    parts = []
    if pushed:
        parts.append(f"已上传 {len(pushed)} 条")
    if unchanged:
        parts.append(f"无需上传 {len(unchanged)} 条")
    if conflicts:
        parts.append(f"{len(conflicts)} 条远端已有更新，请先拉取合并后再上传")
    if duplicates:
        parts.append(f"{len(duplicates)} 条发票已存在于共享区（已拦截重复提交）")
    if locked:
        parts.append(f"{len(locked)} 条已被审核锁定")
    if failed:
        parts.append(f"{len(failed)} 条失败")
    message = "；".join(parts) + "。" if parts else "没有条目被上传。"
    return {
        "pushed": pushed,
        "unchanged": unchanged,
        "conflicts": conflicts,
        "duplicates": duplicates,
        "locked": locked,
        "failed": failed,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Pull preview & apply (reusing Phase 1 entry_merge + bindle_import)
# ---------------------------------------------------------------------------


def _hub_link_by_remote(conn, server_url: str, space_id: str) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT h.entry_id, h.server_url, h.space_id, h.space_name, h.remote_entry_id,
                  h.remote_revision, h.owner_user_id, h.owner_name, h.review_state,
                  h.review_note, h.last_synced_at
             FROM hub_entry_links h
             JOIN entries e ON e.id = h.entry_id
            WHERE h.server_url = ? AND h.space_id = ?""",
        (server_url, space_id),
    ).fetchall()
    return {str(r["remote_entry_id"]): dict(r) for r in rows if r["remote_entry_id"]}


def preview_pull(
    client: HubClient,
    entries_repo: EntryRepo,
    space_id: str,
    *,
    adapter_service=None,
    target_scheme_id: str | None = None,
    listing: dict | None = None,
) -> dict:
    listing = listing if listing is not None else client.list_space_entries(space_id, include_withdrawn=True)
    space = listing.get("space") or {"id": space_id, "name": ""}
    space_name = str(space.get("name") or "")
    remote_entries = list(listing.get("entries") or [])

    conn = entries_repo.db.conn
    service = storage._adapter_service(entries_repo, adapter_service)
    links_by_remote = _hub_link_by_remote(conn, client.base_url, space_id)
    by_number, by_hash = storage._entry_identity_maps(conn)

    active_remotes: list[tuple[dict, dict, str]] = []
    withdrawn_local: list[dict] = []

    for remote in remote_entries:
        raw_payload = copy.deepcopy(remote.get("payload") or {})
        raw_payload.pop("payees", None)
        storage._sanitize_exchange_entry(raw_payload)
        remote_id = str(remote.get("id") or "")
        link = links_by_remote.get(remote_id)
        matched_id = link["entry_id"] if link else storage._matching_entry_id(raw_payload, by_number, by_hash)

        if remote.get("review_state") == "withdrawn":
            if matched_id:
                already_acked = (
                    link is not None
                    and str(link.get("review_state") or "") == "withdrawn"
                    and int(link.get("remote_revision") or 0) >= int(remote.get("revision") or 0)
                )
                if not already_acked:
                    withdrawn_local.append({
                        "local_entry_id": matched_id,
                        "remote_entry_id": remote_id,
                        "invoice_no": str(remote.get("invoice_no") or raw_payload.get("invoice_no") or ""),
                        "seller": str(raw_payload.get("seller") or ""),
                        "owner_name": str(remote.get("owner_name") or ""),
                        "updated_at": str(remote.get("updated_at") or ""),
                        "revision": int(remote.get("revision") or 0),
                    })
            continue

        # Normalize untrusted attachment stored_path to safe, unique per-entry virtual paths
        # so '..' or colliding stored_paths across entries can never cause path traversal or clobbering.
        safe_attachments = []
        for att_idx, att in enumerate(raw_payload.get("attachments") or []):
            if not isinstance(att, dict):
                continue
            cleaned_att = dict(att)
            raw_orig = Path(str(cleaned_att.get("original_name") or cleaned_att.get("stored_path") or f"file_{att_idx}")).name
            safe_name = re.sub(r"[^a-zA-Z0-9._\-\u4e00-\u9fff]", "_", raw_orig)[:120] or f"file_{att_idx}"
            cleaned_att["stored_path"] = f"hub_{remote_id}/{att_idx}_{safe_name}"
            safe_attachments.append(cleaned_att)
        raw_payload["attachments"] = safe_attachments

        # Server stores ancestors newest-first; keep newest-first for entry_merge.find_common_base.
        ancestors = list(dict.fromkeys([
            *(str(a) for a in (remote.get("ancestors") or []) if str(a)),
            *(str(a) for a in (raw_payload.get("exchange_ancestors") or []) if str(a)),
        ]))
        raw_payload["exchange_ancestors"] = ancestors
        profile = remote.get("profile") or {}
        if profile.get("id") and not raw_payload.get("profile_id"):
            raw_payload["profile_id"] = profile["id"]
        if profile.get("name") and not raw_payload.get("profile_name"):
            raw_payload["profile_name"] = profile["name"]
        if profile.get("reviewer") and not raw_payload.get("reviewer"):
            raw_payload["reviewer"] = profile["reviewer"]
        active_remotes.append((remote, raw_payload, matched_id))

    matched_ids = {m for _, _, m in active_remotes if m}
    snapshots = storage._import_preview_snapshots(entries_repo, matched_ids)
    exchange_bases = load_exchange_bases(conn, matched_ids)
    profiles_list = [dict(r) for r in conn.execute("SELECT id, name, reviewer FROM profiles").fetchall()]
    profile_by_id = {p["id"]: p for p in profiles_list}

    target_revision_id = ""
    if target_scheme_id and service:
        target = service.get_scheme(target_scheme_id)
        target_revision_id = target.get("current_revision_id") or target.get("revision_id") or ""

    counts = {"new": 0, "merge": 0, "conflict": 0, "unchanged": 0}
    preview_entries: list[dict] = []
    declared_profiles: dict[str, dict] = {}

    for remote, entry, matched_id in active_remotes:
        remote_id = str(remote.get("id") or "")
        link = links_by_remote.get(remote_id)
        hub_meta = {
            "remote_entry_id": remote_id,
            "space_id": space_id,
            "space_name": space_name,
            "revision": int(remote.get("revision") or 0),
            "owner_user_id": str(remote.get("owner_user_id") or ""),
            "owner_name": str(remote.get("owner_name") or ""),
            "review_state": str(remote.get("review_state") or ""),
            "review_note": str(remote.get("review_note") or ""),
            "updated_by_name": str(remote.get("updated_by_name") or ""),
            "updated_at": str(remote.get("updated_at") or ""),
        }
        entry["hub_meta"] = hub_meta
        profile = remote.get("profile") or {}
        pid = str(entry.get("profile_id") or profile.get("id") or "__fallback__")
        if pid not in declared_profiles:
            declared_profiles[pid] = {
                "id": pid,
                "name": str(profile.get("name") or entry.get("profile_name") or remote.get("owner_name") or ""),
                "reviewer": str(profile.get("reviewer") or entry.get("reviewer") or ""),
                "is_default": False,
            }

        local_review = link or {}
        review_changed = (
            str(local_review.get("review_state") or "") != hub_meta["review_state"]
            or str(local_review.get("review_note") or "") != hub_meta["review_note"]
        )
        entry["hub_review_changed"] = bool(matched_id and review_changed)
        source_adapter = entry.get("adapter") or {}
        entry["adapter_compatibility"] = {
            "available": bool(source_adapter.get("definition")),
            "source_revision_digest": source_adapter.get("source_revision_digest", ""),
            "required_capabilities": [],
            "missing_capabilities": [],
        }
        if source_adapter.get("definition"):
            try:
                from ..adapters.transfer import supported_rule_capabilities
                required, missing = supported_rule_capabilities(source_adapter["definition"])
                entry["adapter_compatibility"].update(
                    required_capabilities=required,
                    missing_capabilities=missing,
                    available=not missing,
                )
            except (ValueError, TypeError, KeyError) as exc:
                entry["adapter_compatibility"].update(available=False, missing_capabilities=[str(exc)])
        if source_adapter:
            public_package = source_adapter.get("definition", {}).get("manifest", {}).get("package_id", "")
            entry["extension_merge_preview"] = storage._extension_merge_preview(
                entries_repo, matched_id, source_adapter, public_package
            )
        revision_id = (snapshots.get(matched_id) or {}).get("scheme_revision_id") if matched_id else target_revision_id
        entry["role_mapping_preview"] = storage._role_mapping_preview(entries_repo, entry, revision_id)

        if matched_id:
            existing = snapshots.get(matched_id) or {}
            plan = plan_entry_merge(
                existing,
                entry,
                local_bases=exchange_bases.get(matched_id, {}),
                local_batch_note=str(existing.get("_batch_note") or ""),
                local_extension_values=existing.get("_extension_values"),
                extension_preview=entry.get("extension_merge_preview"),
                include_notes=True,
                include_tags=True,
            )
            entry["existing_entry_id"] = matched_id
            entry["existing_batches"] = existing.get("batches") or []
            entry["existing_profile"] = profile_by_id.get(existing.get("profile_id")) or {}
            entry["merge_preview"] = plan
            entry["merge_changes"] = list(plan.get("summary") or [])
            if plan.get("has_conflicts"):
                action = "conflict"
            elif plan.get("items") or plan.get("has_changes") or review_changed:
                action = "merge"
            else:
                action = "unchanged"
            entry["import_action"] = action
            counts[action] += 1
        else:
            entry["existing_entry_id"] = ""
            entry["existing_batches"] = []
            entry["merge_preview"] = None
            entry["merge_changes"] = []
            entry["import_action"] = "new"
            counts["new"] += 1
        preview_entries.append(entry)

    by_name = defaultdict(list)
    for profile in profiles_list:
        by_name[str(profile.get("name") or "").strip()].append(profile)
    profile_choices = {}
    for source_profile in declared_profiles.values():
        source_id = str(source_profile.get("id") or "")
        if not source_id or str(source_profile.get("reviewer") or "").strip():
            continue
        candidates = by_name.get(str(source_profile.get("name") or "").strip(), [])
        if len(candidates) > 1:
            profile_choices[source_id] = {"status": "ambiguous", "candidates": candidates}
        elif len(candidates) == 1:
            profile_choices[source_id] = {"status": "unique", "candidates": candidates}

    return {
        "space": space,
        "entries": preview_entries,
        "profiles": list(declared_profiles.values()),
        "counts": counts,
        "import_plan": counts,
        "profile_mapping_preview": profile_choices,
        "target_scheme_id": target_scheme_id or "",
        "withdrawn_local": withdrawn_local,
        "verified": True,
        "tampered": False,
        "tampered_files": [],
        "options": {"include_notes": True, "include_tags": True},
        "preview_version": storage._local_preview_version(entries_repo, matched_ids, snapshots=snapshots),
    }


def _selected_entry_indexes(opts: dict, total: int) -> set[int] | None:
    if "selected_entry_indexes" not in opts or opts.get("selected_entry_indexes") is None:
        return None
    out: set[int] = set()
    for val in opts.get("selected_entry_indexes") or []:
        idx = int(val)
        if 0 <= idx < total:
            out.add(idx)
    return out


def _selected_profile_ids(opts: dict) -> set[str] | None:
    if "selected_profile_ids" not in opts or opts.get("selected_profile_ids") is None:
        return None
    return {str(v or "") for v in (opts.get("selected_profile_ids") or [])}


def _parse_entry_batch_overrides(opts: dict) -> dict[int, dict]:
    overrides: dict[int, dict] = {}
    for raw_idx, raw_ov in (opts.get("entry_batch_overrides") or {}).items():
        idx = int(raw_idx)
        ov = dict(raw_ov or {})
        mode = str(ov.get("mode") or "default")
        if mode == "default":
            continue
        if mode not in {"keep", "none", "existing", "new"}:
            raise HubError("单条批次设置无效，请重新选择。")
        norm = {"mode": mode}
        if mode == "existing":
            norm["batch_id"] = str(ov.get("batch_id") or "").strip()
            if not norm["batch_id"]:
                raise HubError("请为单条设置选择报账批次。")
        elif mode == "new":
            norm["batch_name"] = str(ov.get("batch_name") or "").strip()
            if not norm["batch_name"]:
                raise HubError("请填写单条设置的新批次名称。")
        overrides[idx] = norm
    return overrides


def apply_pull(
    client: HubClient,
    entries_repo: EntryRepo,
    attachments_repo: AttachmentRepo,
    space_id: str,
    profile_id: str,
    options: dict | None = None,
    *,
    preview: dict | None = None,
    adapter_service=None,
    cancel_check=None,
    on_before_commit=None,
) -> dict:
    opts = options if isinstance(options, dict) else {}
    service = storage._adapter_service(entries_repo, adapter_service)
    requested_scheme_id = str(opts.get("scheme_id") or "").strip() or None
    has_positional_opts = any(
        opts.get(k) for k in ("selected_entry_indexes", "entry_merge_decisions", "entry_batch_overrides", "role_mappings")
    )
    if preview is None and has_positional_opts:
        raise HubError("同步预览已过期，请重新点击「拉取合并」后再确认。")
    inspected = copy.deepcopy(preview) if preview is not None else preview_pull(
        client, entries_repo, space_id, adapter_service=service, target_scheme_id=requested_scheme_id
    )
    space = inspected.get("space") or {"id": space_id, "name": ""}
    space_name = str(space.get("name") or "")
    entries = inspected.get("entries") or []

    conn = entries_repo.db.conn
    if preview is not None and inspected.get("preview_version"):
        preview_matched = {str(e.get("existing_entry_id") or "") for e in entries if e.get("existing_entry_id")}
        current_version = storage._local_preview_version(entries_repo, preview_matched)
        if current_version != inspected["preview_version"]:
            raise HubError("预览后本机条目已发生变化，请重新打开「拉取合并」核对。")
    selected_entry_indexes = _selected_entry_indexes(opts, len(entries))
    selected_profile_ids = _selected_profile_ids(opts)
    profile_overrides = opts.get("profile_overrides") or {}
    profile_mappings = opts.get("profile_mappings") or {}
    role_mappings_by_entry = opts.get("role_mappings") or {}
    raw_merge_decisions = opts.get("entry_merge_decisions") or {}
    import_tags = [str(tag).strip() for tag in (opts.get("tags") or []) if str(tag).strip()]
    target_batch_id = str(opts.get("batch_id") or "").strip()
    new_batch_name = str(opts.get("batch_name") or "").strip()
    move_existing_entries = bool(opts.get("move_existing_entries"))
    entry_batch_overrides = _parse_entry_batch_overrides(opts)

    if target_batch_id and new_batch_name:
        raise HubError("已有批次和新建批次不能同时指定。")
    if target_batch_id:
        row = conn.execute("SELECT id FROM batches WHERE id = ?", (target_batch_id,)).fetchone()
        if not row:
            raise HubError("所选报账批次不存在。")

    # Re-plan merge against current local DB right before downloading blobs / writing.
    by_number, by_hash = storage._entry_identity_maps(conn)
    links_by_remote = _hub_link_by_remote(conn, client.base_url, space_id)

    # Determine which blobs need downloading from COS/hub.
    selected_rows: list[tuple[int, dict, str]] = []
    existing_ids_needed: set[str] = set()
    for idx, entry in enumerate(entries):
        if selected_entry_indexes is not None and idx not in selected_entry_indexes:
            continue
        source_pid = str(entry.get("profile_id") or "__fallback__")
        if selected_profile_ids is not None and source_pid not in selected_profile_ids:
            continue
        remote_id = str((entry.get("hub_meta") or {}).get("remote_entry_id") or "")
        link = links_by_remote.get(remote_id)
        matched_id = link["entry_id"] if link else storage._matching_entry_id(entry, by_number, by_hash)
        selected_rows.append((idx, entry, matched_id))
        if matched_id:
            existing_ids_needed.add(matched_id)

    local_snapshots = storage._import_preview_snapshots(entries_repo, existing_ids_needed)
    local_bases_map = load_exchange_bases(conn, existing_ids_needed)
    pre_download_matched = set(existing_ids_needed)
    pre_download_version = (
        storage._local_preview_version(entries_repo, pre_download_matched, snapshots=local_snapshots)
        if pre_download_matched
        else ""
    )

    # Collect (sha256, remote_entry_id, arcname) for only the missing files actually needed.
    needed_downloads: dict[str, tuple[str, str]] = {}  # sha -> (remote_entry_id, original_name)
    member_by_sha: dict[str, list[str]] = defaultdict(list)

    for idx, entry, matched_id in selected_rows:
        remote_id = str((entry.get("hub_meta") or {}).get("remote_entry_id") or "")
        if not matched_id:
            for att in entry.get("attachments") or []:
                sha = str(att.get("sha256") or "").strip().lower()
                stored_path = str(att.get("stored_path") or "")
                if sha and stored_path:
                    member_by_sha[sha].append(f"attachments/{stored_path}")
                    needed_downloads.setdefault(sha, (remote_id, str(att.get("original_name") or "")))
            continue

        existing = local_snapshots.get(matched_id) or {}
        local_shas = {str(a.get("sha256") or "").lower() for a in (existing.get("attachments") or []) if a.get("sha256")}
        src_adapter = entry.get("adapter") or {}
        pub_pkg = src_adapter.get("definition", {}).get("manifest", {}).get("package_id", "") if src_adapter else ""
        ext_preview = storage._extension_merge_preview(entries_repo, matched_id, src_adapter, pub_pkg) if src_adapter else None
        plan = plan_entry_merge(
            existing,
            entry,
            local_bases=local_bases_map.get(matched_id, {}),
            local_batch_note=str(existing.get("_batch_note") or ""),
            local_extension_values=existing.get("_extension_values"),
            extension_preview=ext_preview,
            include_notes=True,
            include_tags=True,
        )
        entry_decisions = storage.validate_entry_decisions(
            plan,
            raw_merge_decisions.get(str(idx)) if isinstance(raw_merge_decisions, dict) else None,
        )
        for item in plan.get("items") or []:
            if item.get("kind") != "attachment_group":
                continue
            choice = entry_decisions.get(item["key"], "keep_local")
            if choice == "keep_local":
                continue
            group_key = item["group_key"]
            target_shas = (
                set(item.get("three_way_shas") or [])
                if choice == "three_way"
                else None
            )
            for att in entry.get("attachments") or []:
                if storage.attachment_group_key(att) != group_key:
                    continue
                sha = str(att.get("sha256") or "").strip().lower()
                stored_path = str(att.get("stored_path") or "")
                if not sha or not stored_path or sha in local_shas:
                    continue
                if target_shas is not None and sha not in target_shas:
                    continue
                member_by_sha[sha].append(f"attachments/{stored_path}")
                needed_downloads.setdefault(sha, (remote_id, str(att.get("original_name") or "")))

    # Download missing blobs to a temporary directory before opening the DB write transaction.
    with tempfile.TemporaryDirectory(prefix="tidoc-hub-pull-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        member_paths: dict[str, Path] = {}
        for sha, (remote_id, _orig_name) in needed_downloads.items():
            if cancel_check is not None and cancel_check():
                raise HubError("同步已取消。", code="cancelled")
            safe_sha = _validate_sha256(sha)
            presigned = client.presign_download(safe_sha, space_id, remote_id)
            dest = tmp_root / safe_sha
            client.download_blob(presigned["download_url"], dest, expected_sha256=safe_sha)
            for arcname in member_by_sha.get(sha, []):
                member_paths[arcname] = dest

        archive = _DownloadedBlobArchive(member_paths)
        now = datetime.now().isoformat(timespec="seconds")
        package_profiles = {str(p.get("id") or ""): p for p in (inspected.get("profiles") or []) if p.get("id")}
        created_files: list[Path] = []
        created_dirs: list[Path] = []
        removed_files: list[Path] = []
        imported_ids: list[str] = []
        updated_ids: list[str] = []
        status_updated_ids: list[str] = []
        withdrawn_updated = 0
        merged_details: list[dict] = []
        skipped: list[dict] = []
        destination_by_entry_index: dict[int, str] = {}
        default_batch_entry_indexes: set[int] = set()
        pending_batch_notes: list[tuple[str, str, bool]] = []
        imported_exchange_records: list[tuple[str, dict]] = []

        try:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")

            if pre_download_matched:
                live_version = storage._local_preview_version(entries_repo, pre_download_matched)
                if live_version != pre_download_version:
                    raise HubError("同步下载期间本机条目已发生变化，请重试。", code="local_changed")
            live_by_number, live_by_hash = storage._entry_identity_maps(conn)
            live_links_by_remote = _hub_link_by_remote(conn, client.base_url, space_id)
            for _idx, entry, expected_matched in selected_rows:
                rid = str((entry.get("hub_meta") or {}).get("remote_entry_id") or "")
                live_link = live_links_by_remote.get(rid)
                live_matched = live_link["entry_id"] if live_link else storage._matching_entry_id(entry, live_by_number, live_by_hash)
                if live_matched != expected_matched:
                    raise HubError("同步下载期间本机条目已发生变化，请重试。", code="local_changed")
            if target_batch_id and not conn.execute("SELECT 1 FROM batches WHERE id = ?", (target_batch_id,)).fetchone():
                raise HubError("所选报账批次不存在。", code="local_changed")
            if on_before_commit is not None:
                on_before_commit(conn)

            existing_profiles = [dict(r) for r in conn.execute("SELECT id, name, reviewer FROM profiles").fetchall()]
            profile_count_before = len(existing_profiles)
            created_profile_ids: list[str] = []
            source_profile_map: dict[str, str] = {}
            for src_id, dst_id in (profile_mappings.items() if isinstance(profile_mappings, dict) else []):
                if str(dst_id or "").strip():
                    source_profile_map[str(src_id)] = str(dst_id).strip()

            def resolve_profile(source_profile: dict | None, source_id: str) -> str:
                if source_id and source_id in source_profile_map:
                    return source_profile_map[source_id]
                name = str((source_profile or {}).get("name") or "").strip()
                reviewer = str((source_profile or {}).get("reviewer") or "").strip()
                if not name:
                    return profile_id
                if reviewer:
                    for p in existing_profiles:
                        if p["name"] == name and str(p.get("reviewer") or "").strip() == reviewer:
                            if source_id:
                                source_profile_map[source_id] = p["id"]
                            return p["id"]
                else:
                    same_name = [p for p in existing_profiles if p["name"] == name]
                    if len(same_name) == 1:
                        if source_id:
                            source_profile_map[source_id] = same_name[0]["id"]
                        return same_name[0]["id"]
                    if len(same_name) > 1:
                        raise HubError(f"本机存在多位名为「{name}」的报账人，请先在预览中指定对应身份。")
                import uuid
                new_pid = uuid.uuid4().hex
                conn.execute(
                    """INSERT INTO profiles(id, name, reviewer, is_default, created_at)
                       VALUES(?,?,?,0,?)""",
                    (new_pid, name, reviewer, now),
                )
                created_profile_ids.append(new_pid)
                existing_profiles.append({"id": new_pid, "name": name, "reviewer": reviewer})
                if source_id:
                    source_profile_map[source_id] = new_pid
                return new_pid

            restorer = bindle_import._EntryRestorer(
                entries_repo=entries_repo,
                attachments_repo=attachments_repo,
                requested_scheme_id=requested_scheme_id,
                adapter_service=service,
                inspected=inspected,
                import_tags=import_tags,
                package_profiles=package_profiles,
                resolve_profile=resolve_profile,
                source_profile_map=source_profile_map,
                created_files=created_files,
                created_dirs=created_dirs,
                role_mappings_by_entry=role_mappings_by_entry if isinstance(role_mappings_by_entry, dict) else {},
                now=now,
            )
            for idx, entry, matched_id in selected_rows:
                hub_meta = entry.get("hub_meta") or {}
                remote_link_payload = {
                    "id": hub_meta.get("remote_entry_id") or "",
                    "revision": hub_meta.get("revision") or 0,
                    "owner_user_id": hub_meta.get("owner_user_id") or "",
                    "owner_name": hub_meta.get("owner_name") or "",
                    "review_state": hub_meta.get("review_state") or "",
                    "review_note": hub_meta.get("review_note") or "",
                }
                invoice_no = str(entry.get("invoice_no") or "").strip()
                source_pid = str(entry.get("profile_id") or "__fallback__")
                if matched_id:
                    destination_by_entry_index[idx] = matched_id
                    if move_existing_entries:
                        default_batch_entry_indexes.add(idx)
                    role_map = role_mappings_by_entry.get(str(idx), {}) if isinstance(role_mappings_by_entry, dict) else {}
                    entry_decisions = (
                        raw_merge_decisions.get(str(idx))
                        if isinstance(raw_merge_decisions, dict)
                        else None
                    )
                    changes, _applied = storage._merge_existing_entry(
                        conn,
                        entries_repo,
                        attachments_repo,
                        archive,
                        matched_id,
                        entry,
                        import_tags,
                        now,
                        created_files,
                        False,
                        role_mappings=role_map if isinstance(role_map, dict) else {},
                        merge_decisions=entry_decisions,
                        removed_files=removed_files,
                        package_options={"include_notes": True, "include_tags": True},
                    )
                    incoming_batch_note = str(entry.get("batch_note") or "").strip()
                    if incoming_batch_note and _applied.get("batch_note") != "keep_local":
                        pending_batch_notes.append((matched_id, incoming_batch_note, False))
                    imported_exchange_records.append((matched_id, entry))
                    upsert_hub_link(
                        conn,
                        matched_id,
                        server_url=client.base_url,
                        space_id=space_id,
                        space_name=space_name,
                        remote_entry=remote_link_payload,
                        synced_at=now,
                    )
                    if changes:
                        updated_ids.append(matched_id)
                        merged_details.append({
                            "entry_id": matched_id,
                            "invoice_no": invoice_no,
                            "seller": entry.get("seller", ""),
                            "changes": changes,
                        })
                    elif entry.get("hub_review_changed"):
                        status_updated_ids.append(matched_id)
                    else:
                        skipped.append({
                            "_entry_id": matched_id,
                            "invoice_no": invoice_no,
                            "seller": entry.get("seller", ""),
                        })
                    continue

                lookup_pid = "" if source_pid == "__fallback__" else source_pid
                source_profile = package_profiles.get(lookup_pid) or {
                    "name": entry.get("profile_name") or hub_meta.get("owner_name") or "",
                    "reviewer": entry.get("reviewer") or "",
                }
                override = profile_overrides.get(source_pid) or profile_overrides.get("__fallback__")
                if override:
                    source_profile = dict(source_profile)
                    for k in ("name", "reviewer"):
                        if k in override:
                            source_profile[k] = str(override.get(k) or "").strip()
                dest_pid = resolve_profile(source_profile, lookup_pid)
                new_id = restorer.restore(entry, idx, dest_pid, invoice_no, archive)
                imported_ids.append(new_id)
                destination_by_entry_index[idx] = new_id
                default_batch_entry_indexes.add(idx)
                incoming_batch_note = str(entry.get("batch_note") or "").strip()
                if incoming_batch_note:
                    pending_batch_notes.append((new_id, incoming_batch_note, True))
                imported_exchange_records.append((new_id, entry))
                upsert_hub_link(
                    conn,
                    new_id,
                    server_url=client.base_url,
                    space_id=space_id,
                    space_name=space_name,
                    remote_entry=remote_link_payload,
                    synced_at=now,
                )

            # Also update local hub_entry_links for withdrawn remote entries if matched locally
            for w in inspected.get("withdrawn_local") or []:
                local_id = w.get("local_entry_id")
                if local_id:
                    cur = conn.execute(
                        """UPDATE hub_entry_links
                              SET review_state = 'withdrawn', remote_revision = ?, last_synced_at = ?
                            WHERE entry_id = ? AND server_url = ? AND space_id = ?""",
                        (int(w.get("revision") or 0), now, local_id, client.base_url, space_id),
                    )
                    withdrawn_updated += int(cur.rowcount or 0)

            (target_batch_id, batch_created, global_batch_created,
             individually_assigned, globally_assigned) = bindle_import._assign_batches(
                conn, now, default_batch_entry_indexes, entry_batch_overrides,
                destination_by_entry_index, target_batch_id, new_batch_name,
            )

            for note_entry_id, incoming_note, is_new in pending_batch_notes:
                batch_row = conn.execute(
                    "SELECT batch_id, note FROM batch_entries WHERE entry_id = ? LIMIT 1",
                    (note_entry_id,),
                ).fetchone()
                note_applied = False
                if batch_row:
                    merged_bnote = merge_text(str(batch_row["note"] or ""), incoming_note)
                    if merged_bnote != str(batch_row["note"] or ""):
                        conn.execute(
                            "UPDATE batch_entries SET note = ? WHERE batch_id = ? AND entry_id = ?",
                            (merged_bnote, batch_row["batch_id"], note_entry_id),
                        )
                        note_applied = True
                else:
                    field_row = conn.execute(
                        "SELECT current FROM entry_fields WHERE entry_id = ? AND field = 'notes'",
                        (note_entry_id,),
                    ).fetchone()
                    current_notes = str(field_row["current"] or "") if field_row else ""
                    if incoming_note not in current_notes:
                        tagged = incoming_note if incoming_note.startswith(BATCH_NOTE_PREFIX.strip()) else f"{BATCH_NOTE_PREFIX}{incoming_note}"
                        next_notes = merge_text(current_notes, tagged)
                        if field_row is None:
                            conn.execute(
                                """INSERT INTO entry_fields(entry_id, field, origin, current, modified, value_source)
                                   VALUES(?, 'notes', '', ?, 0, '')""",
                                (note_entry_id, next_notes),
                            )
                        else:
                            conn.execute(
                                "UPDATE entry_fields SET current = ? WHERE entry_id = ? AND field = 'notes'",
                                (next_notes, note_entry_id),
                            )
                        if not is_new:
                            conn.execute(
                                """INSERT INTO field_history(entry_id, field, old_value, new_value, profile_id, changed_at)
                                   VALUES(?, 'notes', ?, ?, '', ?)""",
                                (note_entry_id, current_notes, next_notes, now),
                            )
                        note_applied = True
                if note_applied and not is_new:
                    conn.execute("UPDATE entries SET updated_at = ? WHERE id = ?", (now, note_entry_id))
                    if note_entry_id not in updated_ids:
                        updated_ids.append(note_entry_id)
                    skipped = [item for item in skipped if item.get("_entry_id") != note_entry_id]

            for item in skipped:
                item.pop("_entry_id", None)

            affected_ids = list(dict.fromkeys(imported_ids + updated_ids))
            if affected_ids and created_profile_ids:
                has_default = conn.execute("SELECT 1 FROM profiles WHERE is_default = 1 LIMIT 1").fetchone()
                if not has_default:
                    conn.execute("UPDATE profiles SET is_default = 1 WHERE id = ?", (created_profile_ids[0],))
                profile_count_after = conn.execute("SELECT COUNT(*) c FROM profiles").fetchone()["c"]
                if profile_count_before < 2 <= profile_count_after:
                    conn.execute(
                        """INSERT INTO meta(key, value) VALUES('tidoc.multiClaimantMode', '1')
                           ON CONFLICT(key) DO UPDATE SET value = excluded.value"""
                    )

            for affected_id in affected_ids:
                entries_repo.recompute_status(affected_id, commit=False)
                if entries_repo.has_imported_payment_recognition(affected_id):
                    entries_repo.refresh_payment_check(
                        affected_id, commit=False, remind_pending=False, imported_only=True
                    )
                else:
                    has_local_pay_rec = conn.execute(
                        """SELECT 1 FROM attachments
                            WHERE entry_id = ? AND type = 'payment_screenshot'
                              AND COALESCE(recognition_status, '') <> ''
                              AND recognition_version NOT LIKE '%:imported'
                            LIMIT 1""",
                        (affected_id,),
                    ).fetchone() is not None
                    if not has_local_pay_rec:
                        entries_repo.clear_payment_check(affected_id, commit=False)

            remote_snapshot_digests: dict[str, str] = {}
            if imported_exchange_records:
                post_snapshots = storage._import_preview_snapshots(
                    entries_repo, {entry_id for entry_id, _ in imported_exchange_records}
                )
                for rec_entry_id, incoming_entry in imported_exchange_records:
                    inc_snap = build_entry_snapshot(incoming_entry)
                    remote_snapshot_digests[rec_entry_id] = record_exchange_base(
                        conn, rec_entry_id, inc_snap, "import", now
                    )
                    post_local = post_snapshots.get(rec_entry_id)
                    if post_local is not None:
                        local_snap = build_entry_snapshot(
                            post_local,
                            batch_note=str(post_local.get("_batch_note") or ""),
                            extension_values=post_local.get("_extension_values"),
                        )
                        record_exchange_base(conn, rec_entry_id, local_snap, "import", now)

            conn.commit()
        except BaseException:
            conn.rollback()
            for file_path in reversed(created_files):
                try:
                    file_path.unlink(missing_ok=True)
                except OSError:
                    pass
            for folder in created_dirs:
                if folder.is_dir():
                    try:
                        shutil.rmtree(folder)
                    except OSError:
                        pass
            raise

    for old_path in removed_files:
        try:
            old_path.unlink(missing_ok=True)
        except OSError:
            pass

    parts = []
    if imported_ids:
        parts.append(f"拉取新增 {len(imported_ids)} 条")
    if updated_ids:
        parts.append(f"更新 {len(updated_ids)} 条")
    if status_updated_ids:
        parts.append(f"同步审核状态 {len(status_updated_ids)} 条")
    if skipped and not (imported_ids or updated_ids or status_updated_ids):
        parts.append(f"{len(skipped)} 条无变化")
    if inspected.get("withdrawn_local"):
        parts.append(f"发现 {len(inspected['withdrawn_local'])} 条已被撤回")
    message = "；".join(parts) + "。" if parts else "没有条目需要同步。"

    return {
        "imported": len(imported_ids),
        "updated": len(updated_ids),
        "status_updated": len(status_updated_ids),
        "withdrawn_updated": withdrawn_updated,
        "entry_ids": imported_ids,
        "updated_entry_ids": updated_ids,
        "status_updated_entry_ids": status_updated_ids,
        "remote_snapshot_digests": remote_snapshot_digests,
        "merged": merged_details,
        "skipped": skipped,
        "withdrawn_local": inspected.get("withdrawn_local") or [],
        "batch_id": target_batch_id if globally_assigned else "",
        "batch_created": batch_created,
        "message": message,
    }


def review_entry(
    client: HubClient,
    entries_repo: EntryRepo,
    attachments_repo: AttachmentRepo,
    entry_id: str,
    *,
    review_state: str,
    review_note: str = "",
    push_local: bool = True,
    adapter_service=None,
) -> dict:
    conn = entries_repo.db.conn
    link = get_hub_link(conn, entry_id)
    if not link or link["server_url"] != client.base_url or not link["remote_entry_id"]:
        raise HubError("该条目尚未关联到当前共享区。")
    if push_local:
        res = push_entries(
            client,
            entries_repo,
            attachments_repo,
            link["space_id"],
            [entry_id],
            space_name=link["space_name"],
            adapter_service=adapter_service,
            review_state=review_state,
            review_note=review_note,
        )
        if res["pushed"]:
            return {"entry": res["pushed"][0], "link": res["pushed"][0]["link"], "message": "已更新共享区审核状态并同步本机修改。"}
        if res["conflicts"]:
            raise HubError("共享区中该条目已有新版本，请先拉取合并后再提交审核结果。", code="revision_conflict", payload=res["conflicts"][0].get("payload"))
        err = (res["failed"] or res["locked"] or res["duplicates"])
        raise HubError(err[0]["error"] if err else "提交审核状态失败。")

    remote_entry = client.review_entry(
        link["space_id"],
        link["remote_entry_id"],
        review_state=review_state,
        review_note=review_note,
        base_revision=int(link["remote_revision"] or 0),
    )
    now = datetime.now().isoformat(timespec="seconds")
    updated_link = upsert_hub_link(
        conn,
        entry_id,
        server_url=client.base_url,
        space_id=link["space_id"],
        space_name=link["space_name"],
        remote_entry=remote_entry,
        synced_at=now,
    )
    conn.commit()
    return {"entry": remote_entry, "link": updated_link, "message": "已更新共享区审核状态。"}


def withdraw_entry(client: HubClient, entries_repo: EntryRepo, entry_id: str) -> dict:
    conn = entries_repo.db.conn
    link = get_hub_link(conn, entry_id)
    if not link or link["server_url"] != client.base_url or not link["remote_entry_id"]:
        raise HubError("该条目尚未关联到当前共享区。")
    remote_entry = client.withdraw_entry(
        link["space_id"],
        link["remote_entry_id"],
        base_revision=int(link["remote_revision"] or 0),
    )
    now = datetime.now().isoformat(timespec="seconds")
    updated_link = upsert_hub_link(
        conn,
        entry_id,
        server_url=client.base_url,
        space_id=link["space_id"],
        space_name=link["space_name"],
        remote_entry=remote_entry,
        synced_at=now,
    )
    conn.commit()
    return {"entry": remote_entry, "link": updated_link, "message": "已从共享区撤回该条目（本机数据不受影响）。"}

"""Blob storage backends for tidoc_hub: Tencent Cloud COS and signed local files."""

from __future__ import annotations

import abc
import hashlib
import hmac
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from .cos_sign import CosSigner

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OWNER_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def validate_sha256(value: str) -> str:
    cleaned = str(value or "").strip().lower()
    if not SHA256_RE.match(cleaned):
        raise ValueError("无效的文件摘要（sha256）。")
    return cleaned


def validate_owner_id(owner_id: str) -> str:
    cleaned = str(owner_id or "").strip()
    if not OWNER_ID_RE.match(cleaned):
        raise ValueError("无效的用户标识。")
    return cleaned


class BlobStore(abc.ABC):
    mode: str = ""

    def storage_key(self, owner_id: str, sha256: str) -> str:
        # Scoped per-owner under blobs/* (matches COS CAM policy `bucket/blobs/*`),
        # preventing cross-member poisoning, existence oracles, and unauthorized blob reuse.
        return f"blobs/{validate_owner_id(owner_id)}/{validate_sha256(sha256)}"

    @abc.abstractmethod
    def presign_put(
        self,
        storage_key: str,
        content_type: str = "application/octet-stream",
        *,
        size_bytes: int | None = None,
        expires: int = 300,
    ) -> dict:
        ...

    @abc.abstractmethod
    def presign_get(self, storage_key: str, *, expires: int = 300, download_name: str | None = None) -> dict:
        ...

    @abc.abstractmethod
    def head_object(self, storage_key: str) -> tuple[bool, int, str]:
        ...


class CosBlobStore(BlobStore):
    mode = "cos"

    def __init__(self, signer: CosSigner):
        self.signer = signer

    def presign_put(
        self,
        storage_key: str,
        content_type: str = "application/octet-stream",
        *,
        size_bytes: int | None = None,
        expires: int = 300,
    ) -> dict:
        return self.signer.presign_put(storage_key, content_type, size_bytes=size_bytes, expires=expires)

    def presign_get(self, storage_key: str, *, expires: int = 300, download_name: str | None = None) -> dict:
        return self.signer.presign_get(storage_key, expires=expires, download_name=download_name)

    def head_object(self, storage_key: str) -> tuple[bool, int, str]:
        return self.signer.head_object(storage_key)


class LocalBlobStore(BlobStore):
    """HMAC-signed local filesystem blob store for tests and self-hosted deployments."""

    mode = "local"

    def __init__(self, blobs_dir: str | Path, public_base_url: str, secret: str = ""):
        self.blobs_dir = Path(blobs_dir)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.public_base_url = str(public_base_url or "").rstrip("/")
        self.secret = (secret or secrets.token_hex(32)).encode("utf-8")

    @staticmethod
    def parse_key(storage_key: str) -> tuple[str, str]:
        parts = str(storage_key or "").strip("/").split("/")
        if len(parts) != 3 or parts[0] != "blobs":
            raise ValueError("无效的存储键。")
        return validate_owner_id(parts[1]), validate_sha256(parts[2])

    def path_for_key(self, storage_key: str) -> Path:
        owner_id, digest = self.parse_key(storage_key)
        return self.blobs_dir / owner_id / digest[:2] / digest

    def _sign(self, op: str, owner_id: str, sha256: str, exp: int, size_bytes: int = 0) -> str:
        msg = f"{op}:{owner_id}:{sha256}:{exp}:{size_bytes}".encode("utf-8")
        return hmac.new(self.secret, msg, hashlib.sha256).hexdigest()

    def verify_signature(
        self,
        op: str,
        owner_id: str,
        sha256: str,
        exp_raw: str,
        sig: str,
        size_raw: str = "0",
    ) -> tuple[bool, int]:
        try:
            owner = validate_owner_id(owner_id)
            digest = validate_sha256(sha256)
            exp = int(exp_raw)
            size_bytes = int(size_raw or 0)
        except (TypeError, ValueError):
            return False, 0
        if exp < int(time.time()):
            return False, 0
        expected = self._sign(op, owner, digest, exp, size_bytes)
        return hmac.compare_digest(expected, str(sig or "")), size_bytes

    def presign_put(
        self,
        storage_key: str,
        content_type: str = "application/octet-stream",
        *,
        size_bytes: int | None = None,
        expires: int = 300,
    ) -> dict:
        owner_id, digest = self.parse_key(storage_key)
        exp = int(time.time()) + max(30, int(expires))
        sz = int(size_bytes or 0)
        sig = self._sign("put", owner_id, digest, exp, sz)
        headers = {"Content-Type": content_type or "application/octet-stream"}
        if sz > 0:
            headers["Content-Length"] = str(sz)
        return {
            "upload_url": f"{self.public_base_url}/_local_blobs/{quote(owner_id, safe='')}/{digest}?op=put&exp={exp}&sz={sz}&sig={sig}",
            "method": "PUT",
            "headers": headers,
            "expires_in": int(expires),
        }

    def presign_get(self, storage_key: str, *, expires: int = 300, download_name: str | None = None) -> dict:
        owner_id, digest = self.parse_key(storage_key)
        exp = int(time.time()) + max(30, int(expires))
        sig = self._sign("get", owner_id, digest, exp, 0)
        return {
            "download_url": f"{self.public_base_url}/_local_blobs/{quote(owner_id, safe='')}/{digest}?op=get&exp={exp}&sig={sig}",
            "expires_in": int(expires),
        }

    def write_blob(self, owner_id: str, sha256: str, data: bytes) -> int:
        storage_key = self.storage_key(owner_id, sha256)
        digest = validate_sha256(sha256)
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            raise ValueError("上传的文件内容与声明的 sha256 不一致。")
        dest = self.path_for_key(storage_key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp file avoids races on concurrent PUTs for the same digest.
        tmp = dest.parent / f".{digest}.{secrets.token_hex(6)}.part"
        try:
            tmp.write_bytes(data)
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
        return len(data)

    def head_object(self, storage_key: str) -> tuple[bool, int, str]:
        _, digest = self.parse_key(storage_key)
        path = self.path_for_key(storage_key)
        if not path.is_file():
            return False, 0, ""
        return True, path.stat().st_size, digest

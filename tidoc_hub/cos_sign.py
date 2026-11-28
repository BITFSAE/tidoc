"""Tencent Cloud COS V5 XML API signer (HMAC-SHA1, zero external dependencies).

Implements the official COS V5 request signature specification for presigned
PUT/GET/HEAD URLs and server-side HEAD object existence checks.
"""

from __future__ import annotations

import hashlib
import hmac
import ssl
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPSHandler, Request, build_opener


def _cos_quote(value: str) -> str:
    """COS V5 URL encoding: RFC 3986 unreserved characters only (uppercase percent-hex)."""
    return quote(str(value), safe="-_.~")


def _default_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if sys.platform == "darwin":
        try:
            roots = subprocess.run(
                [
                    "/usr/bin/security", "find-certificate", "-a", "-p",
                    "/System/Library/Keychains/SystemRootCertificates.keychain",
                ],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout
            if "BEGIN CERTIFICATE" in roots:
                ctx.load_verify_locations(cadata=roots)
        except (OSError, subprocess.SubprocessError, ssl.SSLError):
            pass
    return ctx


class CosSigner:
    """Signs Tencent Cloud COS V5 presigned URLs and HEAD requests."""

    def __init__(self, secret_id: str, secret_key: str, bucket: str, region: str):
        self.secret_id = str(secret_id or "").strip()
        self.secret_key = str(secret_key or "").strip()
        self.bucket = str(bucket or "").strip()
        self.region = str(region or "").strip()
        if not (self.secret_id and self.secret_key and self.bucket and self.region):
            raise ValueError("COS 配置不完整，需要 COS_SECRET_ID / COS_SECRET_KEY / COS_BUCKET / COS_REGION。")
        self.host = f"{self.bucket}.cos.{self.region}.myqcloud.com"
        self._opener = build_opener(HTTPSHandler(context=_default_ssl_context()))

    def _build_signature(
        self,
        method: str,
        key: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        expires: int = 300,
        now: int | None = None,
    ) -> dict[str, str]:
        ts = int(time.time() if now is None else now)
        start_ts = ts - 60
        end_ts = ts + max(30, int(expires))
        key_time = f"{start_ts};{end_ts}"

        clean_key = str(key or "").lstrip("/")
        pathname = f"/{clean_key}"

        norm_params: list[tuple[str, str]] = []
        for k, v in (params or {}).items():
            norm_params.append((_cos_quote(str(k).lower()), _cos_quote(str(v))))
        norm_params.sort(key=lambda item: item[0])
        url_param_list = ";".join(k for k, _ in norm_params)
        http_parameters = "&".join(f"{k}={v}" for k, v in norm_params)

        hdr_map = {"host": self.host}
        for k, v in (headers or {}).items():
            hdr_map[str(k).strip().lower()] = str(v).strip()
        norm_headers = sorted(
            ((_cos_quote(k), _cos_quote(v)) for k, v in hdr_map.items()),
            key=lambda item: item[0],
        )
        header_list = ";".join(k for k, _ in norm_headers)
        http_headers = "&".join(f"{k}={v}" for k, v in norm_headers)

        http_string = f"{method.lower()}\n{pathname}\n{http_parameters}\n{http_headers}\n"
        http_string_sha1 = hashlib.sha1(http_string.encode("utf-8")).hexdigest()
        string_to_sign = f"sha1\n{key_time}\n{http_string_sha1}\n"

        sign_key = hmac.new(
            self.secret_key.encode("utf-8"),
            key_time.encode("utf-8"),
            hashlib.sha1,
        ).hexdigest()
        signature = hmac.new(
            sign_key.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).hexdigest()

        return {
            "pathname": pathname,
            "key_time": key_time,
            "header_list": header_list,
            "url_param_list": url_param_list,
            "signature": signature,
            "http_parameters": http_parameters,
            "http_string": http_string,
            "string_to_sign": string_to_sign,
        }

    def presign_url(
        self,
        method: str,
        key: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        expires: int = 300,
        now: int | None = None,
    ) -> str:
        sig = self._build_signature(method, key, params=params, headers=headers, expires=expires, now=now)
        encoded_path = "/" + "/".join(quote(seg, safe="-_.~") for seg in key.lstrip("/").split("/"))
        auth_parts = [
            "q-sign-algorithm=sha1",
            f"q-ak={quote(self.secret_id, safe='')}",
            f"q-sign-time={quote(sig['key_time'], safe=';')}",
            f"q-key-time={quote(sig['key_time'], safe=';')}",
            f"q-header-list={quote(sig['header_list'], safe=';')}",
            f"q-url-param-list={quote(sig['url_param_list'], safe=';')}",
            f"q-signature={sig['signature']}",
        ]
        query_parts = []
        if sig["http_parameters"]:
            query_parts.append(sig["http_parameters"])
        query_parts.extend(auth_parts)
        return f"https://{self.host}{encoded_path}?{'&'.join(query_parts)}"

    def presign_put(
        self,
        key: str,
        content_type: str = "application/octet-stream",
        *,
        size_bytes: int | None = None,
        expires: int = 300,
    ) -> dict:
        signed_headers: dict[str, str] = {}
        req_headers = {"Content-Type": content_type or "application/octet-stream"}
        if size_bytes is not None and int(size_bytes) > 0:
            signed_headers["content-length"] = str(int(size_bytes))
            req_headers["Content-Length"] = str(int(size_bytes))
        url = self.presign_url("PUT", key, headers=signed_headers or None, expires=expires)
        return {
            "upload_url": url,
            "method": "PUT",
            "headers": req_headers,
            "expires_in": int(expires),
        }

    def presign_get(self, key: str, *, expires: int = 300, download_name: str | None = None) -> dict:
        params = {}
        if download_name:
            safe_name = quote(str(download_name), safe="-_.~")
            params["response-content-disposition"] = f"attachment; filename*=UTF-8''{safe_name}"
        url = self.presign_url("GET", key, params=params or None, expires=expires)
        return {
            "download_url": url,
            "expires_in": int(expires),
        }

    def authorization_header(self, method: str, key: str, *, expires: int = 120, now: int | None = None) -> str:
        sig = self._build_signature(method, key, expires=expires, now=now)
        return (
            f"q-sign-algorithm=sha1"
            f"&q-ak={self.secret_id}"
            f"&q-sign-time={sig['key_time']}"
            f"&q-key-time={sig['key_time']}"
            f"&q-header-list={sig['header_list']}"
            f"&q-url-param-list={sig['url_param_list']}"
            f"&q-signature={sig['signature']}"
        )

    def head_object(self, key: str, *, timeout: int = 15) -> tuple[bool, int, str]:
        encoded_path = "/" + "/".join(quote(seg, safe="-_.~") for seg in key.lstrip("/").split("/"))
        url = f"https://{self.host}{encoded_path}"
        auth = self.authorization_header("HEAD", key, expires=120)
        req = Request(url, headers={"Host": self.host, "Authorization": auth}, method="HEAD")
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                size = int(resp.headers.get("Content-Length") or 0)
                etag = str(resp.headers.get("ETag") or "").strip('"')
                return True, size, etag
        except HTTPError as exc:
            if exc.code == 404:
                return False, 0, ""
            raise RuntimeError(f"COS 对象检查失败（HTTP {exc.code}）。") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"无法连接 COS 服务：{exc}") from exc

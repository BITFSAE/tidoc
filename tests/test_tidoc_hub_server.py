"""Unit tests for tidoc_hub server, COS V5 signer, RBAC, and workflow states."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import pytest

from tidoc_hub.cos_sign import CosSigner
from tidoc_hub.server import HubConfig, create_server


def _http_json(url: str, method: str = "GET", body: dict | None = None, token: str = "") -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, json.loads(raw) if raw else {}


def _http_raw(url: str, method: str = "GET", data: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes]:
    req = Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture
def hub_env(tmp_path):
    cfg = HubConfig(
        host="127.0.0.1",
        port=0,
        prefix="/tidoc-hub",
        data_dir=str(tmp_path / "hub-data"),
        storage="local",
        join_rate_limit=5,
    )
    server = create_server(cfg)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}/tidoc-hub"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    admin_user, admin_token = server.db.create_user("管理员A", role="admin", reviewer="队长")
    try:
        yield {
            "server": server,
            "base_url": base_url,
            "admin": admin_user,
            "admin_token": admin_token,
        }
    finally:
        server.shutdown()
        server.close_resources()
        thread.join(timeout=5)


def test_cos_v5_signer_canonical_signature():
    signer = CosSigner("AKID_TEST", "SECRET_KEY_TEST", "bitfsae-tidoc-1416420925", "ap-beijing")
    now = 1700000000
    url = signer.presign_url("PUT", "blobs/" + "a" * 64, expires=300, now=now)
    parsed = urlsplit(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "bitfsae-tidoc-1416420925.cos.ap-beijing.myqcloud.com"
    assert parsed.path == "/blobs/" + "a" * 64
    qs = parse_qs(parsed.query)
    assert qs["q-sign-algorithm"] == ["sha1"]
    assert qs["q-ak"] == ["AKID_TEST"]
    key_time = f"{now - 60};{now + 300}"
    assert qs["q-key-time"] == [key_time]
    assert qs["q-header-list"] == ["host"]

    # Independently recompute COS V5 HMAC-SHA1 signature
    http_string = f"put\n/blobs/{'a' * 64}\n\nhost=bitfsae-tidoc-1416420925.cos.ap-beijing.myqcloud.com\n"
    http_sha1 = hashlib.sha1(http_string.encode()).hexdigest()
    string_to_sign = f"sha1\n{key_time}\n{http_sha1}\n"
    sign_key = hmac.new(b"SECRET_KEY_TEST", key_time.encode(), hashlib.sha1).hexdigest()
    expected_sig = hmac.new(sign_key.encode(), string_to_sign.encode(), hashlib.sha1).hexdigest()
    assert qs["q-signature"] == [expected_sig]

    # PUT with size_bytes signs content-length;host
    put_res = signer.presign_put("blobs/u1/" + "a" * 64, "application/pdf", size_bytes=1234)
    put_qs = parse_qs(urlsplit(put_res["upload_url"]).query)
    assert put_qs["q-header-list"] == ["content-length;host"]
    assert put_res["headers"]["Content-Length"] == "1234"

    # GET with download_name puts response-content-disposition into q-url-param-list
    get_res = signer.presign_get("blobs/u1/" + "b" * 64, download_name="付款 截图.png")
    get_qs = parse_qs(urlsplit(get_res["download_url"]).query)
    assert "response-content-disposition" in get_qs["q-url-param-list"][0]
    assert "q-signature" in get_qs


def test_hub_auth_invites_and_rate_limit(hub_env):
    base = hub_env["base_url"]
    admin_tok = hub_env["admin_token"]

    st, data = _http_json(f"{base}/health")
    assert st == 200 and data["ok"] and data["storage"] == "local"

    st, info = _http_json(f"{base}/api/v1/info")
    assert st == 200 and info["api_version"] == 1

    # Prefix isolation
    root_port = hub_env["server"].server_address[1]
    st, _ = _http_json(f"http://127.0.0.1:{root_port}/health")
    assert st == 404

    # Create single-use invite
    st, inv_res = _http_json(
        f"{base}/api/v1/admin/invites",
        "POST",
        {"role": "member", "max_uses": 1, "expires_days": 7, "note": "测试邀请"},
        token=admin_tok,
    )
    assert st == 200
    code = inv_res["invite"]["code"]

    # Join succeeds once
    st, join1 = _http_json(f"{base}/api/v1/join", "POST", {"invite_code": code, "name": "队员甲", "reviewer": "管理员A"})
    assert st == 200 and join1["token"].startswith("th_") and join1["user"]["role"] == "member"
    member_tok = join1["token"]

    # Second use fails (exhausted)
    st, join2 = _http_json(f"{base}/api/v1/join", "POST", {"invite_code": code, "name": "队员乙"})
    assert st == 400 and join2["code"] == "invite_exhausted"

    # Member cannot access admin endpoints
    st, err = _http_json(f"{base}/api/v1/admin/invites", "GET", token=member_tok)
    assert st == 403 and err["code"] == "forbidden"
    st, err = _http_json(f"{base}/api/v1/spaces", "POST", {"name": "非法空间"}, token=member_tok)
    assert st == 403

    # Admin cannot disable self or demote last admin
    st, err = _http_json(
        f"{base}/api/v1/admin/users/{hub_env['admin']['id']}",
        "PATCH",
        {"disabled": True},
        token=admin_tok,
    )
    assert st == 400 and err["code"] == "cannot_disable_self"

    # Revoke invite blocks join
    st, inv2 = _http_json(f"{base}/api/v1/admin/invites", "POST", {"role": "member", "max_uses": 5}, token=admin_tok)
    code2 = inv2["invite"]["code"]
    st, _ = _http_json(f"{base}/api/v1/admin/invites/{code2}/revoke", "POST", {}, token=admin_tok)
    assert st == 200
    st, err = _http_json(f"{base}/api/v1/join", "POST", {"invite_code": code2, "name": "队员丙"})
    assert st == 400 and err["code"] == "invite_revoked"

    # Rate limit triggers after 5 join requests in window
    for _ in range(3):
        st, err = _http_json(f"{base}/api/v1/join", "POST", {"invite_code": "BAD", "name": "X"})
    assert st == 429 and err["code"] == "rate_limited"


def test_hub_rbac_blob_isolation_dedup_lock_and_withdraw(hub_env):
    base = hub_env["base_url"]
    admin_tok = hub_env["admin_token"]
    _, tok_a = hub_env["server"].db.create_user("队员A", role="member")
    _, tok_b = hub_env["server"].db.create_user("队员B", role="member")

    st, sp1_res = _http_json(f"{base}/api/v1/spaces", "POST", {"name": "11月报账"}, token=admin_tok)
    assert st == 200
    sp1 = sp1_res["space"]["id"]
    st, sp2_res = _http_json(f"{base}/api/v1/spaces", "POST", {"name": "12月报账"}, token=admin_tok)
    assert st == 200
    sp2 = sp2_res["space"]["id"]

    # Member A uploads a blob
    blob_bytes = b"%PDF-1.4 sample invoice content"
    blob_sha = hashlib.sha256(blob_bytes).hexdigest()
    st, pre = _http_json(
        f"{base}/api/v1/blobs/presign-upload",
        "POST",
        {"sha256": blob_sha, "size_bytes": len(blob_bytes), "content_type": "application/pdf", "original_name": "inv.pdf"},
        token=tok_a,
    )
    assert st == 200 and pre["exists"] is False

    # Wrong length rejected on PUT (enforces declared size_bytes)
    st_bad_len, _ = _http_raw(pre["upload_url"], "PUT", data=b"tampered", headers={"Content-Type": "application/pdf"})
    assert st_bad_len == 400

    # Same length but wrong bytes rejected on PUT (enforces sha256)
    st_bad, _ = _http_raw(pre["upload_url"], "PUT", data=b"X" * len(blob_bytes), headers=pre["headers"])
    assert st_bad == 400

    # Correct bytes succeed + confirm
    st_put, _ = _http_raw(pre["upload_url"], "PUT", data=blob_bytes, headers=pre["headers"])
    assert st_put == 200
    st_conf, conf = _http_json(f"{base}/api/v1/blobs/confirm", "POST", {"sha256": blob_sha}, token=tok_a)
    assert st_conf == 200 and conf["size_bytes"] == len(blob_bytes)

    # Same-user dedup returns exists=True; different user gets exists=False (no cross-member oracle/theft)
    st_dedup_a, dedup_a = _http_json(
        f"{base}/api/v1/blobs/presign-upload",
        "POST",
        {"sha256": blob_sha, "size_bytes": len(blob_bytes), "content_type": "application/pdf"},
        token=tok_a,
    )
    assert st_dedup_a == 200 and dedup_a["exists"] is True

    st_dedup_b, dedup_b = _http_json(
        f"{base}/api/v1/blobs/presign-upload",
        "POST",
        {"sha256": blob_sha, "size_bytes": len(blob_bytes), "content_type": "application/pdf"},
        token=tok_b,
    )
    assert st_dedup_b == 200 and dedup_b["exists"] is False

    # Member B cannot attach Member A's blob without uploading it themselves
    st_steal, steal_err = _http_json(
        f"{base}/api/v1/spaces/{sp2}/entries/push",
        "POST",
        {
            "invoice_no": "26112000000099999999",
            "invoice_sha256": blob_sha,
            "snapshot_digest": "9" * 64,
            "payload": {"invoice_no": "26112000000099999999"},
            "blob_shas": [blob_sha],
        },
        token=tok_b,
    )
    assert st_steal == 400 and steal_err["code"] == "missing_blobs"

    # Member B uploads their own copy for later duplicate/resubmit tests
    st_put_b, _ = _http_raw(dedup_b["upload_url"], "PUT", data=blob_bytes, headers=dedup_b["headers"])
    assert st_put_b == 200
    st_conf_b, _ = _http_json(f"{base}/api/v1/blobs/confirm", "POST", {"sha256": blob_sha}, token=tok_b)
    assert st_conf_b == 200

    # Member A pushes entry (even tries smuggling payees -> server strips it)
    st, pushed = _http_json(
        f"{base}/api/v1/spaces/{sp1}/entries/push",
        "POST",
        {
            "invoice_no": "26112000000012345678",
            "invoice_sha256": blob_sha,
            "snapshot_digest": "1" * 64,
            "ancestors": [],
            "profile": {"id": "p1", "name": "队员A", "reviewer": "管理员A"},
            "payload": {"invoice_no": "26112000000012345678", "seller": "测试商家", "payees": [{"bank": "secret"}]},
            "blob_shas": [blob_sha],
        },
        token=tok_a,
    )
    assert st == 200 and pushed["created"] is True
    entry_a_id = pushed["entry"]["id"]
    assert "payees" not in pushed["entry"]["payload"]

    # Member B privacy isolation:
    # 1) Space counts for B must be 0
    st, sp_list_b = _http_json(f"{base}/api/v1/spaces", "GET", token=tok_b)
    assert st == 200
    assert next(s for s in sp_list_b["spaces"] if s["id"] == sp1)["entry_count"] == 0
    # 2) Space entries list for B is empty
    st, ent_list_b = _http_json(f"{base}/api/v1/spaces/{sp1}/entries", "GET", token=tok_b)
    assert st == 200 and ent_list_b["entries"] == []
    # 3) Direct GET on A's entry returns 403
    st, _ = _http_json(f"{base}/api/v1/spaces/{sp1}/entries/{entry_a_id}", "GET", token=tok_b)
    assert st == 403
    # 4) Presign-download on A's blob via A's entry returns 403
    st, _ = _http_json(
        f"{base}/api/v1/blobs/presign-download",
        "POST",
        {"sha256": blob_sha, "space_id": sp1, "entry_id": entry_a_id},
        token=tok_b,
    )
    assert st == 403
    # 5) Admin CAN presign-download A's blob
    st, dl = _http_json(
        f"{base}/api/v1/blobs/presign-download",
        "POST",
        {"sha256": blob_sha, "space_id": sp1, "entry_id": entry_a_id},
        token=admin_tok,
    )
    assert st == 200
    st_get, dl_bytes = _http_raw(dl["download_url"], "GET")
    assert st_get == 200 and dl_bytes == blob_bytes

    # Global duplicate invoice check across members and spaces
    st, dup = _http_json(
        f"{base}/api/v1/spaces/{sp2}/entries/push",
        "POST",
        {
            "invoice_no": "26112000000012345678",
            "invoice_sha256": blob_sha,
            "snapshot_digest": "2" * 64,
            "payload": {"invoice_no": "26112000000012345678"},
            "blob_shas": [blob_sha],
        },
        token=tok_b,
    )
    assert st == 409 and dup["code"] == "duplicate_invoice"

    # Admin flags needs_fix -> increments rev to 2
    st, rev2 = _http_json(
        f"{base}/api/v1/spaces/{sp1}/entries/{entry_a_id}/review",
        "POST",
        {"review_state": "needs_fix", "review_note": "付款截图模糊", "base_revision": 1},
        token=admin_tok,
    )
    assert st == 200 and rev2["entry"]["revision"] == 2 and rev2["entry"]["review_state"] == "needs_fix"

    # Member A pushing with stale base_revision=1 gets 409 revision_conflict
    st, conf_res = _http_json(
        f"{base}/api/v1/spaces/{sp1}/entries/push",
        "POST",
        {
            "remote_entry_id": entry_a_id,
            "base_revision": 1,
            "invoice_no": "26112000000012345678",
            "snapshot_digest": "3" * 64,
            "payload": {"invoice_no": "26112000000012345678"},
            "blob_shas": [blob_sha],
        },
        token=tok_a,
    )
    assert st == 409 and conf_res["code"] == "revision_conflict" and conf_res["server_revision"] == 2

    # Member A pushes with base_revision=2 -> auto transitions needs_fix -> updated
    st, rev3 = _http_json(
        f"{base}/api/v1/spaces/{sp1}/entries/push",
        "POST",
        {
            "remote_entry_id": entry_a_id,
            "base_revision": 2,
            "invoice_no": "26112000000012345678",
            "snapshot_digest": "3" * 64,
            "payload": {"invoice_no": "26112000000012345678"},
            "blob_shas": [blob_sha],
        },
        token=tok_a,
    )
    assert st == 200 and rev3["entry"]["revision"] == 3 and rev3["entry"]["review_state"] == "updated"
    assert "1" * 64 in rev3["entry"]["ancestors"]

    # Admin locks entry -> member gets 423 on push and withdraw
    st, rev4 = _http_json(
        f"{base}/api/v1/spaces/{sp1}/entries/{entry_a_id}/review",
        "POST",
        {"review_state": "locked", "review_note": "已归档", "base_revision": 3},
        token=admin_tok,
    )
    assert st == 200 and rev4["entry"]["review_state"] == "locked"

    st, lock_err = _http_json(
        f"{base}/api/v1/spaces/{sp1}/entries/push",
        "POST",
        {
            "remote_entry_id": entry_a_id,
            "base_revision": 4,
            "invoice_no": "26112000000012345678",
            "snapshot_digest": "4" * 64,
            "payload": {"invoice_no": "26112000000012345678"},
            "blob_shas": [blob_sha],
        },
        token=tok_a,
    )
    assert st == 423 and lock_err["code"] == "entry_locked"

    st, lock_w = _http_json(
        f"{base}/api/v1/spaces/{sp1}/entries/{entry_a_id}/withdraw",
        "POST",
        {"base_revision": 4},
        token=tok_a,
    )
    assert st == 423 and lock_w["code"] == "entry_locked"

    # Admin withdraws -> frees invoice_registry so B can now submit that invoice
    st, w_res = _http_json(
        f"{base}/api/v1/spaces/{sp1}/entries/{entry_a_id}/withdraw",
        "POST",
        {"base_revision": 4},
        token=admin_tok,
    )
    assert st == 200 and w_res["entry"]["review_state"] == "withdrawn"

    # Admin cannot review a withdrawn entry back to active without re-submission
    st_rev_w, rev_w_err = _http_json(
        f"{base}/api/v1/spaces/{sp1}/entries/{entry_a_id}/review",
        "POST",
        {"review_state": "submitted", "review_note": ""},
        token=admin_tok,
    )
    assert st_rev_w == 409 and rev_w_err["code"] == "entry_withdrawn"

    st, resub = _http_json(
        f"{base}/api/v1/spaces/{sp2}/entries/push",
        "POST",
        {
            "invoice_no": "26112000000012345678",
            "invoice_sha256": blob_sha,
            "snapshot_digest": "5" * 64,
            "payload": {"invoice_no": "26112000000012345678"},
            "blob_shas": [blob_sha],
        },
        token=tok_b,
    )
    assert st == 200 and resub["created"] is True
    entry_b_id = resub["entry"]["id"]

    # Member cannot withdraw from a closed space
    st_close, _ = _http_json(f"{base}/api/v1/spaces/{sp2}", "PATCH", {"status": "closed"}, token=admin_tok)
    assert st_close == 200
    st_w_closed, w_closed_err = _http_json(
        f"{base}/api/v1/spaces/{sp2}/entries/{entry_b_id}/withdraw",
        "POST",
        {"base_revision": 1},
        token=tok_b,
    )
    assert st_w_closed == 423 and w_closed_err["code"] == "space_closed"

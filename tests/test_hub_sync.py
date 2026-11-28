"""End-to-end integration tests: two Tidoc desktop Api instances syncing via tidoc_hub."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from tidoc.api import Api
from tidoc.db.entries import PAYMENT_AI_RECOGNITION_VERSION
from tidoc.engine.models import ParsedInvoice
from tidoc.services.entry_merge import snapshot_digest
from tidoc.services.hub_sync import HubError
from tidoc_hub.server import HubConfig, create_server


def _write_file(tmp_path: Path, name: str, content: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(content)
    return p


@pytest.fixture
def sync_env(tmp_path):
    cfg = HubConfig(
        host="127.0.0.1",
        port=0,
        prefix="/tidoc-hub",
        data_dir=str(tmp_path / "hub"),
        storage="local",
    )
    server = create_server(cfg)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}/tidoc-hub"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _, admin_token = server.db.create_user("李汇总", role="admin", reviewer="队长")

    admin_api = Api(data_root=tmp_path / "admin-data")
    member_api = Api(data_root=tmp_path / "member-data")
    member_b_api = Api(data_root=tmp_path / "member-b-data")

    admin_pid = admin_api.profiles.create("李汇总", "队长", is_default=True)["id"]
    member_pid = member_api.profiles.create("张队员", "李汇总", is_default=True)["id"]
    member_b_pid = member_b_api.profiles.create("王队员", "李汇总", is_default=True)["id"]

    try:
        yield {
            "server": server,
            "base_url": base_url,
            "admin_token": admin_token,
            "admin_api": admin_api,
            "member_api": member_api,
            "member_b_api": member_b_api,
            "admin_pid": admin_pid,
            "member_pid": member_pid,
            "member_b_pid": member_b_pid,
            "tmp_path": tmp_path,
        }
    finally:
        admin_api.db.close()
        member_api.db.close()
        member_b_api.db.close()
        server.shutdown()
        server.close_resources()
        thread.join(timeout=5)


def test_full_member_admin_roundtrip_without_member_ai_key(sync_env):
    base = sync_env["base_url"]
    admin_api: Api = sync_env["admin_api"]
    member_api: Api = sync_env["member_api"]
    member_b_api: Api = sync_env["member_b_api"]
    tmp = sync_env["tmp_path"]

    # 1. Default offline state
    assert admin_api.hub_settings()["data"]["connected"] is False
    assert member_api.hub_settings()["data"]["connected"] is False

    # 2. Admin connects via token, creates space & member invite
    conn_res = admin_api.hub_connect_token(base, sync_env["admin_token"])
    assert conn_res["ok"] and conn_res["data"]["hub"]["role"] == "admin"

    sp_res = admin_api.hub_create_space("2026年11月报账", "十一月集中报账")
    assert sp_res["ok"]
    space_id = sp_res["data"]["space"]["id"]

    inv_res = admin_api.hub_create_invite("member", 5, 30, "队员邀请")
    assert inv_res["ok"]
    invite_code = inv_res["data"]["invite"]["code"]

    # 3. Member A & Member B join via invite code
    j_a = member_api.hub_join(base, invite_code, "张队员", "李汇总")
    assert j_a["ok"] and j_a["data"]["hub"]["role"] == "member"
    j_b = member_b_api.hub_join(base, invite_code, "王队员", "李汇总")
    assert j_b["ok"]

    # 4. Member A creates entry with invoice + bad payment screenshot (order page) + note
    parsed = ParsedInvoice(
        invoice_no="26112000000099887766",
        invoice_date="2026-11-20",
        seller="北京测试电子有限公司",
        total="128.50",
        buyer_name="北京理工大学",
        buyer_tax_id="12100000400008469X",
    )
    m_entry_id = member_api.entries.create(sync_env["member_pid"], title="北京理工大学", parsed=parsed)
    inv_file = _write_file(tmp, "inv.pdf", b"%PDF-1.4 invoice 99887766")
    bad_pay = _write_file(tmp, "order_page.png", b"\x89PNG\r\n\x1a\nbad-order-page")
    member_api.attachments.add(m_entry_id, inv_file, "invoice_pdf")
    member_api.attachments.add(m_entry_id, bad_pay, "payment_screenshot")
    member_api.entries.update_field(m_entry_id, "notes", "车队传感器采购", sync_env["member_pid"])

    # Push to shared space
    push1 = member_api.hub_push_entries(space_id, [m_entry_id], "2026年11月报账")
    assert push1["ok"] and len(push1["data"]["pushed"]) == 1
    assert push1["data"]["pushed"][0]["revision"] == 1
    assert member_api.entries.get(m_entry_id)["hub"]["review_state"] == "submitted"

    # Member B trying to push the same invoice number is blocked
    mb_entry_id = member_b_api.entries.create(sync_env["member_b_pid"], title="北京理工大学", parsed=parsed)
    member_b_api.attachments.add(mb_entry_id, inv_file, "invoice_pdf")
    dup_push = member_b_api.hub_push_entries(space_id, [mb_entry_id], "2026年11月报账")
    assert dup_push["ok"] and len(dup_push["data"]["duplicates"]) == 1 and len(dup_push["data"]["pushed"]) == 0

    # Browsing projects display fields only and respects server-side ownership without pulling.
    listed = admin_api.hub_list_space_entries(space_id)
    assert listed == {"ok": True, "data": {"entries": [{
        "invoice_no": parsed.invoice_no,
        "seller": parsed.seller,
        "title": "北京理工大学",
        "total": "128.50",
        "owner_name": "张队员",
        "review_state": "submitted",
        "review_note": "",
    }]}}
    assert member_api.hub_list_space_entries(space_id) == listed
    assert member_b_api.hub_list_space_entries(space_id)["data"]["entries"] == []

    # 5. Admin previews and pulls space entries
    prev_a1 = admin_api.hub_preview_pull(space_id)
    assert prev_a1["ok"] and prev_a1["data"]["counts"]["new"] == 1
    pull_a1 = admin_api.hub_apply_pull(space_id, sync_env["admin_pid"], {})
    assert pull_a1["ok"] and pull_a1["data"]["imported"] == 1
    a_entry_id = pull_a1["data"]["entry_ids"][0]
    a_entry = admin_api.entries.get(a_entry_id)
    assert a_entry["invoice_no"] == "26112000000099887766"
    assert a_entry["hub"]["owner_name"] == "张队员"
    assert a_entry["hub"]["remote_revision"] == 1

    # 6. Admin adds inspection PDF + records payment AI warning on the bad screenshot,
    #    then marks needs_fix with review_note and pushes local fixes back!
    insp_file = _write_file(tmp, "inspection.pdf", b"%PDF-1.4 inspection result 1st")
    admin_api.attachments.add(a_entry_id, insp_file, "inspection_pdf")
    pay_att_admin = next(a for a in admin_api.entries.get(a_entry_id)["attachments"] if a["type"] == "payment_screenshot")
    admin_api.db.conn.execute(
        """UPDATE attachments
              SET recognition_version = ?, recognition_status = 'review',
                  recognized_value = '', recognition_message = '订单页，请上传付款账单截图'
            WHERE id = ?""",
        (f"{PAYMENT_AI_RECOGNITION_VERSION}:deepseek", pay_att_admin["id"]),
    )
    admin_api.db.conn.commit()
    admin_api.entries.refresh_payment_check(a_entry_id, remind_pending=False)

    rev_res = admin_api.hub_review_entry(
        a_entry_id,
        "needs_fix",
        "付款截图为订单页，请更换为微信/支付宝支付账单截图",
        push_local=True,
    )
    assert rev_res["ok"] and rev_res["data"]["link"]["review_state"] == "needs_fix"
    assert rev_res["data"]["link"]["remote_revision"] == 2

    # 7. Member A (NO AI Key configured!) pulls Admin's update:
    #    gets inspection PDF + review_note + imported payment AI warning!
    prev_m1 = member_api.hub_preview_pull(space_id)
    assert prev_m1["ok"]
    assert prev_m1["data"]["counts"]["conflict"] == 0
    assert prev_m1["data"]["counts"]["merge"] == 1
    pull_m1 = member_api.hub_apply_pull(space_id, sync_env["member_pid"], {})
    assert pull_m1["ok"] and pull_m1["data"]["updated"] == 1
    m_after = member_api.entries.get(m_entry_id)
    assert m_after["has_inspection"] is True
    assert m_after["hub"]["review_state"] == "needs_fix"
    assert "订单页" in m_after["hub"]["review_note"]
    assert "[付款截图识别]" in (m_after["check_message"] or "")

    # 8. Member A replaces bad screenshot with good receipt screenshot and re-pushes
    old_pay = next(a for a in m_after["attachments"] if a["type"] == "payment_screenshot")
    member_api.attachments.delete(old_pay["id"])
    good_pay = _write_file(tmp, "wechat_receipt.png", b"\x89PNG\r\n\x1a\ngood-wechat-bill-128.50")
    member_api.attachments.add(m_entry_id, good_pay, "payment_screenshot")
    member_api._refresh_payment_ai_check(m_entry_id)
    assert "[付款截图识别]" not in (member_api.entries.get(m_entry_id)["check_message"] or "")

    push2 = member_api.hub_push_entries(space_id, [m_entry_id], "2026年11月报账")
    assert push2["ok"] and len(push2["data"]["pushed"]) == 1
    assert push2["data"]["pushed"][0]["review_state"] == "updated"
    assert push2["data"]["pushed"][0]["revision"] == 3

    # 9. Admin pulls Member A's fix -> 3-way merge auto-replaces bad screenshot with zero conflict
    #    and preserves Admin's inspection PDF + clears stale payment warning!
    prev_a2 = admin_api.hub_preview_pull(space_id)
    assert prev_a2["ok"]
    plan = prev_a2["data"]["entries"][0]["merge_preview"]
    assert plan["mode"] == "three_way"
    assert plan["has_conflicts"] is False
    assert plan["has_replacements"] is True

    pull_a2 = admin_api.hub_apply_pull(space_id, sync_env["admin_pid"], {})
    assert pull_a2["ok"] and pull_a2["data"]["updated"] == 1
    a_final = admin_api.entries.get(a_entry_id)
    pay_names = [a["original_name"] for a in a_final["attachments"] if a["type"] == "payment_screenshot"]
    assert pay_names == ["wechat_receipt.png"]
    assert a_final["has_inspection"] is True
    assert a_final["hub"]["review_state"] == "updated"
    assert "[付款截图识别]" not in (a_final["check_message"] or "")

    # 10. Optimistic lock: Admin locks (rev 4), Member tries stale push -> locked/conflict
    lock_res = admin_api.hub_review_entry(a_entry_id, "locked", "核对通过已归档", push_local=False)
    assert lock_res["ok"] and lock_res["data"]["link"]["review_state"] == "locked"
    stale_push = member_api.hub_push_entries(space_id, [m_entry_id], "2026年11月报账")
    assert stale_push["ok"] and len(stale_push["data"]["locked"]) == 1

    # 11. Admin unlocks & Member withdraws -> Admin preview_pull reports withdrawn_local
    unlock_res = admin_api.hub_review_entry(a_entry_id, "submitted", "", push_local=False)
    assert unlock_res["ok"]
    # Member pulls latest rev first then withdraws
    member_api.hub_apply_pull(space_id, sync_env["member_pid"], {})
    w_res = member_api.hub_withdraw_entry(m_entry_id)
    assert w_res["ok"] and w_res["data"]["link"]["review_state"] == "withdrawn"
    assert admin_api.hub_list_space_entries(space_id)["data"]["entries"] == []

    prev_a3 = admin_api.hub_preview_pull(space_id)
    assert prev_a3["ok"] and len(prev_a3["data"]["withdrawn_local"]) == 1
    assert prev_a3["data"]["withdrawn_local"][0]["local_entry_id"] == a_entry_id

    # 12. Admin acknowledges withdrawal -> local link becomes withdrawn and banner clears on next preview
    ack_res = admin_api.hub_apply_pull(space_id, sync_env["admin_pid"], {"selected_entry_indexes": []})
    assert ack_res["ok"]
    assert admin_api.entries.get(a_entry_id)["hub"]["review_state"] == "withdrawn"
    prev_a4 = admin_api.hub_preview_pull(space_id)
    assert prev_a4["ok"] and prev_a4["data"]["withdrawn_local"] == []


def test_pull_preview_marks_review_only_changes(sync_env):
    admin = sync_env["admin_api"]
    member = sync_env["member_api"]
    url = sync_env["base_url"]
    member_pid = sync_env["member_pid"]

    assert admin.hub_connect_token(url, sync_env["admin_token"])["ok"]
    space_id = admin.hub_create_space("审核测试")["data"]["space"]["id"]
    invite = admin.hub_create_invite("member", 1)["data"]["invite"]["code"]
    assert member.hub_join(url, invite, "张队员")["ok"]
    entry_id = member.entries.create(member_pid, parsed=ParsedInvoice(invoice_no="26999000000000000101"))
    assert len(member.hub_push_entries(space_id, [entry_id])["data"]["pushed"]) == 1
    admin_entry = admin.hub_apply_pull(space_id, sync_env["admin_pid"])["data"]["entry_ids"][0]
    local_state = "submitted"

    # First change the status and note, then edit only the note.
    for note in ("请补付款截图", "请补含商户名称的付款截图"):
        assert admin.hub_review_entry(admin_entry, "needs_fix", note, push_local=False)["ok"]
        preview = member.hub_preview_pull(space_id)["data"]
        row = preview["entries"][0]
        assert preview["counts"] == {"new": 0, "merge": 1, "conflict": 0, "unchanged": 0}
        assert row["import_action"] == "merge"
        assert row["hub_review_changed"] is True
        assert not row["merge_preview"]["items"]
        assert row["hub_meta"]["review_note"] == note
        assert member.entries.get(entry_id)["hub"]["review_state"] == local_state

        applied = member.hub_apply_pull(space_id, member_pid, {"selected_entry_indexes": [0]})["data"]
        assert applied["status_updated_entry_ids"] == [entry_id]
        assert applied["updated"] == 0
        local_state = "needs_fix"
        assert member.entries.get(entry_id)["hub"]["review_note"] == note
        row = member.hub_preview_pull(space_id)["data"]["entries"][0]
        assert row["import_action"] == "unchanged"
        assert row["hub_review_changed"] is False


def _connect_test_member(env):
    admin = env["admin_api"]
    member = env["member_api"]
    url = env["base_url"]
    assert admin.hub_connect_token(url, env["admin_token"])["ok"]
    space_id = admin.hub_create_space("上传测试")["data"]["space"]["id"]
    invite = admin.hub_create_invite("member", 1)["data"]["invite"]["code"]
    assert member.hub_join(url, invite, "张队员")["ok"]
    return member, member._hub_client(), space_id


def test_lost_push_response_can_be_retried_without_creating_another_draft(sync_env, monkeypatch):
    member, client, space_id = _connect_test_member(sync_env)
    entry_id = member.entries.create(sync_env["member_pid"], parsed=ParsedInvoice(seller="无票号示例"))
    push = client.push_entry

    def drop_reply(space_id, body):
        assert push(space_id, body)["entry"]["revision"] == 1
        raise HubError("模拟服务器已保存，客户端没收到响应")

    with monkeypatch.context() as patch:
        patch.setattr(client, "push_entry", drop_reply)
        failed = member.hub_push_entries(space_id, [entry_id])["data"]
        assert len(failed["failed"]) == 1
        assert not member.entries.get(entry_id).get("hub")

    # The client must also recognize this origin if it was edited before the retry.
    with monkeypatch.context() as patch:
        def edit_payload(sid, body):
            changed = {**body, "profile": {**body["profile"], "name": "其他姓名"}}
            del changed["submission_id"]
            changed["submission_id"] = snapshot_digest(changed)
            return push(sid, changed)
        patch.setattr(client, "push_entry", edit_payload)
        assert len(member.hub_push_entries(space_id, [entry_id])["data"]["conflicts"]) == 1

    [accepted] = member.hub_push_entries(space_id, [entry_id])["data"]["pushed"]
    [remote] = client.list_space_entries(space_id)["entries"]
    assert accepted["revision"] == remote["revision"] == 1
    assert accepted["remote_entry_id"] == remote["id"]
    assert member._hub_client() is client


def test_batched_upload_runs_three_transfers_and_retry_reuses_successes(sync_env, monkeypatch):
    member, client, space_id = _connect_test_member(sync_env)
    entry_id = member.entries.create(sync_env["member_pid"], parsed=ParsedInvoice(invoice_no="26999000000000000201"))
    paths = [
        _write_file(sync_env["tmp_path"], f"test{index}.txt", f"sample {index}".encode())
        for index in range(3)
    ]
    attachments = [member.attachments.add(entry_id, path, "other") for path in paths]
    failed_path = attachments[1]["stored_path"]
    barrier = threading.Barrier(3, timeout=3)
    upload = client.upload_blob
    request = client._request_json
    calls = []

    def track(method, path, *args, **kwargs):
        calls.append(path.rsplit("/", 1)[-1])
        return request(method, path, *args, **kwargs)

    retries = []
    def drop_one(url, path, headers):
        barrier.wait()
        if path.as_posix().endswith(failed_path):
            raise HubError("模拟附件传输断开")
        return upload(url, path, headers)

    monkeypatch.setattr(client, "_request_json", track)
    with monkeypatch.context() as patch:
        patch.setattr(client, "upload_blob", drop_one)
        assert len(member.hub_push_entries(space_id, [entry_id])["data"]["failed"]) == 1
    assert calls == ["presign-upload-batch", "confirm-batch"]

    def track_retry(url, path, headers):
        retries.append(path.as_posix())
        return upload(url, path, headers)

    calls.clear()
    monkeypatch.setattr(client, "upload_blob", track_retry)
    assert len(member.hub_push_entries(space_id, [entry_id])["data"]["pushed"]) == 1
    assert len(retries) == 1 and retries[0].endswith(failed_path)
    assert calls == ["presign-upload-batch", "confirm", "push"]
    assert "payload" not in client.list_space_entries(space_id, summary=True)["entries"][0]
    assert client.list_space_entries(space_id)["entries"][0]["blob_shas"]


def test_client_falls_back_for_older_server(sync_env, monkeypatch):
    member, client, space_id = _connect_test_member(sync_env)
    request = client._request_json
    calls = []

    def old_request(method, path, *args, **kwargs):
        calls.append(path.rsplit("/", 1)[-1])
        if path.endswith("/presign-upload-batch"):
            raise HubError("接口不存在。", status=404, code="not_found")
        if kwargs.get("query"):
            kwargs["query"].pop("view", None)
        return request(method, path, *args, **kwargs)

    monkeypatch.setattr(client, "_request_json", old_request)
    entry_id = member.entries.create(sync_env["member_pid"], parsed=ParsedInvoice(invoice_no="26999000000000000301"))
    for n in range(2):
        path = _write_file(sync_env["tmp_path"], f"old-{n}.txt", f"legacy sample {n}".encode())
        member.attachments.add(entry_id, path, "other")
    for _ in range(2):
        assert len(member.hub_push_entries(space_id, [entry_id])["data"]["pushed"]) == 1
    assert calls.count("presign-upload-batch") == 1
    assert calls.count("presign-upload") == 4
    assert calls.count("confirm") == 2
    assert member.hub_list_space_entries(space_id)["data"]["entries"][0]["invoice_no"] == "26999000000000000301"


def test_incremental_push_skips_unchanged_entries_and_bound_files(sync_env, monkeypatch):
    member, client, space_id = _connect_test_member(sync_env)
    profile = sync_env["member_pid"]
    ids = [
        member.entries.create(profile, parsed=ParsedInvoice(invoice_no=f"2699900000000000040{i}"))
        for i in range(3)
    ]
    for i, eid in enumerate(ids):
        attachment = _write_file(sync_env["tmp_path"], f"initial-{i}.txt", f"content {i}".encode())
        member.attachments.add(eid, attachment, "other")
    assert len(member.hub_push_entries(space_id, ids)["data"]["pushed"]) == 3
    member.entries.update_field(ids[1], "notes", "只改备注", profile)

    requests = []
    exchange = client._request_json

    def count(method, route, *args, **kwargs):
        requests.append(route.rsplit("/", 1)[-1])
        return exchange(method, route, *args, **kwargs)

    monkeypatch.setattr(client, "_request_json", count)
    plan = member.hub_prepare_push(space_id, ids)["data"]["plan_id"]
    rows = [member.hub_push_entries(space_id, [eid], plan_id=plan)["data"] for eid in ids]
    assert [len(row["unchanged"]) for row in rows] == [1, 0, 1]
    assert rows[1]["pushed"][0]["revision"] == 2
    assert requests == ["upload-state", "push", "push", "push"]

    # A lost acknowledgement for an existing entry must survive a newer version in the check.
    member.entries.update_field(ids[1], "notes", "模拟回复丢失", profile)
    submit = client.push_entry
    def lose_response(space, body):
        submit(space, body)
        raise HubError("回复丢失")

    with monkeypatch.context() as patch:
        patch.setattr(client, "push_entry", lose_response)
        plan = member.hub_prepare_push(space_id, ids)["data"]["plan_id"]
        assert member.hub_push_entries(space_id, [ids[1]], plan_id=plan)["data"]["failed"]
    plan = member.hub_prepare_push(space_id, ids)["data"]["plan_id"]
    [recovered] = member.hub_push_entries(space_id, [ids[1]], plan_id=plan)["data"]["pushed"]
    assert recovered["revision"] == 3


def test_rejected_post_does_not_corrupt_the_next_reused_request(sync_env):
    from tidoc.services.hub_sync import HubClient

    client = HubClient(sync_env['base_url'], 'invalid-token')
    with pytest.raises(HubError) as error:
        client._request_json('POST', '/api/v1/spaces', {'name': '未授权空间'})
    assert error.value.status == 401
    client.token = sync_env['admin_token']
    assert client.list_spaces() == []


@pytest.mark.parametrize('remote_change', ['review', 'content'])
def test_lost_response_retry_requires_pull_if_remote_moved(sync_env, monkeypatch, remote_change):
    member, client, space_id = _connect_test_member(sync_env)
    entry_id = member.entries.create(sync_env['member_pid'], parsed=ParsedInvoice(invoice_no='26999000000000000501'))
    submit = client.push_entry
    def lose_response(space, body):
        submit(space, body)
        raise HubError('回复丢失')
    with monkeypatch.context() as patch:
        patch.setattr(client, 'push_entry', lose_response)
        assert member.hub_push_entries(space_id, [entry_id])['data']['failed']
    assert not member.entries.get(entry_id).get('hub')
    admin = sync_env['admin_api']
    admin_entry = admin.hub_apply_pull(space_id, sync_env['admin_pid'])['data']['entry_ids'][0]
    if remote_change == 'content':
        admin.entries.update_field(admin_entry, 'notes', '审核人补充的备注')
    assert admin.hub_review_entry(admin_entry, 'needs_fix', '请补截图', remote_change == 'content')['ok']
    [before] = client.list_space_entries(space_id)['entries']
    plan = member.hub_prepare_push(space_id, [entry_id])['data']['plan_id']
    result = member.hub_push_entries(space_id, [entry_id], plan_id=plan)['data']
    assert len(result['conflicts']) == 1
    assert not result['pushed'] and not result['unchanged']
    assert not member.entries.get(entry_id).get('hub')
    [after] = client.list_space_entries(space_id)['entries']
    assert after == before


@pytest.mark.parametrize('remote_change', ['review', 'content'])
def test_prepared_unchanged_entry_is_checked_again_atomically(sync_env, remote_change):
    member, client, space_id = _connect_test_member(sync_env)
    entry_id = member.entries.create(sync_env['member_pid'], parsed=ParsedInvoice(invoice_no='26999000000000000601'))
    assert member.hub_push_entries(space_id, [entry_id])['data']['pushed']
    original_link = dict(member.entries.get(entry_id)['hub'])
    plan = member.hub_prepare_push(space_id, [entry_id])['data']['plan_id']
    admin = sync_env['admin_api']
    admin_entry = admin.hub_apply_pull(space_id, sync_env['admin_pid'])['data']['entry_ids'][0]
    if remote_change == 'content':
        admin.entries.update_field(admin_entry, 'notes', '审核人补充的备注')
    assert admin.hub_review_entry(admin_entry, 'needs_fix', '请核对', remote_change == 'content')['ok']
    [before] = client.list_space_entries(space_id)['entries']
    result = member.hub_push_entries(space_id, [entry_id], plan_id=plan)['data']
    assert len(result['conflicts']) == 1
    assert not result['pushed'] and not result['unchanged']
    assert member.entries.get(entry_id)['hub'] == original_link
    [after] = client.list_space_entries(space_id)['entries']
    assert after == before

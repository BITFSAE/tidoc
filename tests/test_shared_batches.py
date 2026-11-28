"""Real multi-client local tidoc_hub integration tests for shared batches and near-realtime sync."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from tidoc.api import Api
from tidoc.engine.models import ParsedInvoice
from tidoc.services.entry_merge import build_entry_snapshot, load_exchange_bases, snapshot_digest
from tidoc.services.hub_sync import HubClient, HubError
from tidoc_hub.server import HubConfig, create_server


def _write_file(tmp_path: Path, name: str, content: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(content)
    return p


class _TrackedHubServer:
    """Wraps a real local tidoc_hub server and records every HTTP request path."""

    def __init__(self, tmp_path: Path):
        cfg = HubConfig(
            host="127.0.0.1",
            port=0,
            prefix="/tidoc-hub",
            data_dir=str(tmp_path / "hub-server"),
            storage="local",
        )
        self.server = create_server(cfg)
        self.requests: list[tuple[str, str]] = []
        self._req_lock = threading.Lock()
        orig_dispatch = self.server.RequestHandlerClass._dispatch
        tracker = self

        def tracked_dispatch(handler, method: str):
            with tracker._req_lock:
                tracker.requests.append((method, handler.path.split("?", 1)[0]))
            return orig_dispatch(handler, method)

        # Subclass handler per-server instance so monkeypatching never leaks across tests.
        class TrackedHandler(self.server.RequestHandlerClass):
            def _dispatch(handler_self, method: str) -> None:
                with tracker._req_lock:
                    tracker.requests.append((method, handler_self.path.split("?", 1)[0]))
                return orig_dispatch(handler_self, method)

        self.server.RequestHandlerClass = TrackedHandler
        port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}/tidoc-hub"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def clear_requests(self) -> None:
        with self._req_lock:
            self.requests.clear()

    def snapshot_requests(self) -> list[tuple[str, str]]:
        with self._req_lock:
            return list(self.requests)

    def close(self) -> None:
        self.server.shutdown()
        self.server.close_resources()
        self.thread.join(timeout=5)


def _wait_batch_settled(api: Api, batch_id: str, *, timeout: float = 8.0) -> tuple[dict, dict]:
    """Poll hub_sync_tick until the given batch finishes any active or background check."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = api.hub_sync_tick()
        assert res["ok"], res
        data = res["data"]
        info = data["batches"].get(batch_id)
        if info and info["state"] != "syncing" and not info.get("checking"):
            return info, data
        time.sleep(0.03)
    raise AssertionError(f"Batch {batch_id} remained syncing after {timeout}s")


def _wait_sync_state(api: Api, batch_id: str, expected_states: set[str], *, timeout: float = 8.0) -> tuple[dict, dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = api.hub_sync_tick()
        assert res["ok"], res
        data = res["data"]
        info = data["batches"].get(batch_id)
        if info and info["state"] in expected_states:
            return info, data
        time.sleep(0.03)
    raise AssertionError(f"Batch {batch_id} did not reach {expected_states} within {timeout}s")


@pytest.fixture
def env(tmp_path: Path):
    tracked = _TrackedHubServer(tmp_path)
    _, admin_token = tracked.server.db.create_user("李汇总", role="admin", reviewer="队长")
    admin_api = Api(data_root=tmp_path / "admin-data")
    member_a_api = Api(data_root=tmp_path / "member-a-data")
    member_b_api = Api(data_root=tmp_path / "member-b-data")

    # Speed up auto-sync interval for tests while preserving real 5s default on Api().
    for a in (admin_api, member_a_api, member_b_api):
        a._hub_sync_manager()._interval_seconds = 0.05

    admin_pid = admin_api.profiles.create("李汇总", "队长", is_default=True)["id"]
    member_a_pid = member_a_api.profiles.create("张队员", "李汇总", is_default=True)["id"]
    member_b_pid = member_b_api.profiles.create("王队员", "李汇总", is_default=True)["id"]

    try:
        yield {
            "hub": tracked,
            "base_url": tracked.base_url,
            "admin_token": admin_token,
            "admin_api": admin_api,
            "member_a_api": member_a_api,
            "member_b_api": member_b_api,
            "admin_pid": admin_pid,
            "member_a_pid": member_a_pid,
            "member_b_pid": member_b_pid,
            "tmp_path": tmp_path,
        }
    finally:
        admin_api.db.close()
        member_a_api.db.close()
        member_b_api.db.close()
        tracked.close()


def test_default_zero_network_and_link_persistence(env):
    hub: _TrackedHubServer = env["hub"]
    base = env["base_url"]
    admin_api: Api = env["admin_api"]
    member_api: Api = env["member_a_api"]
    tmp = env["tmp_path"]

    # 1. Zero network by default before any connection
    assert admin_api.hub_batch_links() == {"ok": True, "data": {"batches": {}}}
    tick0 = admin_api.hub_sync_tick()
    assert tick0 == {"ok": True, "data": {"batches": {}, "generation": 0}}
    assert hub.snapshot_requests() == []
    assert admin_api._hub_sync_manager()._worker is None

    # Connect admin and member
    assert admin_api.hub_connect_token(base, env["admin_token"])["ok"]
    inv = admin_api.hub_create_invite("member", 5, 30)["data"]["invite"]["code"]
    assert member_api.hub_join(base, inv, "张队员", "李汇总")["ok"]

    # 2. Admin links existing local batch by creating same-name remote space
    local_b1 = admin_api.create_batch("十一月集中报账")["data"]
    link1 = admin_api.hub_link_batch(batch_id=local_b1["id"], space_id="")
    assert link1["ok"]
    assert link1["data"]["batch_id"] == local_b1["id"]
    space_id_1 = link1["data"]["shared"]["space_id"]
    assert link1["data"]["shared"]["space_name"] == "十一月集中报账"
    assert link1["data"]["shared"]["auto_sync"] is False
    assert link1["data"]["shared"]["state"] == "manual"

    # Member cannot create remote space via hub_link_batch(batch_id=..., space_id='')
    m_local_b = member_api.create_batch("越权空间")["data"]
    denied = member_api.hub_link_batch(batch_id=m_local_b["id"], space_id="")
    assert not denied["ok"]

    # 3. 1-to-1 uniqueness: cannot link a second local batch to space_id_1
    local_b2 = admin_api.create_batch("另一个本地批次")["data"]
    dup_link = admin_api.hub_link_batch(batch_id=local_b2["id"], space_id=space_id_1)
    assert not dup_link["ok"]

    # 4. Member links from remote space_id without batch_id -> creates same-name local batch
    link_m = member_api.hub_link_batch(batch_id="", space_id=space_id_1, auto_sync=False)
    assert link_m["ok"]
    m_batch_id = link_m["data"]["batch_id"]
    m_batch = member_api.get_batch(m_batch_id)["data"]
    assert m_batch["name"] == "十一月集中报账"
    assert m_batch["shared_batch"]["space_id"] == space_id_1

    # Both batch_id and space_id provided: validate and link existing local batch to existing remote space
    space_2 = admin_api.hub_create_space("十二月报账")["data"]["space"]["id"]
    link_both = admin_api.hub_link_batch(batch_id=local_b2["id"], space_id=space_2, auto_sync=False)
    assert link_both["ok"]
    assert link_both["data"]["shared"]["space_id"] == space_2
    assert admin_api.hub_unlink_batch(local_b2["id"])["ok"]

    # Zero network on tick while auto_sync is False
    hub.clear_requests()
    assert admin_api.hub_sync_tick()["ok"]
    assert member_api.hub_sync_tick()["ok"]
    assert hub.snapshot_requests() == []

    # list_batches / reorder_batches / get_batch include shared_batch
    listed = admin_api.list_batches()["data"]["batches"]
    b1_row = next(b for b in listed if b["id"] == local_b1["id"])
    assert b1_row["shared_batch"]["space_id"] == space_id_1
    reordered = admin_api.reorder_batches([local_b2["id"], local_b1["id"]])["data"]["batches"]
    assert next(b for b in reordered if b["id"] == local_b1["id"])["shared_batch"]["space_id"] == space_id_1
    assert next(b for b in reordered if b["id"] == local_b2["id"])["shared_batch"] is None

    # 5. Restart persistence + bindle export does NOT include credentials or links
    assert admin_api.hub_set_batch_auto_sync(local_b1["id"], True)["ok"]
    eid = admin_api.entries.create(env["admin_pid"], parsed=ParsedInvoice(invoice_no="26110000000000000001"))
    admin_api.add_entries_to_batch(local_b1["id"], [eid])
    bindle_path = tmp / "export_test.tidoc"
    from tidoc.services.bindle import export_bindle
    export_bindle(admin_api.entries, admin_api.attachments, [eid], bindle_path)
    import zipfile
    with zipfile.ZipFile(bindle_path) as zf:
        entries_json = zf.read("entries.json").decode("utf-8")
        assert env["admin_token"] not in entries_json
        assert space_id_1 not in entries_json

    admin_api.db.close()
    restarted = Api(data_root=tmp / "admin-data")
    try:
        links_after = restarted.hub_batch_links()["data"]["batches"]
        assert links_after[local_b1["id"]]["space_id"] == space_id_1
        assert links_after[local_b1["id"]]["auto_sync"] is True
        assert links_after[local_b1["id"]]["state"] != "syncing"
        assert restarted.get_batch(local_b1["id"])["data"]["shared_batch"]["space_id"] == space_id_1
    finally:
        restarted.db.close()


def test_auto_sync_upload_pull_no_repush_and_other_batch_preservation(env):
    hub: _TrackedHubServer = env["hub"]
    base = env["base_url"]
    admin_api: Api = env["admin_api"]
    member_api: Api = env["member_a_api"]
    tmp = env["tmp_path"]

    assert admin_api.hub_connect_token(base, env["admin_token"])["ok"]
    inv = admin_api.hub_create_invite("member", 5, 30)["data"]["invite"]["code"]
    assert member_api.hub_join(base, inv, "张队员", "李汇总")["ok"]

    admin_batch = admin_api.create_batch("十一月车队报账")["data"]["id"]
    space_id = admin_api.hub_link_batch(batch_id=admin_batch, space_id="", auto_sync=True)["data"]["shared"]["space_id"]
    member_batch = member_api.hub_link_batch(batch_id="", space_id=space_id, auto_sync=False)["data"]["batch_id"]

    # Member creates entry with invoice + payment screenshot + notes and triggers manual sync
    parsed = ParsedInvoice(
        invoice_no="26112000000011223344",
        invoice_date="2026-11-20",
        seller="测试器件店",
        total="88.00",
        buyer_name="北京理工大学",
        buyer_tax_id="12100000400008469X",
    )
    m_eid = member_api.entries.create(env["member_a_pid"], title="北京理工大学", parsed=parsed)
    inv_pdf = _write_file(tmp, "m_inv.pdf", b"%PDF-1.4 inv-11223344")
    pay_png = _write_file(tmp, "m_pay.png", b"\x89PNG\r\n\x1a\npay-88")
    member_api.attachments.add(m_eid, inv_pdf, "invoice_pdf")
    member_api.attachments.add(m_eid, pay_png, "payment_screenshot")
    member_api.entries.update_field(m_eid, "notes", "初始备注", env["member_a_pid"])
    member_api.add_entries_to_batch(member_batch, [m_eid])

    sync_res = member_api.hub_sync_batch(member_batch)
    assert sync_res["ok"]
    assert sync_res["data"]["shared"]["state"] == "syncing"
    # Immediate tick while/after queueing must never regress to manual
    imm_tick = member_api.hub_sync_tick()["data"]
    assert imm_tick["batches"][member_batch]["state"] in ("syncing", "synced")

    m_info, _ = _wait_batch_settled(member_api, member_batch)
    assert m_info["state"] == "synced"
    assert member_api.entries.get(m_eid)["hub"]["remote_revision"] == 1

    # No-op check: auto_sync=False makes 0 requests on tick; enabling auto_sync makes ONLY 1 GET /sync-state
    hub.clear_requests()
    assert member_api.hub_sync_tick()["ok"]
    assert hub.snapshot_requests() == []
    # Use a wider interval during this single-tick probe so slow CI runners do not re-enqueue
    # a second periodic /sync-state before _wait_batch_settled observes the settled state.
    member_mgr = member_api._hub_sync_manager()
    prev_interval = member_mgr._interval_seconds
    member_mgr._interval_seconds = 5.0
    try:
        assert member_api.hub_set_batch_auto_sync(member_batch, True)["ok"]
        _wait_batch_settled(member_api, member_batch)
        reqs = hub.snapshot_requests()
        assert reqs == [("GET", f"/tidoc-hub/api/v1/spaces/{space_id}/sync-state")]
        assert member_api.hub_set_batch_auto_sync(member_batch, False)["ok"]
    finally:
        member_mgr._interval_seconds = prev_interval

    # Admin auto-sync pulls new entry into admin_batch and bumps generation
    gen_before = admin_api.hub_sync_tick()["data"]["generation"]
    a_info, a_tick = _wait_sync_state(admin_api, admin_batch, {"synced"})
    assert a_info["state"] == "synced"
    assert a_tick["generation"] > gen_before
    a_batch_data = admin_api.get_batch(admin_batch)["data"]
    assert len(a_batch_data["entry_ids"]) == 1
    a_eid = a_batch_data["entry_ids"][0]
    a_entry = admin_api.entries.get(a_eid)
    assert a_entry["invoice_no"] == "26112000000011223344"
    assert len(a_entry["attachments"]) == 2

    # Unchanged ticks on admin must NOT increment generation or re-apply unchanged entry
    gen_stable = a_tick["generation"]
    updated_at_before = a_entry["updated_at"]
    hub.clear_requests()
    time.sleep(0.06)
    admin_mgr = admin_api._hub_sync_manager()
    admin_prev_interval = admin_mgr._interval_seconds
    admin_mgr._interval_seconds = 5.0
    try:
        a_info2, a_tick2 = _wait_batch_settled(admin_api, admin_batch)
        assert a_tick2["generation"] == gen_stable
        assert admin_api.entries.get(a_eid)["updated_at"] == updated_at_before
        assert hub.snapshot_requests() == [("GET", f"/tidoc-hub/api/v1/spaces/{space_id}/sync-state")]
    finally:
        admin_mgr._interval_seconds = admin_prev_interval
        with admin_mgr._lock:
            rt = admin_mgr._runtime.get(admin_batch)
            if rt is not None:
                rt["next_due_at"] = time.monotonic() + admin_prev_interval

    # Member modifies field + adds new attachment -> manual sync uploads ONLY changed entry (new blob only)
    phys_png = _write_file(tmp, "m_phys.png", b"\x89PNG\r\n\x1a\nphysical-item")
    member_api.attachments.add(m_eid, phys_png, "physical_image")
    member_api.entries.update_field(m_eid, "paid_amount", "88.00", env["member_a_pid"])
    hub.clear_requests()
    assert member_api.hub_sync_batch(member_batch)["ok"]
    m_info2, _ = _wait_batch_settled(member_api, member_batch)
    assert m_info2["state"] == "synced"
    assert member_api.entries.get(m_eid)["hub"]["remote_revision"] == 2
    # Only 1 blob uploaded (physical_image), not the existing 2 blobs
    put_reqs = [r for r in hub.snapshot_requests() if r[0] == "PUT"]
    assert len(put_reqs) == 1

    # Admin auto-sync pulls the non-conflicting field + new attachment automatically
    time.sleep(0.06)
    a_info3, a_tick3 = _wait_batch_settled(admin_api, admin_batch)
    assert a_info3["state"] == "synced"
    assert a_tick3["generation"] > gen_stable
    a_updated = admin_api.entries.get(a_eid)
    assert a_updated["fields"]["paid_amount"]["current"] == "88.00"
    assert len(a_updated["attachments"]) == 3

    # Verify matched entry in ANOTHER local batch is NOT stolen into linked batch
    other_batch = admin_api.create_batch("已有其他本地批次")["data"]["id"]
    admin_api.move_entries_between_batches(admin_batch, other_batch, [a_eid])
    member_api.entries.update_field(m_eid, "actual_item_name", "电源模块", env["member_a_pid"])
    assert member_api.hub_sync_batch(member_batch)["ok"]
    _wait_batch_settled(member_api, member_batch)

    time.sleep(0.06)
    gen_before_other = admin_api.hub_sync_tick()["data"]["generation"]
    _wait_batch_settled(admin_api, admin_batch)
    a_tick_other = admin_api.hub_sync_tick()["data"]
    assert a_tick_other["generation"] > gen_before_other
    assert admin_api.entries.get(a_eid)["fields"]["actual_item_name"]["current"] == "电源模块"
    # Entry must remain in other_batch, NOT pulled back into admin_batch
    assert admin_api.batches.batches_of_entry(a_eid)[0]["id"] == other_batch
    assert admin_api.get_batch(admin_batch)["data"]["entry_ids"] == []


def test_same_field_conflict_preserves_values_and_stops_repush(env):
    hub: _TrackedHubServer = env["hub"]
    base = env["base_url"]
    admin_api: Api = env["admin_api"]
    member_api: Api = env["member_a_api"]

    assert admin_api.hub_connect_token(base, env["admin_token"])["ok"]
    inv = admin_api.hub_create_invite("member", 5, 30)["data"]["invite"]["code"]
    assert member_api.hub_join(base, inv, "张队员", "李汇总")["ok"]

    admin_batch = admin_api.create_batch("冲突测试批次")["data"]["id"]
    space_id = admin_api.hub_link_batch(batch_id=admin_batch, space_id="", auto_sync=False)["data"]["shared"]["space_id"]
    member_batch = member_api.hub_link_batch(batch_id="", space_id=space_id, auto_sync=False)["data"]["batch_id"]

    m_eid = member_api.entries.create(env["member_a_pid"], parsed=ParsedInvoice(invoice_no="26112000000055667788"))
    member_api.entries.update_field(m_eid, "paid_amount", "100.00", env["member_a_pid"])
    member_api.add_entries_to_batch(member_batch, [m_eid])
    assert member_api.hub_sync_batch(member_batch)["ok"]
    _wait_batch_settled(member_api, member_batch)

    assert admin_api.hub_sync_batch(admin_batch)["ok"]
    _wait_batch_settled(admin_api, admin_batch)
    a_eid = admin_api.get_batch(admin_batch)["data"]["entry_ids"][0]

    # Both sides edit paid_amount from rev=1 base
    member_api.entries.update_field(m_eid, "paid_amount", "111.00", env["member_a_pid"])
    admin_api.entries.update_field(a_eid, "paid_amount", "222.00", env["admin_pid"])

    # Member pushes first -> rev=2
    assert member_api.hub_sync_batch(member_batch)["ok"]
    m_info, _ = _wait_batch_settled(member_api, member_batch)
    assert m_info["state"] == "synced"

    # Admin auto-sync sees conflict -> must NOT lose 222.00 locally or overwrite 111.00 remotely
    assert admin_api.hub_set_batch_auto_sync(admin_batch, True)["ok"]
    a_info, _ = _wait_sync_state(admin_api, admin_batch, {"conflict"})
    assert a_info["state"] == "conflict"
    assert a_info["conflict_count"] >= 1
    assert admin_api.entries.get(a_eid)["fields"]["paid_amount"]["current"] == "222.00"
    remote_entry = admin_api._hub_client().get_space_entry(space_id, member_api.entries.get(m_eid)["hub"]["remote_entry_id"])
    assert remote_entry["payload"]["fields"]["paid_amount"]["current"] == "111.00"

    # Subsequent auto-sync ticks must NOT endlessly re-push or re-fetch conflicted entry
    hub.clear_requests()
    for _ in range(3):
        time.sleep(0.06)
        _wait_batch_settled(admin_api, admin_batch)
    reqs = hub.snapshot_requests()
    assert not any(path.endswith("/push") for _, path in reqs)
    assert not any(f"/entries/{remote_entry['id']}" in path for _, path in reqs)

    # Manual pull resolution clears conflict state back to synced
    prev = admin_api.hub_preview_pull(space_id)["data"]
    assert prev["counts"]["conflict"] == 1
    applied = admin_api.hub_apply_pull(
        space_id, env["admin_pid"], {"entry_merge_decisions": {"0": {"field:paid_amount": "use_incoming"}}}
    )
    assert applied["ok"]
    assert admin_api.entries.get(a_eid)["fields"]["paid_amount"]["current"] == "111.00"
    assert admin_api.get_batch(admin_batch)["data"]["shared_batch"]["state"] == "synced"


def test_review_lock_withdraw_and_cross_account_isolation(env):
    hub: _TrackedHubServer = env["hub"]
    base = env["base_url"]
    admin_api: Api = env["admin_api"]
    member_a: Api = env["member_a_api"]
    member_b: Api = env["member_b_api"]

    assert admin_api.hub_connect_token(base, env["admin_token"])["ok"]
    inv = admin_api.hub_create_invite("member", 5, 30)["data"]["invite"]["code"]
    assert member_a.hub_join(base, inv, "张队员", "李汇总")["ok"]
    assert member_b.hub_join(base, inv, "王队员", "李汇总")["ok"]

    admin_batch = admin_api.create_batch("审核与隔离")["data"]["id"]
    space_id = admin_api.hub_link_batch(batch_id=admin_batch, space_id="", auto_sync=False)["data"]["shared"]["space_id"]
    batch_a = member_a.hub_link_batch(batch_id="", space_id=space_id, auto_sync=False)["data"]["batch_id"]
    batch_b = member_b.hub_link_batch(batch_id="", space_id=space_id, auto_sync=False)["data"]["batch_id"]

    ea_id = member_a.entries.create(env["member_a_pid"], parsed=ParsedInvoice(invoice_no="26112000000088001122"))
    member_a.add_entries_to_batch(batch_a, [ea_id])
    assert member_a.hub_sync_batch(batch_a)["ok"]
    _wait_batch_settled(member_a, batch_a)

    # Member B must NOT see or pull Member A's entry
    assert member_b.hub_sync_batch(batch_b)["ok"]
    b_info, _ = _wait_batch_settled(member_b, batch_b)
    assert b_info["state"] == "synced"
    assert member_b.get_batch(batch_b)["data"]["entry_ids"] == []

    # Admin pulls A's entry and locks it
    assert admin_api.hub_sync_batch(admin_batch)["ok"]
    _wait_batch_settled(admin_api, admin_batch)
    admin_eid = admin_api.get_batch(admin_batch)["data"]["entry_ids"][0]
    assert admin_api.hub_review_entry(admin_eid, "locked", "审核已锁定", push_local=False)["ok"]

    # Member A syncs -> receives locked status, generation increments
    gen_a0 = member_a.hub_sync_tick()["data"]["generation"]
    assert member_a.hub_sync_batch(batch_a)["ok"]
    _, a_tick1 = _wait_batch_settled(member_a, batch_a)
    assert a_tick1["generation"] > gen_a0
    assert member_a.entries.get(ea_id)["hub"]["review_state"] == "locked"

    # Member A edits locked entry locally & enables auto_sync -> must NOT push or loop
    member_a.entries.update_field(ea_id, "notes", "锁定后尝试修改", env["member_a_pid"])
    assert member_a.hub_set_batch_auto_sync(batch_a, True)["ok"]
    a_locked_info, _ = _wait_sync_state(member_a, batch_a, {"conflict"})
    assert a_locked_info["state"] == "conflict"
    hub.clear_requests()
    time.sleep(0.06)
    _wait_batch_settled(member_a, batch_a)
    assert not any(path.endswith("/push") for _, path in hub.snapshot_requests())

    # Admin unlocks -> Member A automatically pulls unlock and pushes pending local note!
    assert admin_api.hub_review_entry(admin_eid, "submitted", "解锁允许修改", push_local=False)["ok"]
    time.sleep(0.06)
    a_unlocked, _ = _wait_sync_state(member_a, batch_a, {"synced"})
    assert a_unlocked["state"] == "synced"
    assert member_a.entries.get(ea_id)["hub"]["review_state"] in ("submitted", "updated")
    remote_after = admin_api._hub_client().get_space_entry(space_id, member_a.entries.get(ea_id)["hub"]["remote_entry_id"])
    assert remote_after["payload"]["fields"]["notes"]["current"] == "锁定后尝试修改"

    # Member A withdraws -> Admin syncs -> Admin keeps local entry & batch, link becomes withdrawn
    assert member_a.hub_set_batch_auto_sync(batch_a, False)["ok"]
    assert member_a.hub_withdraw_entry(ea_id)["ok"]
    gen_adm0 = admin_api.hub_sync_tick()["data"]["generation"]
    assert admin_api.hub_sync_batch(admin_batch)["ok"]
    adm_w_info, adm_w_tick = _wait_batch_settled(admin_api, admin_batch)
    assert adm_w_info["state"] == "synced"
    assert adm_w_tick["generation"] > gen_adm0
    adm_entry_after = admin_api.entries.get(admin_eid)
    assert adm_entry_after is not None
    assert adm_entry_after["hub"]["review_state"] == "withdrawn"
    assert admin_api.get_batch(admin_batch)["data"]["entry_ids"] == [admin_eid]

    # Account switch / disconnect immediately deactivates links without touching local data
    assert member_a.hub_set_batch_auto_sync(batch_a, True)["ok"]
    assert member_a.hub_disconnect()["ok"]
    paused = member_a.get_batch(batch_a)["data"]["shared_batch"]
    assert paused["state"] == "paused"
    assert paused["auto_sync"] is False
    hub.clear_requests()
    assert member_a.hub_sync_tick()["ok"]
    assert hub.snapshot_requests() == []
    assert member_a.entries.get(ea_id) is not None

    # Switching to a different account keeps old account's link paused
    inv2 = admin_api.hub_create_invite("member", 2, 30)["data"]["invite"]["code"]
    assert member_a.hub_join(base, inv2, "新账号队员", "李汇总")["ok"]
    assert member_a.get_batch(batch_a)["data"]["shared_batch"]["state"] == "paused"
    hub.clear_requests()
    assert member_a.hub_sync_tick()["ok"]
    assert hub.snapshot_requests() == []


def test_archive_unlink_backoff_and_old_server_upgrade_status(env, monkeypatch):
    hub: _TrackedHubServer = env["hub"]
    base = env["base_url"]
    admin_api: Api = env["admin_api"]

    assert admin_api.hub_connect_token(base, env["admin_token"])["ok"]
    b_id = admin_api.create_batch("生命周期测试")["data"]["id"]
    space_id = admin_api.hub_link_batch(batch_id=b_id, space_id="", auto_sync=True)["data"]["shared"]["space_id"]
    _wait_sync_state(admin_api, b_id, {"synced"})

    # 1. Archiving local batch stops all future requests
    assert admin_api.archive_batch(b_id, True)["ok"]
    assert admin_api.get_batch(b_id)["data"]["shared_batch"]["state"] == "paused"
    hub.clear_requests()
    assert admin_api.hub_sync_tick()["ok"]
    assert hub.snapshot_requests() == []
    assert admin_api.archive_batch(b_id, False)["ok"]

    # 2. Error backoff: force sync_state error and verify backoff skips immediate ticks
    mgr = admin_api._hub_sync_manager()
    mgr._interval_seconds = 0.05
    mgr._max_backoff_seconds = 2.0
    client = mgr._get_worker_client(base, env["admin_token"])
    calls = 0

    def fail_once(sid):
        nonlocal calls
        calls += 1
        raise HubError("模拟网络超时", status=503)

    with monkeypatch.context() as patch:
        patch.setattr(client, "sync_state", fail_once)
        err_info, _ = _wait_sync_state(admin_api, b_id, {"error"})
        assert err_info["state"] == "error"
        calls_at_error = calls
        # Immediate tick within backoff must NOT trigger another request
        admin_api.hub_sync_tick()
        time.sleep(0.03)
        assert calls == calls_at_error

    # 3. Old server without /sync-state gives clear upgrade-needed state
    old_client = mgr._get_worker_client(base, env["admin_token"])
    orig_req = old_client._request_json

    def old_server_req(method, path, *args, **kwargs):
        if path.endswith("/sync-state"):
            raise HubError("接口不存在。", status=404, code="not_found")
        if path == "/api/v1/info":
            res = orig_req(method, path, *args, **kwargs)
            res["features"] = ["batch_blob_upload", "incremental_push"]
            return res
        return orig_req(method, path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(old_client, "_request_json", old_server_req)
        assert admin_api.hub_sync_batch(b_id)["ok"]
        up_info, _ = _wait_sync_state(admin_api, b_id, {"error"})
        assert up_info["needs_upgrade"] is True
        assert "升级" in up_info["message"]
        assert up_info["auto_sync"] is False

    # 4. Unlink preserves local data and stops syncing
    eid = admin_api.entries.create(env["admin_pid"], parsed=ParsedInvoice(invoice_no="26112000000033445566"))
    admin_api.add_entries_to_batch(b_id, [eid])
    assert admin_api.hub_unlink_batch(b_id)["ok"]
    assert admin_api.get_batch(b_id)["data"]["shared_batch"] is None
    assert admin_api.entries.get(eid) is not None
    hub.clear_requests()
    assert admin_api.hub_sync_tick()["ok"]
    assert hub.snapshot_requests() == []


def test_concurrency_no_api_lock_stall_local_edit_races_and_clean_shutdown(env, monkeypatch):
    base = env["base_url"]
    admin_api: Api = env["admin_api"]
    member_api: Api = env["member_a_api"]
    tmp = env["tmp_path"]

    assert admin_api.hub_connect_token(base, env["admin_token"])["ok"]
    inv = admin_api.hub_create_invite("member", 5, 30)["data"]["invite"]["code"]
    assert member_api.hub_join(base, inv, "张队员", "李汇总")["ok"]

    admin_batch = admin_api.create_batch("并发测试")["data"]["id"]
    space_id = admin_api.hub_link_batch(batch_id=admin_batch, space_id="", auto_sync=False)["data"]["shared"]["space_id"]
    member_batch = member_api.hub_link_batch(batch_id="", space_id=space_id, auto_sync=False)["data"]["batch_id"]

    # Member creates entry with attachment and pushes
    m_eid = member_api.entries.create(env["member_a_pid"], parsed=ParsedInvoice(invoice_no="26112000000077112233"))
    member_api.entries.update_field(m_eid, "notes", "初始笔记", env["member_a_pid"])
    member_api.add_entries_to_batch(member_batch, [m_eid])
    assert member_api.hub_sync_batch(member_batch)["ok"]
    _wait_batch_settled(member_api, member_batch)

    assert admin_api.hub_sync_batch(admin_batch)["ok"]
    _wait_batch_settled(admin_api, admin_batch)
    a_eid = admin_api.get_batch(admin_batch)["data"]["entry_ids"][0]

    # Case 1: Member adds a new attachment and pushes -> Admin pulls while slow download_blob runs.
    # Verify Api._api_lock is NOT blocked, AND local edit during download prevents overwrite!
    extra_img = _write_file(tmp, "slow_dl.png", b"\x89PNG\r\n\x1a\nslow-download-content")
    member_api.attachments.add(m_eid, extra_img, "physical_image")
    assert member_api.hub_sync_batch(member_batch)["ok"]
    _wait_batch_settled(member_api, member_batch)

    mgr_admin = admin_api._hub_sync_manager()
    adm_client = mgr_admin._get_worker_client(base, env["admin_token"])
    dl_entered = threading.Event()
    dl_release = threading.Event()
    orig_dl = adm_client.download_blob

    def slow_download(url, dest_path, **kwargs):
        dl_entered.set()
        assert dl_release.wait(timeout=5)
        return orig_dl(url, dest_path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(adm_client, "download_blob", slow_download)
        assert admin_api.hub_sync_batch(admin_batch)["ok"]
        assert dl_entered.wait(timeout=5)

        # Main thread MUST NOT be blocked by _api_lock while worker downloads!
        t0 = time.monotonic()
        assert admin_api.list_batches()["ok"]
        assert admin_api.entries.update_field(a_eid, "notes", "下载期间本机新编辑", env["admin_pid"])
        assert time.monotonic() - t0 < 1.0

        dl_release.set()
        _wait_batch_settled(admin_api, admin_batch)

    # Local edit must survive intact, and next sync round cleanly merges both and pushes to hub!
    assert admin_api.entries.get(a_eid)["fields"]["notes"]["current"] == "下载期间本机新编辑"
    assert admin_api.hub_sync_batch(admin_batch)["ok"]
    _wait_batch_settled(admin_api, admin_batch)
    a_final = admin_api.entries.get(a_eid)
    assert a_final["fields"]["notes"]["current"] == "下载期间本机新编辑"
    assert any(a["original_name"] == "slow_dl.png" for a in a_final["attachments"])

    # Member pulls admin's merged revision before starting Case 2 so Case 2 tests a clean push race
    assert member_api.hub_sync_batch(member_batch)["ok"]
    _wait_batch_settled(member_api, member_batch)
    assert member_api.entries.get(m_eid)["fields"]["notes"]["current"] == "下载期间本机新编辑"

    # Case 2: Edit during in-flight push -> snapshot recorded is sent snapshot ("发送前A"),
    # and post-send edit ("发送中B") is picked up in the next round!
    mgr_mem = member_api._hub_sync_manager()
    mem_client = mgr_mem._get_worker_client(base, member_api._hub_token())
    member_api.entries.update_field(m_eid, "notes", "发送前A", env["member_a_pid"])

    push_entered = threading.Event()
    push_release = threading.Event()
    orig_push = mem_client.push_entry

    def slow_push(sid, body):
        push_entered.set()
        assert push_release.wait(timeout=5)
        return orig_push(sid, body)

    with monkeypatch.context() as patch:
        patch.setattr(mem_client, "push_entry", slow_push)
        assert member_api.hub_sync_batch(member_batch)["ok"]
        assert push_entered.wait(timeout=5)
        # User edits locally while push of "发送前A" is in flight
        member_api.entries.update_field(m_eid, "notes", "发送中B", env["member_a_pid"])
        push_release.set()
        _wait_batch_settled(member_api, member_batch)

    # Recorded exchange base must match the sent snapshot ("发送前A"), not "发送中B"
    bases = load_exchange_bases(member_api.db.conn, {m_eid}).get(m_eid, {})
    assert any((snap.get("fields") or {}).get("notes") == "发送前A" for snap in bases.values())

    # Next sync picks up "发送中B" automatically / on manual sync
    assert member_api.hub_sync_batch(member_batch)["ok"]
    _wait_batch_settled(member_api, member_batch)
    rid = member_api.entries.get(m_eid)["hub"]["remote_entry_id"]
    remote_now = mem_client.get_space_entry(space_id, rid)
    assert remote_now["payload"]["fields"]["notes"]["current"] == "发送中B"

    # Case 3: Disconnect while in-flight request is running -> does not overwrite new disconnected state
    member_api.entries.update_field(m_eid, "notes", "断开前修改", env["member_a_pid"])
    probe_entered = threading.Event()
    probe_release = threading.Event()
    orig_probe = mem_client.sync_state

    def slow_probe(sid):
        probe_entered.set()
        assert probe_release.wait(timeout=5)
        return orig_probe(sid)

    with monkeypatch.context() as patch:
        patch.setattr(mem_client, "sync_state", slow_probe)
        assert member_api.hub_sync_batch(member_batch)["ok"]
        assert probe_entered.wait(timeout=5)
        assert member_api.hub_disconnect()["ok"]
        probe_release.set()
        time.sleep(0.1)

    assert member_api.hub_settings()["data"]["connected"] is False
    assert member_api.get_batch(member_batch)["data"]["shared_batch"]["state"] == "paused"

    # Case 4: Closing db stops worker thread promptly
    worker = mgr_admin._worker
    admin_api.db.close()
    assert worker is None or not worker.is_alive()


def test_local_delete_does_not_resurrect_until_remote_updates_or_manual_pull(env):
    base = env["base_url"]
    admin_api: Api = env["admin_api"]
    member_api: Api = env["member_a_api"]
    tmp = env["tmp_path"]

    assert admin_api.hub_connect_token(base, env["admin_token"])["ok"]
    inv = admin_api.hub_create_invite("member", 5, 30)["data"]["invite"]["code"]
    assert member_api.hub_join(base, inv, "张队员", "李汇总")["ok"]

    admin_batch = admin_api.create_batch("删除防复活测试")["data"]["id"]
    space_id = admin_api.hub_link_batch(batch_id=admin_batch, space_id="", auto_sync=True)["data"]["shared"]["space_id"]
    member_batch = member_api.hub_link_batch(batch_id="", space_id=space_id, auto_sync=False)["data"]["batch_id"]

    m_eid = member_api.entries.create(
        env["member_a_pid"],
        parsed=ParsedInvoice(invoice_no="26112000000099001122", seller="电子件商行", total="66.00"),
    )
    member_api.attachments.add(m_eid, _write_file(tmp, "del_inv.pdf", b"%PDF-1.4 del"), "invoice_pdf")
    member_api.add_entries_to_batch(member_batch, [m_eid])
    assert member_api.hub_sync_batch(member_batch)["ok"]
    _wait_batch_settled(member_api, member_batch)

    # Admin auto-sync pulls the entry
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        admin_api.hub_sync_tick()
        if admin_api.entries.list(batch_id=admin_batch):
            break
        time.sleep(0.03)
    admin_entries = admin_api.entries.list(batch_id=admin_batch)
    assert len(admin_entries) == 1
    a_eid = admin_entries[0]["id"]

    # Admin deletes the entry locally
    assert admin_api.delete_entry(a_eid)["ok"]
    assert admin_api.entries.list(batch_id=admin_batch) == []

    # Multiple auto-sync ticks AND an explicit manual sync must NOT resurrect the deleted entry
    for _ in range(10):
        admin_api.hub_sync_tick()
        time.sleep(0.03)
    assert admin_api.hub_sync_batch(admin_batch)["ok"]
    _wait_batch_settled(admin_api, admin_batch)
    assert admin_api.entries.list(batch_id=admin_batch) == []

    # When member updates the remote entry to a newer revision, admin pulls the updated version
    member_api.entries.update_field(m_eid, "notes", "补充说明", env["member_a_pid"])
    assert member_api.hub_sync_batch(member_batch)["ok"]
    _wait_batch_settled(member_api, member_batch)

    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        admin_api.hub_sync_tick()
        if admin_api.entries.list(batch_id=admin_batch):
            break
        time.sleep(0.03)
    restored = admin_api.entries.list(batch_id=admin_batch)
    assert len(restored) == 1
    assert admin_api.entries.get(restored[0]["id"])["fields"]["notes"]["current"] == "补充说明"


def test_pull_merge_does_not_swallow_unpushed_local_edits(env):
    base = env["base_url"]
    admin_api: Api = env["admin_api"]
    member_api: Api = env["member_a_api"]
    tmp = env["tmp_path"]

    assert admin_api.hub_connect_token(base, env["admin_token"])["ok"]
    inv = admin_api.hub_create_invite("member", 5, 30)["data"]["invite"]["code"]
    assert member_api.hub_join(base, inv, "张队员", "李汇总")["ok"]

    admin_batch = admin_api.create_batch("合并回推测试")["data"]["id"]
    space_id = admin_api.hub_link_batch(batch_id=admin_batch, space_id="", auto_sync=False)["data"]["shared"]["space_id"]
    member_batch = member_api.hub_link_batch(batch_id="", space_id=space_id, auto_sync=False)["data"]["batch_id"]

    m_eid = member_api.entries.create(
        env["member_a_pid"],
        parsed=ParsedInvoice(invoice_no="26112000000088112233", seller="五金店", total="120.00"),
    )
    member_api.attachments.add(m_eid, _write_file(tmp, "merge_inv.pdf", b"%PDF-1.4 merge"), "invoice_pdf")
    member_api.add_entries_to_batch(member_batch, [m_eid])
    assert member_api.hub_sync_batch(member_batch)["ok"]
    _wait_batch_settled(member_api, member_batch)

    assert admin_api.hub_sync_batch(admin_batch)["ok"]
    _wait_batch_settled(admin_api, admin_batch)
    a_eid = admin_api.entries.list(batch_id=admin_batch)[0]["id"]

    # Path A (auto/batch sync): Admin edits notes locally (unpushed), while member adds payment screenshot & pushes
    admin_api.entries.update_field(a_eid, "notes", "本地未推备注A", env["admin_pid"])
    member_api.attachments.add(m_eid, _write_file(tmp, "pay_a.png", b"\x89PNG\r\n\x1a\npayA"), "payment_screenshot")
    assert member_api.hub_sync_batch(member_batch)["ok"]
    _wait_batch_settled(member_api, member_batch)

    # Admin syncs -> pulls payment screenshot AND must immediately push preserved local notes
    assert admin_api.hub_sync_batch(admin_batch)["ok"]
    info_a, _ = _wait_batch_settled(admin_api, admin_batch)
    assert info_a["state"] == "synced"
    rid = admin_api.entries.get(a_eid)["hub"]["remote_entry_id"]
    client = HubClient(base, env["admin_token"])
    try:
        remote_a = client.get_space_entry(space_id, rid)
        assert remote_a["payload"]["fields"]["notes"]["current"] == "本地未推备注A"
        assert any(a["type"] == "payment_screenshot" for a in remote_a["payload"]["attachments"])
    finally:
        client.close()

    # Path B (manual hub_apply_pull): Member pulls rev 3 first, then admin edits notes while member edits actual_item_name
    assert member_api.hub_sync_batch(member_batch)["ok"]
    _wait_batch_settled(member_api, member_batch)

    admin_api.entries.update_field(a_eid, "notes", "手动拉取后保留的备注B", env["admin_pid"])
    member_api.entries.update_field(m_eid, "actual_item_name", "M6不锈钢螺栓", env["member_a_pid"])
    assert member_api.hub_sync_batch(member_batch)["ok"]
    _wait_batch_settled(member_api, member_batch)

    assert admin_api.hub_preview_pull(space_id)["ok"]
    assert admin_api.hub_apply_pull(space_id, env["admin_pid"], {"batch_id": admin_batch})["ok"]
    # Subsequent sync must push admin's preserved notes instead of swallowing it as already synced
    assert admin_api.hub_sync_batch(admin_batch)["ok"]
    info_b, _ = _wait_batch_settled(admin_api, admin_batch)
    assert info_b["state"] == "synced"
    client = HubClient(base, env["admin_token"])
    try:
        remote_b = client.get_space_entry(space_id, rid)
        assert remote_b["payload"]["fields"]["notes"]["current"] == "手动拉取后保留的备注B"
        assert remote_b["payload"]["fields"]["actual_item_name"]["current"] == "M6不锈钢螺栓"
    finally:
        client.close()


def test_relink_batch_to_another_space_separates_other_space_and_allows_detach(env):
    base = env["base_url"]
    admin_api: Api = env["admin_api"]
    tmp = env["tmp_path"]

    assert admin_api.hub_connect_token(base, env["admin_token"])["ok"]
    local_batch = admin_api.create_batch("批次一")["data"]["id"]
    space_1 = admin_api.hub_link_batch(batch_id=local_batch, space_id="", auto_sync=False)["data"]["shared"]["space_id"]
    space_2 = admin_api.hub_create_space("批次二")["data"]["space"]["id"]

    eid = admin_api.entries.create(
        env["admin_pid"],
        parsed=ParsedInvoice(invoice_no="26112000000077665544", seller="测试供应商", total="50.00"),
    )
    admin_api.attachments.add(eid, _write_file(tmp, "relink.pdf", b"%PDF-1.4 relink"), "invoice_pdf")
    admin_api.add_entries_to_batch(local_batch, [eid])
    assert admin_api.hub_sync_batch(local_batch)["ok"]
    _wait_batch_settled(admin_api, local_batch)

    # Unlink from space_1 and relink to space_2
    assert admin_api.hub_unlink_batch(local_batch)["ok"]
    relink = admin_api.hub_link_batch(batch_id=local_batch, space_id=space_2, auto_sync=False)
    assert relink["ok"]
    assert relink["data"]["shared"]["other_space_count"] == 1
    assert relink["data"]["shared"]["conflict_count"] == 0

    assert admin_api.hub_sync_batch(local_batch)["ok"]
    info, _ = _wait_batch_settled(admin_api, local_batch)
    assert info["state"] == "partial"
    assert info["conflict_count"] == 0
    assert info["other_space_count"] == 1
    assert "批次一" in info["message"]
    assert "锁定或冲突" not in info["message"]

    # Withdraw from space_1 first (or detach directly) then detach old link -> uploads cleanly to space_2
    assert admin_api.hub_withdraw_entry(eid)["ok"]
    detach = admin_api.hub_detach_other_space_entries(local_batch)
    assert detach["ok"]
    assert detach["data"]["detached"] == 1
    assert detach["data"]["shared"]["other_space_count"] == 0

    assert admin_api.hub_sync_batch(local_batch)["ok"]
    info2, _ = _wait_batch_settled(admin_api, local_batch)
    assert info2["state"] == "synced"
    assert info2["other_space_count"] == 0
    assert admin_api.entries.get(eid)["hub"]["space_id"] == space_2


def test_suppressed_conflict_does_not_flicker_syncing_and_exposes_report(env):
    base = env["base_url"]
    admin_api: Api = env["admin_api"]
    member_api: Api = env["member_a_api"]

    assert admin_api.hub_connect_token(base, env["admin_token"])["ok"]
    inv = admin_api.hub_create_invite("member", 5, 30)["data"]["invite"]["code"]
    assert member_api.hub_join(base, inv, "张队员", "李汇总")["ok"]

    admin_batch = admin_api.create_batch("稳态测试")["data"]["id"]
    space_id = admin_api.hub_link_batch(batch_id=admin_batch, space_id="", auto_sync=False)["data"]["shared"]["space_id"]
    member_batch = member_api.hub_link_batch(batch_id="", space_id=space_id, auto_sync=False)["data"]["batch_id"]

    m_eid = member_api.entries.create(
        env["member_a_pid"],
        parsed=ParsedInvoice(invoice_no="26112000000099112233", seller="稳态器件店", total="66.00"),
    )
    member_api.add_entries_to_batch(member_batch, [m_eid])
    assert member_api.hub_sync_batch(member_batch)["ok"]
    _wait_batch_settled(member_api, member_batch)

    assert admin_api.hub_sync_batch(admin_batch)["ok"]
    _wait_batch_settled(admin_api, admin_batch)
    a_eid = admin_api.get_batch(admin_batch)["data"]["entry_ids"][0]
    assert admin_api.hub_review_entry(a_eid, "locked", "已锁定", push_local=False)["ok"]

    # Member pulls locked state then edits locally -> settles into 'conflict'
    assert member_api.hub_sync_batch(member_batch)["ok"]
    _wait_batch_settled(member_api, member_batch)
    member_api.entries.update_field(m_eid, "notes", "锁后修改", env["member_a_pid"])
    assert member_api.hub_set_batch_auto_sync(member_batch, True)["ok"]
    info_c, _ = _wait_sync_state(member_api, member_batch, {"conflict"})
    assert info_c["state"] == "conflict"

    # Subsequent auto-sync ticks while nothing changes must NEVER flicker back to 'syncing'
    observed_states = []
    for _ in range(12):
        time.sleep(0.025)
        tick_info = member_api.hub_sync_tick()["data"]["batches"][member_batch]
        observed_states.append(tick_info["state"])
    assert observed_states and all(s == "conflict" for s in observed_states), observed_states

    # Per-entry report explains the exact entry and reason
    rep_res = member_api.hub_batch_sync_report(member_batch)
    assert rep_res["ok"]
    items = rep_res["data"]["report"]["items"]
    assert len(items) == 1
    assert items[0]["entry_id"] == m_eid
    assert items[0]["seller"] == "稳态器件店"
    assert items[0]["status"] == "locked"
    assert "锁定" in items[0]["message"]


def test_stop_and_resume_batch_sync_preserves_progress(env, monkeypatch):
    base = env["base_url"]
    admin_api: Api = env["admin_api"]
    tmp = env["tmp_path"]

    assert admin_api.hub_connect_token(base, env["admin_token"])["ok"]
    local_batch = admin_api.create_batch("断点续传批次")["data"]["id"]
    space_id = admin_api.hub_link_batch(batch_id=local_batch, space_id="", auto_sync=True)["data"]["shared"]["space_id"]
    _wait_batch_settled(admin_api, local_batch)

    eids = []
    for idx in range(3):
        eid = admin_api.entries.create(
            env["admin_pid"],
            parsed=ParsedInvoice(invoice_no=f"2611200000004400000{idx}", seller=f"供应商{idx}", total="10.00"),
        )
        admin_api.attachments.add(eid, _write_file(tmp, f"stop_{idx}.pdf", f"%PDF-1.4 {idx}".encode()), "invoice_pdf")
        eids.append(eid)
    admin_api.add_entries_to_batch(local_batch, eids)

    mgr = admin_api._hub_sync_manager()
    client = mgr._get_worker_client(base, env["admin_token"])
    orig_push = client.push_entry
    first_pushed = threading.Event()

    def push_and_stop(sid, body):
        res = orig_push(sid, body)
        first_pushed.set()
        time.sleep(0.05)
        return res

    with monkeypatch.context() as patch:
        patch.setattr(client, "push_entry", push_and_stop)
        assert admin_api.hub_sync_batch(local_batch)["ok"]
        assert first_pushed.wait(timeout=5.0)
        stop_res = admin_api.hub_stop_batch_sync(local_batch)
        assert stop_res["ok"]

    info_stopped, _ = _wait_batch_settled(admin_api, local_batch)
    assert info_stopped["state"] == "stopped"
    assert info_stopped["can_resume"] is True

    # Auto-sync tick must stay stopped and NOT auto-restart until user resumes
    time.sleep(0.08)
    tick_after = admin_api.hub_sync_tick()["data"]["batches"][local_batch]
    assert tick_after["state"] == "stopped"

    rep = admin_api.hub_batch_sync_report(local_batch)["data"]["report"]
    statuses = [it["status"] for it in rep["items"]]
    assert "pushed" in statuses
    assert "stopped" in statuses

    # Resume with hub_sync_batch -> pushes remaining entries and settles to 'synced'
    assert admin_api.hub_sync_batch(local_batch)["ok"]
    info_done, _ = _wait_batch_settled(admin_api, local_batch)
    assert info_done["state"] == "synced"
    assert all(admin_api.entries.get(eid)["hub"]["remote_entry_id"] for eid in eids)

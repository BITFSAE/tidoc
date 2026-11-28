"""Coverage for shared-batch scheme binding, rebind recovery, and the BITFSAE recovery prompt.

All names, invoices and material files in this module are fictional fixtures.
"""

from __future__ import annotations

import threading
import time
from copy import deepcopy
from pathlib import Path

import pytest

from tidoc.api import Api
from tidoc.api_shared import APP_LAST_SEEN_VERSION_KEY
from tidoc.engine.models import ParsedInvoice
from tidoc.services import bindle as bindle_service
from tidoc.services.hub_batch_sync import _resolve_pull_scheme_id
from tidoc_hub.server import HubConfig, create_server

BITFSAE = "org.bitfsae.reimbursement"
GENERIC = "org.tidoc.generic"
LEGACY_NOTICE = "来自其他报账方案的材料；本机尚未绑定该方案规则。"


# --------------------------------------------------------------------------- helpers


def _scheme(api: Api, package_id: str) -> dict:
    return next(
        scheme
        for scheme in api.adapters.list_schemes(include_disabled=True)
        if scheme["package_id"] == package_id
    )


def _builtin_bitfsae(api: Api) -> dict:
    legacy_id = api.adapters._meta("adapter.legacy.scheme_id", "") or ""
    return next(
        scheme
        for scheme in api.adapters.list_schemes(include_disabled=True)
        if scheme["package_id"] == BITFSAE and scheme["id"] != legacy_id
    )


def _setup_bitfsae(api: Api) -> dict:
    scheme = _scheme(api, BITFSAE)
    api.adapters.complete_adapter_setup(scheme["id"])
    return api.adapters.get_scheme(scheme["id"])


def _setup_generic(api: Api) -> dict:
    scheme = _scheme(api, GENERIC)
    api.adapters.complete_adapter_setup(scheme["id"])
    return api.adapters.get_scheme(scheme["id"])


def _profile(api: Api, name: str = "虚构报账人") -> dict:
    return api.profiles.create(name, "虚构审核人")


def _invoice(number: str, **changes) -> ParsedInvoice:
    values = {
        "invoice_no": number,
        "invoice_date": "2026-11-20",
        "seller": "虚构测试商店",
        "total": "88.00",
        "buyer_name": "北京理工大学",
        "buyer_tax_id": "12100000400009127B",
    }
    values.update(changes)
    return ParsedInvoice(**values)


def _material(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _add_material(api: Api, tmp_path: Path, entry_id: str, name: str, att_type: str,
                  role_id: str | None = None) -> dict:
    suffixes = {
        "invoice_pdf": ".pdf",
        "payment_screenshot": ".png",
        "inspection_pdf": ".pdf",
        "other": ".png",
    }
    tag = f"{att_type}:{name}".encode("utf-8")
    header = b"%PDF-1.4 fictional " if att_type in ("invoice_pdf", "inspection_pdf") else b"\x89PNG\r\n\x1a\nfictional "
    path = _material(tmp_path, name + suffixes.get(att_type, ""), header + tag)
    return api.attachments.add(entry_id, path, att_type, role_id=role_id)


def _bind_attachment_revision(api: Api, entry_id: str, revision_id: str) -> None:
    api.db.conn.execute(
        "UPDATE attachments SET role_definition_revision_id=? WHERE entry_id=?",
        (revision_id, entry_id),
    )
    api.db.conn.commit()


def _set_check(api: Api, entry_id: str, status: str, message: str) -> None:
    api.db.conn.execute(
        "UPDATE entries SET check_status=?, check_message=?, check_reviewed='[]' WHERE id=?",
        (status, message, entry_id),
    )
    api.db.conn.commit()


def _set_status(api: Api, entry_id: str, status: str) -> None:
    api.db.conn.execute("UPDATE entries SET status=? WHERE id=?", (status, entry_id))
    api.db.conn.commit()


def _legacy_scheme(api: Api, bit: dict, *, inspection_required: bool,
                   is_default: bool, name: str = "原有报账方案") -> tuple[dict, str]:
    definition = deepcopy(bit["definition"])
    if inspection_required:
        role = next(role for role in definition["materials"] if role["id"] == "inspection_pdf")
        role["min_count"] = 1
    record = api.adapters.packages.revision_record(bit["current_revision_id"])
    revision = api.adapters.packages.store_revision(definition, record["content_hash"])
    scheme = api.adapters.packages.create_scheme(name, revision, is_default=is_default)
    return scheme, revision


def _export_bundle(tmp_path: Path, *, with_extensions: bool = False,
                   check_status: str = "pass", check_message: str = "",
                   status: str = "complete", number: str = "FICT-BUNDLE-1") -> tuple[Path, str]:
    source = Api(tmp_path / "bundle-source")
    try:
        _setup_bitfsae(source)
        person = _profile(source)
        entry_id = source.entries.create(person["id"], parsed=_invoice(number))
        _add_material(source, tmp_path, entry_id, "bundle-invoice", "invoice_pdf")
        _add_material(source, tmp_path, entry_id, "bundle-payment", "payment_screenshot")
        if with_extensions:
            source.adapters.save_extension_values(
                "entry", entry_id,
                {"purchase_purpose": "虚构采购用途", "storage_location": "虚构存放地点"},
            )
        source.entries.recompute_status(entry_id)
        _set_status(source, entry_id, status)
        _set_check(source, entry_id, check_status, check_message)
        path = bindle_service.export_bindle(
            source.entries, source.attachments, [entry_id],
            tmp_path / f"{number}.tidoc", {person["id"]: person},
            adapter_service=source.adapters,
        )
        return path, person["name"]
    finally:
        source.db.close()


def _legacy_default_api(tmp_path: Path) -> tuple[Api, dict, dict]:
    api = Api(tmp_path / "data")
    bit = _scheme(api, BITFSAE)
    legacy, _revision = _legacy_scheme(api, bit, inspection_required=True, is_default=True)
    api.adapters._set_meta("adapter.legacy.scheme_id", legacy["id"])
    api.adapters.complete_adapter_setup()
    return api, legacy, _builtin_bitfsae(api)


# --------------------------------------------------------------------------- hub helpers


class _HubServer:
    def __init__(self, tmp_path: Path):
        config = HubConfig(
            host="127.0.0.1",
            port=0,
            prefix="/tidoc-hub",
            data_dir=str(tmp_path / "hub-server"),
            storage="local",
        )
        self.server = create_server(config)
        port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}/tidoc-hub"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.close_resources()
        self.thread.join(timeout=5)


def _wait_settled(api: Api, batch_id: str, *, timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = api.hub_sync_tick()
        assert result["ok"], result
        info = result["data"]["batches"].get(batch_id)
        if info and info["state"] != "syncing":
            return info
        time.sleep(0.03)
    raise AssertionError(f"batch {batch_id} stayed syncing")


@pytest.fixture
def hub_env(tmp_path: Path):
    hub = _HubServer(tmp_path)
    _, admin_token = hub.server.db.create_user("虚构汇总人", role="admin", reviewer="虚构审核人")
    admin = Api(tmp_path / "admin-data")
    member = Api(tmp_path / "member-data")
    _setup_bitfsae(admin)
    _setup_bitfsae(member)
    admin_pid = _profile(admin, "虚构汇总人")["id"]
    member_pid = _profile(member, "虚构队员")["id"]
    try:
        yield {
            "hub": hub,
            "base_url": hub.base_url,
            "admin_token": admin_token,
            "admin": admin,
            "member": member,
            "admin_pid": admin_pid,
            "member_pid": member_pid,
            "tmp_path": tmp_path,
        }
    finally:
        admin.db.close()
        member.db.close()
        hub.close()


def _linked_batches(env: dict, *, admin_auto_sync: bool = False) -> tuple[str, str, str]:
    admin: Api = env["admin"]
    member: Api = env["member"]
    assert admin.hub_connect_token(env["base_url"], env["admin_token"])["ok"]
    invite = admin.hub_create_invite("member", 5, 30)["data"]["invite"]["code"]
    assert member.hub_join(env["base_url"], invite, "虚构队员", "虚构汇总人")["ok"]
    admin_batch = admin.create_batch("虚构共享批次")["data"]["id"]
    space_id = admin.hub_link_batch(
        batch_id=admin_batch, space_id="", auto_sync=admin_auto_sync
    )["data"]["shared"]["space_id"]
    member_batch = member.hub_link_batch(
        batch_id="", space_id=space_id, auto_sync=False
    )["data"]["batch_id"]
    return admin_batch, member_batch, space_id


def _push_entry(env: dict, member_batch: str, number: str) -> str:
    member: Api = env["member"]
    entry_id = member.entries.create(env["member_pid"], parsed=_invoice(number))
    _add_material(member, env["tmp_path"], entry_id, f"{number}-invoice", "invoice_pdf")
    _add_material(member, env["tmp_path"], entry_id, f"{number}-payment", "payment_screenshot")
    _add_material(member, env["tmp_path"], entry_id, f"{number}-inspection", "inspection_pdf")
    member.add_entries_to_batch(member_batch, [entry_id])
    assert member.hub_sync_batch(member_batch)["ok"]
    _wait_settled(member, member_batch)
    return entry_id


# --------------------------------------------------------------------------- pull scheme binding


def test_resolve_pull_scheme_id_prefers_batch_scheme_then_default(tmp_path, monkeypatch):
    api = Api(tmp_path / "data")
    bit = _setup_bitfsae(api)
    generic = _scheme(api, GENERIC)
    try:
        assert _resolve_pull_scheme_id(api.adapters, bit["id"]) == bit["id"]
        assert _resolve_pull_scheme_id(api.adapters, f"  {generic['id']}  ") == generic["id"]
        assert _resolve_pull_scheme_id(api.adapters, "") == bit["id"]
        assert _resolve_pull_scheme_id(api.adapters, "missing-scheme") == bit["id"]

        # A disabled batch scheme must not be used; the local default wins.
        api.adapters.disable_scheme(generic["id"])
        assert _resolve_pull_scheme_id(api.adapters, generic["id"]) == bit["id"]

        def broken_default():
            raise ValueError("no default")

        monkeypatch.setattr(api.adapters, "default_binding", broken_default)
        assert _resolve_pull_scheme_id(api.adapters, generic["id"]) == ""
    finally:
        api.db.close()


def test_shared_batch_pull_binds_entries_to_batch_scheme_with_live_materials(hub_env):
    admin: Api = hub_env["admin"]
    bit = _scheme(admin, BITFSAE)
    admin_batch, member_batch, _space = _linked_batches(hub_env)
    member_entry = _push_entry(hub_env, member_batch, "FICT-HUB-1")

    assert admin.hub_sync_batch(admin_batch)["ok"]
    info = _wait_settled(admin, admin_batch)
    assert info["state"] == "synced"
    entry_ids = admin.get_batch(admin_batch)["data"]["entry_ids"]
    assert len(entry_ids) == 1

    entry = admin.entries.get(entry_ids[0])
    assert entry["invoice_no"] == "FICT-HUB-1"
    assert entry["scheme_id"] == bit["id"]
    assert entry["scheme_revision_id"] == bit["current_revision_id"]
    assert entry["title_profile_id"] == "bit_university"
    assert entry["completeness"]["ready"] is True
    assert entry["completeness"]["status"] == "complete"
    codes = {diagnostic["code"] for diagnostic in entry["diagnostics"]}
    assert "EXTERNAL_SCHEME_PENDING" not in codes
    assert "来自其他报账方案的材料" not in entry["check_message"]
    assert "本机尚未绑定该方案规则" not in entry["check_message"]
    assert all(
        attachment["role_definition_revision_id"] == bit["current_revision_id"]
        for attachment in entry["attachments"]
    )

    listed = next(row for row in admin.entries.list(batch_id=admin_batch))
    assert listed["role_counts"]["invoice"] == 1
    assert listed["role_counts"]["payment_screenshot"] == 1
    assert listed["role_counts"]["inspection_pdf"] == 1

    # The member's local entry keeps its own local binding.
    member_entry_data = hub_env["member"].entries.get(member_entry)
    assert member_entry_data is not None
    assert member_entry_data["scheme_id"] is not None


def test_shared_batch_pull_falls_back_to_default_when_batch_scheme_disabled(hub_env):
    admin: Api = hub_env["admin"]
    bit = _scheme(admin, BITFSAE)
    generic = _scheme(admin, GENERIC)
    admin_batch, member_batch, _space = _linked_batches(hub_env)
    _push_entry(hub_env, member_batch, "FICT-HUB-2")

    admin.batches.set_default_binding(admin_batch, generic["id"])
    admin.adapters.disable_scheme(generic["id"])

    assert admin.hub_sync_batch(admin_batch)["ok"]
    _wait_settled(admin, admin_batch)
    entry_ids = admin.get_batch(admin_batch)["data"]["entry_ids"]
    assert len(entry_ids) == 1
    entry = admin.entries.get(entry_ids[0])
    assert entry["scheme_id"] == bit["id"]
    assert entry["scheme_revision_id"] == bit["current_revision_id"]
    assert "本机尚未绑定该方案规则" not in entry["check_message"]


# --------------------------------------------------------------------------- bindle import binding


def test_unbound_external_import_keeps_pending_diagnostic_out_of_check_message(tmp_path):
    path, person_name = _export_bundle(
        tmp_path, check_status="pass", check_message="", status="complete", number="FICT-UNBOUND"
    )
    target = Api(tmp_path / "target")
    try:
        _setup_bitfsae(target)
        claimant = _profile(target, person_name)
        result = target.import_bindle(path, claimant["id"])
        assert result["ok"], result
        entry = target.entries.get(result["data"]["entry_ids"][0])

        assert entry["scheme_id"] is None
        assert entry["scheme_revision_id"] is None
        assert entry["status"] == "partial"
        assert entry["check_status"] == "pass"
        assert "来自其他报账方案的材料" not in entry["check_message"]
        assert "本机尚未绑定该方案规则" not in entry["check_message"]
        pending = [d for d in entry["diagnostics"] if d["code"] == "EXTERNAL_SCHEME_PENDING"]
        assert pending and pending[0]["severity"] == "required"
        assert entry["adapter_sources"][0]["definition"]["manifest"]["package_id"] == BITFSAE
    finally:
        target.db.close()


def test_bound_external_import_strips_legacy_notice_and_matches_title(tmp_path):
    path, person_name = _export_bundle(
        tmp_path, check_status="warning", check_message=LEGACY_NOTICE,
        status="partial", number="FICT-BOUND",
    )
    target = Api(tmp_path / "target")
    try:
        bit = _setup_bitfsae(target)
        claimant = _profile(target, person_name)
        result = target.import_bindle(path, claimant["id"], options={"scheme_id": bit["id"]})
        assert result["ok"], result
        entry = target.entries.get(result["data"]["entry_ids"][0])

        assert entry["scheme_id"] == bit["id"]
        assert entry["scheme_revision_id"] == bit["current_revision_id"]
        assert entry["title_profile_id"] == "bit_university"
        assert entry["check_status"] == "pass"
        assert entry["check_message"] == ""
        assert "来自其他报账方案的材料" not in entry["check_message"]
        assert entry["completeness"]["ready"] is True
        assert all(
            attachment["role_definition_revision_id"] == bit["current_revision_id"]
            for attachment in entry["attachments"]
        )
    finally:
        target.db.close()


# --------------------------------------------------------------------------- rebind


def test_rebind_from_generic_keeps_builtin_materials_live_and_cleans_checks(tmp_path):
    api = Api(tmp_path / "data")
    try:
        generic = _setup_generic(api)
        bit = _scheme(api, BITFSAE)
        person = _profile(api)
        entry_id = api.entries.create(
            person["id"], parsed=_invoice("FICT-REBIND-1"),
            scheme_id=generic["id"], scheme_revision_id=generic["current_revision_id"],
        )
        _add_material(api, tmp_path, entry_id, "rebind-invoice", "invoice_pdf")
        _add_material(api, tmp_path, entry_id, "rebind-payment", "payment_screenshot")
        _bind_attachment_revision(api, entry_id, generic["current_revision_id"])
        _set_check(
            api, entry_id, "warning",
            f"{LEGACY_NOTICE}；购买方抬头「北京理工大学」不在已配置的抬头内。",
        )

        preview = api.adapters.preview_rebind([entry_id], bit["id"])
        assert preview["ok"] is True
        mapped = preview["entries"][0]
        assert mapped["status"] == "complete"
        assert mapped["title_profile_id"] == "bit_university"
        assert not any(
            diagnostic.get("severity") == "required" for diagnostic in mapped["diagnostics"]
        )
        assert all(
            attachment["role_definition_revision_id"] is None
            for attachment in mapped["attachments"]
        )

        result = api.adapters.apply_rebind(preview["preview_id"])
        assert result["changed"] == 1
        entry = api.entries.get(entry_id)
        assert entry["scheme_id"] == bit["id"]
        assert entry["scheme_revision_id"] == bit["current_revision_id"]
        assert entry["title_profile_id"] == "bit_university"
        assert entry["check_status"] == "pass"
        assert entry["check_message"] == ""
        assert entry["completeness"]["ready"] is True
        assert entry["completeness"]["status"] == "complete"
        assert all(
            attachment["role_definition_revision_id"] is None
            for attachment in entry["attachments"]
        )
        listed = next(row for row in api.entries.list() if row["id"] == entry_id)
        assert listed["role_counts"]["invoice"] == 1
        assert listed["role_counts"]["payment_screenshot"] == 1
    finally:
        api.db.close()


def test_rebind_from_legacy_scheme_maps_inspection_materials(tmp_path):
    api = Api(tmp_path / "data")
    try:
        bit = _setup_bitfsae(api)
        legacy, legacy_revision = _legacy_scheme(
            api, bit, inspection_required=True, is_default=False
        )
        person = _profile(api)
        entry_id = api.entries.create(
            person["id"], parsed=_invoice("FICT-REBIND-2"),
            scheme_id=legacy["id"], scheme_revision_id=legacy_revision,
        )
        _add_material(api, tmp_path, entry_id, "legacy-invoice", "invoice_pdf")
        _add_material(api, tmp_path, entry_id, "legacy-payment", "payment_screenshot")
        _add_material(api, tmp_path, entry_id, "legacy-inspection", "inspection_pdf")
        _bind_attachment_revision(api, entry_id, legacy_revision)
        _set_check(api, entry_id, "warning", LEGACY_NOTICE)

        preview = api.adapters.preview_rebind([entry_id], bit["id"])
        assert preview["ok"] is True
        mapped = preview["entries"][0]
        inspection = next(a for a in mapped["attachments"] if a["type"] == "inspection_pdf")
        assert inspection["role_id"] == "inspection_pdf"
        assert inspection["role_definition_revision_id"] is None
        assert all(a["role_definition_revision_id"] is None for a in mapped["attachments"])

        api.adapters.apply_rebind(preview["preview_id"])
        entry = api.entries.get(entry_id)
        assert entry["scheme_id"] == bit["id"]
        assert entry["scheme_revision_id"] == bit["current_revision_id"]
        assert "本机尚未绑定该方案规则" not in entry["check_message"]
        assert "来自其他报账方案的材料" not in entry["check_message"]
        assert all(
            attachment["role_definition_revision_id"] is None
            for attachment in entry["attachments"]
        )
        assert entry["completeness"]["ready"] is True
    finally:
        api.db.close()


def test_rebind_unbound_entry_merges_source_extension_values(tmp_path):
    path, person_name = _export_bundle(
        tmp_path, with_extensions=True, check_status="warning",
        check_message=LEGACY_NOTICE, status="partial", number="FICT-REBIND-3",
    )
    api = Api(tmp_path / "data")
    try:
        bit = _setup_bitfsae(api)
        claimant = _profile(api, person_name)
        imported = api.import_bindle(path, claimant["id"])
        assert imported["ok"], imported
        entry_id = imported["data"]["entry_ids"][0]
        unbound = api.entries.get(entry_id)
        assert unbound["scheme_id"] is None
        assert unbound["check_status"] == "warning"
        assert "本机尚未绑定该方案规则" in unbound["check_message"]

        preview = api.adapters.preview_rebind([entry_id], bit["id"])
        mapped = preview["entries"][0]
        assert mapped["title_profile_id"] == "bit_university"
        assert all(a["role_definition_revision_id"] is None for a in mapped["attachments"])
        api.adapters.apply_rebind(preview["preview_id"])

        entry = api.entries.get(entry_id)
        assert entry["scheme_id"] == bit["id"]
        assert entry["scheme_revision_id"] == bit["current_revision_id"]
        assert entry["check_status"] == "pass"
        assert entry["check_message"] == ""
        assert entry["extension_values"]["purchase_purpose"] == "虚构采购用途"
        assert entry["extension_values"]["storage_location"] == "虚构存放地点"
        assert entry["completeness"]["ready"] is True
        assert all(a["role_definition_revision_id"] is None for a in entry["attachments"])
    finally:
        api.db.close()


def test_rebind_preserves_other_warnings_and_blocked_status(tmp_path):
    api = Api(tmp_path / "data")
    try:
        generic = _setup_generic(api)
        bit = _scheme(api, BITFSAE)
        person = _profile(api)
        warning_entry = api.entries.create(
            person["id"], parsed=_invoice("FICT-REBIND-WARN"),
            scheme_id=generic["id"], scheme_revision_id=generic["current_revision_id"],
        )
        _add_material(api, tmp_path, warning_entry, "warn-invoice", "invoice_pdf")
        _add_material(api, tmp_path, warning_entry, "warn-payment", "payment_screenshot")
        _bind_attachment_revision(api, warning_entry, generic["current_revision_id"])
        warning_text = "[付款截图识别] 付款信息待核对。"
        _set_check(
            api, warning_entry, "warning",
            f"来自其他报账方案的材料；本机尚未绑定该方案规则。；{warning_text}",
        )

        blocked_entry = api.entries.create(
            person["id"], parsed=_invoice("FICT-REBIND-BLOCKED"),
            scheme_id=generic["id"], scheme_revision_id=generic["current_revision_id"],
        )
        _add_material(api, tmp_path, blocked_entry, "blocked-invoice", "invoice_pdf")
        _bind_attachment_revision(api, blocked_entry, generic["current_revision_id"])
        blocked_text = "[发票信息] 虚构严重问题。"
        _set_check(
            api, blocked_entry, "blocked",
            f"{LEGACY_NOTICE}；{blocked_text}",
        )

        preview = api.adapters.preview_rebind([warning_entry, blocked_entry], bit["id"])
        assert preview["ok"] is True
        api.adapters.apply_rebind(preview["preview_id"])

        warning_result = api.entries.get(warning_entry)
        assert warning_result["check_status"] == "warning"
        assert warning_result["check_message"] == warning_text
        assert "本机尚未绑定该方案规则" not in warning_result["check_message"]

        blocked_result = api.entries.get(blocked_entry)
        assert blocked_result["check_status"] == "blocked"
        assert blocked_result["check_message"] == blocked_text
        assert "来自其他报账方案的材料" not in blocked_result["check_message"]
    finally:
        api.db.close()


def test_rebind_custom_roles_still_require_explicit_mapping(tmp_path):
    api = Api(tmp_path / "data")
    try:
        generic = _setup_generic(api)
        bit = _scheme(api, BITFSAE)
        custom_id = "custom:org.tidoc.generic:receipt"
        definition = deepcopy(generic["definition"])
        base_role = deepcopy(next(role for role in definition["materials"] if role["id"] == "other"))
        base_role.update(id=custom_id, label="虚构回执", extensions=[".png"], min_count=0)
        definition["materials"].append(base_role)
        if "declared_materials" in definition:
            definition["declared_materials"] = sorted(set(definition["declared_materials"]) | {custom_id})
        record = api.adapters.packages.revision_record(generic["current_revision_id"])
        revision = api.adapters.packages.store_revision(definition, record["content_hash"])
        custom_scheme = api.adapters.packages.create_scheme("虚构自定义材料方案", revision)

        person = _profile(api)
        entry_id = api.entries.create(
            person["id"], parsed=_invoice("FICT-REBIND-CUSTOM"),
            scheme_id=custom_scheme["id"], scheme_revision_id=revision,
        )
        _add_material(api, tmp_path, entry_id, "custom-invoice", "invoice_pdf")
        _add_material(api, tmp_path, entry_id, "custom-receipt", "other", role_id=custom_id)
        _bind_attachment_revision(api, entry_id, revision)

        preview = api.adapters.preview_rebind([entry_id], bit["id"])
        mapped = preview["entries"][0]
        custom = next(a for a in mapped["attachments"] if a["role_id"] == custom_id)
        assert custom["role_definition_revision_id"] == revision
        api.adapters.apply_rebind(preview["preview_id"])

        entry = api.entries.get(entry_id)
        custom_after = next(a for a in entry["attachments"] if a["role_id"] == custom_id)
        assert custom_after["type"] == "other"
        assert custom_after["role_definition_revision_id"] == revision
    finally:
        api.db.close()


# --------------------------------------------------------------------------- recovery prompt


@pytest.mark.parametrize(
    ("version", "in_window"),
    [
        ("0.1.44-beta.4", True),
        ("0.1.44", True),
        ("0.1.45-beta.1", True),
        ("0.1.45", True),
        ("0.1.46-beta.1", False),
        ("0.1.46", False),
    ],
)
def test_recovery_prompt_version_window(tmp_path, version, in_window):
    api, _legacy, _bit = _legacy_default_api(tmp_path)
    try:
        status = api.adapters.bitfsae_scheme_recovery_status(
            current_version=version, upgraded=True
        )
        assert status["needed"] is True
        assert status["in_window"] is in_window
        assert status["should_prompt"] is in_window
    finally:
        api.db.close()


def test_recovery_prompt_requires_upgrade_and_needed_state(tmp_path):
    api, _legacy, _bit = _legacy_default_api(tmp_path)
    try:
        assert api.adapters.bitfsae_scheme_recovery_status(
            current_version="0.1.44", upgraded=False
        )["should_prompt"] is False
        assert api.adapters.bitfsae_scheme_recovery_status(
            current_version="0.1.46", upgraded=True
        )["should_prompt"] is False
    finally:
        api.db.close()

    clean = Api(tmp_path / "clean")
    try:
        bit = _setup_bitfsae(clean)
        legacy, _revision = _legacy_scheme(
            clean, bit, inspection_required=False, is_default=False
        )
        clean.adapters._set_meta("adapter.legacy.scheme_id", legacy["id"])
        status = clean.adapters.bitfsae_scheme_recovery_status(
            current_version="0.1.44", upgraded=True
        )
        assert status["is_default_bitfsae"] is True
        assert status["active_entry_count"] == 0
        assert status["active_batch_count"] == 0
        assert status["needed"] is False
        assert status["should_prompt"] is False
    finally:
        clean.db.close()


def test_recovery_dismiss_suppresses_version_then_reappears_or_stays_off(tmp_path):
    api, _legacy, _bit = _legacy_default_api(tmp_path)
    try:
        api.adapters.dismiss_bitfsae_scheme_recovery(
            permanent=False, current_version="0.1.44"
        )
        assert api.adapters.bitfsae_scheme_recovery_status(
            current_version="0.1.44", upgraded=True
        )["should_prompt"] is False
        assert api.adapters.bitfsae_scheme_recovery_status(
            current_version="0.1.45-beta.1", upgraded=True
        )["should_prompt"] is True

        api.adapters.dismiss_bitfsae_scheme_recovery(
            permanent=True, current_version="0.1.45-beta.1"
        )
        status = api.adapters.bitfsae_scheme_recovery_status(
            current_version="0.1.45", upgraded=True
        )
        assert status["opted_out"] is True
        assert status["should_prompt"] is False
        assert api.adapters.bitfsae_scheme_recovery_status(
            current_version="0.1.44-beta.4", upgraded=True
        )["should_prompt"] is False
    finally:
        api.db.close()


def test_restore_default_bitfsae_scheme_migrates_payees_batches_and_entries(tmp_path):
    api = Api(tmp_path / "data")
    try:
        bit_seed = _scheme(api, BITFSAE)
        generic = _scheme(api, GENERIC)
        legacy, legacy_revision = _legacy_scheme(
            api, bit_seed, inspection_required=True, is_default=True
        )
        api.adapters._set_meta("adapter.legacy.scheme_id", legacy["id"])
        api.adapters.complete_adapter_setup()
        bit = _builtin_bitfsae(api)

        person_a = _profile(api, "虚构报账人甲")
        person_b = _profile(api, "虚构报账人乙")

        active_batch = api.create_batch("虚构活动批次")["data"]["id"]
        archived_batch = api.create_batch("虚构归档批次")["data"]["id"]
        api.batches.set_default_binding(active_batch, legacy["id"])
        api.batches.set_default_binding(archived_batch, legacy["id"])
        api.archive_batch(archived_batch, True)

        def make_entry(pid: str, scheme_id: str | None, revision_id: str | None,
                       number: str) -> str:
            entry_id = api.entries.create(
                pid, parsed=_invoice(number),
                scheme_id=scheme_id, scheme_revision_id=revision_id,
            )
            _add_material(api, tmp_path, entry_id, f"{number}-invoice", "invoice_pdf")
            _add_material(api, tmp_path, entry_id, f"{number}-payment", "payment_screenshot")
            return entry_id

        legacy_entry = make_entry(person_a["id"], legacy["id"], legacy_revision, "FICT-ACTIVE-L")
        unbound_entry = make_entry(person_b["id"], None, None, "FICT-ACTIVE-U")
        archived_legacy = make_entry(person_a["id"], legacy["id"], legacy_revision, "FICT-ARCH-L")
        archived_unbound = make_entry(person_b["id"], None, None, "FICT-ARCH-U")
        # entries.create binds to the current default; make the unbound fixtures truly unbound.
        for entry_id in (unbound_entry, archived_unbound):
            api.db.conn.execute(
                "UPDATE entries SET scheme_id=NULL, scheme_revision_id=NULL, title_profile_id=NULL WHERE id=?",
                (entry_id,),
            )
            api.db.conn.commit()
            _bind_attachment_revision(api, entry_id, generic["current_revision_id"])
        _bind_attachment_revision(api, legacy_entry, legacy_revision)
        _bind_attachment_revision(api, archived_legacy, legacy_revision)
        _set_check(
            api, legacy_entry, "warning",
            "来自其他报账方案的材料；本机尚未绑定该方案规则。",
        )
        _set_check(
            api, unbound_entry, "warning",
            "本机尚未绑定该方案规则。",
        )
        api.add_entries_to_batch(active_batch, [legacy_entry, unbound_entry])
        api.add_entries_to_batch(archived_batch, [archived_legacy, archived_unbound])

        payee_a = api.adapters.save_payee(
            None, {"name": "虚构收款人甲", "bank_name": "虚构银行", "account_number": "0000"}
        )
        payee_b = api.adapters.save_payee(
            None, {"name": "虚构收款人乙", "bank_name": "虚构银行", "account_number": "1111"}
        )
        api.adapters.set_scheme_payee(legacy["id"], payee_a["id"])
        api.adapters.set_payee_mapping(legacy["id"], person_a["id"], payee_a["id"])
        api.adapters.set_payee_mapping(legacy["id"], person_b["id"], payee_a["id"])
        api.adapters.set_payee_mapping(bit["id"], person_b["id"], payee_b["id"])
        api.adapters.payees.set_batch(active_batch, legacy["id"], payee_a["id"])
        api.adapters.payees.set_batch(archived_batch, legacy["id"], payee_a["id"])

        before = api.adapters.bitfsae_scheme_recovery_status(
            current_version="0.1.44", upgraded=True
        )
        assert before["should_prompt"] is True
        assert before["active_entry_count"] == 2
        assert before["active_batch_count"] == 1

        result = api.adapters.restore_default_bitfsae_scheme(current_version="0.1.44")
        assert result["target_scheme_id"] == bit["id"]
        assert result["rebound_entries"] == 2
        assert result["updated_batches"] == 1
        assert result["migrated_payees"] == 3
        assert result["disabled_legacy"] is True

        default = api.adapters.get_scheme()
        assert default["id"] == bit["id"]
        assert api.adapters.get_scheme(legacy["id"])["disabled"] is True
        assert api.adapters.get_scheme(generic["id"])["disabled"] is False

        migrated = api.adapters.get_scheme(bit["id"])
        assert migrated["payee"]["id"] == payee_a["id"]
        assert migrated["profile_payee_mappings"][person_a["id"]] == payee_a["id"]
        assert migrated["profile_payee_mappings"][person_b["id"]] == payee_b["id"]

        for entry_id in (legacy_entry, unbound_entry):
            entry = api.entries.get(entry_id)
            assert entry["scheme_id"] == bit["id"]
            assert entry["scheme_revision_id"] == bit["current_revision_id"]
            assert entry["title_profile_id"] == "bit_university"
            assert entry["check_status"] == "pass"
            assert entry["check_message"] == ""
            assert entry["completeness"]["ready"] is True
            assert all(
                attachment["role_definition_revision_id"] is None
                for attachment in entry["attachments"]
            )

        archived_legacy_entry = api.entries.get(archived_legacy)
        assert archived_legacy_entry["scheme_id"] == legacy["id"]
        assert all(
            attachment["role_definition_revision_id"] == legacy_revision
            for attachment in archived_legacy_entry["attachments"]
        )
        archived_unbound_entry = api.entries.get(archived_unbound)
        assert archived_unbound_entry["scheme_id"] is None
        assert all(
            attachment["role_definition_revision_id"] == generic["current_revision_id"]
            for attachment in archived_unbound_entry["attachments"]
        )

        assert api.get_batch(active_batch)["data"]["default_scheme_id"] == bit["id"]
        assert api.get_batch(archived_batch)["data"]["default_scheme_id"] == legacy["id"]
        active_payee = api.db.conn.execute(
            "SELECT payee_id FROM batch_payee_links WHERE batch_id=? AND scheme_id=?",
            (active_batch, bit["id"]),
        ).fetchone()
        assert active_payee and active_payee["payee_id"] == payee_a["id"]
        archived_payee = api.db.conn.execute(
            "SELECT 1 FROM batch_payee_links WHERE batch_id=? AND scheme_id=?",
            (archived_batch, bit["id"]),
        ).fetchone()
        assert archived_payee is None
    finally:
        api.db.close()


def test_startup_update_state_includes_scheme_recovery(tmp_path):
    api, _legacy, _bit = _legacy_default_api(tmp_path)
    try:
        api.set_app_preference(APP_LAST_SEEN_VERSION_KEY, "0.0.1")
        state = api.startup_update_state()["data"]
        assert state["upgraded"] is True
        recovery = state["scheme_recovery"]
        assert recovery["should_prompt"] is True
        assert recovery["current_version"] == state["current_version"]
        assert recovery["target_scheme_id"] == _bit["id"]
        assert recovery["active_entry_count"] == 0

        again = api.startup_update_state()["data"]
        assert again["upgraded"] is False
        assert again["scheme_recovery"]["should_prompt"] is False
    finally:
        api.db.close()

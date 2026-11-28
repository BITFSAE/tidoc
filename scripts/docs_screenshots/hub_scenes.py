"""共享区截图使用本机测试服务、虚构发票和真实的客户端界面。"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from decimal import Decimal

from tidoc.api import Api
from tidoc.db import TYPE_INSPECTION, TYPE_INVOICE_PDF
from tidoc.engine.models import ParsedInvoice, ParsedItem
from tidoc.services.hub_sync import HubClient, HubError
from tidoc_hub.server import HubConfig, create_server

from . import bridge
from .data import PACKAGE_ID, Backend, _blank_pdf


SPACE_NAME = "11 月报账"
REVIEW_NOTE = "请补一张能看到付款金额和商户名称的付款截图。"


def _prepare(api, name, reviewer):
    backend = Backend(api)
    backend.set_app_preference("tidoc.update.autoCheck", "0")
    scheme = next(item for item in backend.list_schemes() if item["package_id"] == PACKAGE_ID)
    backend.complete_adapter_setup(scheme["id"])
    return backend, backend.create_profile(name, reviewer, True)["id"]


def _add_entry(api, profile, work_dir, number, seller, total):
    invoice = ParsedInvoice(
        invoice_no=f"26999000000000000{number}",
        invoice_date="2026-11-15",
        seller=seller,
        total=total,
        buyer_name="示例工学院",
    )
    entry_id = api.entries.create(profile, parsed=invoice)
    pdf = work_dir / f"发票_{number}.pdf"
    _blank_pdf(pdf, 200 + number)
    attachment = api.attachments.add(entry_id, str(pdf), TYPE_INVOICE_PDF)
    api.entries.recompute_status(entry_id)
    return entry_id, attachment["stored_path"]


def _seed_reviews(reviewer, member, member_profile, space_id, work_dir):
    rows = [("示例五金商行", "128.50"), ("示例实验器材商行", "358.00"), ("示例文具店", "42.80")]
    ids = [_add_entry(member._api, member_profile, work_dir, number, *row)[0]
           for number, row in enumerate(rows, start=101)]
    pushed = member.hub_push_entries(space_id, ids, SPACE_NAME)["pushed"]
    if len(pushed) != len(ids):
        raise RuntimeError("共享区截图的示例发票上传失败")

    # 一项只有审核变化，另一项有新材料。两项都由真实服务端保存。
    reviewer.hub_preview_pull(space_id)
    local_ids = reviewer.hub_apply_pull(space_id)["entry_ids"]
    by_invoice = {reviewer.get_entry(entry_id)["invoice_no"]: entry_id for entry_id in local_ids}
    reviewer.hub_review_entry(by_invoice["26999000000000000101"], "needs_fix", REVIEW_NOTE, False)
    inspection_id = by_invoice["26999000000000000102"]
    inspection = work_dir / "补充查验单.pdf"
    _blank_pdf(inspection, 420)
    reviewer._api.attachments.add(inspection_id, str(inspection), TYPE_INSPECTION)
    reviewer.hub_review_entry(inspection_id, "submitted", "", True)
    return ids


def _seed_upload(api, profile, work_dir, hub_url, token):
    rows = [("示例五金商行", "128.50"), ("示例文具店", "42.80"),
            ("示例线材商行", "89.10"), ("示例办公用品店", "310.00")]
    entries = [_add_entry(api, profile, work_dir, number, *row)
               for number, row in enumerate(rows, start=201)]
    client = HubClient(hub_url, token)
    upload = client.upload_blob

    def simulated_disconnect(url, file_path, headers):
        if file_path == api.data_root.attachments_dir / entries[1][1]:
            raise HubError("上传附件 发票_202.pdf 失败：连接中断，请重试。")
        return upload(url, file_path, headers)

    client.upload_blob = simulated_disconnect
    api._hub_client = lambda: client
    return [entry_id for entry_id, _ in entries]


def _seed_item_difference(reviewer, member, member_profile, space_id):
    item = ParsedItem("示例波纹管", "示例波纹管", "米", Decimal("3"), Decimal("18.00"))
    invoice = ParsedInvoice(
        invoice_no="26999000000000000301", invoice_date="2026-11-15", seller="示例五金商行",
        total=Decimal("18.00"), buyer_name="示例工学院", items=[item],
    )
    entry_id = member._api.entries.create(member_profile, parsed=invoice)
    if not member.hub_push_entries(space_id, [entry_id], SPACE_NAME)["pushed"]:
        raise RuntimeError("明细对比的示例发票上传失败")
    [review_entry_id] = reviewer.hub_apply_pull(space_id)["entry_ids"]
    row_id = reviewer.get_entry(review_entry_id)["items"][0]["id"]
    reviewer.update_item(row_id, {"quantity": "5", "unit_price": "3.60"})
    reviewer.hub_review_entry(review_entry_id, "submitted", "", True)
    local_row_id = member.get_entry(entry_id)["items"][0]["id"]
    member.update_item(local_row_id, {"quantity": "4", "unit_price": "4.50"})
    return [entry_id]


@contextmanager
def make_server(work_dir, scene, sprite_titles, sprite_heights, sprite_width):
    work_dir.mkdir()
    hub = create_server(HubConfig(port=0, data_dir=str(work_dir / "hub")))
    threading.Thread(target=hub.serve_forever, daemon=True).start()
    member_api = reviewer_api = web = None
    try:
        hub_url = f"http://127.0.0.1:{hub.server_port}/tidoc-hub"
        member_api = Api(str(work_dir / "member"))
        reviewer_api = Api(str(work_dir / "reviewer"))
        member, member_profile = _prepare(member_api, "示例队员", "示例审核人")
        reviewer, _ = _prepare(reviewer_api, "示例审核人", "示例复核人")
        _, token = hub.db.create_user("示例审核人", role="admin")
        reviewer.hub_connect_token(hub_url, token)
        space_id = reviewer.hub_create_space(SPACE_NAME)["space"]["id"]
        reviewer.hub_create_space("12 月报账")
        invite = reviewer.hub_create_invite("member", 1)["invite"]["code"]
        member.hub_join(hub_url, invite, "示例队员", "示例审核人")

        if scene.startswith("hub-upload-"):
            ids = _seed_upload(member_api, member_profile, work_dir, hub_url, member_api._hub_token())
        elif scene == "hub-item-diff":
            ids = _seed_item_difference(reviewer, member, member_profile, space_id)
        else:
            ids = _seed_reviews(reviewer, member, member_profile, space_id, work_dir)

        web = bridge.make_server(
            member_api, sprite_titles, sprite_heights, sprite_width,
            hub_shot={"spaceId": space_id, "entryIds": ids},
        )
        yield web
    finally:
        if web is not None:
            web.shutdown()
            web.server_close()
        for api in (member_api, reviewer_api):
            if api is not None:
                api.db.close()
        hub.shutdown()
        hub.close_resources()

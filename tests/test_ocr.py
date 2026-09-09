"""阿里云 OCR 识别组件测试：解析、差异策略、落库、子进程 IPC、更新安装。

不联网：阿里云调用通过 monkeypatch / 假组件可执行文件替代。
"""

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from tidoc_ocr.ocr import normalize_invoice_data

# 一份贴近官方文档字段名的 RecognizeInvoice data 样本
SAMPLE_DATA = {
    "invoiceCode": "",
    "invoiceNumber": "24122000000012345678",
    "invoiceDate": "2026年01月31日",
    "purchaserName": "北京理工大学",
    "purchaserTaxNumber": "12100000400009127B",
    "totalAmount": "¥1,130.00",
    "sellerName": "某某科技有限公司",
    "invoiceDetails": [
        {
            "itemName": "*信息技术服务*软件开发服务",
            "specification": "V2.1",
            "unit": "次",
            "quantity": "2.00000000",
            "unitPrice": "500.00",
            "amount": "1000.00",
            "taxRate": "6%",
            "tax": "60.00",
        },
        {
            # 价外费用续行：同名、无数量，金额应并入上一行
            "itemName": "*信息技术服务*软件开发服务",
            "amount": "60.00",
            "tax": "10.00",
        },
    ],
}


def _normalized():
    return normalize_invoice_data(SAMPLE_DATA)


# ---------------------------------------------------------------- 组件：解析
def test_normalize_maps_official_fields():
    out = _normalized()
    assert out["invoice_no"] == "24122000000012345678"
    assert out["buyer_name"] == "北京理工大学"
    assert out["buyer_tax_id"] == "12100000400009127B"
    assert out["invoice_date"] == "2026-01-31"
    assert out["total"] == "1130.00"
    assert out["closure_pass"] is True
    assert out["items"][0]["actual_name"] == "软件开发服务"
    assert out["items"][0]["spec"] == "V2.1"
    assert out["items"][0]["unit"] == "次"
    assert out["items"][0]["quantity"] == "2"


def test_normalize_merges_continuation_lines_without_quantity():
    out = _normalized()
    assert len(out["items"]) == 1
    assert out["items"][0]["total"] == "1130.00"
    assert out["item_sum"] == "1130.00"


def test_normalize_flags_broken_closure():
    data = json.loads(json.dumps(SAMPLE_DATA))
    data["totalAmount"] = "999.00"
    out = normalize_invoice_data(data)
    assert out["closure_pass"] is False
    assert out["item_sum"] == "1130.00"


def test_normalize_tolerates_alias_keys_and_dirty_numbers():
    out = normalize_invoice_data({
        "invoiceNo": "24122000000012345670",
        "buyerName": "北京理工大学教育基金会",
        "amountWithTax": "￥88.88",
        "invoiceDetails": [
            {"goodsName": "*电子元件*单片机", "qty": "3", "amount": "79.90", "taxAmount": "8.98"},
        ],
    })
    assert out["invoice_no"] == "24122000000012345670"
    assert out["buyer_name"] == "北京理工大学教育基金会"
    assert out["closure_pass"] is True
    assert out["items"][0]["quantity"] == "3"


def test_component_self_test_reports_missing_dependencies():
    proc = subprocess.run(
        [sys.executable, "-m", "tidoc_ocr", "--self-test"],
        text=True, capture_output=True, check=False,
    )
    payload = json.loads(proc.stdout)
    assert payload["component"] == "tidoc_ocr"
    import tidoc_ocr
    if tidoc_ocr.is_available():
        assert proc.returncode == 0
        assert payload["ok"] is True
    else:
        assert proc.returncode == 1
        assert payload["ok"] is False
        assert payload["missing"]


def test_build_credential_supports_installed_sdk_generation():
    import pytest as _pytest

    import tidoc_ocr
    if not tidoc_ocr.is_available():
        _pytest.skip("阿里云依赖未安装")
    from tidoc_ocr.ocr import _build_credential
    credential = _build_credential("fake-id", "fake-secret")
    assert credential is not None


# ---------------------------------------------------------------- 组件：CLI IPC
def test_cli_ipc_contract(tmp_path):
    """--input/--result JSON 文件协议：密钥错误等单张失败不阻断批次。"""
    input_path = tmp_path / "input.json"
    result_path = tmp_path / "result.json"
    input_path.write_text(json.dumps({
        "credentials": {"access_key_id": "fake", "access_key_secret": "fake"},
        "tasks": [{"entry_id": "e1", "file_path": str(tmp_path / "none.pdf")}],
    }), "utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "tidoc_ocr", "--input", str(input_path), "--result", str(result_path)],
        text=True, capture_output=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(result_path.read_text("utf-8"))
    assert result["ok"] is True
    row = result["data"]["results"][0]
    assert row["entry_id"] == "e1"
    assert row["ok"] is False  # 文件不存在 → 单张失败
    assert row["error"]


def test_cli_ipc_rejects_missing_credentials(tmp_path):
    input_path = tmp_path / "input.json"
    result_path = tmp_path / "result.json"
    input_path.write_text(json.dumps({"credentials": {}, "tasks": []}), "utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "tidoc_ocr", "--input", str(input_path), "--result", str(result_path)],
        text=True, capture_output=True, check=False,
    )
    assert proc.returncode == 1
    result = json.loads(result_path.read_text("utf-8"))
    assert result["ok"] is False
    assert "AccessKey" in result["error"]


# ---------------------------------------------------------------- 核心适配器：外部组件 IPC
FAKE_COMPONENT_HEAD = "#!/usr/bin/env python3\nimport json, sys\nfrom pathlib import Path\n"
FAKE_COMPONENT_BODY = """\
args = sys.argv[1:]
inp = json.loads(Path(args[args.index("--input") + 1]).read_text("utf-8"))
out = Path(args[args.index("--result") + 1])
out.write_text(json.dumps({"ok": True, "data": {"results": [
    {"entry_id": t["entry_id"], "ok": True, "raw_data": RAW, "normalized": NORM, "error": ""}
    for t in inp["tasks"]
]}}, ensure_ascii=False), "utf-8")
"""


def _fake_component_path(tmp_path: Path) -> Path:
    # Windows 的 CreateProcess 不能直接执行脚本文件，需要 .bat 包装
    return tmp_path / ("tidoc_ocr.bat" if sys.platform == "win32" else "tidoc_ocr")


def _write_fake_component(path: Path, raw: dict, normalized: dict) -> None:
    head = (
        FAKE_COMPONENT_HEAD
        + f"RAW = {raw!r}\nNORM = {normalized!r}\n"
    )
    if sys.platform == "win32":
        impl = path.with_suffix(".impl.py")
        impl.write_text(head + FAKE_COMPONENT_BODY, "utf-8")
        path.write_text(f'@echo off\r\n"{sys.executable}" "{impl}" %*\r\n', "utf-8")
    else:
        path.write_text(head + FAKE_COMPONENT_BODY, "utf-8")


def test_invoke_ocr_external_mode(tmp_path, monkeypatch):
    """外部组件模式：input.json 传递任务与密钥，result.json 回传逐条结果。"""
    from tidoc.services import ocr as ocr_service

    fake = _fake_component_path(tmp_path)
    _write_fake_component(fake, SAMPLE_DATA, _normalized())
    fake.chmod(0o755)
    monkeypatch.setattr(ocr_service, "component_status", lambda cd=None: {
        "available": True, "mode": "external", "path": str(fake), "missing": [],
    })

    invoice = tmp_path / "发票_01.pdf"
    invoice.write_bytes(b"%PDF-1.4 fake")
    results = ocr_service.invoke_ocr(
        [{"entry_id": "e1", "file_path": str(invoice)}],
        {"access_key_id": "id", "access_key_secret": "secret"},
        components_dir=tmp_path / "components",
    )
    assert len(results) == 1
    assert results[0]["ok"] is True
    assert results[0]["normalized"]["invoice_no"] == "24122000000012345678"


def test_invoke_ocr_requires_available_component(tmp_path, monkeypatch):
    from tidoc.services import ocr as ocr_service

    monkeypatch.setattr(ocr_service, "component_status", lambda cd=None: {
        "available": False, "mode": "missing", "missing": ["OCR 识别组件"],
    })
    with pytest.raises(RuntimeError, match="OCR 识别组件"):
        ocr_service.invoke_ocr([{"entry_id": "e1", "file_path": "x.pdf"}], {}, tmp_path)


def test_windows_external_ocr_does_not_open_console(tmp_path, monkeypatch):
    from tidoc.services import ocr as ocr_service

    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        result_path = Path(cmd[cmd.index("--result") + 1])
        result_path.write_text(
            json.dumps({"ok": True, "data": {"results": []}}), "utf-8"
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ocr_service.sys, "platform", "win32")
    monkeypatch.setattr(ocr_service.subprocess, "CREATE_NO_WINDOW", 0x1234, raising=False)
    monkeypatch.setattr(ocr_service.subprocess, "run", fake_run)

    assert ocr_service._invoke_ocr_external("tidoc-ocr.exe", [], {}) == []
    assert seen["creationflags"] == 0x1234


# ---------------------------------------------------------------- 差异策略
def _entry(repos, source="pdf", **parsed_kwargs):
    from tidoc.engine.models import ParsedInvoice

    parsed = ParsedInvoice(
        invoice_no="24122000000012345678",
        invoice_date="",
        seller="某某科技有限公司",
        buyer_name="北京理工大学",
        total=Decimal("1130.00"),
        source=source,
        **parsed_kwargs,
    )
    profile_id = repos["profiles"].create("张三", "李四")["id"]
    entry_id = repos["entries"].create(profile_id, parsed=parsed)
    return repos["entries"].get(entry_id)


def test_plan_pdf_source_fills_fields_but_leaves_items_pending(repos):
    from tidoc.services.ocr import apply_plan, plan_entry_update

    entry = _entry(repos, source="pdf")  # 无明细、无日期
    normalized = _normalized()
    plan = plan_entry_update(entry, normalized, human_modified=set())

    actions = {row["field"]: row["action"] for row in plan["field_rows"]}
    assert actions["invoice_date"] == "fill"          # 本地为空 → 补齐
    assert actions["seller"] == "same"
    assert actions["total"] == "same"
    assert actions["buyer_name"] == "same"
    assert plan["items_action"] == "pending"

    applied = apply_plan(repos["entries"], entry["id"], plan, normalized)
    assert applied["applied_fields"] == ["invoice_date", "buyer_tax_id"]
    assert applied["items_replaced"] is False

    updated = repos["entries"].get(entry["id"])
    assert updated["invoice_date"] == "2026-01-31"
    assert updated["items"] == []
    # 发票字段补齐后，明细差异仍等待用户确认。
    recheck = plan_entry_update(updated, normalized, set())
    assert recheck["items_action"] == "pending"
    assert all(row["action"] == "same" for row in recheck["field_rows"] if row["ocr"])


def test_plan_xml_source_never_overwrites_items(repos):
    from tidoc.services.ocr import plan_entry_update

    entry = _entry(repos, source="xml")
    normalized = _normalized()
    plan = plan_entry_update(entry, normalized)
    assert plan["source_xml"] is True
    assert plan["items_action"] == "pending"           # XML 是权威数据，只提示比对
    actions = {row["field"]: row["action"] for row in plan["field_rows"]}
    assert actions["invoice_date"] == "fill"           # 空字段仍可补
    assert actions["seller"] == "same"


def test_plan_respects_human_modified_locked_fields(repos):
    from tidoc.services.ocr import plan_entry_update

    entry = _entry(repos, source="pdf")
    repos["entries"].correct_locked_field(entry["id"], "seller", "人工确认过的销售方", "")
    normalized = _normalized()
    normalized["seller"] = "别的销售方"
    plan = plan_entry_update(
        repos["entries"].get(entry["id"]), normalized,
        human_modified=repos["entries"].human_modified_locked_fields(entry["id"]),
    )
    actions = {row["field"]: row["action"] for row in plan["field_rows"]}
    assert actions["seller"] == "local_preferred"
    assert "seller" not in plan["pending"]


def test_plan_seller_prefers_local_and_ignores_parenthesis_width(repos):
    from tidoc.services.ocr import apply_plan, plan_entry_update

    entry = _entry(repos, source="pdf")
    normalized = _normalized()
    normalized["seller"] = "某某科技有限公司(个体工商户)"
    repos["db"].conn.execute(
        "UPDATE entries SET seller = ? WHERE id = ?",
        ("某某科技有限公司（个体工商户）", entry["id"]),
    )
    entry = repos["entries"].get(entry["id"])
    plan = plan_entry_update(entry, normalized)
    actions = {row["field"]: row["action"] for row in plan["field_rows"]}
    assert actions["seller"] == "same"

    normalized["seller"] = "某某智扭科技有限公司"
    plan = plan_entry_update(entry, normalized)
    actions = {row["field"]: row["action"] for row in plan["field_rows"]}
    assert actions["seller"] == "local_preferred"
    assert "seller" not in plan["pending"]
    apply_plan(repos["entries"], entry["id"], plan, normalized)
    assert repos["entries"].get(entry["id"])["seller"] == "某某科技有限公司（个体工商户）"


def test_plan_broken_closure_downgrades_to_pending(repos):
    from tidoc.services.ocr import plan_entry_update

    entry = _entry(repos, source="pdf")
    normalized = _normalized()
    normalized["closure_pass"] = False
    plan = plan_entry_update(entry, normalized)
    assert plan["items_action"] == "pending"


def test_plan_total_and_invoice_no_differences_are_pending(repos):
    from tidoc.services.ocr import plan_entry_update

    entry = _entry(repos, source="pdf")
    normalized = _normalized()
    normalized["total"] = "999.00"
    normalized["invoice_no"] = "12345678"
    plan = plan_entry_update(entry, normalized)
    actions = {row["field"]: row["action"] for row in plan["field_rows"]}
    assert actions["total"] == "pending"
    assert actions["invoice_no"] == "pending"


# ---------------------------------------------------------------- 落库 / 采用
def _attach_invoice_pdf(repos, entry_id, name="发票_01.pdf"):
    src = repos["root"].root / "src"
    src.mkdir(parents=True, exist_ok=True)
    pdf = src / name
    pdf.write_bytes(b"%PDF-1.4 fake invoice")
    return repos["attachments"].add(entry_id, pdf, "invoice_pdf")


def _attach_invoice_xml(repos, entry_id, name="发票_01.xml"):
    src = repos["root"].root / "src"
    src.mkdir(parents=True, exist_ok=True)
    xml = src / name
    xml.write_text(
        '<?xml version="1.0"?><Invoice><EIid>24122000000012345678</EIid></xml>',
        "utf-8",
    )
    return repos["attachments"].add(entry_id, xml, "invoice_xml")


def test_ocr_repo_records_and_pending_lifecycle(repos):
    repo = repos["db"]
    from tidoc.db import OcrRepo

    ocr = OcrRepo(repo)
    entry = _entry(repos)
    first = ocr.record(entry["id"], raw_json="{}", normalized="{}", closure_pass=True,
                       pending=["seller", "items"])
    assert ocr.pending_fields(entry["id"]) == ["seller", "items"]
    assert ocr.pending_entry_ids() == {entry["id"]}
    assert ocr.count_calls() == 1

    ocr.mark_applied(first["id"], ["items"])
    assert ocr.pending_fields(entry["id"]) == ["items"]
    ocr.mark_applied(first["id"], [])
    assert ocr.pending_fields(entry["id"]) == []
    assert ocr.pending_entry_ids() == set()
    assert ocr.get(first["id"])["applied_at"]  # 全部处理完时记录应用时间

    # 新结果覆盖待确认状态；失败行不计入 pending
    ocr.record(entry["id"], status="failed", error="网络错误")
    assert ocr.latest(entry["id"]) is not None
    assert ocr.latest_failed(entry["id"])["error"] == "网络错误"
    assert ocr.count_calls() == 2


def test_apply_ocr_field_and_items_from_saved_result(repos):
    from tidoc.db import OcrRepo
    from tidoc.services.ocr import apply_ocr_field, apply_ocr_items, result_view

    ocr = OcrRepo(repos["db"])
    entry = _entry(repos, source="xml")  # XML 来源：只走人工采用
    repos["entries"].correct_locked_field(entry["id"], "total", "1000.00", "")
    normalized = _normalized()
    ocr.record(entry["id"], raw_json=json.dumps(SAMPLE_DATA),
               normalized=json.dumps(normalized, ensure_ascii=False),
               closure_pass=normalized["closure_pass"])

    out = apply_ocr_field(repos["entries"], ocr, entry["id"], "total")
    assert out["entry"]["total"] == "1130.00"
    assert out["pending"] == ["items"]
    view = result_view(repos["entries"], ocr, entry["id"])
    assert view["plan"]["items_action"] == "pending"

    out = apply_ocr_items(repos["entries"], ocr, entry["id"])
    assert out["pending"] == []
    updated = repos["entries"].get(entry["id"])
    assert len(updated["items"]) == 1
    assert ocr.pending_entry_ids() == set()
    # 采用明细后重算校验：金额已闭合，但 XML 原值仍缺购买方税号，继续提醒核对。
    assert updated["check_status"] == "warning"
    assert "购买方税号" in updated["check_message"]


# ---------------------------------------------------------------- 编排（API 级，不联网）
def test_run_ocr_for_entries_end_to_end(repos, monkeypatch):
    from tidoc.db import OcrRepo
    from tidoc.services import ocr as ocr_service

    entry = _entry(repos, source="pdf")
    _attach_invoice_pdf(repos, entry["id"])
    ocr = OcrRepo(repos["db"])
    normalized = _normalized()

    def fake_invoke(tasks, credentials, components_dir=None):
        assert credentials == {"access_key_id": "id", "access_key_secret": "secret"}
        return [{
            "entry_id": tasks[0]["entry_id"], "ok": True,
            "raw_data": SAMPLE_DATA, "normalized": normalized, "error": "",
        }]

    monkeypatch.setattr(ocr_service, "invoke_ocr", fake_invoke)
    result = ocr_service.run_ocr_for_entries(
        repos["entries"], ocr, repos["root"].attachments_dir, [entry["id"]],
        {"access_key_id": "id", "access_key_secret": "secret"},
    )
    assert result["called"] == 1
    row = result["results"][0]
    assert row["ok"] is True
    assert row["items_replaced"] is False
    assert row["pending"] == ["items"]
    assert "invoice_date" in row["applied_fields"]
    # 原始响应与解析快照都已落库
    saved = ocr.latest(entry["id"])
    assert json.loads(saved["raw_json"])["invoiceNumber"] == SAMPLE_DATA["invoiceNumber"]
    assert json.loads(saved["normalized"])["closure_pass"] is True
    changes = saved["applied_changes_data"]
    assert changes["fields"][0] == {
        "field": "invoice_date",
        "label": "发票日期",
        "before": "",
        "after": "2026-01-31",
        "action": "fill",
    }
    assert changes["items"] is None


def test_run_ocr_can_skip_current_existing_result(repos, monkeypatch):
    from tidoc.db import OcrRepo
    from tidoc.services import ocr as ocr_service

    entry = _entry(repos, source="pdf")
    attachment = _attach_invoice_pdf(repos, entry["id"])
    ocr = OcrRepo(repos["db"])
    ocr.record(entry["id"], file_sha256=attachment["sha256"], normalized="{}")

    def unexpected_invoke(*args, **kwargs):
        raise AssertionError("已识别且文件未变化时不应再次调用")

    monkeypatch.setattr(ocr_service, "invoke_ocr", unexpected_invoke)
    result = ocr_service.run_ocr_for_entries(
        repos["entries"], ocr, repos["root"].attachments_dir, [entry["id"]],
        {"access_key_id": "id", "access_key_secret": "secret"},
        skip_existing=True,
    )
    assert result["called"] == 0
    assert result["skipped"] == [{
        "entry_id": entry["id"], "reason": "当前发票已有识别结果，已跳过",
    }]


def test_spec_only_ocr_difference_does_not_change_items(repos, monkeypatch):
    from tidoc.db import OcrRepo
    from tidoc.engine.models import ParsedInvoice, ParsedItem
    from tidoc.services import ocr as ocr_service

    profile_id = repos["profiles"].create("张三", "李四")["id"]
    parsed = ParsedInvoice(
        invoice_no="24122000000012345679",
        total=Decimal("12.00"),
        source="pdf",
        items=[ParsedItem(
            name="*电子元件*模块", actual_name="模块", unit="个",
            quantity=Decimal("1"), total=Decimal("12.00"), spec="",
        )],
    )
    entry_id = repos["entries"].create(profile_id, parsed=parsed)
    _attach_invoice_pdf(repos, entry_id)
    ocr = OcrRepo(repos["db"])
    normalized = {
        "invoice_no": parsed.invoice_no,
        "invoice_date": "",
        "seller": "",
        "buyer_name": "",
        "buyer_tax_id": "",
        "total": "12.00",
        "closure_pass": True,
        "items": [{
            "name": "*电子元件*模块", "actual_name": "模块", "unit": "个",
            "quantity": "1", "total": "12.00", "spec": "ABC-01",
        }],
    }

    monkeypatch.setattr(ocr_service, "invoke_ocr", lambda *args, **kwargs: [{
        "entry_id": entry_id, "ok": True, "raw_data": {},
        "normalized": normalized, "error": "",
    }])
    result = ocr_service.run_ocr_for_entries(
        repos["entries"], ocr, repos["root"].attachments_dir, [entry_id],
        {"access_key_id": "id", "access_key_secret": "secret"},
    )

    assert result["results"][0]["items_replaced"] is False
    assert result["results"][0]["items_action"] == "same"
    updated = repos["entries"].get(entry_id)["items"][0]
    assert updated["quantity"] == "1"
    assert updated["spec"] == ""
    assert ocr.latest(entry_id)["applied_changes_data"] == {}


def test_ocr_leaves_aligned_item_blank_difference_pending(repos, monkeypatch):
    from tidoc.db import OcrRepo
    from tidoc.engine.models import ParsedInvoice, ParsedItem
    from tidoc.services import ocr as ocr_service

    profile_id = repos["profiles"].create("张三", "李四")["id"]
    parsed = ParsedInvoice(
        invoice_no="24122000000012345679", total=Decimal("12.00"), source="pdf",
        items=[ParsedItem(
            name="*电子元件*模块", actual_name="模块", unit="",
            quantity=Decimal("1"), total=Decimal("12.00"), spec="本地型号",
        )],
    )
    entry_id = repos["entries"].create(profile_id, parsed=parsed)
    _attach_invoice_pdf(repos, entry_id)
    ocr = OcrRepo(repos["db"])
    normalized = {
        "invoice_no": parsed.invoice_no, "invoice_date": "", "seller": "",
        "buyer_name": "", "buyer_tax_id": "", "total": "12.00",
        "closure_pass": True,
        "items": [{
            "name": "*电子元件*模块", "actual_name": "模块", "unit": "个",
            "quantity": "1", "total": "12.00", "spec": "云端型号",
        }],
    }
    monkeypatch.setattr(ocr_service, "invoke_ocr", lambda *args, **kwargs: [{
        "entry_id": entry_id, "ok": True, "raw_data": {},
        "normalized": normalized, "error": "",
    }])
    result = ocr_service.run_ocr_for_entries(
        repos["entries"], ocr, repos["root"].attachments_dir, [entry_id],
        {"access_key_id": "id", "access_key_secret": "secret"},
    )

    assert result["results"][0]["items_action"] == "pending"
    updated = repos["entries"].get(entry_id)["items"][0]
    assert updated["unit"] == ""
    assert updated["quantity"] == "1"
    assert updated["spec"] == "本地型号"


def test_quantity_difference_is_pending_even_when_amount_closes(repos):
    from tidoc.engine.models import ParsedItem
    from tidoc.services.ocr import plan_entry_update

    entry = _entry(repos, source="pdf", items=[ParsedItem(
        name="*电子元件*模块", actual_name="模块", unit="个",
        quantity=Decimal("1"), total=Decimal("1130.00"),
    )])
    normalized = _normalized()
    normalized["items"] = [{
        "name": "*电子元件*模块", "actual_name": "模块", "unit": "个",
        "quantity": "2", "total": "1130.00", "spec": "不参与比对",
    }]

    plan = plan_entry_update(entry, normalized)
    assert plan["closure_pass"] is True
    assert plan["items_action"] == "pending"
    assert "items" in plan["pending"]


def test_run_ocr_skips_xml_entries_without_flag(repos, monkeypatch):
    from tidoc.db import OcrRepo
    from tidoc.services import ocr as ocr_service

    entry = _entry(repos, source="xml")
    _attach_invoice_pdf(repos, entry["id"])
    _attach_invoice_xml(repos, entry["id"])
    ocr = OcrRepo(repos["db"])
    called = []

    def fake_invoke(tasks, credentials, components_dir=None):
        called.extend(tasks)
        return []

    monkeypatch.setattr(ocr_service, "invoke_ocr", fake_invoke)
    result = ocr_service.run_ocr_for_entries(
        repos["entries"], ocr, repos["root"].attachments_dir, [entry["id"]],
        {"access_key_id": "id", "access_key_secret": "secret"},
    )
    assert result["called"] == 0
    assert called == []
    assert result["skipped"][0]["reason"].startswith("已有 XML")

    result = ocr_service.run_ocr_for_entries(
        repos["entries"], ocr, repos["root"].attachments_dir, [entry["id"]],
        {"access_key_id": "id", "access_key_secret": "secret"},
        include_xml=True,
    )
    assert result["called"] == 1
    assert len(called) == 1


def test_run_ocr_records_failures(repos, monkeypatch):
    from tidoc.db import OcrRepo
    from tidoc.services import ocr as ocr_service

    entry = _entry(repos, source="pdf")
    _attach_invoice_pdf(repos, entry["id"])
    ocr = OcrRepo(repos["db"])

    def fake_invoke(tasks, credentials, components_dir=None):
        return [{
            "entry_id": tasks[0]["entry_id"], "ok": False,
            "raw_data": None, "normalized": None, "error": "AccessKey 无效",
        }]

    monkeypatch.setattr(ocr_service, "invoke_ocr", fake_invoke)
    result = ocr_service.run_ocr_for_entries(
        repos["entries"], ocr, repos["root"].attachments_dir, [entry["id"]],
        {"access_key_id": "id", "access_key_secret": "secret"},
    )
    assert result["results"][0]["ok"] is False
    assert ocr.latest_failed(entry["id"])["error"] == "AccessKey 无效"
    assert ocr.latest(entry["id"]) is None


# ---------------------------------------------------------------- 徽标批量重算
def test_latest_ok_rows_and_mark_applied_skip_write(repos):
    from tidoc.db import OcrRepo

    ocr = OcrRepo(repos["db"])
    entry = _entry(repos)
    first = ocr.record(entry["id"], normalized="{}", pending=["seller"])
    second = ocr.record(entry["id"], normalized="{}", pending=[])

    rows = ocr.latest_ok_rows([entry["id"]])
    assert list(rows) == [entry["id"]]
    assert rows[entry["id"]]["id"] == second["id"]      # 取最新成功行，而非旧行
    assert rows[entry["id"]]["id"] != first["id"]
    assert rows[entry["id"]]["pending_list"] == []
    assert ocr.latest_ok_rows(["missing-entry"]) == {}

    # 差异与已存值一致时跳过写入：列表刷新高频重算不能每次都 commit
    conn = repos["db"].conn
    before = conn.total_changes
    ocr.mark_applied(second["id"], [])
    assert conn.total_changes == before
    ocr.mark_applied(second["id"], ["seller"])
    assert conn.total_changes > before


def test_imported_ocr_result_is_not_counted_as_local_call(repos):
    from tidoc.db import OcrRepo

    ocr = OcrRepo(repos["db"])
    entry = _entry(repos)
    ocr.record(entry["id"], normalized="{}", is_local_call=False)
    assert ocr.latest(entry["id"])["is_local_call"] is False
    assert ocr.count_calls() == 0


def test_sync_ocr_states_recomputes_after_manual_correction(repos):
    """人工修正后与已存识别结果出现差异时，徽标要在下次列表刷新时出现。"""
    from tidoc.db import OcrRepo
    from tidoc.services.ocr import sync_ocr_states

    ocr = OcrRepo(repos["db"])
    entry = _entry(repos, source="pdf")
    normalized = _normalized()  # seller 与本地一致
    normalized["items"] = []
    ocr.record(entry["id"], normalized=json.dumps(normalized, ensure_ascii=False),
               closure_pass=True)

    listed = repos["entries"].list()
    pending, recognized = sync_ocr_states(repos["entries"], ocr, listed)
    assert pending == set()
    assert recognized == {entry["id"]}

    repos["entries"].correct_locked_field(entry["id"], "seller", "人工确认销售方", "")
    listed = repos["entries"].list()
    pending, _ = sync_ocr_states(repos["entries"], ocr, listed)
    assert pending == set()
    assert ocr.pending_fields(entry["id"]) == []
    # 列表条目未被注入明细（保持列表载荷轻量），明细只参与比对
    assert "items" not in listed[0]


# ---------------------------------------------------------------- API 桥
@pytest.fixture
def ocr_component_ready(monkeypatch):
    """本机没装阿里云 SDK 时也能测 API 层：把组件探测伪造成可用。"""
    from tidoc.services import ocr as ocr_service

    monkeypatch.setattr(
        ocr_service, "component_status",
        lambda cd=None: {"available": True, "mode": "python", "missing": []},
    )


def test_api_ocr_status_credentials_and_preview(api, monkeypatch):
    from tidoc.engine.models import ParsedInvoice

    status = api.ocr_component_status()["data"]
    assert status["credentials_configured"] is False
    assert status["total_calls"] == 0

    api.save_ocr_credentials("LTAI5tFakeKeyId12345", "fake-secret")
    status = api.ocr_component_status()["data"]
    assert status["credentials_configured"] is True
    assert status["access_key_id_masked"].startswith("LTAI5t")
    assert "fake-secret" not in status["access_key_id_masked"]

    parsed = ParsedInvoice(invoice_no="24122000000012345678", seller="销售方",
                           total=Decimal("1130.00"), source="pdf")
    profile_id = api.profiles.create("张三", "李四")["id"]
    entry = api.entries.create(profile_id, parsed=parsed)
    preview = api.ocr_preview([entry])["data"]["entries"][0]
    assert preview["has_invoice_pdf"] is False  # 尚无 PDF，预检要提示

    api.clear_ocr_credentials()
    assert api.ocr_component_status()["data"]["credentials_configured"] is False


def test_api_run_ocr_guards_and_pending_flag(api, monkeypatch, ocr_component_ready):
    from tidoc.engine.models import ParsedInvoice
    from tidoc.services import ocr as ocr_service

    parsed = ParsedInvoice(invoice_no="24122000000012345678", seller="销售方",
                           total=Decimal("1130.00"), source="pdf")
    profile_id = api.profiles.create("张三", "李四")["id"]
    entry_id = api.entries.create(profile_id, parsed=parsed)

    # 未配密钥 → 明确报错
    res = api.run_ocr_recognition([entry_id])
    assert res["ok"] is False
    assert "AccessKey" in res["error"]

    api.save_ocr_credentials("LTAI5tFakeKeyId12345", "fake-secret")
    src = Path(api.data_root.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    pdf = src / "发票_01.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    api.attachments.add(entry_id, pdf, "invoice_pdf")

    def fake_invoke(tasks, credentials, components_dir=None):
        return [{
            "entry_id": tasks[0]["entry_id"], "ok": True,
            "raw_data": SAMPLE_DATA, "normalized": _normalized(), "error": "",
        }]

    monkeypatch.setattr(ocr_service, "invoke_ocr", fake_invoke)
    res = api.run_ocr_recognition([entry_id])["data"]
    assert res["called"] == 1
    assert res["results"][0]["items_replaced"] is False
    assert res["results"][0]["pending"] == ["items"]
    preview = api.ocr_preview([entry_id])["data"]["entries"][0]
    assert preview["existing_result"] is True
    assert preview["existing_current_result"] is True

    # 带差异的条目在列表里能被识别出来（卡片徽标数据源）
    entries = api.list_entries()["data"]
    target = next(e for e in entries if e["id"] == entry_id)
    assert target["ocr_pending"] is True
    assert api.get_entry(entry_id)["data"]["items"] == []


def test_api_run_ocr_pending_drives_entry_flag(api, monkeypatch, ocr_component_ready):
    from tidoc.engine.models import ParsedInvoice
    from tidoc.services import ocr as ocr_service

    parsed = ParsedInvoice(invoice_no="24122000000012345678", seller="原销售方",
                           total=Decimal("1130.00"), source="pdf")
    profile_id = api.profiles.create("张三", "李四")["id"]
    entry_id = api.entries.create(profile_id, parsed=parsed)
    api.save_ocr_credentials("LTAI5tFakeKeyId12345", "fake-secret")
    src = Path(api.data_root.root) / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "发票_01.pdf").write_bytes(b"%PDF-1.4 fake")
    api.attachments.add(entry_id, src / "发票_01.pdf", "invoice_pdf")

    normalized = _normalized()
    normalized["seller"] = "新销售方"          # 与本地不同 → 保留软件值
    normalized["closure_pass"] = False        # 闭合失败 → 明细也待确认

    def fake_invoke(tasks, credentials, components_dir=None):
        return [{
            "entry_id": tasks[0]["entry_id"], "ok": True,
            "raw_data": SAMPLE_DATA, "normalized": normalized, "error": "",
        }]

    monkeypatch.setattr(ocr_service, "invoke_ocr", fake_invoke)
    res = api.run_ocr_recognition([entry_id])["data"]
    assert res["results"][0]["pending"] == ["items"]
    assert res["results"][0]["local_preferred_fields"] == ["seller"]

    detail = api.get_entry(entry_id)["data"]
    assert detail["ocr_pending"] is True
    view = api.get_ocr_result(entry_id)["data"]
    assert view["plan"]["items_action"] == "pending"

    out = api.apply_ocr_field(entry_id, "seller")["data"]
    assert out["entry"]["seller"] == "新销售方"
    assert "items" in out["pending"]
    assert api.get_entry(entry_id)["data"]["ocr_pending"] is True

    api.apply_ocr_items(entry_id)
    assert api.get_entry(entry_id)["data"]["ocr_pending"] is False


# ---------------------------------------------------------------- schema / 更新安装
def test_latest_schema_keeps_ocr_results_table(repos):
    from tidoc.db.schema import SCHEMA_VERSION

    version = repos["db"].conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()["value"]
    assert int(version) == SCHEMA_VERSION
    tables = {
        row["name"] for row in repos["db"].conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "ocr_results" in tables
    # 条目删除时 OCR 结果级联清理
    from tidoc.db import OcrRepo

    ocr = OcrRepo(repos["db"])
    entry = _entry(repos)
    ocr.record(entry["id"])
    repos["entries"].delete(entry["id"])
    assert ocr.latest(entry["id"]) is None


def test_schema_v9_restores_automatically_overwritten_seller_but_keeps_manual_edit(repos):
    from tidoc.db import Database, EntryRepo, OcrRepo

    first = _entry(repos)
    repos["entries"].ocr_update_locked_field(first["id"], "seller", "某某智扭科技有限公司")
    first_result = OcrRepo(repos["db"]).record(first["id"], normalized="{}")
    repos["db"].conn.execute(
        "UPDATE field_history SET changed_at = ? "
        "WHERE entry_id = ? AND field = '[阿里云OCR]seller'",
        (first_result["created_at"], first["id"]),
    )

    second = _entry(repos)
    repos["entries"].ocr_update_locked_field(second["id"], "seller", "云端销售方")
    second_result = OcrRepo(repos["db"]).record(second["id"], normalized="{}")
    repos["db"].conn.execute(
        "UPDATE field_history SET changed_at = ? "
        "WHERE entry_id = ? AND field = '[阿里云OCR]seller'",
        (second_result["created_at"], second["id"]),
    )
    repos["entries"].correct_locked_field(second["id"], "seller", "人工确认销售方")

    db_path = repos["root"].db_path
    repos["db"].conn.execute(
        "UPDATE meta SET value = '8' WHERE key = 'schema_version'"
    )
    repos["db"].conn.commit()
    repos["db"].close()

    upgraded = Database(db_path)
    entries = EntryRepo(upgraded)
    assert entries.get(first["id"])["seller"] == "某某科技有限公司"
    assert entries.get(second["id"])["seller"] == "人工确认销售方"
    rollback = upgraded.conn.execute(
        "SELECT old_value, new_value FROM field_history "
        "WHERE entry_id = ? AND field = '[阿里云OCR撤回]seller'",
        (first["id"],),
    ).fetchone()
    assert dict(rollback) == {
        "old_value": "某某智扭科技有限公司",
        "new_value": "某某科技有限公司",
    }
    upgraded.close()


def test_install_ocr_component_from_local_manifest(tmp_path):
    from tidoc.services.updater import install_ocr_component, installed_component_info, sha256_file

    artifact = tmp_path / "tidoc_ocr"
    artifact.write_text("#!/bin/sh\nexit 0\n")
    manifest = {
        "components": {
            "ocr": {
                "name": "OCR 识别组件",
                "latest": "0.1.0",
                "platforms": {
                    "macos": {
                        "url": artifact.as_uri(),
                        "sha256": sha256_file(artifact),
                        "filename": artifact.name,
                        "format": "binary",
                        "executable_name": "tidoc_ocr",
                    }
                },
            }
        }
    }
    result = install_ocr_component(manifest, tmp_path / "components", tmp_path / "updates", plat="macos")
    assert result.version == "0.1.0"
    info = installed_component_info(tmp_path / "components", "ocr", "macos")
    assert info["valid"] is True
    assert Path(info["executable"]).exists()


def test_check_updates_lists_ocr_component(tmp_path):
    from tidoc.services.updater import check_updates, sha256_file

    artifact = tmp_path / "tidoc-ocr-windows-v0.1.0.exe"
    artifact.write_bytes(b"ocr")
    manifest = {
        "components": {
            "core": {
                "name": "tidoc 核心", "latest": "0.1.0",
                "platforms": {"windows": {
                    "url": "https://example.com/core.exe",
                    "sha256": "0" * 64, "filename": "core.exe",
                }},
            },
            "ocr": {
                "name": "OCR 识别组件", "latest": "0.1.0",
                "platforms": {"windows": {
                    "url": artifact.as_uri(),
                    "sha256": sha256_file(artifact),
                    "filename": artifact.name,
                }},
            },
        }
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), "utf-8")
    status = check_updates(tmp_path / "components", manifest_path.as_uri(), plat="windows")
    names = {row["component"] for row in status["updates"]}
    assert names == {"core", "ocr"}
    ocr_row = next(row for row in status["updates"] if row["component"] == "ocr")
    assert ocr_row["available"] is True

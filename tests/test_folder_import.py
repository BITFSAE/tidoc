"""文件夹批量导入的分组启发式测试。"""

from pathlib import Path

import pytest

from tidoc.services import scan_files, scan_folder
from tidoc.engine import parse_pdf

SAMPLE_DIR = Path("/Users/poli/invoice2docx/invoices")
requires_sample_data = pytest.mark.skipif(
    not SAMPLE_DIR.is_dir(),
    reason="本地发票样本目录不存在",
)


@requires_sample_data
def test_scan_folder_groups_by_prefix():
    r = scan_folder(SAMPLE_DIR)
    # 批量导入按发票 PDF 建条目；付款截图/查验单不靠命名自动绑定。
    assert r["total_files"] > 0
    assert len(r["groups"]) >= 10

    g26 = next((g for g in r["groups"] if any(f["name"].startswith("26") for f in g["files"])), None)
    assert g26 is not None
    types = {f["type"] for f in g26["files"]}
    assert "invoice_pdf" in types
    assert "payment_screenshot" not in types
    assert "inspection_pdf" not in types


@requires_sample_data
def test_scan_folder_type_classification():
    r = scan_folder(SAMPLE_DIR)
    for g in r["groups"]:
        for f in g["files"]:
            assert f["type"] in {"invoice_pdf", "invoice_xml"}
    assert any(f["type"] == "payment_screenshot" for f in r["ignored"])
    assert any(f["type"] == "inspection_pdf" for f in r["ignored"])
    assert r["matched_xml_count"] > 0


@requires_sample_data
def test_scan_files_accepts_multi_selected_invoice_files():
    paths = [
        f"{SAMPLE_DIR}/28+电子元件+390.05+发票.pdf",
        f"{SAMPLE_DIR}/20260425205440942/立创商城发票-7556653A-26957000000103383672.xml",
    ]
    r = scan_files(paths)
    assert r["invoice_pdf_count"] == 1
    assert r["matched_xml_count"] == 1
    assert len(r["groups"]) == 1
    assert {f["type"] for f in r["groups"][0]["files"]} == {"invoice_pdf", "invoice_xml"}


def test_scan_folder_accepts_messy_invoice_pdfs_without_xml(tmp_path):
    (tmp_path / "微信保存的文件(1).pdf").write_bytes(b"not a real pdf")
    (tmp_path / "A-77889900.xml").write_text("<bad/>", encoding="utf-8")
    (tmp_path / "付款截图.png").write_bytes(b"img")

    r = scan_folder(tmp_path)
    assert r["invoice_pdf_count"] == 1
    assert len(r["groups"]) == 1
    assert r["groups"][0]["files"][0]["type"] == "invoice_pdf"
    assert r["ungrouped"]
    assert r["ignored"]


def test_scan_folder_detects_tax_verification_platform_pdf(tmp_path):
    from pypdf import PdfWriter

    pdf = tmp_path / "26.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=842, height=595)
    writer.add_metadata({"/Title": "国家税务总局全国增值税发票查验平台"})
    with pdf.open("wb") as f:
        writer.write(f)

    r = scan_folder(tmp_path)
    assert r["invoice_pdf_count"] == 0
    assert not r["groups"]
    assert r["ignored"][0]["type"] == "inspection_pdf"


def test_scan_folder_skips_pdf_containing_multiple_invoices(tmp_path):
    from pypdf import PdfWriter

    pdf = tmp_path / "合并发票文件.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=842, height=595)
    writer.add_metadata({
        "/Title": "电子发票 发票号码：26952000001672381651",
        "/Subject": "发票号码：26332000004713530326 开票日期：2026年07月01日",
    })
    with pdf.open("wb") as f:
        writer.write(f)

    r = scan_folder(tmp_path)

    assert r["invoice_pdf_count"] == 0
    assert not r["groups"]
    assert r["ignored"][0]["type"] == "other"
    assert "包含 2 张发票" in r["ignored"][0]["warning"]


def test_single_invoice_pdf_does_not_count_bank_account_as_second_invoice(tmp_path):
    from pypdf import PdfWriter

    pdf = tmp_path / "220+发票.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=842, height=595)
    writer.add_metadata({
        "/Title": "电子发票（普通发票） 发票号码：26332000006299955796",
        "/Subject": "销方开户银行：中国建设银行；银行账号：33050163744100000448",
    })
    with pdf.open("wb") as f:
        writer.write(f)

    r = scan_folder(tmp_path)

    assert r["invoice_pdf_count"] == 1
    assert len(r["groups"]) == 1
    assert not r["ignored"]


def test_scan_folder_skips_duplicate_invoice_number_groups(tmp_path, monkeypatch):
    from pypdf import PdfWriter
    from tidoc.services import folder_import

    monkeypatch.setattr(
        folder_import,
        "_parse_invoice_no",
        lambda _path, _att_type: ("26957000000168907686", ""),
    )

    for name in ("下载一.pdf", "下载二.pdf"):
        writer = PdfWriter()
        writer.add_blank_page(width=842, height=595)
        writer.add_metadata({
            "/Title": "电子发票 发票号码：26957000000168907686 开票日期：2026年07月13日",
            "/Subject": "购买方 北京理工大学教育基金会 销售方 深圳市立创电子商务有限公司 价税合计",
        })
        with (tmp_path / name).open("wb") as f:
            writer.write(f)

    r = scan_folder(tmp_path)

    assert r["invoice_pdf_count"] == 1
    assert len(r["groups"]) == 1
    assert len(r["ignored"]) == 1
    assert "发票号相同" in r["ignored"][0]["warning"]


def test_pdf_content_beats_filename_keyword_for_type_classification(tmp_path):
    from pypdf import PdfWriter

    pdf = tmp_path / "查验后补发票.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=842, height=595)
    writer.add_metadata({
        "/Title": "电子发票 发票号码：26952000001672381651 开票日期：2026年07月01日",
        "/Subject": "购买方 北京理工大学教育基金会 销售方 深圳市测试有限公司 价税合计",
    })
    with pdf.open("wb") as f:
        writer.write(f)

    r = scan_folder(tmp_path)
    assert r["invoice_pdf_count"] == 1
    assert len(r["groups"]) == 1
    assert r["groups"][0]["files"][0]["type"] == "invoice_pdf"
    assert not r["ignored"]


def test_extract_invoice_no_from_tax_verification_pdf_metadata(tmp_path):
    from pypdf import PdfWriter
    from tidoc.services.folder_import import extract_pdf_invoice_no

    pdf = tmp_path / "查验单.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=842, height=595)
    writer.add_metadata({
        "/Title": "国家税务总局全国增值税发票查验平台",
        "/Subject": "发票号码：26952000001672381651",
    })
    with pdf.open("wb") as f:
        writer.write(f)

    assert extract_pdf_invoice_no(pdf) == "26952000001672381651"


def test_extract_invoice_no_accepts_ocr_spaced_digits(tmp_path):
    from pypdf import PdfWriter
    from tidoc.services.folder_import import extract_pdf_invoice_no

    pdf = tmp_path / "查验单.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=842, height=595)
    writer.add_metadata({
        "/Title": "国家税务总局全国增值税发票查验平台",
        "/Subject": "261 17000000609873879 北京理工大学教育基金会",
    })
    with pdf.open("wb") as f:
        writer.write(f)

    assert extract_pdf_invoice_no(pdf) == "26117000000609873879"


def test_extract_payment_amount_from_ocr_text():
    from tidoc.services.folder_import import _payment_amount_from_text

    assert _payment_amount_from_text("全部账单\n-128.62\n支付成功") == "128.62"
    assert _payment_amount_from_text("交易详情\n- ¥816.84\n余额¥4,640.69") == "816.84"
    assert _payment_amount_from_text("深圳市立创电子商务有限公司\n- ￥45.74") == "45.74"
    assert _payment_amount_from_text("全 部 账 单 先 用 后 付 一 128 · 62 支 付 成 功") == "128.62"
    assert _payment_amount_from_text("账 单 管 理 一 83 ． 1 0 交 易 成 功") == "83.10"
    assert _payment_amount_from_text(
        "Ik**8\n-34.20\n支付成功\n光伏熔断器 ZTPV-25 熔芯R015"
    ) == "34.20"
    assert _payment_amount_from_text(
        "Ik**8-34.20支付成功光伏熔断器ZTPV-25"
    ) == "34.20"
    assert _payment_amount_from_text(
        "商品型号 ZTPV -25\n-34.20\n支付成功"
    ) == "34.20"


def test_material_binding_suggestion_requires_unique_match():
    from tidoc.services.folder_import import suggest_material_bindings

    entries = [
        {"id": "a", "invoice_no": "111", "total": "128.62"},
        {"id": "b", "invoice_no": "222", "total": "200.13"},
        {"id": "c", "invoice_no": "333", "total": "200.13"},
    ]
    planned = suggest_material_bindings([
        {"path": "pay-a.jpg", "type": "payment_screenshot", "paid_amount": "128.62"},
        {"path": "pay-duplicate.jpg", "type": "payment_screenshot", "paid_amount": "200.13"},
        {"path": "pay-missing.jpg", "type": "payment_screenshot", "paid_amount": ""},
        {"path": "pay-none.jpg", "type": "payment_screenshot", "paid_amount": "9.99"},
        {"path": "inspection.pdf", "type": "inspection_pdf", "invoice_no": "111"},
    ], entries)

    assert planned[0]["suggested_entry_id"] == "a"
    assert planned[1]["suggested_entry_id"] == ""
    assert "2 个金额相同" in planned[1]["binding_reason"]
    assert "未识别到付款金额" in planned[2]["binding_reason"]
    assert "没有金额相同" in planned[3]["binding_reason"]
    assert planned[4]["suggested_entry_id"] == "a"


def test_payment_binding_has_no_false_warning_when_ocr_is_disabled():
    from tidoc.services.folder_import import suggest_material_bindings

    planned = suggest_material_bindings(
        [{"path": "pay.jpg", "type": "payment_screenshot", "paid_amount": ""}],
        [{"id": "a", "total": "32.29"}],
        payment_ocr_enabled=False,
    )

    assert planned[0]["suggested_entry_id"] == ""
    assert planned[0]["binding_reason"] == ""


def test_windows_payment_ocr_runs_powershell_hidden(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from tidoc.services import folder_import

    calls = []

    def fake_run(cmd, **kwargs):
        script_path = cmd[cmd.index("-File") + 1]
        calls.append((cmd, kwargs, open(script_path, encoding="utf-8").read()))
        return SimpleNamespace(stdout="-12.34", stderr="")

    image = tmp_path / "付款截图.png"
    image.write_bytes(b"img")
    monkeypatch.setattr(folder_import.sys, "platform", "win32")
    monkeypatch.setattr(folder_import.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(folder_import.subprocess, "run", fake_run)

    assert folder_import.extract_payment_image_amount(image) == "12.34"
    cmd, kwargs, script = calls[0]
    assert cmd[0] == "powershell.exe"
    assert "-WindowStyle" in cmd
    assert "Hidden" in cmd
    assert kwargs["creationflags"] == 0x08000000
    assert "Prepare-PaymentImage" in script
    assert "$maxDimension = 2400.0" in script


def test_windows_tax_verification_ocr_runs_powershell_hidden(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from tidoc.services import folder_import

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(stdout="发票号码：26952000001672381651", stderr="")

    pdf = tmp_path / "查验单.pdf"
    pdf.write_bytes(b"pdf")
    monkeypatch.setattr(folder_import.sys, "platform", "win32")
    monkeypatch.setattr(folder_import.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(folder_import.subprocess, "run", fake_run)

    assert folder_import.extract_pdf_invoice_no(pdf) == "26952000001672381651"
    cmd, kwargs = calls[0]
    assert "-WindowStyle" in cmd
    assert "Hidden" in cmd
    assert kwargs["creationflags"] == 0x08000000


def test_api_classifies_inspection_pdf_material_invoice_no(api, tmp_path):
    from pypdf import PdfWriter

    pdf = tmp_path / "26.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=842, height=595)
    writer.add_metadata({
        "/Title": "国家税务总局全国增值税发票查验平台",
        "/Subject": "发票号码：26952000001672381651",
    })
    with pdf.open("wb") as f:
        writer.write(f)

    result = api.classify_material_files([str(pdf)])["data"]
    assert result[0]["type"] == "inspection_pdf"
    assert result[0]["invoice_no"] == "26952000001672381651"


def test_scan_folder_does_not_match_short_numeric_pdf_stem_to_xml(tmp_path):
    (tmp_path / "26.pdf").write_bytes(b"not a real pdf")
    (tmp_path / "立创商城发票-7556653A-26957000000103383662.xml").write_text("<bad/>", encoding="utf-8")

    r = scan_folder(tmp_path)
    assert r["invoice_pdf_count"] == 1
    assert r["matched_xml_count"] == 0
    assert r["groups"][0]["files"][0]["name"] == "26.pdf"
    assert len(r["groups"][0]["files"]) == 1
    assert r["ungrouped"][0]["type"] == "invoice_xml"


@requires_sample_data
def test_known_messy_sample_pdfs_parse_core_fields():
    samples = [
        ("26+稳压电源+128.62+发票.pdf", "深圳市驿生胜利科技有限公司", "直流稳压电源"),
        ("27+pc耗材+200.13+发票.pdf", "深圳拓竹科技有限公司", "3D打印机线材"),
        ("26332000004713530326-北京理工大学教育基金会.pdf", "杭州深度求索人工智能基础技术研究有限公司", "技术服务"),
        ("71+排插+107+发票.pdf", "北京京东金禾贸易有限公司", "公牛"),
    ]
    for filename, seller, item_keyword in samples:
        inv = parse_pdf(f"{SAMPLE_DIR}/{filename}")
        assert inv.invoice_no
        assert inv.buyer_name == "北京理工大学教育基金会"
        assert inv.seller == seller
        assert inv.items
        assert item_keyword in inv.items[0].actual_name


@requires_sample_data
def test_batch_create_entries(api):
    from tidoc.services import scan_folder as sf
    p = api.create_profile("张三", "李老师")["data"]
    scan = sf(SAMPLE_DIR)
    # 取前两组做批量创建
    picked = scan["groups"][:2]
    groups = [{"label": g["label"],
               "files": [{"path": f["path"], "type": f["type"]} for f in g["files"]]}
              for g in picked]
    res = api.batch_create_entries(p["id"], groups)["data"]
    assert res["created"] == 2
    assert len(res["created_entries"]) == 2
    assert {item["group"] for item in res["created_entries"]} == {g["label"] for g in picked}
    assert not res["failed"]
    for eid in res["entry_ids"]:
        e = api.get_entry(eid)["data"]
        assert e["has_invoice"]
        assert not e["has_payment"]
        assert not e["has_inspection"]

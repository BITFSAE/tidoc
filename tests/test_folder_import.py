"""文件夹批量导入的分组启发式测试。"""

import re
from decimal import Decimal
from pathlib import Path

import pytest

from tidoc.services import scan_files, scan_folder
from tidoc.engine import parse_pdf

# 依赖真实发票样本的测试通过 conftest 里的 sample_dir 夹具取得目录：
# 设置环境变量 TIDOC_INVOICE_SAMPLES 才会运行，否则跳过。


def test_scan_folder_groups_by_prefix(sample_dir):
    r = scan_folder(sample_dir)
    # 批量导入按发票 PDF 建条目；付款截图/查验单不靠命名自动绑定。
    assert r["total_files"] > 0
    assert len(r["groups"]) >= 10

    g26 = next((g for g in r["groups"] if any(f["name"].startswith("26") for f in g["files"])), None)
    assert g26 is not None
    types = {f["type"] for f in g26["files"]}
    assert "invoice_pdf" in types
    assert "payment_screenshot" not in types
    assert "inspection_pdf" not in types


def test_scan_folder_type_classification(sample_dir):
    r = scan_folder(sample_dir)
    for g in r["groups"]:
        for f in g["files"]:
            assert f["type"] in {"invoice_pdf", "invoice_xml"}
    assert any(f["type"] == "payment_screenshot" for f in r["ignored"])
    assert any(f["type"] == "inspection_pdf" for f in r["ignored"])
    assert r["matched_xml_count"] > 0


def test_scan_files_accepts_multi_selected_invoice_files(sample_dir):
    # 样本里有「发票 PDF + 同号 XML」成对的文件时，只多选这两个文件也应配成一组。
    paired = next(
        (
            g for g in scan_folder(sample_dir)["groups"]
            if {f["type"] for f in g["files"]} == {"invoice_pdf", "invoice_xml"}
        ),
        None,
    )
    if paired is None:
        pytest.skip("样本目录里没有成对的发票 PDF 和 XML")
    paths = [f["path"] for f in paired["files"]]
    for selection in (paths, paths[::-1]):
        r = scan_files(selection)
        assert r["invoice_pdf_count"] == 1
        assert r["matched_xml_count"] == 1
        assert len(r["groups"]) == 1
        assert {f["type"] for f in r["groups"][0]["files"]} == {"invoice_pdf", "invoice_xml"}


def test_scan_files_pairs_selected_pdf_and_xml_by_invoice_number(tmp_path):
    number = "12345678901234567890"
    pdf = tmp_path / f"{number}.pdf"
    xml = tmp_path / f"数电发票-{number}.xml"
    other = tmp_path / "98765432109876543210.pdf"
    for path in (pdf, xml, other):
        path.write_bytes(b"placeholder")

    for selection in ([pdf, xml], [xml, pdf]):
        r = scan_files(selection)
        assert r["invoice_pdf_count"] == 1
        assert r["matched_xml_count"] == 1
        assert len(r["groups"]) == 1
        assert {f["type"] for f in r["groups"][0]["files"]} == {"invoice_pdf", "invoice_xml"}
        # 没有被选中的文件不会混进来。
        assert other.name not in {f["name"] for f in r["groups"][0]["files"]}

    r = scan_files([pdf, xml, other])
    assert r["invoice_pdf_count"] == 2
    assert r["matched_xml_count"] == 1
    assert len(r["groups"]) == 2


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
        payment_ai_enabled=False,
    )

    assert planned[0]["suggested_entry_id"] == ""
    assert planned[0]["binding_reason"] == ""


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


def test_sample_pdfs_parse_core_fields(sample_pdfs):
    """样本集里每张发票 PDF 都能解析出核心字段，且金额自洽。

    只断言通用性质，不依赖具体样本内容：
    - 发票号码是 20 位数字，购买方、销售方、第一条明细名称非空，明细名称已去掉 *分类* 星号；
    - 各条明细金额之和等于价税合计；
    - 样本若按「序号+类别+金额+发票.pdf」命名，文件名里的金额应等于解析出的价税合计。
    """
    name_amount = re.compile(r"\+(\d+(?:\.\d+)?)\+发票\.pdf$")
    problems = []
    for path in sample_pdfs:
        name = Path(path).name
        inv = parse_pdf(path)
        if not re.fullmatch(r"\d{20}", inv.invoice_no):
            problems.append((name, "发票号码"))
        if not inv.buyer_name:
            problems.append((name, "购买方"))
        if not inv.seller:
            problems.append((name, "销售方"))
        if not inv.items:
            problems.append((name, "明细"))
            continue
        if not inv.items[0].actual_name or inv.items[0].actual_name.startswith("*"):
            problems.append((name, "明细名称"))
        if sum((item.total for item in inv.items), Decimal("0")) != inv.total:
            problems.append((name, "明细合计与价税合计不一致"))
        match = name_amount.search(name)
        if match and Decimal(match.group(1)) != inv.total:
            problems.append((name, "价税合计与文件名金额不一致"))
    assert not problems, problems


def test_batch_create_entries(sample_dir, api):
    from tidoc.services import scan_folder as sf
    p = api.create_profile("张三", "李老师")["data"]
    scan = sf(sample_dir)
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

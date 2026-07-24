import sys
import time
from decimal import Decimal
from types import SimpleNamespace

from tidoc.engine.models import ParsedInvoice
from tidoc.services.invoice_verification import (
    build_verification_info,
    changed_pdf_candidates,
    make_prefill_script,
    make_print_compatibility_script,
    snapshot_pdfs,
)


def test_verification_info_uses_entry_total(repos):
    profile = repos["profiles"].create("张三", "李老师")
    parsed = ParsedInvoice(
        invoice_no="26957000000168907686",
        invoice_date="2026-07-13",
        total=Decimal("76.32"),
    )
    entry_id = repos["entries"].create(profile["id"], parsed=parsed)

    info = build_verification_info(repos["entries"].get(entry_id))

    assert info["invoice_no"] == "26957000000168907686"
    assert info["invoice_date"] == "20260713"
    assert info["verification_value"] == "76.32"
    assert info["verification_value_label"] == "价税合计"
    assert info["complete"] is True


def test_prefill_script_only_fills_and_focuses_captcha():
    script = make_prefill_script({
        "invoice_no": "26957000000168907686",
        "invoice_date": "20260713",
        "verification_value": "76.32",
    })

    assert "document.getElementById('yzm')" in script
    assert "checkfp.click" not in script
    assert "fpdm" not in script
    assert "26957000000168907686" in script
    assert "76.32" in script
    assert "不区分大小写" in script
    assert "点击官网“打印”" in script
    assert "自动归入条目" in script
    assert "datepicker('setDate'" in script
    assert "datepicker('hide'" in script
    assert "input.blur()" in script
    assert "FocusEvent('focusout'" in script


def test_print_compatibility_uses_top_level_native_print():
    script = make_print_compatibility_script("26957000000168907686")

    assert "jq.fn.printArea = nativePrintArea" in script
    assert "topWindow.print()" in script
    assert "@media print" in script
    assert "size: A4 landscape" in script
    assert "tidoc-native-print-host" in script
    assert "querySelectorAll('iframe')" in script
    assert "MutationObserver" in script
    assert "checkfp" not in script
    assert "查验单-26957000000168907686" in script
    assert "topDocument.title = printTitle" in script


def test_changed_pdf_candidates_only_returns_session_changes(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    old = downloads / "old.pdf"
    old.write_bytes(b"%PDF-old")
    before = snapshot_pdfs([downloads])
    started_ns = time.time_ns()

    fresh = downloads / "fresh.pdf"
    fresh.write_bytes(b"%PDF-new")

    assert changed_pdf_candidates([downloads], before, started_ns) == [fresh.resolve()]


def test_verification_status_auto_attaches_native_printed_pdf(
    api, tmp_path, monkeypatch
):
    from tidoc.services import folder_import, invoice_verification

    profile = api.create_profile("张三", "李老师")["data"]
    entry_id = api.entries.create(
        profile["id"],
        parsed=ParsedInvoice(
            invoice_no="26957000000168907686",
            invoice_date="2026-07-13",
            total=Decimal("76.32"),
        ),
    )
    printed = tmp_path / "查验结果.pdf"
    printed.write_bytes(b"%PDF-native-print")

    class FakeWindow:
        def __init__(self):
            self.destroyed = False

        def destroy(self):
            self.destroyed = True

    window = FakeWindow()
    api._verification_sessions["session"] = {
        "entry_id": entry_id,
        "invoice_no": "26957000000168907686",
        "window": window,
        "window_closed": False,
        "ssl_bypass_acquired": False,
        "attached": None,
        "watch_directories": [tmp_path],
        "before": {},
        "started_ns": time.time_ns(),
        "candidate_sizes": {},
        "seen_candidates": set(),
        "last_message": "",
    }
    monkeypatch.setattr(
        invoice_verification, "changed_pdf_candidates", lambda *_args: [printed]
    )
    monkeypatch.setattr(
        folder_import,
        "classify_pdf_attachment_type",
        lambda _path: "inspection_pdf",
    )
    monkeypatch.setattr(
        folder_import,
        "extract_pdf_invoice_no",
        lambda _path: "26957000000168907686",
    )

    first = api.invoice_verification_status("session")
    second = api.invoice_verification_status("session")

    assert first["data"]["state"] == "processing"
    assert second["data"]["state"] == "attached"
    attachment = second["data"]["attachment"]
    assert attachment["type"] == "inspection_pdf"
    assert attachment["note"] == "由全国增值税发票查验平台原生打印保存"
    assert window.destroyed is True


def test_api_opens_official_site_only_after_explicit_start(
    api, tmp_path, monkeypatch
):
    class FakeEvent:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

        def wait(self, timeout=None):
            return True

    class FakeWindow:
        def __init__(self):
            self.events = SimpleNamespace(loaded=FakeEvent(), closed=FakeEvent())
            self.destroyed = False
            self.script = ""

        def evaluate_js(self, script):
            self.script = script

        def destroy(self):
            self.destroyed = True

    fake_window = FakeWindow()
    created = {}

    def create_window(title, **kwargs):
        created["ignored_ssl_errors_while_opening"] = webview.settings[
            "IGNORE_SSL_ERRORS"
        ]
        created.update(title=title, **kwargs)
        return fake_window

    import webview
    original_ignore_ssl_errors = webview.settings["IGNORE_SSL_ERRORS"]
    monkeypatch.setattr(webview, "create_window", create_window)
    profile = api.create_profile("张三", "李老师")["data"]
    entry_id = api.entries.create(
        profile["id"],
        parsed=ParsedInvoice(
            invoice_no="26957000000168907686",
            invoice_date="2026-07-13",
            total=Decimal("76.32"),
        ),
    )

    result = api.start_invoice_verification(entry_id, {
        "invoice_no": "26957000000168907686",
        "invoice_date": "20260713",
        "verification_value": "76.32",
        "watch_directory": str(tmp_path),
    })

    assert result["ok"] is True
    assert created["url"] == "https://inv-veri.chinatax.gov.cn/index.html"
    assert "maximized" not in created
    assert created["width"] == 1360
    assert created["height"] == 860
    assert created["ignored_ssl_errors_while_opening"] is True
    assert webview.settings["IGNORE_SSL_ERRORS"] is True
    assert str(tmp_path.resolve()) in result["data"]["watch_directories"]
    assert "查验单-26957000000168907686" in fake_window.script
    assert api.close_invoice_verification(result["data"]["session_id"])["ok"] is True
    assert fake_window.destroyed is True
    assert webview.settings["IGNORE_SSL_ERRORS"] is original_ignore_ssl_errors


def test_verification_ssl_bypass_waits_for_last_window(api):
    import webview

    original_ignore_ssl_errors = webview.settings["IGNORE_SSL_ERRORS"]
    first = {"ssl_bypass_acquired": False}
    second = {"ssl_bypass_acquired": False}
    try:
        api._acquire_verification_ssl_bypass(first)
        api._acquire_verification_ssl_bypass(second)

        api._release_verification_ssl_bypass(first)
        assert webview.settings["IGNORE_SSL_ERRORS"] is True

        api._release_verification_ssl_bypass(second)
        assert webview.settings["IGNORE_SSL_ERRORS"] is original_ignore_ssl_errors
    finally:
        api._release_verification_ssl_bypass(first)
        api._release_verification_ssl_bypass(second)
        webview.settings["IGNORE_SSL_ERRORS"] = original_ignore_ssl_errors


def test_verification_print_info_uses_landscape(api):
    if sys.platform != "darwin":
        return
    import AppKit

    info = AppKit.NSPrintInfo.sharedPrintInfo().copy()
    api._configure_verification_print_info(info)

    assert int(info.orientation()) == AppKit.NSPaperOrientationLandscape
    assert info.paperSize().width > info.paperSize().height

import sys
import time
from decimal import Decimal
from types import SimpleNamespace

from tidoc.engine.models import ParsedInvoice
from tidoc.services.invoice_verification import (
    build_verification_info,
    changed_pdf_candidates,
    make_finish_pdf_export_script,
    make_prefill_script,
    make_print_compatibility_script,
    make_trigger_print_script,
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
    assert "回到 tidoc 保存到条目" in script


def test_print_compatibility_uses_top_level_native_print():
    script = make_print_compatibility_script()

    assert "jq.fn.printArea = nativePrintArea" in script
    assert "topWindow.print()" in script
    assert "@media print" in script
    assert "tidoc-native-print-host" in script
    assert "querySelectorAll('iframe')" in script
    assert "MutationObserver" in script
    assert "checkfp" not in script


def test_trigger_print_script_reuses_visible_result():
    script = make_trigger_print_script()

    assert "button.click()" in script
    assert "querySelectorAll('iframe')" in script
    assert "打印" in script
    assert "checkfp" not in script


def test_direct_pdf_export_prepares_without_print_dialog():
    script = make_trigger_print_script(suppress_print=True)
    finish = make_finish_pdf_export_script()

    assert "window.__tidocSuppressPrint = true" in script
    assert "tidoc-native-exporting" in script
    assert "width" in script and "height" in script
    assert "classList.remove('tidoc-native-exporting')" in finish


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


def test_api_opens_official_site_only_after_explicit_start(api, monkeypatch):
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
    })

    assert result["ok"] is True
    assert created["url"] == "https://inv-veri.chinatax.gov.cn/index.html"
    assert created["ignored_ssl_errors_while_opening"] is True
    assert webview.settings["IGNORE_SSL_ERRORS"] is True
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


def test_native_verification_pdf_export_writes_callback_data(api, tmp_path, monkeypatch):
    if sys.platform != "darwin":
        return
    import Foundation
    from PyObjCTools import AppHelper

    payload = b"%PDF-1.7\n%%EOF\n"
    pdf_data = Foundation.NSData.dataWithBytes_length_(payload, len(payload))

    class FakeNativeWebView:
        def createPDFWithConfiguration_completionHandler_(self, config, callback):
            assert config.rect().size.width == 1120
            assert config.rect().size.height == 700
            callback(pdf_data, None)

    class FakeNativeWindow:
        def contentView(self):
            return FakeNativeWebView()

    monkeypatch.setattr(
        AppHelper, "callAfter", lambda function, *args: function(*args)
    )
    destination = tmp_path / "inspection.pdf"

    api._export_verification_webview_pdf(
        SimpleNamespace(native=FakeNativeWindow()),
        destination,
        1120,
        700,
    )

    assert destination.read_bytes() == payload


def test_verification_pdf_export_dispatches_to_windows(api, tmp_path, monkeypatch):
    called = {}

    def fake_windows_export(window, destination):
        called["window"] = window
        called["destination"] = destination

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        type(api),
        "_export_verification_webview_pdf_windows",
        staticmethod(fake_windows_export),
    )
    window = SimpleNamespace()
    destination = tmp_path / "inspection.pdf"

    api._export_verification_webview_pdf(window, destination, 1120, 700)

    assert called == {"window": window, "destination": destination}


def test_windows_pdf_export_uses_webview2_landscape(api):
    import inspect

    source = inspect.getsource(api._export_verification_webview_pdf_windows)

    assert "PrintToPdfAsync" in source
    assert "CoreWebView2PrintOrientation.Landscape" in source
    assert "ShouldPrintBackgrounds = True" in source
    assert 'header != b"%PDF"' in source

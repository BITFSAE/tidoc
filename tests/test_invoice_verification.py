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
    verification_print_title,
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


def test_verification_print_title_sanitizes_invoice_number():
    assert (
        verification_print_title(" 26957/000000168907686 ")
        == "查验单-26957000000168907686"
    )


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
    import tidoc.api as api_module
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
    trashed = []
    api._verification_sessions["session"] = {
        "entry_id": entry_id,
        "invoice_no": "26957000000168907686",
        "window": window,
        "window_closed": False,
        "attached": None,
        "watch_directories": [tmp_path],
        "before": {},
        "started_ns": time.time_ns(),
        "candidate_sizes": {},
        "seen_candidates": set(),
        "last_message": "",
        "trash_source_after_archive": True,
    }
    monkeypatch.setattr(api_module, "send2trash", lambda path: trashed.append(path))
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
    assert second["data"]["source_trashed"] is True
    assert second["data"]["cleanup_warning"] == ""
    assert trashed == [str(printed)]
    assert window.destroyed is True


def test_verification_trash_failure_keeps_successful_attachment(
    api, tmp_path, monkeypatch
):
    import tidoc.api as api_module
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
    api._verification_sessions["session"] = {
        "entry_id": entry_id,
        "invoice_no": "26957000000168907686",
        "window": None,
        "window_closed": True,
        "attached": None,
        "watch_directories": [tmp_path],
        "before": {},
        "started_ns": time.time_ns(),
        "candidate_sizes": {str(printed): printed.stat().st_size},
        "seen_candidates": set(),
        "last_message": "",
        "trash_source_after_archive": True,
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

    def fail_to_trash(_path):
        raise OSError("系统拒绝移动")

    monkeypatch.setattr(api_module, "send2trash", fail_to_trash)

    result = api.invoice_verification_status("session")

    assert result["data"]["state"] == "attached"
    assert result["data"]["attachment"]["type"] == "inspection_pdf"
    assert result["data"]["source_trashed"] is False
    assert "系统拒绝移动" in result["data"]["cleanup_warning"]
    assert printed.exists()


def test_verification_without_readable_number_is_not_auto_attached(
    api, tmp_path, monkeypatch
):
    from tidoc.services import folder_import, invoice_verification

    invoice_no = "26957000000168907686"
    profile = api.create_profile("张三", "李老师")["data"]
    entry_id = api.entries.create(
        profile["id"],
        parsed=ParsedInvoice(
            invoice_no=invoice_no,
            invoice_date="2026-07-13",
            total=Decimal("76.32"),
        ),
    )
    generic = tmp_path / "国家税务总局全国增值税发票查验平台.pdf"
    generic.write_bytes(b"%PDF-native-print")
    api._verification_sessions["session"] = {
        "entry_id": entry_id,
        "invoice_no": invoice_no,
        "window": None,
        "window_closed": True,
        "attached": None,
        "watch_directories": [tmp_path],
        "before": {},
        "started_ns": time.time_ns(),
        "candidate_sizes": {str(generic): generic.stat().st_size},
        "seen_candidates": set(),
        "last_message": "",
        "trash_source_after_archive": False,
    }
    monkeypatch.setattr(
        invoice_verification, "changed_pdf_candidates", lambda *_args: [generic]
    )
    monkeypatch.setattr(
        folder_import,
        "classify_pdf_attachment_type",
        lambda _path: "inspection_pdf",
    )
    monkeypatch.setattr(
        folder_import,
        "extract_pdf_invoice_no",
        lambda _path: "",
    )

    result = api.invoice_verification_status("session")

    assert result["data"]["state"] == "window_closed"
    assert "未能确认发票号码" in result["data"]["message"]
    assert api.attachments.list(entry_id) == []


def test_verification_tidoc_filename_does_not_replace_content_confirmation(
    api, tmp_path, monkeypatch
):
    from tidoc.services import folder_import, invoice_verification

    invoice_no = "26957000000168907686"
    profile = api.create_profile("张三", "李老师")["data"]
    entry_id = api.entries.create(
        profile["id"],
        parsed=ParsedInvoice(
            invoice_no=invoice_no,
            invoice_date="2026-07-13",
            total=Decimal("76.32"),
        ),
    )
    printed = tmp_path / f"查验单-{invoice_no}.pdf"
    printed.write_bytes(b"%PDF-native-print")
    api._verification_sessions["session"] = {
        "entry_id": entry_id,
        "invoice_no": invoice_no,
        "window": None,
        "window_closed": True,
        "attached": None,
        "watch_directories": [tmp_path],
        "before": {},
        "started_ns": time.time_ns(),
        "candidate_sizes": {str(printed): printed.stat().st_size},
        "seen_candidates": set(),
        "last_message": "",
        "trash_source_after_archive": False,
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
        lambda _path: "",
    )

    result = api.invoice_verification_status("session")

    assert result["data"]["state"] == "window_closed"
    assert "未能确认发票号码" in result["data"]["message"]
    assert api.attachments.list(entry_id) == []


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
        created.update(title=title, **kwargs)
        return fake_window

    import webview
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

    preferences = api.set_invoice_verification_preferences({
        "watch_directory": str(tmp_path),
        "trash_source_after_archive": True,
    })
    assert preferences["ok"] is True

    result = api.start_invoice_verification(entry_id, {
        "invoice_no": "26957000000168907686",
        "invoice_date": "20260713",
        "verification_value": "76.32",
    })

    assert result["ok"] is True
    assert created["url"] == "https://inv-veri.chinatax.gov.cn/index.html"
    assert "maximized" not in created
    assert created["width"] == 1360
    assert created["height"] == 860
    assert str(tmp_path.resolve()) in result["data"]["watch_directories"]
    assert "查验单-26957000000168907686" in fake_window.script
    assert fake_window._tidoc_print_title == "查验单-26957000000168907686"
    session = api._verification_sessions[result["data"]["session_id"]]
    assert session["trash_source_after_archive"] is True
    assert api.close_invoice_verification(result["data"]["session_id"])["ok"] is True
    assert fake_window.destroyed is True


def test_verification_preferences_validate_custom_directory(api, tmp_path):
    valid = tmp_path / "printed"
    valid.mkdir()

    saved = api.set_invoice_verification_preferences({
        "watch_directory": str(valid),
        "trash_source_after_archive": True,
    })
    loaded = api.invoice_verification_preferences()

    assert saved["ok"] is True
    assert saved["data"]["watch_directory"] == str(valid.resolve())
    assert saved["data"]["trash_source_after_archive"] is True
    assert loaded["data"] == saved["data"]

    disabled = api.set_invoice_verification_preferences({
        "trash_source_after_archive": "0",
    })
    missing = api.set_invoice_verification_preferences({
        "watch_directory": str(tmp_path / "missing"),
    })
    inside_data = api.set_invoice_verification_preferences({
        "watch_directory": str(api.data_root.root),
    })

    assert disabled["data"]["trash_source_after_archive"] is False
    assert missing["ok"] is False
    assert "不存在" in missing["error"]
    assert inside_data["ok"] is False
    assert "不能位于 tidoc 数据目录内" in inside_data["error"]


def test_verification_print_info_uses_landscape(api):
    if sys.platform != "darwin":
        return
    import AppKit

    info = AppKit.NSPrintInfo.sharedPrintInfo().copy()
    api._configure_verification_print_info(info)

    assert int(info.orientation()) == AppKit.NSPaperOrientationLandscape
    assert info.paperSize().width > info.paperSize().height


def test_macos_verification_print_sets_native_job_title(api):
    if sys.platform != "darwin":
        return
    import AppKit

    class FakePrintOperation:
        def __init__(self):
            self.job_title = ""
            self.ran = False

        def setJobTitle_(self, title):
            self.job_title = title

        def runOperationModalForWindow_delegate_didRunSelector_contextInfo_(
            self, window, delegate, selector, context
        ):
            self.ran = True

    class FakeNativeWebView:
        def __init__(self):
            self.operation = FakePrintOperation()
            self.print_info = None

        def _printOperationWithPrintInfo_(self, info):
            self.print_info = info
            return self.operation

        def window(self):
            return None

    native_webview = FakeNativeWebView()
    api._run_macos_verification_print(
        native_webview, "查验单-26957000000168907686"
    )

    assert native_webview.operation.job_title == "查验单-26957000000168907686"
    assert native_webview.operation.ran is True
    assert (
        int(native_webview.print_info.orientation())
        == AppKit.NSPaperOrientationLandscape
    )

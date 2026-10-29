"""Windows 打印桥、异步输出和失败后的手动兜底。"""

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tidoc.api_verification import InvoiceVerificationApi
from tidoc.services import verification_windows


def test_windows_print_bridge_only_handles_its_own_session(monkeypatch):
    forwarded, printed = [], []

    class Browser:
        def on_script_notify(self, sender, args):
            forwarded.append(args.get_WebMessageAsJson())

    monkeypatch.setitem(sys.modules, 'webview.platforms.edgechromium', SimpleNamespace(EdgeChrome=Browser))
    verification_windows.install_pdf_print()
    installed = Browser.on_script_notify
    verification_windows.install_pdf_print()
    assert Browser.on_script_notify is installed
    browser = Browser()
    session = {'id': 'session', 'window_closed': False}
    browser.pywebview_window = SimpleNamespace(
        _tidoc_verification_session=session, _tidoc_save_pdf=printed.append,
    )

    def send(message):
        browser.on_script_notify(None, SimpleNamespace(get_WebMessageAsJson=lambda: json.dumps(message)))

    send([verification_windows.PRINT_MESSAGE, 'other-session'])
    send(['console', '{}', 'console'])
    send([verification_windows.PRINT_MESSAGE, 'session'])
    session['window_closed'] = True
    send([verification_windows.PRINT_MESSAGE, 'session'])
    assert printed == [browser]
    assert len(forwarded) == 2
    browser.pywebview_window = SimpleNamespace()
    send([verification_windows.PRINT_MESSAGE, 'session'])
    assert len(forwarded) == 3


@pytest.mark.parametrize('success', [True, False])
def test_windows_pdf_completion_defers_cleanup_and_preserves_manual_fallback(tmp_path, monkeypatch, success):
    api = InvoiceVerificationApi()
    directory = tempfile.TemporaryDirectory(dir=tmp_path)
    output = Path(directory.name) / '查验单.pdf'
    session = {'auto_save_dir': directory, 'auto_save_path': output, 'window_closed': False}
    calls, manual = [], []
    browser = SimpleNamespace(webview=SimpleNamespace(CoreWebView2=SimpleNamespace(ShowPrintUI=lambda: manual.append(True))))
    monkeypatch.setattr(verification_windows, 'run_pdf_print', lambda *args: calls.append(args))
    api._save_windows_verification_pdf(browser, session)
    api._save_windows_verification_pdf(browser, session)
    assert len(calls) == 1
    assert session['auto_save_printing']
    output.write_bytes(b'%PDF-test')
    api._cleanup_verification_temp(session)
    assert output.exists()
    calls[0][2](success)
    assert not session['auto_save_printing']
    assert not Path(directory.name).exists()
    if not success:
        assert session['auto_save_rejected']
        api._save_windows_verification_pdf(browser, session)
        assert manual == [True]


def test_windows_pdf_runtime_failure_keeps_system_print_available(tmp_path, monkeypatch):
    api = InvoiceVerificationApi()
    session = {'auto_save_path': tmp_path / 'output.pdf', 'window_closed': False}
    monkeypatch.setattr(verification_windows, 'run_pdf_print', lambda *args: (_ for _ in ()).throw(RuntimeError('runtime')))
    api._save_windows_verification_pdf(None, session)
    assert not session['auto_save_printing']
    assert session['auto_save_rejected']
    assert '点击官网' in session['last_message']

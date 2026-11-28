"""Finder open events must enter the same preview queue as secondary launches."""

import sys
import threading
from queue import SimpleQueue
from types import SimpleNamespace

from tidoc.app import _serve_instance_requests
from tidoc.services import macos_documents


def test_document_delegate_accepts_bindles_and_preserves_webview_behavior(monkeypatch, tmp_path):
    class BaseDelegate:
        def applicationShouldTerminate_(self, _app):
            return "webview-termination-policy"

    browser = SimpleNamespace(AppDelegate=BaseDelegate)
    monkeypatch.setattr(macos_documents.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "webview.platforms.cocoa", SimpleNamespace(BrowserView=browser))
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSApplicationDelegateReplySuccess=0, NSApplicationDelegateReplyFailure=1,
    ))
    paths = []
    replies = []
    macos_documents.install_document_handler(paths.append)
    delegate = browser.AppDelegate()
    app = SimpleNamespace(replyToOpenOrPrint_=replies.append)
    first = tmp_path / "中文 空格.tidoc"
    second = tmp_path / "第二份.TIDOC"
    other = tmp_path / "发票.pdf"
    directory = tmp_path / "文件夹.tidoc"
    for path in (first, second, other):
        path.write_bytes(b"fixture")
    directory.mkdir()

    assert delegate.applicationShouldTerminate_(app) == "webview-termination-policy"
    assert delegate.application_openFile_(app, str(first)) is True
    assert delegate.application_openFile_(app, str(other)) is False
    assert delegate.application_openFile_(app, str(directory)) is False
    assert delegate.application_openFile_(app, str(tmp_path / "缺失.tidoc")) is False
    delegate.application_openFiles_(app, [str(first), str(second)])
    delegate.application_openFiles_(app, [str(other), str(second)])
    assert paths == [str(first), str(first), str(second), str(second)]
    assert replies == [0, 1]


def test_non_macos_does_not_load_cocoa(monkeypatch):
    monkeypatch.setattr(macos_documents.sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "webview.platforms.cocoa", None)
    macos_documents.install_document_handler(lambda _path: None)


def test_native_open_is_queued_before_frontend_notification(tmp_path):
    bindle = tmp_path / "Finder 材料.tidoc"
    bindle.write_bytes(b"fixture")
    requests = SimpleQueue()
    requests.put(str(bindle))  # Event may arrive before the frontend is ready.
    paths = []
    actions = []
    stop = threading.Event()

    class Window:
        def show(self):
            actions.append("show")

        def restore(self):
            pass

        def evaluate_js(self, script):
            assert paths == [str(bindle)]
            actions.append(script)
            stop.set()
            raise RuntimeError("frontend is still loading")

    _serve_instance_requests(
        SimpleNamespace(pop_requests=lambda: []),
        SimpleNamespace(queue_launch_file=paths.append),
        Window(), stop, requests,
    )
    assert actions == ["show", "window.handleSecondaryLaunch && window.handleSecondaryLaunch();"]
    assert requests.empty()


def test_native_open_batches_multiple_files_before_notifying_frontend(tmp_path):
    first = tmp_path / "一.tidoc"
    second = tmp_path / "二.tidoc"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    requests = SimpleQueue()
    requests.put(str(first))
    requests.put(str(second))
    paths = []
    scripts = []
    stop = threading.Event()

    class Window:
        def show(self):
            pass

        def restore(self):
            pass

        def evaluate_js(self, script):
            assert paths == [str(first), str(second)]
            scripts.append(script)
            stop.set()

    _serve_instance_requests(
        SimpleNamespace(pop_requests=lambda: []),
        SimpleNamespace(queue_launch_file=paths.append),
        Window(), stop, requests,
    )
    assert scripts == ["window.handleSecondaryLaunch && window.handleSecondaryLaunch();"]

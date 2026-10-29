"""WebView2 查验单异步输出；不向远程页面暴露业务 API。"""

from __future__ import annotations

import json
from pathlib import Path

PRINT_MESSAGE = "_tidocVerificationPrint"


def install_pdf_print() -> None:
    """在 pywebview 分发前接收本窗口的打印通知，其余消息照常处理。"""
    from webview.platforms.edgechromium import EdgeChrome

    original = EdgeChrome.on_script_notify
    if getattr(original, "_tidoc_pdf_print", False):
        return

    def on_script_notify(browser, sender, args):
        window = browser.pywebview_window
        session = getattr(window, "_tidoc_verification_session", None)
        if session:
            try:
                message = json.loads(args.get_WebMessageAsJson())
            except (ValueError, TypeError):
                message = None
            if message == [PRINT_MESSAGE, session["id"]]:
                if not session.get("window_closed"):
                    window._tidoc_save_pdf(browser)
                return
        return original(browser, sender, args)

    on_script_notify._tidoc_pdf_print = True
    EdgeChrome.on_script_notify = on_script_notify


def run_pdf_print(browser, output: Path, completed) -> None:
    """必须在 WebView2 的 UI 线程调用；回调也回到同一线程。"""
    from System import Action, Boolean
    from System.Threading.Tasks import Task
    from Microsoft.Web.WebView2.Core import CoreWebView2PrintOrientation

    core = browser.webview.CoreWebView2
    settings = core.Environment.CreatePrintSettings()
    settings.Orientation = CoreWebView2PrintOrientation.Landscape
    settings.PageWidth = 210 / 25.4
    settings.PageHeight = 297 / 25.4
    # 缩放和居中由页面内容完成（见 make_print_compatibility_script），与 macOS 一致。
    settings.ScaleFactor = 1.0
    settings.ShouldPrintBackgrounds = True
    settings.ShouldPrintHeaderAndFooter = False
    # 页边距由官网打印兼容样式里的 @page 统一提供。
    settings.MarginTop = settings.MarginBottom = 0
    settings.MarginLeft = settings.MarginRight = 0

    def done(task):
        try:
            success = bool(task.Result)
        except Exception:
            success = False
        completed(success)

    core.PrintToPdfAsync(str(output), settings).ContinueWith(
        Action[Task[Boolean]](done), browser.syncContextTaskScheduler
    )

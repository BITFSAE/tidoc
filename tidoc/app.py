"""tidoc 应用入口。

双击即进主界面：PyWebView 原生窗口加载 web/index.html，后端 API 通过
window.pywebview.api 暴露给前端。不开本地 HTTP 端口（设计文档第 2、4 节）。
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import webview

from .api import Api

WEB_DIR = Path(__file__).parent / "web"
_STDERR_SUPPRESS_PATTERNS = (
    "NSSoftLinking - The function '_TSMMenuKeyTransWithModifiersBeginWithEvent'",
)


def _install_native_stderr_filter() -> None:
    """Filter known harmless macOS framework warnings emitted below Python."""
    if sys.platform != "darwin" or "--debug" in sys.argv:
        return
    read_fd, write_fd = os.pipe()
    saved_stderr = os.dup(2)
    os.dup2(write_fd, 2)
    os.close(write_fd)

    def pump() -> None:
        pending = b""
        while True:
            chunk = os.read(read_fd, 4096)
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                text = line.decode("utf-8", "replace")
                if not any(pattern in text for pattern in _STDERR_SUPPRESS_PATTERNS):
                    os.write(saved_stderr, line + b"\n")
        if pending:
            text = pending.decode("utf-8", "replace")
            if not any(pattern in text for pattern in _STDERR_SUPPRESS_PATTERNS):
                os.write(saved_stderr, pending)

    threading.Thread(target=pump, daemon=True).start()


def web_dir() -> Path:
    """Return the frontend resource directory in source and PyInstaller builds."""
    candidates: list[Path] = []
    bundle_root = getattr(sys, "_MEIPASS", "")
    if bundle_root:
        root = Path(bundle_root)
        candidates.extend((root / "tidoc" / "web", root / "web"))
    candidates.extend((
        WEB_DIR,
        Path(sys.executable).resolve().parent / "tidoc" / "web",
    ))
    for path in candidates:
        if (path / "index.html").is_file():
            return path
    return candidates[0]


def web_app_url(index: Path | None = None) -> str:
    """入口必须保持纯 file URI；Windows WebView2 会把查询参数当作文件名。"""
    page = index or (web_dir() / "index.html")
    return page.as_uri()


def main() -> None:
    _install_native_stderr_filter()
    from .db.paths import resolve_data_root
    api = Api(resolve_data_root())
    index = web_dir() / "index.html"
    # 在线查验允许官网发起下载；PyWebView 会展示系统保存对话框，
    # 主窗口不会自动访问网络或静默写入下载目录。
    webview.settings["ALLOW_DOWNLOADS"] = True
    window = webview.create_window(
        "tidoc",
        url=web_app_url(index),
        js_api=api,
        width=1160,
        height=780,
        min_size=(920, 640),
    )
    api.bind_window(window)
    debug = "--debug" in sys.argv
    webview.start(debug=debug)


if __name__ == "__main__":
    main()

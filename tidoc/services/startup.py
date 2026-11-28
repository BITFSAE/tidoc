"""Offline startup recovery and read-only checks before an update rollback."""

from __future__ import annotations

import sqlite3
import webbrowser
from contextlib import closing
from html import escape

from tidoc import __version__
from tidoc.db.paths import resolve_data_root
from tidoc.db.schema import DatabaseVersionError, SCHEMA_VERSION, schema_version
from tidoc.single_instance import SingleInstance


def check_data_compatibility() -> bool:
    """Report whether this build may open the current data without changing it.

    The check runs before an older program is restored over a failed update. It only
    reads the version row: no migration, no journal-mode change and no main window.
    Any failure to prove compatibility (locked instance, unreadable file, invalid
    version) must count as incompatible so the caller keeps the newer program.
    """
    instance = SingleInstance()
    if not instance.acquire():
        return False
    try:
        path = resolve_data_root() / "tidoc.sqlite"
        if not path.exists():
            return True
        if not path.is_file():
            return False
        uri = path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            version = schema_version(conn)
            return version is None or version <= SCHEMA_VERSION
    finally:
        instance.release()


class StartupActions:
    def open_downloads(self, channel="stable"):
        url = (
            "https://github.com/BITFSAE/tidoc/releases"
            if channel == "beta"
            else "https://tidoc.bitfsae.com/start/install.html"
        )
        return webbrowser.open(url)


def startup_error_html(error: Exception | None = None, *, update_recovery=False) -> str:
    if update_recovery:
        title = "更新未完成"
        message = "新版没有成功打开，当前数据无法确认可供旧版使用。为保留现有数据，已停止自动退回旧版。"
        advice = "请重新安装当前版本或更新版本。旧程序和更新文件已保留，现有发票与材料也会保留。"
    elif isinstance(error, DatabaseVersionError):
        title = "请使用更新版本的 Tidoc"
        message = "这份数据已由更新版本的 Tidoc 处理，当前版本无法打开。现有数据已保留。"
        advice = "请安装此前使用的版本或更新版本。如果用过测试版，请先查看测试版下载。"
    else:
        title = "Tidoc 未能启动"
        message = "启动过程中发生错误，请重新安装当前版本或更新版本。"
        advice = "请保留现有数据目录。若仍无法打开，请向维护者提供启动方式和软件版本。"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><style>
:root {{ color-scheme: light dark; font: 14px/1.65 -apple-system,'Segoe UI',sans-serif; }}
body {{ margin: 0; padding: 32px; background: light-dark(#f4f1ea,#141414);
color: light-dark(#292725,#eeeeec); }}
h1 {{ font-size: 20px; margin: 0 0 18px; }}
p {{ margin: 12px 0; }}
small {{ opacity: .65; }}
.actions {{ display: flex; gap: 10px; margin-top: 24px; flex-wrap: wrap; }}
button {{ font: inherit; padding: 7px 13px; border-radius: 6px; cursor: pointer;
border: 1px solid light-dark(#c9c4ba,#555); background: light-dark(#fff,#292929);
color: inherit; }}
button:focus-visible {{ outline: 2px solid #5687b2; outline-offset: 3px; }}
</style></head><body><h1>{escape(title)}</h1><small>Tidoc {escape(__version__)}</small>
<p>{escape(message)}</p><p>{escape(advice)}</p><div class="actions">
<button onclick="window.pywebview.api.open_downloads('stable')">打开下载页面</button>
<button onclick="window.pywebview.api.open_downloads('beta')">查看测试版下载</button>
</div></body></html>"""


def show_startup_error(error: Exception | None = None, *, update_recovery=False) -> None:
    import webview

    webview.create_window(
        "Tidoc · 启动帮助",
        html=startup_error_html(error, update_recovery=update_recovery),
        js_api=StartupActions(),
        width=580,
        height=390,
        min_size=(480, 350),
    )
    webview.start()

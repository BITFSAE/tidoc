"""子进程启动参数。"""
from __future__ import annotations

import subprocess
import sys


def hidden_window_options() -> dict:
    """Windows 上无窗口应用启动控制台程序（打印/OCR 组件、PowerShell）会弹出黑色终端；
    返回把它藏起来的 subprocess 参数，其他平台为空。"""
    if not sys.platform.startswith("win"):
        return {}
    options: dict = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    startupinfo = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo is not None:
        info = startupinfo()
        info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        info.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        options["startupinfo"] = info
    return options

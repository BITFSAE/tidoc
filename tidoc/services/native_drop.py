"""拖入文件的真实路径。

页面拿到的 File 对象只有名字和内容，没有文件在磁盘上的位置；把内容读进页面、转成 base64 再
交给后端，每个文件要多出好几份内存副本。PyWebView 在注册了 Python 端的拖放监听后，会把
原生拖放里的文件路径随事件一起交给后端，这里把它们按文件名记下来，前端随后凭名字和大小认领。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable

# 页面事件和原生路径几乎同时到达；超过这个时间还没等到，就当没有原生路径，退回读取文件内容。
CLAIM_TIMEOUT_SECONDS = 1.0
RECORD_LIFETIME_SECONDS = 60.0
MAX_RECORDS = 512


class NativeDropRegistry:
    """线程安全：监听回调在 PyWebView 的工作线程里执行，认领发生在 API 调用线程里。"""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 lifetime: float = RECORD_LIFETIME_SECONDS, max_records: int = MAX_RECORDS):
        self.enabled = False
        self._clock = clock
        self._lifetime = lifetime
        self._max_records = max_records
        self._records: list[dict] = []
        self._changed = threading.Condition()

    def record(self, event) -> None:
        """PyWebView 拖放事件的回调；只收集带原生路径的文件。"""
        try:
            files = (event.get("dataTransfer") or {}).get("files") or []
        except AttributeError:
            return
        now = self._clock()
        with self._changed:
            self._prune(now)
            for item in files:
                path = item.get("pywebviewFullPath") if isinstance(item, dict) else None
                if not isinstance(path, str) or not path:
                    continue
                size = item.get("size")
                self._records.append({
                    "name": str(item.get("name") or ""),
                    "size": size if isinstance(size, int) else None,
                    "path": path,
                    "at": now,
                })
            del self._records[:-self._max_records]
            self._changed.notify_all()

    def claim(self, files: list[dict], timeout: float = CLAIM_TIMEOUT_SECONDS) -> list[str] | None:
        """按名字和大小取走这批文件的本机路径；缺任何一个、或有文件读不到时返回 None。"""
        if not self.enabled or not files:
            return None
        wanted = [(str(item.get("name") or ""), item.get("size")) for item in files]
        deadline = self._clock() + timeout
        with self._changed:
            while True:
                matched = self._match(wanted)
                if matched is not None:
                    for record in matched:
                        self._records.remove(record)
                    break
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return None
                self._changed.wait(remaining)
        paths = [record["path"] for record in matched]
        # 受系统保护的位置（如微信的缓存）、目录和已经被移走的文件走原来的读取流程，
        # 由它给出能看懂的提示。
        if not all(_readable_file(path) for path in paths):
            return None
        return paths

    def _match(self, wanted: list[tuple[str, object]]) -> list[dict] | None:
        available = list(self._records)
        matched: list[dict] = []
        for name, size in wanted:
            found = next((
                record for record in available
                if record["name"] == name
                and (size is None or record["size"] is None or record["size"] == size)
            ), None)
            if found is None:
                return None
            available.remove(found)
            matched.append(found)
        return matched

    def _prune(self, now: float) -> None:
        self._records = [record for record in self._records if now - record["at"] <= self._lifetime]


def _readable_file(path: str) -> bool:
    try:
        target = Path(path)
        if not target.is_file() or not os.access(target, os.R_OK):
            return False
        with target.open("rb"):
            return True
    except OSError:
        return False

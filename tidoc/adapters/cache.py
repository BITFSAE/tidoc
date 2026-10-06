"""Small bounded store for short-lived previews that are kept in memory."""
from __future__ import annotations

from collections import OrderedDict
import threading
import time


class PreviewCache:
    """Keeps the most recent previews for a limited time; older ones are dropped.

    A dropped preview looks like a missing one to callers, which already report it as
    "the preview expired, please preview again". Nothing here is persisted.
    """

    def __init__(self, max_items=8, ttl_seconds=3600, clock=time.monotonic):
        if max_items < 1 or ttl_seconds <= 0:
            raise ValueError('预览缓存的容量和有效期必须为正数。')
        self.max_items = max_items
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._items = OrderedDict()
        self._lock = threading.RLock()

    def _purge(self):
        now = self._clock()
        for key in [key for key, (expires, _) in self._items.items() if expires <= now]:
            del self._items[key]

    def __setitem__(self, key, value):
        with self._lock:
            self._purge()
            self._items.pop(key, None)
            self._items[key] = (self._clock() + self.ttl_seconds, value)
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)

    def __getitem__(self, key):
        with self._lock:
            self._purge()
            return self._items[key][1]

    def get(self, key, default=None):
        with self._lock:
            self._purge()
            item = self._items.get(key)
            return default if item is None else item[1]

    def pop(self, key, default=None):
        with self._lock:
            self._purge()
            item = self._items.pop(key, None)
            return default if item is None else item[1]

    def __contains__(self, key):
        with self._lock:
            self._purge()
            return key in self._items

    def __len__(self):
        with self._lock:
            self._purge()
            return len(self._items)

"""Single-instance lock and file-based activation handoff.

The lock is held by the open file descriptor, so a crashed process releases it
automatically. Secondary launches leave a small request file for the primary
process instead of opening a second SQLite connection and WebView window.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path


def runtime_dir() -> Path:
    """Return a stable, per-user runtime directory outside the movable data root."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "tidoc" / "runtime"
    if sys.platform.startswith("win"):
        base = Path(
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or tempfile.gettempdir()
        )
        return base / "tidoc" / "runtime"
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or Path.home() / ".cache")
    return base / "tidoc"


class SingleInstance:
    """Own the Tidoc process lock and exchange secondary-launch requests."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else runtime_dir()
        self.lock_path = self.root / "instance.lock"
        self.requests_dir = self.root / "requests"
        self._lock_file = None
        self.instance_id = ""
        self._target_instance_id = ""

    def acquire(self) -> bool:
        """Acquire the process lock without waiting; return False if already held."""
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        handle = self.lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # The owner writes its id immediately after locking. A competing process
            # can observe the lock a few milliseconds earlier, so retry briefly.
            for _attempt in range(10):
                handle.seek(1)
                self._target_instance_id = handle.read().decode("ascii", "ignore").strip()
                if self._target_instance_id:
                    break
                time.sleep(0.01)
            handle.close()
            return False
        self.instance_id = uuid.uuid4().hex
        handle.seek(1)
        handle.truncate()
        handle.write(self.instance_id.encode("ascii"))
        handle.flush()
        self._lock_file = handle
        return True

    def release(self) -> None:
        """Release the process lock if this object owns it."""
        handle = self._lock_file
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._lock_file = None
            self.instance_id = ""

    def send_activation(self, launch_file: str = "") -> None:
        """Queue an activation request for the running primary instance."""
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        request_id = f"{time.time_ns()}-{uuid.uuid4().hex}"
        pending = self.requests_dir / f".{request_id}.tmp"
        ready = self.requests_dir / f"{request_id}.json"
        payload = {
            "action": "activate",
            "target_instance_id": self._target_instance_id,
            "launch_file": str(launch_file or ""),
        }
        pending.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(pending, ready)

    def pop_requests(self) -> list[dict]:
        """Consume all complete requests currently waiting for the primary."""
        if not self.requests_dir.is_dir():
            return []
        requests = []
        for path in sorted(self.requests_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if (
                    isinstance(payload, dict)
                    and payload.get("action") == "activate"
                    and payload.get("target_instance_id") == self.instance_id
                ):
                    requests.append(payload)
            except (OSError, ValueError, TypeError):
                pass
            finally:
                try:
                    path.unlink()
                except OSError:
                    pass
        return requests

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("Tidoc is already running")
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.release()

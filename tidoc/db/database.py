"""SQLite connection, consistent backups and composable transactions."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import threading
import uuid

from .schema import init_db


def backup_connection(conn: sqlite3.Connection, target: str | Path) -> Path:
    """Use SQLite's backup API, including committed pages in WAL."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError('备份文件已存在。')
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with sqlite3.connect(str(temporary)) as destination:
            conn.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('数据库备份校验失败。')
        with temporary.open('rb') as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.adapter_service = None
        # Backup written by this start's schema upgrade, if any.
        self.migration_backup = None
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.execute('PRAGMA busy_timeout=5000')
        try:
            # Version refusal happens before journal mode or any schema write.
            self.migration_backup = init_db(self.conn)
            self.conn.execute('PRAGMA journal_mode=WAL')
            self.conn.execute('PRAGMA synchronous=NORMAL')
        except BaseException:
            self.conn.close()
            raise

    @contextmanager
    def transaction(self):
        """Nested callers use savepoints; only the outer scope commits."""
        with self._lock:
            nested = self.conn.in_transaction
            name = 'tidoc_' + uuid.uuid4().hex
            self.conn.execute('SAVEPOINT ' + name if nested else 'BEGIN IMMEDIATE')
            try:
                yield self.conn
                self.conn.execute('RELEASE SAVEPOINT ' + name) if nested else self.conn.commit()
            except BaseException:
                if nested:
                    self.conn.execute('ROLLBACK TO SAVEPOINT ' + name)
                    self.conn.execute('RELEASE SAVEPOINT ' + name)
                else:
                    self.conn.rollback()
                raise

    def backup(self, target: str | Path) -> Path:
        if self.conn.in_transaction:
            raise ValueError('请先完成当前保存再备份。')
        with self._lock:
            return backup_connection(self.conn, target)

    def close(self) -> None:
        self.conn.close()

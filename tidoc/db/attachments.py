"""附件（Attachment）仓库。设计文档第 5 节存储布局、8.2 上传。

把用户选的文件复制进 attachments/<entry_id>/，用规范文件名保存，算 sha256。
"""

from __future__ import annotations

import hashlib
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from .database import Database
from .paths import DataRoot

# 附件类型（设计文档第 5 节）
TYPE_INVOICE_PDF = "invoice_pdf"
TYPE_INVOICE_XML = "invoice_xml"
TYPE_PAYMENT = "payment_screenshot"
TYPE_INSPECTION = "inspection_pdf"
TYPE_OTHER = "other"

# 各类型的规范命名前缀
_NAME_PREFIX = {
    TYPE_INVOICE_PDF: "发票",
    TYPE_INVOICE_XML: "发票",
    TYPE_PAYMENT: "付款截图",
    TYPE_INSPECTION: "查验单",
    TYPE_OTHER: "附件",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class AttachmentRepo:
    def __init__(self, db: Database, data_root: DataRoot):
        self.db = db
        self.data_root = data_root

    def add(self, entry_id: str, src_path: str | Path, att_type: str, note: str = "") -> dict:
        src = Path(src_path)
        if not src.exists():
            raise FileNotFoundError(f"文件不存在：{src}")
        sha = _sha256(src)
        existing = self.db.conn.execute(
            "SELECT original_name FROM attachments WHERE entry_id = ? AND sha256 = ? LIMIT 1",
            (entry_id, sha),
        ).fetchone()
        if existing:
            raise ValueError(f"这份文件已添加过：{existing['original_name']}")
        att_id = uuid.uuid4().hex
        dest_dir = self.data_root.entry_dir(entry_id)
        stored_name = self._unique_name(dest_dir, entry_id, att_type, src.suffix)
        dest = dest_dir / stored_name
        try:
            shutil.copy2(src, dest)
            rel = f"{entry_id}/{stored_name}"
            self.db.conn.execute(
                """INSERT INTO attachments(id, entry_id, type, original_name, stored_path,
                   sha256, note, added_at) VALUES(?,?,?,?,?,?,?,?)""",
                (att_id, entry_id, att_type, src.name, rel, sha, note, _now()),
            )
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            if dest.exists():
                dest.unlink()
            _remove_empty_dir(dest_dir)
            raise
        return self.get(att_id)

    def _unique_name(self, dest_dir: Path, entry_id: str, att_type: str, suffix: str) -> str:
        prefix = _NAME_PREFIX.get(att_type, "附件")
        # 付款截图可多张，编号；其余同类型也编号避免覆盖
        existing = self.db.conn.execute(
            "SELECT COUNT(*) c FROM attachments WHERE entry_id = ? AND type = ?",
            (entry_id, att_type),
        ).fetchone()["c"]
        seq = existing + 1
        name = f"{prefix}_{seq:02d}{suffix}"
        while (dest_dir / name).exists():
            seq += 1
            name = f"{prefix}_{seq:02d}{suffix}"
        return name

    def get(self, att_id: str) -> dict:
        row = self.db.conn.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
        d = {k: row[k] for k in row.keys()} if row else {}
        if d:
            d["abs_path"] = str(self.data_root.attachments_dir / d["stored_path"])
        return d

    def list(self, entry_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM attachments WHERE entry_id = ? ORDER BY added_at", (entry_id,)
        ).fetchall()
        out = []
        for row in rows:
            d = {k: row[k] for k in row.keys()}
            d["abs_path"] = str(self.data_root.attachments_dir / d["stored_path"])
            out.append(d)
        return out

    def delete(self, att_id: str) -> dict:
        att = self.get(att_id)
        if not att:
            return {"cleanup_warning": ""}
        abs_path = Path(att["abs_path"])
        quarantine = abs_path.with_name(f".deleting-{att_id}-{abs_path.name}")
        moved = False
        if abs_path.exists():
            abs_path.rename(quarantine)
            moved = True
        try:
            self.db.conn.execute("DELETE FROM attachments WHERE id = ?", (att_id,))
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            if moved and quarantine.exists():
                quarantine.rename(abs_path)
            raise
        warning = ""
        if moved and quarantine.exists():
            try:
                quarantine.unlink()
            except OSError as exc:
                warning = f"附件记录已删除，但文件清理失败：{exc}"
        _remove_empty_dir(abs_path.parent)
        return {"cleanup_warning": warning}

    def set_note(self, att_id: str, note: str) -> dict:
        self.db.conn.execute("UPDATE attachments SET note = ? WHERE id = ?", (note, att_id))
        self.db.conn.commit()
        return self.get(att_id)

    def update(self, att_id: str, att_type: str | None = None,
               src_path: str | Path | None = None, note: str | None = None) -> dict:
        att = self.get(att_id)
        if not att:
            raise FileNotFoundError(f"附件不存在：{att_id}")

        new_type = att_type or att["type"]
        original_name = att["original_name"]
        stored_path = att["stored_path"]
        sha = att["sha256"]
        old_abs = Path(att["abs_path"])
        new_abs: Path | None = None
        rollback_rename = False

        if src_path:
            src = Path(src_path)
            if not src.exists():
                raise FileNotFoundError(f"文件不存在：{src}")
            new_sha = _sha256(src)
            dup = self.db.conn.execute(
                "SELECT original_name FROM attachments WHERE entry_id = ? AND sha256 = ? AND id <> ? LIMIT 1",
                (att["entry_id"], new_sha, att_id),
            ).fetchone()
            if dup:
                raise ValueError(f"这份文件已添加过：{dup['original_name']}")
            dest_dir = self.data_root.entry_dir(att["entry_id"])
            stored_name = self._unique_name(dest_dir, att["entry_id"], new_type, src.suffix)
            dest = dest_dir / stored_name
            try:
                shutil.copy2(src, dest)
            except Exception:
                if dest.exists():
                    dest.unlink()
                _remove_empty_dir(dest_dir)
                raise
            new_abs = dest
            original_name = src.name
            stored_path = f"{att['entry_id']}/{stored_name}"
            sha = new_sha
        elif att_type and att_type != att["type"]:
            if old_abs.exists():
                dest_dir = self.data_root.entry_dir(att["entry_id"])
                stored_name = self._unique_name(dest_dir, att["entry_id"], new_type, old_abs.suffix)
                dest = dest_dir / stored_name
                old_abs.rename(dest)
                new_abs = dest
                rollback_rename = True
                stored_path = f"{att['entry_id']}/{stored_name}"

        try:
            self.db.conn.execute(
                """UPDATE attachments
                   SET type = ?, original_name = ?, stored_path = ?, sha256 = ?,
                       note = COALESCE(?, note)
                   WHERE id = ?""",
                (new_type, original_name, stored_path, sha, note, att_id),
            )
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            if rollback_rename and new_abs and new_abs.exists():
                new_abs.rename(old_abs)
            elif new_abs and new_abs.exists():
                new_abs.unlink()
                _remove_empty_dir(new_abs.parent)
            raise

        updated = self.get(att_id)
        if src_path and old_abs.exists() and new_abs != old_abs:
            try:
                old_abs.unlink()
            except OSError as exc:
                updated["cleanup_warning"] = f"附件已替换，但旧文件清理失败：{exc}"
        return updated


def _remove_empty_dir(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass

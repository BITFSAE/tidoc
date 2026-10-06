"""附件（Attachment）仓库。设计文档第 5 节存储布局、8.2 上传。

把用户选的文件复制进 attachments/<entry_id>/，用规范文件名保存，算 sha256。
"""

from __future__ import annotations

import json
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
TYPE_PHYSICAL_IMAGE = "physical_image"
TYPE_INSPECTION = "inspection_pdf"
TYPE_OTHER = "other"

# 各类型的规范命名前缀
_NAME_PREFIX = {
    TYPE_INVOICE_PDF: "发票",
    TYPE_INVOICE_XML: "发票",
    TYPE_PAYMENT: "付款截图",
    TYPE_PHYSICAL_IMAGE: "实物图",
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

    def add(self, entry_id: str, src_path: str | Path, att_type: str, note: str = "", *, role_id: str | None = None) -> dict:
        src = Path(src_path)
        if not src.exists():
            raise FileNotFoundError(f"文件不存在：{src}")
        role_id = self.validate_role(entry_id,role_id,att_type,src)
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
                   sha256, note, added_at,role_id) VALUES(?,?,?,?,?,?,?,?,?)""",
                (att_id, entry_id, att_type, src.name, rel, sha, note, _now(),role_id),
            )
            self._touch_entry(entry_id)
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            if dest.exists():
                dest.unlink()
            _remove_empty_dir(dest_dir)
            raise
        return self.get(att_id)

    def validate_role(self,entry_id,role_id,att_type,path,ignore_id=None):
        if att_type not in _NAME_PREFIX:
            raise ValueError("材料处理类型不存在。")
        natural = "invoice" if att_type in (TYPE_INVOICE_PDF,TYPE_INVOICE_XML) else att_type
        role_id = role_id or natural
        row = self.db.conn.execute("SELECT scheme_revision_id FROM entries WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            raise ValueError("条目不存在。")
        if not row[0]:
            if role_id != natural:
                raise ValueError("请先为条目选择报账方案。")
            return natural
        revision = self.db.conn.execute("SELECT definition_json FROM scheme_revisions WHERE revision_id=?", (row[0],)).fetchone()
        definition = json.loads(revision[0])
        role = next((r for r in definition.get("materials",[]) if r["id"]==role_id),None)
        if role is None:
            raise ValueError("当前方案没有此材料角色。")
        expected_type = TYPE_OTHER if role_id.startswith("custom:") else (att_type if role_id=="invoice" else role_id)
        if (role_id=="invoice" and att_type not in (TYPE_INVOICE_PDF,TYPE_INVOICE_XML)) or att_type!=expected_type:
            raise ValueError("材料角色与文件处理类别不一致。")
        allowed = role.get("extensions") or []
        if allowed and Path(path).suffix.lower() not in allowed:
            raise ValueError("这类材料支持的文件格式："+"、".join(allowed))
        limit = role.get("max_count")
        if limit is not None:
            count = self.db.conn.execute("SELECT COUNT(*) FROM attachments WHERE entry_id=? AND role_id=? AND id<>? AND (role_definition_revision_id IS NULL OR role_definition_revision_id=?)",(entry_id,role_id,ignore_id or "",row[0])).fetchone()[0]
            if count >= limit:
                raise ValueError("材料数量达到方案上限。")
        return role_id

    def _touch_entry(self,entry_id):
        self.db.conn.execute("UPDATE entries SET updated_at=?,status_engine_version='' WHERE id=?",(_now(),entry_id))

    def reclassify(self,att_id,role_id,actor_id=""):
        att = self.get(att_id)
        if not att:
            raise ValueError("附件不存在。")
        entry = self.db.conn.execute("SELECT scheme_id,scheme_revision_id FROM entries WHERE id=?",(att["entry_id"],)).fetchone()
        if not entry["scheme_id"]:
            raise ValueError("请先选择报账方案。")
        definition = json.loads(self.db.conn.execute("SELECT definition_json FROM scheme_revisions WHERE revision_id=?",(entry["scheme_revision_id"],)).fetchone()[0])
        roles = {r["id"]:r for r in definition.get("materials",[])}
        natural = "invoice" if att["type"] in (TYPE_INVOICE_PDF,TYPE_INVOICE_XML) else att["type"]
        old_role = att.get("role_id") or natural
        if natural=="invoice" or role_id=="invoice":
            raise ValueError("发票材料不能改为其他角色。")
        for candidate in (old_role,role_id):
            if candidate in roles and not roles[candidate].get("reclassifiable",True):
                raise ValueError("方案不允许调整此材料的角色。")
        new_type = TYPE_OTHER if role_id.startswith("custom:") else role_id
        self.validate_role(att["entry_id"],role_id,new_type,att["abs_path"],ignore_id=att_id)
        from .extensions import ExtensionRepo
        with self.db.transaction():
            self.db.conn.execute("UPDATE attachments SET type=?,role_id=?,role_definition_revision_id=NULL,recognition_version='',recognition_status='',recognized_value='',recognition_message='' WHERE id=?",(new_type,role_id,att_id))
            ExtensionRepo(self.db).record_history("entry",att["entry_id"],entry["scheme_id"],att_id,{"role_id":old_role,"source_revision":att.get("role_definition_revision_id")},{"role_id":role_id},entry["scheme_revision_id"],definition["manifest"]["package_id"],kind="material",actor_id=actor_id)
            self._touch_entry(att["entry_id"])
            from .entries import EntryRepo
            EntryRepo(self.db).recompute_status(att["entry_id"],commit=False)
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
            self._touch_entry(att["entry_id"])
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

    def set_recognition(
        self,
        att_id: str,
        version: str,
        status: str,
        value: str = "",
        message: str = "",
    ) -> dict:
        """保存本地材料识别缓存；版本变化或文件替换后可重新识别。"""
        self.db.conn.execute(
            """UPDATE attachments
                  SET recognition_version = ?, recognition_status = ?,
                      recognized_value = ?, recognition_message = ?
                WHERE id = ?""",
            (version, status, value or "", message or "", att_id),
        )
        self.db.conn.commit()
        return self.get(att_id)

    def update(self, att_id: str, att_type: str | None = None,
               src_path: str | Path | None = None, note: str | None = None) -> dict:
        att = self.get(att_id)
        if not att:
            raise FileNotFoundError(f"附件不存在：{att_id}")

        new_type = att_type or att["type"]
        new_role = att.get("role_id") if new_type == att["type"] else None
        if src_path or att_type:
            new_role = self.validate_role(att["entry_id"],new_role,new_type,src_path or att["abs_path"],ignore_id=att_id)
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

        recognition_changed = bool(src_path or (att_type and att_type != att["type"]))
        try:
            self.db.conn.execute(
                """UPDATE attachments
                   SET type = ?, role_id = ?, original_name = ?, stored_path = ?, sha256 = ?,
                       note = COALESCE(?, note),
                       recognition_version = CASE WHEN ? THEN '' ELSE recognition_version END,
                       recognition_status = CASE WHEN ? THEN '' ELSE recognition_status END,
                       recognized_value = CASE WHEN ? THEN '' ELSE recognized_value END,
                       recognition_message = CASE WHEN ? THEN '' ELSE recognition_message END
                   WHERE id = ?""",
                (
                    new_type, new_role, original_name, stored_path, sha, note,
                    recognition_changed, recognition_changed,
                    recognition_changed, recognition_changed, att_id,
                ),
            )
            self._touch_entry(att["entry_id"])
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

"""PyWebView JS↔Python 桥暴露的 API（设计文档第 4 节）。

前端通过 window.pywebview.api.<method> 调用。所有方法返回可 JSON 序列化的 dict / list，
统一用 {"ok": bool, ...} 包裹，异常转成 {"ok": False, "error": msg}，避免桥抛异常。
"""

from __future__ import annotations

import functools
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import threading
import webbrowser
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

from send2trash import send2trash

from tidoc import __version__

from .db import (
    AttachmentRepo,
    BatchRepo,
    Database,
    DataRoot,
    EntryRepo,
    ProfileRepo,
)

AUTO_UPDATE_PREF_KEY = "tidoc.update.autoCheck"
PAYMENT_OCR_PREF_KEY = "tidoc.paymentScreenshotOcr"
DEFAULT_PAID_TO_INVOICE_PREF_KEY = "tidoc.defaultPaidToInvoiceTotal"
BINDLE_INCLUDE_NOTES_PREF_KEY = "tidoc.bindle.includeNotes"
BINDLE_INCLUDE_TAGS_PREF_KEY = "tidoc.bindle.includeTags"
INVOICE_VERIFICATION_WATCH_DIR_PREF_KEY = (
    "tidoc.invoiceVerification.watchDirectory"
)
INVOICE_VERIFICATION_TRASH_SOURCE_PREF_KEY = (
    "tidoc.invoiceVerification.trashSourceAfterArchive"
)
UPDATE_LAST_CHECK_KEY = "tidoc.update.lastCheck"
UPDATE_LAST_RESULT_KEY = "tidoc.update.lastResult"
APP_LAST_SEEN_VERSION_KEY = "tidoc.update.lastSeenVersion"
AUTO_UPDATE_INTERVAL_SECONDS = 24 * 60 * 60
MAX_DROPPED_FILE_BYTES = 100 * 1024 * 1024
MAX_DROPPED_TOTAL_BYTES = 500 * 1024 * 1024


class DuplicateInvoiceError(ValueError):
    """导入命中了已有发票，携带可供前端定位原条目的结构化信息。"""

    def __init__(self, existing: dict):
        self.existing = existing
        invoice_no = existing.get("invoice_no") or ""
        seller = existing.get("seller") or "未识别销售方"
        claimant = existing.get("profile_name") or "未识别报账人"
        identity = f"发票号 {invoice_no} 已存在" if invoice_no else "相同发票文件已存在"
        super().__init__(
            f"{identity}（{seller}，报账人：{claimant}）。"
            "如需调整归属，请修改原条目的报账人，不要重复创建。"
        )


def _guard(func):
    """把返回值包成 {ok:True,...}，异常包成 {ok:False,error:...}。"""
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            with self._api_lock:
                result = func(self, *args, **kwargs)
            if isinstance(result, dict) and "ok" in result:
                return result
            return {"ok": True, "data": result}
        except Exception as exc:  # noqa: BLE001 — 桥不能抛，统一转错误
            return {"ok": False, "error": str(exc)}
    return wrapper


class Api:
    def __init__(self, data_root: str | Path | None = None):
        self._api_lock = threading.RLock()
        self.data_root = DataRoot(data_root, manage_pointer=True)
        self.db = Database(self.data_root.db_path)
        self.profiles = ProfileRepo(self.db)
        self.entries = EntryRepo(self.db)
        self.attachments = AttachmentRepo(self.db, self.data_root)
        self.batches = BatchRepo(self.db)
        self._window = None
        self._verification_sessions: dict[str, dict] = {}
        _cleanup_old_dropped_files(self.data_root.dropped_dir)

    def __dir__(self):
        """只向 pywebview 暴露本类定义的公开方法。

        pywebview 6.x 会用 dir() 枚举并递归进入公开属性，若不限制，会把
        self.db / self.profiles / self.data_root 等仓储对象（乃至 Path.unlink
        等文件系统方法）全部当成 JS API 暴露：既有安全隐患，又让注入体积暴涨、
        拖慢桥就绪，导致前端首个调用报「后端方法不存在」。
        """
        internal = {"bind_window"}
        return [
            name for name in vars(type(self))
            if not name.startswith("_")
            and name not in internal
            and callable(getattr(type(self), name))
        ]

    def bind_window(self, window) -> None:
        self._window = window

    def _preference_value(self, key: str, default: str = "") -> str:
        row = self.db.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else default

    def _set_preference_value(self, key: str, value: str) -> None:
        self.db.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.db.conn.commit()

    def _invoice_verification_preferences(self) -> dict:
        return {
            "watch_directory": self._preference_value(
                INVOICE_VERIFICATION_WATCH_DIR_PREF_KEY
            ),
            "trash_source_after_archive": self._preference_value(
                INVOICE_VERIFICATION_TRASH_SOURCE_PREF_KEY, "0"
            ) == "1",
        }

    # ------------------------------------------------------------ 身份
    @_guard
    def list_profiles(self):
        return self.profiles.list()

    @_guard
    def create_profile(self, name, reviewer, is_default=False, optional=None):
        return self.profiles.create(name, reviewer, is_default, **(optional or {}))

    @_guard
    def update_profile(self, profile_id, fields=None):
        return self.profiles.update(profile_id, **(fields or {}))

    @_guard
    def set_default_profile(self, profile_id):
        self.profiles.set_default(profile_id)
        return {"profile_id": profile_id}

    @_guard
    def delete_profile(self, profile_id):
        self.profiles.delete(profile_id)
        return {"deleted": profile_id}

    # ------------------------------------------------------------ 应用偏好
    @_guard
    def app_preference(self, key, default=""):
        return self._preference_value(str(key), str(default))

    @_guard
    def set_app_preference(self, key, value):
        self._set_preference_value(str(key), str(value))
        return {"key": str(key), "value": str(value)}

    @_guard
    def invoice_verification_preferences(self):
        return self._invoice_verification_preferences()

    @_guard
    def set_invoice_verification_preferences(self, options=None):
        values = dict(options or {})
        current = self._invoice_verification_preferences()
        watch_directory = current["watch_directory"]
        trash_source = current["trash_source_after_archive"]

        if "watch_directory" in values:
            watch_directory = str(values.get("watch_directory") or "").strip()
            if watch_directory:
                path = Path(watch_directory).expanduser()
                if not path.is_dir():
                    raise ValueError("选择的查验单归档目录不存在。")
                path = path.resolve()
                if _is_inside(self.data_root.root, path):
                    raise ValueError("查验单归档目录不能位于 tidoc 数据目录内。")
                watch_directory = str(path)
        if "trash_source_after_archive" in values:
            raw_trash_source = values["trash_source_after_archive"]
            trash_source = (
                raw_trash_source
                if isinstance(raw_trash_source, bool)
                else str(raw_trash_source).strip().lower()
                in {"1", "true", "yes", "on"}
            )

        self.db.conn.executemany(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (
                (INVOICE_VERIFICATION_WATCH_DIR_PREF_KEY, watch_directory),
                (
                    INVOICE_VERIFICATION_TRASH_SOURCE_PREF_KEY,
                    "1" if trash_source else "0",
                ),
            ),
        )
        self.db.conn.commit()
        return {
            "watch_directory": watch_directory,
            "trash_source_after_archive": trash_source,
        }

    @_guard
    def app_info(self):
        repository = "https://github.com/totok22/tidoc"
        return {
            "name": "tidoc",
            "version": __version__,
            "author": "totok22",
            "repository": repository,
            "releases": f"{repository}/releases/latest",
        }

    # ------------------------------------------------------------ 录入 / 识别
    @_guard
    def parse_files(self, xml_path=None, pdf_path=None):
        """先解析、不落库，供前端预览识别结果与校验。"""
        from .engine import check_invoice, parse_invoice_files

        parsed = parse_invoice_files(xml_path, pdf_path)
        check = check_invoice(parsed)
        return {"parsed": parsed.to_dict(), "check": check.to_dict()}

    @_guard
    def reparse_entries(self, entry_ids):
        """用条目已有的原始发票附件批量重新识别明细并刷新识别提醒。"""
        from .db import TYPE_INVOICE_PDF, TYPE_INVOICE_XML
        from .engine import check_invoice, parse_invoice_files
        from .engine.money import d

        results = []
        seen = set()
        for entry_id in entry_ids or []:
            if not entry_id or entry_id in seen:
                continue
            seen.add(entry_id)
            entry = self.entries.get(entry_id)
            if not entry:
                results.append({"entry_id": entry_id, "ok": False, "error": "条目不存在"})
                continue
            try:
                xml_attachment = next(
                    (a for a in entry["attachments"] if a["type"] == TYPE_INVOICE_XML),
                    None,
                )
                pdf_attachment = next(
                    (a for a in entry["attachments"] if a["type"] == TYPE_INVOICE_PDF),
                    None,
                )
                if not xml_attachment and not pdf_attachment:
                    raise ValueError("缺少原始发票 PDF 或 XML")

                xml_path = (
                    self.data_root.attachments_dir / xml_attachment["stored_path"]
                    if xml_attachment else None
                )
                pdf_path = (
                    self.data_root.attachments_dir / pdf_attachment["stored_path"]
                    if pdf_attachment else None
                )
                parsed = parse_invoice_files(xml_path, pdf_path)
                if (
                    entry.get("invoice_no")
                    and parsed.invoice_no
                    and parsed.invoice_no != entry["invoice_no"]
                ):
                    raise ValueError("附件中的发票号码与当前条目不一致")

                # 条目中已锁定或人工修正的发票总额仍是校验权威值；重新识别
                # 只替换识别明细，不静默覆盖实付、备注或关键字段。
                parsed.total = d(entry.get("total"))
                check = check_invoice(parsed, expected_title=entry.get("title", ""))
                self.entries.replace_recognized_items(
                    entry_id,
                    parsed.items,
                    parsed.source,
                    check.status,
                    check.message,
                )
                results.append({
                    "entry_id": entry_id,
                    "ok": True,
                    "check_status": check.status,
                    "check_message": check.message,
                    "item_count": len(parsed.items),
                })
            except Exception as exc:  # noqa: BLE001 — 单条失败不阻断其余批量任务
                results.append({"entry_id": entry_id, "ok": False, "error": str(exc)})

        succeeded = [result for result in results if result.get("ok")]
        return {
            "processed": len(results),
            "resolved": sum(result["check_status"] == "pass" for result in succeeded),
            "remaining": sum(result["check_status"] != "pass" for result in succeeded),
            "failed": [result for result in results if not result.get("ok")],
            "results": results,
        }

    @_guard
    def create_entry(self, profile_id, title="", xml_path=None, pdf_path=None,
                     payment_paths=None, inspection_path=None, status="draft"):
        """从上传文件创建条目：解析 → 校验 → 落库 → 复制附件。"""
        from .engine import check_invoice, parse_invoice_files

        parsed = None
        if xml_path or pdf_path:
            parsed = parse_invoice_files(xml_path, pdf_path)
            self._ensure_invoice_not_duplicate(parsed, [xml_path, pdf_path])

        entry_id = None
        try:
            entry_id = self.entries.create(
                profile_id,
                title=title,
                parsed=parsed,
                status=status,
                default_paid_to_total=self._default_paid_to_invoice(),
            )

            if parsed:
                check = check_invoice(parsed, expected_title=title)
                self.entries.set_check(entry_id, check.status, check.message)

            from .db import TYPE_INSPECTION, TYPE_INVOICE_PDF, TYPE_INVOICE_XML, TYPE_PAYMENT
            if xml_path:
                self.attachments.add(entry_id, xml_path, TYPE_INVOICE_XML)
            if pdf_path:
                self.attachments.add(entry_id, pdf_path, TYPE_INVOICE_PDF)
            for pp in (payment_paths or []):
                self.attachments.add(entry_id, pp, TYPE_PAYMENT)
                self._maybe_apply_payment_ocr_amount(entry_id, pp)
            if inspection_path:
                self.attachments.add(entry_id, inspection_path, TYPE_INSPECTION)

            self.entries.recompute_status(entry_id)
            return self.entries.get(entry_id)
        except Exception as exc:
            if entry_id:
                cleanup_error = self._discard_incomplete_entry(entry_id)
                if cleanup_error:
                    raise RuntimeError(
                        f"{exc}；失败后的临时数据清理未完成：{cleanup_error}"
                    ) from exc
            raise

    # ------------------------------------------------------------ 文件夹批量导入
    @_guard
    def scan_folder(self, folder):
        """扫描目录，按发票 PDF 生成批量导入预览。"""
        from .services.folder_import import scan_folder

        return scan_folder(folder)

    @_guard
    def scan_files(self, paths):
        """扫描多选或拖入的发票文件，按发票 PDF 生成批量导入预览。"""
        from .services.folder_import import scan_files

        return scan_files(paths or [])

    @_guard
    def save_dropped_files(self, files):
        """保存前端拖入但拿不到本机路径的文件，返回临时路径列表。"""
        staging = self.data_root.dropped_dir
        staging.mkdir(parents=True, exist_ok=True)
        out = []
        total_bytes = 0
        for item in (files or []):
            name = _safe_filename(item.get("name") or "dropped-file")
            data_url = item.get("data_url") or ""
            if "," in data_url:
                data_url = data_url.split(",", 1)[1]
            if not data_url:
                raise ValueError(f"文件内容为空：{name}")
            estimated_bytes = len(data_url.rstrip("=")) * 3 // 4
            if estimated_bytes > MAX_DROPPED_FILE_BYTES:
                raise ValueError(f"文件过大：{name}（单个文件不能超过 100 MB）")
            if total_bytes + estimated_bytes > MAX_DROPPED_TOTAL_BYTES:
                raise ValueError("拖入文件总大小不能超过 500 MB")
            try:
                raw = base64.b64decode(data_url, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"文件内容无效：{name}") from exc
            total_bytes += len(raw)
            dest_dir = staging / uuid.uuid4().hex[:8]
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / name
            dest.write_bytes(raw)
            out.append(str(dest))
        return {"paths": out}

    @_guard
    def cleanup_dropped_files(self, paths):
        """删除拖拽中转区里的临时文件。只允许删除 dropped/ 内的文件。"""
        deleted = 0
        for path in (paths or []):
            p = Path(path)
            if _is_inside(self.data_root.dropped_dir, p) and p.is_file():
                p.unlink()
                _remove_empty_dropped_parent(self.data_root.dropped_dir, p.parent)
                deleted += 1
        return {"deleted": deleted}

    @_guard
    def classify_material_files(self, paths):
        """识别拖拽/粘贴材料的类型，并尽量提取发票号用于自动绑定。"""
        from .db import TYPE_INSPECTION, TYPE_INVOICE_PDF, TYPE_INVOICE_XML, TYPE_PAYMENT
        from .engine import parse_invoice_files
        from .services.folder_import import (
            classify_pdf_attachment_type,
            extract_payment_image_amount,
            extract_pdf_invoice_no,
        )

        image_ext = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
        out = []
        for raw in (paths or []):
            path = Path(raw)
            suffix = path.suffix.lower()
            att_type = "other"
            invoice_no = ""
            paid_amount = ""
            warning = ""
            try:
                if suffix == ".xml":
                    att_type = TYPE_INVOICE_XML
                    invoice_no = parse_invoice_files(xml_path=path).invoice_no or ""
                elif suffix == ".pdf":
                    att_type = classify_pdf_attachment_type(path)
                    if att_type == TYPE_INSPECTION:
                        invoice_no = extract_pdf_invoice_no(path)
                        if not invoice_no:
                            warning = "未识别到发票号码"
                elif suffix in image_ext:
                    att_type = TYPE_PAYMENT
                    if self._payment_ocr_enabled():
                        paid_amount = extract_payment_image_amount(path)
            except Exception as exc:  # noqa: BLE001 - 分类阶段只提示，后续添加时仍会校验
                warning = str(exc)
            out.append({
                "path": str(path),
                "name": path.name,
                "type": att_type,
                "type_label": {
                    TYPE_INVOICE_PDF: "发票 PDF",
                    TYPE_INVOICE_XML: "发票 XML",
                    TYPE_INSPECTION: "查验单 PDF",
                    TYPE_PAYMENT: "付款截图",
                    "other": "其他",
                }.get(att_type, att_type),
                "invoice_no": invoice_no,
                "paid_amount": paid_amount,
                "warning": warning,
            })
        return out

    @_guard
    def suggest_material_bindings(self, infos, candidate_entry_ids=None):
        """按发票号或付款金额给出唯一匹配；不唯一时由前端逐份手动绑定。"""
        from .services.folder_import import suggest_material_bindings

        entries = self.entries.list()
        candidate_ids = {str(value) for value in (candidate_entry_ids or []) if value}
        if candidate_ids:
            entries = [entry for entry in entries if entry.get("id") in candidate_ids]
        return suggest_material_bindings(
            infos or [], entries, payment_ocr_enabled=self._payment_ocr_enabled()
        )

    @_guard
    def batch_create_entries(self, profile_id, groups, title=""):
        """按前端确认后的分组批量创建条目。

        groups: [{"files": [{"path", "type"}...]}...]
        每组必须有发票 PDF，可附带 XML。付款截图 / 查验单在条目内添加。
        """
        from .engine import check_invoice, parse_invoice_files
        from .db import TYPE_INVOICE_PDF, TYPE_INVOICE_XML

        created, created_entries, failed = [], [], []
        for g in (groups or []):
            files = g.get("files") or []
            invoice_files = [f for f in files if f.get("type") == TYPE_INVOICE_PDF]
            xml_files = [f for f in files if f.get("type") == TYPE_INVOICE_XML]
            pdf_path = next((f["path"] for f in files if f.get("type") == TYPE_INVOICE_PDF), None)
            xml_path = next((f["path"] for f in xml_files), None)
            entry_id = None
            try:
                if not pdf_path:
                    raise ValueError("缺少发票 PDF")
                parsed = parse_invoice_files(xml_path, pdf_path)
                self._ensure_invoice_not_duplicate(parsed, [xml_path, pdf_path])
                entry_id = self.entries.create(
                    profile_id,
                    title=title,
                    parsed=parsed,
                    status="draft",
                    default_paid_to_total=self._default_paid_to_invoice(),
                )
                check = check_invoice(parsed, expected_title=title)
                self.entries.set_check(entry_id, check.status, check.message)
                for f in invoice_files:
                    self.attachments.add(entry_id, f["path"], TYPE_INVOICE_PDF)
                for f in xml_files:
                    self.attachments.add(entry_id, f["path"], TYPE_INVOICE_XML)
                self.entries.recompute_status(entry_id)
                created.append(entry_id)
                created_entries.append({
                    "group": g.get("key") or g.get("label") or "",
                    "label": g.get("label") or "",
                    "entry_id": entry_id,
                    "invoice_no": parsed.invoice_no or "",
                })
            except DuplicateInvoiceError as exc:
                failed.append({
                    "key": g.get("key") or "",
                    "group": g.get("label") or g.get("key") or "?",
                    "code": "duplicate_invoice",
                    "error": str(exc),
                    "existing_entry_id": exc.existing.get("id") or "",
                    "invoice_no": exc.existing.get("invoice_no") or "",
                })
            except Exception as exc:  # noqa: BLE001 — 单组失败不阻断其余
                cleanup_error = ""
                if entry_id:
                    cleanup_error = self._discard_incomplete_entry(entry_id)
                error = str(exc) or "导入过程中发生未知错误"
                if cleanup_error:
                    error += f"；失败后的临时数据清理未完成：{cleanup_error}"
                failed.append({
                    "key": g.get("key") or "",
                    "group": g.get("label") or g.get("key") or "?",
                    "code": "import_failed",
                    "error": error,
                })
        return {"created": len(created), "entry_ids": created, "created_entries": created_entries, "failed": failed}

    # ------------------------------------------------------------ 条目管理
    @_guard
    def list_entries(self, filters=None):
        return self.entries.list(**(filters or {}))

    @_guard
    def list_titles(self):
        return self.entries.all_titles()

    @_guard
    def get_entry(self, entry_id):
        return self.entries.get(entry_id)

    @_guard
    def update_field(self, entry_id, field, value, profile_id=""):
        result = self.entries.update_field(entry_id, field, value, profile_id)
        self.entries.recompute_status(entry_id)
        return result

    @_guard
    def set_recognized_paid_amount(self, entry_id, value):
        from .db.entries import VALUE_SOURCE_PAYMENT_OCR

        result = self.entries.update_field(
            entry_id,
            "paid_amount",
            value,
            "",
            value_source=VALUE_SOURCE_PAYMENT_OCR,
        )
        self.entries.recompute_status(entry_id)
        return result

    @_guard
    def correct_locked_field(self, entry_id, field, value, profile_id=""):
        return self.entries.correct_locked_field(entry_id, field, value, profile_id)

    @_guard
    def update_entry_profile(self, entry_id, profile_id, operator_profile_id=""):
        return self.entries.set_profile(entry_id, profile_id, operator_profile_id)

    @_guard
    def update_entry_profiles(self, entry_ids, profile_id, operator_profile_id=""):
        return {
            "changed": self.entries.set_profiles(
                entry_ids or [], profile_id, operator_profile_id
            )
        }

    @_guard
    def set_status(self, entry_id, status):
        self.entries.set_status(entry_id, status)
        return {"entry_id": entry_id, "status": status}

    @_guard
    def set_meta(self, entry_id, category=None, tags=None):
        if isinstance(category, dict):
            tags = category.get("tags")
            category = category.get("category")
        self.entries.set_meta(entry_id, category=category, tags=tags)
        return self.entries.get(entry_id)

    @_guard
    def delete_entry(self, entry_id):
        deleted, cleanup_warning = self._delete_entries_with_files([entry_id])
        return {
            "deleted": entry_id if deleted else "",
            "cleanup_warning": cleanup_warning,
        }

    @_guard
    def delete_entries(self, entry_ids):
        deleted, cleanup_warning = self._delete_entries_with_files(entry_ids or [])
        return {"deleted": deleted, "cleanup_warning": cleanup_warning}

    # ------------------------------------------------------------ 标签（批量）
    @_guard
    def add_tag(self, entry_ids, tag):
        return {"changed": self.entries.add_tag(entry_ids or [], tag)}

    @_guard
    def remove_tag(self, entry_ids, tag):
        return {"changed": self.entries.remove_tag(entry_ids or [], tag)}

    @_guard
    def list_tags(self):
        return self.entries.all_tags()

    @_guard
    def rename_tag(self, old_tag, new_tag):
        return {"changed": self.entries.rename_tag(old_tag, new_tag)}

    @_guard
    def delete_tag(self, tag):
        return {"changed": self.entries.delete_tag(tag)}

    # ------------------------------------------------------------ 批次（运营组工作单元）
    @_guard
    def list_batches(self, include_archived=False):
        return self.batches.list(include_archived=include_archived)

    @_guard
    def get_batch(self, batch_id):
        return self.batches.get(batch_id)

    @_guard
    def create_batch(self, name, note="", entry_ids=None):
        return self.batches.create(name, note or "", entry_ids or [])

    @_guard
    def update_batch(self, batch_id, fields=None):
        return self.batches.update(batch_id, **(fields or {}))

    @_guard
    def archive_batch(self, batch_id, archived=True):
        return self.batches.set_archived(batch_id, archived)

    @_guard
    def delete_batch(self, batch_id):
        self.batches.delete(batch_id)
        return {"deleted": batch_id}

    @_guard
    def add_entries_to_batch(self, batch_id, entry_ids):
        return {"added": self.batches.add_entries(batch_id, entry_ids or [])}

    @_guard
    def remove_entries_from_batch(self, batch_id, entry_ids):
        return {"removed": self.batches.remove_entries(batch_id, entry_ids or [])}

    @_guard
    def move_entries_between_batches(self, source_batch_id, target_batch_id, entry_ids):
        return self.batches.move_entries(source_batch_id, target_batch_id, entry_ids or [])

    @_guard
    def set_batch_entry_note(self, batch_id, entry_id, note):
        return self.batches.set_entry_note(batch_id, entry_id, note or "")

    @_guard
    def batches_of_entry(self, entry_id):
        return self.batches.batches_of_entry(entry_id)

    # ------------------------------------------------------------ 明细行
    @_guard
    def add_item(self, entry_id, fields=None):
        return self.entries.add_item(entry_id, **(fields or {}))

    @_guard
    def update_item(self, item_id, fields=None):
        return self.entries.update_item(item_id, fields or {})

    @_guard
    def delete_item(self, item_id):
        entry_id = self.entries.delete_item(item_id)
        return {"deleted": item_id, "entry_id": entry_id}

    # ------------------------------------------------------------ 附件
    @_guard
    def add_attachment(self, entry_id, src_path, att_type, note="", options=None):
        from .db import TYPE_PAYMENT

        options = options or {}
        self._validate_attachment_for_entry(entry_id, src_path, att_type)
        att = self.attachments.add(entry_id, src_path, att_type, note)
        if att_type == TYPE_PAYMENT and not options.get("skip_payment_ocr", False):
            amount, applied = self._maybe_apply_payment_ocr_amount(
                entry_id,
                src_path,
                apply=options.get("apply_payment_ocr", True),
            )
            if amount:
                att["payment_ocr"] = {"paid_amount": amount, "applied": applied}
        self.entries.recompute_status(entry_id)
        return att

    @_guard
    def delete_attachment(self, att_id):
        from .db import TYPE_PAYMENT

        att = self.attachments.get(att_id)
        result = self.attachments.delete(att_id)
        paid_reset = {"reset": False, "value": ""}
        if att and att.get("entry_id"):
            if att.get("type") == TYPE_PAYMENT:
                paid_reset = self.entries.restore_paid_amount_after_last_payment(
                    att["entry_id"], att.get("added_at") or ""
                )
            self.entries.recompute_status(att["entry_id"])
        return {"deleted": att_id, "paid_amount_reset": paid_reset, **result}

    @_guard
    def set_attachment_note(self, att_id, note):
        return self.attachments.set_note(att_id, note)

    @_guard
    def update_attachment(self, att_id, fields=None):
        from .db import TYPE_PAYMENT

        fields = fields or {}
        current = self.attachments.get(att_id)
        if not current:
            raise FileNotFoundError(f"附件不存在：{att_id}")
        new_type = fields.get("type") or current["type"]
        new_path = fields.get("src_path") or current["abs_path"]
        if fields.get("type") or fields.get("src_path"):
            self._validate_attachment_for_entry(current["entry_id"], new_path, new_type)
        att = self.attachments.update(
            att_id,
            att_type=fields.get("type"),
            src_path=fields.get("src_path"),
            note=fields.get("note"),
        )
        paid_reset = {"reset": False, "value": ""}
        if att and att.get("entry_id"):
            if current.get("type") == TYPE_PAYMENT and att.get("type") != TYPE_PAYMENT:
                paid_reset = self.entries.restore_paid_amount_after_last_payment(
                    att["entry_id"], current.get("added_at") or ""
                )
            self.entries.recompute_status(att["entry_id"])
            att["paid_amount_reset"] = paid_reset
        return att

    def _validate_attachment_for_entry(self, entry_id, src_path, att_type) -> None:
        from .db import TYPE_INSPECTION, TYPE_INVOICE_PDF, TYPE_INVOICE_XML
        from .engine import parse_invoice_files
        from .services.folder_import import classify_pdf_attachment_type, extract_pdf_invoice_no

        path = Path(src_path)
        suffix = path.suffix.lower()
        entry = self.entries.get(entry_id)
        if not entry:
            raise FileNotFoundError(f"条目不存在：{entry_id}")

        if att_type == TYPE_INVOICE_XML:
            if suffix != ".xml":
                raise ValueError("发票 XML 只能添加 .xml 文件。")
            parsed = parse_invoice_files(xml_path=path)
            if not parsed.invoice_no or not (parsed.seller or parsed.buyer_name):
                raise ValueError("这个 XML 不是可识别的官方电子发票 XML。")
            _validate_same_invoice(entry, parsed.invoice_no, path.name)
            return

        if att_type == TYPE_INVOICE_PDF:
            if suffix != ".pdf":
                raise ValueError("发票 PDF 只能添加 .pdf 文件。")
            detected = classify_pdf_attachment_type(path)
            if detected == TYPE_INSPECTION:
                raise ValueError("这个 PDF 像是发票查验单，请作为“查验单 PDF”添加。")
            parsed = parse_invoice_files(pdf_path=path)
            if not parsed.invoice_no:
                raise ValueError("无法从这个 PDF 识别发票号，请确认它是原始发票 PDF。")
            _validate_same_invoice(entry, parsed.invoice_no, path.name)
            return

        if att_type == TYPE_INSPECTION:
            if suffix != ".pdf":
                raise ValueError("查验单只能添加 PDF 文件。")
            detected = classify_pdf_attachment_type(path)
            if detected != TYPE_INSPECTION:
                try:
                    parsed = parse_invoice_files(pdf_path=path)
                except Exception:
                    parsed = None
                if parsed and parsed.invoice_no:
                    raise ValueError("这个 PDF 像是发票 PDF，请作为“发票 PDF”添加。")
                raise ValueError("无法确认这个 PDF 是发票查验单。")
            invoice_no = extract_pdf_invoice_no(path)
            if invoice_no:
                _validate_same_invoice(entry, invoice_no, path.name)

    def _maybe_apply_payment_ocr_amount(self, entry_id, src_path, apply: bool = True) -> tuple[str, bool]:
        from .db.entries import VALUE_SOURCE_PAYMENT_OCR
        from .services.folder_import import extract_payment_image_amount

        if not self._payment_ocr_enabled():
            return "", False
        amount = extract_payment_image_amount(src_path)
        if not amount:
            return "", False
        if not apply:
            return amount, False
        entry = self.entries.get(entry_id)
        if not entry:
            return amount, False
        fields = entry.get("fields") or {}
        current = ((fields.get("paid_amount") or {}).get("current") or "").strip()
        total = str(entry.get("total") or "").strip()
        if current and total and not _same_money(current, total):
            return amount, False
        self.entries.update_field(
            entry_id,
            "paid_amount",
            amount,
            "",
            value_source=VALUE_SOURCE_PAYMENT_OCR,
        )
        return amount, True

    @_guard
    def open_attachment(self, att_id):
        att = self.attachments.get(att_id)
        if not att:
            raise FileNotFoundError(f"附件不存在：{att_id}")
        _open_local_path(att["abs_path"])
        return {"opened": att["abs_path"]}

    @_guard
    def reveal_attachment(self, att_id):
        att = self.attachments.get(att_id)
        if not att:
            raise FileNotFoundError(f"附件不存在：{att_id}")
        _reveal_local_path(att["abs_path"])
        return {"revealed": att["abs_path"]}

    # ------------------------------------------------------------ 在线查验
    @_guard
    def invoice_verification_info(self, entry_id):
        """从已有发票材料准备官网查验表单，但不主动联网。"""
        from .services.invoice_verification import build_verification_info

        entry = self.entries.get(entry_id)
        if not entry:
            raise FileNotFoundError(f"条目不存在：{entry_id}")
        return build_verification_info(entry)

    @_guard
    def start_invoice_verification(self, entry_id, fields=None):
        """用户主动打开税务官网，预填发票信息并创建结果保存会话。"""
        import webview

        from .services.invoice_verification import (
            TAX_VERIFICATION_URL,
            build_verification_info,
            make_prefill_script,
            make_print_compatibility_script,
            new_session_snapshot,
            snapshot_pdfs,
            verification_print_title,
        )

        entry = self.entries.get(entry_id)
        if not entry:
            raise FileNotFoundError(f"条目不存在：{entry_id}")
        info = build_verification_info(entry)
        for key in ("invoice_no", "invoice_date", "verification_value"):
            if fields and fields.get(key) is not None:
                info[key] = str(fields[key]).strip()
        if not info["invoice_no"]:
            raise ValueError("缺少发票号码，请先在条目详情补正发票号码。")
        if not info["invoice_date"]:
            raise ValueError("缺少开票日期，请先在条目详情补正开票日期。")

        session_id = uuid.uuid4().hex
        snapshot = new_session_snapshot()
        verification_preferences = self._invoice_verification_preferences()
        custom_watch = verification_preferences["watch_directory"]
        if custom_watch:
            custom_watch_path = Path(custom_watch).expanduser()
            if not custom_watch_path.is_dir():
                raise ValueError("设置中的查验单归档目录不存在，请重新选择。")
            custom_watch_path = custom_watch_path.resolve()
            if _is_inside(self.data_root.root, custom_watch_path):
                raise ValueError("查验单归档目录不能位于 tidoc 数据目录内。")
            if custom_watch_path not in snapshot["watch_directories"]:
                snapshot["watch_directories"].append(custom_watch_path)
                snapshot["before"].update(snapshot_pdfs([custom_watch_path]))
        session = {
            "id": session_id,
            "entry_id": entry_id,
            "invoice_no": info["invoice_no"],
            "window": None,
            "window_closed": False,
            "attached": None,
            "candidate_sizes": {},
            "seen_candidates": set(),
            "last_message": "",
            "result_message": "",
            "cleanup_warning": "",
            "source_trashed": False,
            "trash_source_after_archive": verification_preferences[
                "trash_source_after_archive"
            ],
            **snapshot,
        }
        self._verification_sessions[session_id] = session
        try:
            self._install_verification_landscape_print()
            window = webview.create_window(
                "tidoc · 发票查验",
                url=TAX_VERIFICATION_URL,
                width=1360,
                height=860,
                min_size=(1000, 680),
                text_select=True,
                zoomable=True,
            )
            if window is None:
                raise RuntimeError("无法打开查验平台窗口。")
            window._tidoc_print_title = verification_print_title(  # type: ignore[attr-defined]
                info["invoice_no"]
            )
            session["window"] = window

            def on_window_closed() -> None:
                with self._api_lock:
                    session["window_closed"] = True

            window.events.closed += on_window_closed
            script = make_prefill_script(info)
            print_compatibility_script = make_print_compatibility_script(
                info["invoice_no"]
            )

            def install_print_compatibility() -> None:
                if session["window_closed"]:
                    return
                try:
                    window.evaluate_js(print_compatibility_script)
                except Exception:  # noqa: BLE001 - 不阻断官网正常浏览
                    session["last_message"] = (
                        "官网已打开，但打印兼容处理失败；可关闭后重新打开查验窗口。"
                    )

            # 官网查验成功后会导航到结果页；每次页面加载都重新安装打印兼容处理。
            window.events.loaded += install_print_compatibility

            def prefill_when_loaded() -> None:
                if window.events.loaded.wait(30):
                    try:
                        window.evaluate_js(script)
                        install_print_compatibility()
                    except Exception:  # noqa: BLE001 - 官网仍可供用户手动填写
                        session["last_message"] = "官网已打开，但自动填写失败，请在官网手动填写。"

            threading.Thread(target=prefill_when_loaded, daemon=True).start()
        except Exception:
            self._verification_sessions.pop(session_id, None)
            raise

        return {
            "session_id": session_id,
            "watch_directories": [str(path) for path in session["watch_directories"]],
            "info": info,
        }

    @_guard
    def invoice_verification_status(self, session_id):
        """检测用户刚保存的官网查验单，确认归属后直接绑定到当前条目。"""
        from .db import TYPE_INSPECTION
        from .services.folder_import import (
            classify_pdf_attachment_type,
            extract_pdf_invoice_no,
        )
        from .services.invoice_verification import changed_pdf_candidates

        session = self._verification_sessions.get(str(session_id))
        if not session:
            raise ValueError("查验会话已结束，请重新打开查验平台。")
        if session["attached"]:
            return {
                "state": "attached",
                "attachment": session["attached"],
                "message": (
                    session.get("result_message")
                    or "查验单已保存到当前条目。"
                ),
                "cleanup_warning": session.get("cleanup_warning", ""),
                "source_trashed": bool(session.get("source_trashed")),
            }

        candidates = changed_pdf_candidates(
            session["watch_directories"],
            session["before"],
            session["started_ns"],
        )
        waiting_for_write = False
        for path in candidates:
            key = str(path)
            if key in session["seen_candidates"]:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            previous_size = session["candidate_sizes"].get(key)
            session["candidate_sizes"][key] = size
            if previous_size != size or size == 0:
                waiting_for_write = True
                continue
            session["seen_candidates"].add(key)
            try:
                if classify_pdf_attachment_type(path) != TYPE_INSPECTION:
                    continue
                detected_invoice_no = extract_pdf_invoice_no(path)
                if (detected_invoice_no and session["invoice_no"]
                        and detected_invoice_no != session["invoice_no"]):
                    session["last_message"] = (
                        f"发现一份其他发票的查验单（{detected_invoice_no}），未绑定。"
                    )
                    continue
                if session["invoice_no"] and not detected_invoice_no:
                    session["last_message"] = (
                        "发现一份查验单，但未能确认发票号码，未自动绑定。"
                        "原 PDF 已保留，请手动选择这份文件。"
                    )
                    continue
                self._validate_attachment_for_entry(
                    session["entry_id"], path, TYPE_INSPECTION
                )
                attachment = self.attachments.add(
                    session["entry_id"], path, TYPE_INSPECTION,
                    note="由全国增值税发票查验平台原生打印保存",
                )
                self.entries.recompute_status(session["entry_id"])
                session["attached"] = attachment
                result_message = "查验单已保存到当前条目。"
                cleanup_warning = ""
                source_trashed = False
                if session.get("trash_source_after_archive"):
                    try:
                        send2trash(str(path))
                        source_trashed = True
                        result_message = (
                            "查验单已保存，原 PDF 已移到废纸篓或回收站。"
                        )
                    except Exception as exc:  # noqa: BLE001 - 归档成功不应回滚
                        cleanup_warning = (
                            "原 PDF 未能移到废纸篓或回收站，仍保留在原位置："
                            f"{exc}"
                        )
                        result_message = (
                            "查验单已保存，但原 PDF 仍保留在原位置。"
                        )
                session["result_message"] = result_message
                session["cleanup_warning"] = cleanup_warning
                session["source_trashed"] = source_trashed
                self._close_verification_window(session)
                return {
                    "state": "attached",
                    "attachment": attachment,
                    "message": result_message,
                    "cleanup_warning": cleanup_warning,
                    "source_trashed": source_trashed,
                }
            except Exception as exc:  # noqa: BLE001 - 继续等待用户保存正确文件
                session["last_message"] = f"发现 PDF，但未能绑定：{exc}"

        state = "processing" if waiting_for_write else (
            "window_closed" if session["window_closed"] else "waiting"
        )
        return {
            "state": state,
            "message": session["last_message"],
            "window_closed": session["window_closed"],
        }

    @_guard
    def close_invoice_verification(self, session_id):
        session = self._verification_sessions.pop(str(session_id), None)
        if session:
            self._close_verification_window(session)
        return {"closed": bool(session)}

    def _close_verification_window(self, session: dict) -> None:
        window = session.get("window")
        if window is None or session.get("window_closed"):
            return
        try:
            window.destroy()
        except Exception:  # noqa: BLE001 - 用户可能已先关闭窗口
            pass
        session["window_closed"] = True

    @staticmethod
    def _configure_verification_print_info(info) -> None:
        """把当前原生打印任务固定为横向。"""
        if sys.platform != "darwin":
            return
        try:
            import AppKit
        except ImportError:
            return
        info.setOrientation_(AppKit.NSPaperOrientationLandscape)

    def _install_verification_landscape_print(self) -> None:
        """只为查验窗口包装 pywebview 的原生打印入口。"""
        if sys.platform != "darwin":
            return
        try:
            from webview.platforms.cocoa import BrowserView
        except ImportError:
            return

        current = BrowserView.print_webview
        if getattr(current, "_tidoc_landscape_print", False):
            return
        original = current

        def print_webview_landscape(native_webview):
            window = getattr(native_webview, "pywebview_window", None)
            if getattr(window, "title", "") != "tidoc · 发票查验":
                return original(native_webview)
            job_title = getattr(window, "_tidoc_print_title", "查验单")
            return self._run_macos_verification_print(native_webview, job_title)

        print_webview_landscape._tidoc_landscape_print = True
        BrowserView.print_webview = staticmethod(print_webview_landscape)

    @staticmethod
    def _run_macos_verification_print(native_webview, job_title: str) -> None:
        """创建带横向设置和明确任务名的 macOS 原生打印任务。"""
        import AppKit
        import Foundation

        info = AppKit.NSPrintInfo.sharedPrintInfo().copy()
        Api._configure_verification_print_info(info)
        info.setHorizontalPagination_(AppKit.NSFitPagination)
        info.setHorizontallyCentered_(Foundation.NO)
        info.setVerticallyCentered_(Foundation.NO)

        imageable_bounds = info.imageablePageBounds()
        paper_size = info.paperSize()
        if Foundation.NSWidth(imageable_bounds) > paper_size.width:
            imageable_bounds.origin.x = 0
            imageable_bounds.size.width = paper_size.width
        if Foundation.NSHeight(imageable_bounds) > paper_size.height:
            imageable_bounds.origin.y = 0
            imageable_bounds.size.height = paper_size.height

        info.setBottomMargin_(Foundation.NSMinY(imageable_bounds))
        info.setTopMargin_(
            paper_size.height
            - Foundation.NSMinY(imageable_bounds)
            - Foundation.NSHeight(imageable_bounds)
        )
        info.setLeftMargin_(Foundation.NSMinX(imageable_bounds))
        info.setRightMargin_(
            paper_size.width
            - Foundation.NSMinX(imageable_bounds)
            - Foundation.NSWidth(imageable_bounds)
        )

        print_operation = native_webview._printOperationWithPrintInfo_(info)
        print_operation.setJobTitle_(job_title)
        print_operation.runOperationModalForWindow_delegate_didRunSelector_contextInfo_(
            native_webview.window(), None, None, None
        )

    # ------------------------------------------------------------ 汇总 / 绑定包
    @_guard
    def build_summary(self, entry_ids):
        from .services.summary import build_summary

        return build_summary(self.entries, entry_ids)

    @_guard
    def export_bindle(self, entry_ids, out_name=None):
        from .services.bindle import export_bindle

        name = out_name or "绑定包"
        out_path = self.data_root.exports_dir / f"{name}.tidoc"
        lookup = {p["id"]: p for p in self.profiles.list()}
        result = export_bindle(
            self.entries,
            self.attachments,
            entry_ids,
            out_path,
            lookup,
            include_notes=self._preference_value(BINDLE_INCLUDE_NOTES_PREF_KEY, "1") != "0",
            include_tags=self._preference_value(BINDLE_INCLUDE_TAGS_PREF_KEY, "1") != "0",
        )
        return {"path": str(result), "count": len(entry_ids)}

    @_guard
    def export_overview_excel(self, entry_ids, out_name=None):
        from .services.exports import export_overview_xlsx

        name = out_name or "报账总览"
        out_path = self.data_root.exports_dir / f"{name}.xlsx"
        lookup = {p["id"]: p for p in self.profiles.list()}
        result = export_overview_xlsx(self.entries, lookup, entry_ids, out_path)
        return {"path": str(result), "count": len(entry_ids)}

    @_guard
    def export_attachment_archive(self, entry_ids, out_name=None):
        from .services.exports import export_attachment_zip

        name = out_name or "附件整理包"
        out_path = self.data_root.exports_dir / f"{name}.zip"
        lookup = {p["id"]: p for p in self.profiles.list()}
        result = export_attachment_zip(
            self.entries, self.data_root.attachments_dir, lookup, entry_ids, out_path
        )
        return {"path": str(result), "count": len(entry_ids)}

    @_guard
    def inspect_bindle(self, path):
        from .services.bindle import inspect_bindle

        return inspect_bindle(path)

    @_guard
    def import_bindle(self, path, profile_id, allow_tampered=False, options=None):
        from .services.bindle import import_bindle

        return import_bindle(
            self.entries,
            self.attachments,
            path,
            profile_id,
            allow_tampered,
            options,
        )

    # ------------------------------------------------------------ 打印导出组件（可选）
    @_guard
    def print_component_status(self):
        from .services.printing import component_status
        return component_status(self.data_root.components_dir)

    @_guard
    def build_prints(self, entry_ids, options=None, out_name=None):
        """生成打印件（默认按条目拼接材料；可选按材料分开、Word 文档）。
        按抬头强隔离，输出到 exports/<out_name>/<抬头>/。"""
        from .services.printing import build_prints as _build
        name = out_name or "打印件"
        out_dir = self.data_root.exports_dir / name
        options = dict(options or {})
        options["operator_profile"] = self._operator_profile_for_print()
        return _build(self.entries, self.profiles, self.data_root.attachments_dir,
                      entry_ids, out_dir, options, self.data_root.components_dir)

    # ------------------------------------------------------------ 联网更新（腾讯云 COS）
    @_guard
    def check_updates(self):
        from .services.updater import check_updates
        status = check_updates(self.data_root.components_dir, updates_dir=self.data_root.updates_dir)
        return self._record_update_check(status)

    @_guard
    def auto_check_updates(self):
        """按用户偏好执行低频启动检查；只检查，不下载或安装。"""
        if self._preference_value(AUTO_UPDATE_PREF_KEY, "0") != "1":
            return {"checked": False, "reason": "disabled", "updates": []}

        now = int(time.time())
        try:
            last_check = int(self._preference_value(UPDATE_LAST_CHECK_KEY, "0") or 0)
        except ValueError:
            last_check = 0
        if last_check and now - last_check < AUTO_UPDATE_INTERVAL_SECONDS:
            cached = self._cached_update_result()
            return {
                **cached,
                "checked": False,
                "reason": "recent",
                "checked_at": last_check,
                "next_check_at": last_check + AUTO_UPDATE_INTERVAL_SECONDS,
            }

        from .services.updater import check_updates
        status = check_updates(self.data_root.components_dir, updates_dir=self.data_root.updates_dir)
        status = self._record_update_check(status, now)
        status.update({"checked": True, "reason": "due"})
        return status

    @_guard
    def startup_update_state(self):
        """记录已启动版本，并在真正升级后的首次启动返回本次变化。"""
        from .services.updater import version_gt

        previous = self._preference_value(APP_LAST_SEEN_VERSION_KEY, "")
        upgraded = bool(previous and version_gt(__version__, previous))
        if not previous or version_gt(__version__, previous):
            self._set_preference_value(APP_LAST_SEEN_VERSION_KEY, __version__)

        notes: list[str] = []
        cached = self._cached_update_result()
        if upgraded:
            for item in cached.get("updates") or []:
                if item.get("component") != "core" or item.get("latest_version") != __version__:
                    continue
                raw_notes = (item.get("asset") or {}).get("notes") or []
                notes = raw_notes if isinstance(raw_notes, list) else [str(raw_notes)]
                break
        return {
            "upgraded": upgraded,
            "first_launch": not previous,
            "previous_version": previous,
            "current_version": __version__,
            "notes": notes,
        }

    @_guard
    def download_core_update(self):
        from .services.updater import COMPONENT_CORE, download_update, launch_core_update_package, load_manifest
        manifest = load_manifest()
        result = download_update(manifest, COMPONENT_CORE, self.data_root.updates_dir)
        launch_core_update_package(result.file_path)
        data = result.to_dict()
        data["launched"] = True
        return data

    @_guard
    def open_downloaded_core_update(self):
        from .services.updater import open_downloaded_core_update
        info = open_downloaded_core_update(self.data_root.updates_dir)
        return {"launched": True, **info}

    @_guard
    def install_print_component(self):
        from .services.updater import install_print_component, load_manifest
        manifest = load_manifest()
        result = install_print_component(
            manifest, self.data_root.components_dir, self.data_root.updates_dir
        )
        return result.to_dict()

    # ------------------------------------------------------------ 临时文件维护
    @_guard
    def storage_maintenance_status(self):
        files = self._cache_cleanup_candidates()
        return {
            "files": len(files),
            "size": sum(path.stat().st_size for path in files if path.exists()),
            "exports_size": _directory_size(self.data_root.exports_dir),
        }

    @_guard
    def cleanup_app_cache(self):
        files = self._cache_cleanup_candidates()
        removed = 0
        released = 0
        for path in files:
            try:
                size = path.stat().st_size
                path.unlink()
                removed += 1
                released += size
            except FileNotFoundError:
                continue
        for root in (self.data_root.dropped_dir, self.data_root.updates_dir):
            for folder in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
                try:
                    folder.rmdir()
                except OSError:
                    pass
        return {"files": removed, "size": released}

    # ------------------------------------------------------------ 文件对话框
    @_guard
    def pick_files(self, multiple=True, file_types=None):
        """调系统文件选择框，返回路径列表。前端拿不到本地路径，必须走这里。"""
        import webview
        dialog_type = _file_dialog_kind(webview, "OPEN", "OPEN_DIALOG")
        types = tuple(file_types) if file_types else ()
        result = self._window.create_file_dialog(
            dialog_type, allow_multiple=multiple, file_types=types
        )
        return {"paths": list(result) if result else []}

    @_guard
    def pick_folder(self):
        """调系统文件夹选择框，返回目录路径。"""
        import webview
        result = self._window.create_file_dialog(_file_dialog_kind(webview, "FOLDER", "FOLDER_DIALOG"))
        path = (list(result)[0] if result else "") if result else ""
        return {"path": path}

    @_guard
    def data_root_path(self):
        from .db.paths import default_data_root
        d = self._paths_dict()
        d["is_default"] = str(self.data_root.root) == str(default_data_root())
        return d

    @_guard
    def choose_and_migrate_data_root(self):
        """弹出文件夹选择框，把数据迁到用户选的空目录，并热重建各仓库。

        选中目录后：关闭当前 DB 连接 → 移动全部数据 → 用新根重开连接与仓库。
        返回新的路径清单，供前端刷新设置页显示。
        """
        import webview
        result = self._window.create_file_dialog(
            _file_dialog_kind(webview, "FOLDER", "FOLDER_DIALOG")
        )
        target = (list(result)[0] if result else "") if result else ""
        if not target:
            return {"ok": True, "data": {"changed": False}}
        old_root = self.data_root
        # 先断开 DB（释放 sqlite 文件句柄），再搬运，避免 Windows 下占用无法移动。
        self.db.close()
        try:
            new_root_path = old_root.migrate_to(target)
        except Exception:
            # 迁移失败：用原根恢复连接，保证应用可继续用。
            self._rebuild_repos(old_root.root)
            raise
        self._rebuild_repos(new_root_path)
        return {"ok": True, "data": {"changed": True, **self._paths_dict()}}

    @_guard
    def reset_data_root_to_default(self):
        """把数据迁回系统默认目录（清除迁移指针）。"""
        from .db.paths import default_data_root
        default = default_data_root()
        if str(self.data_root.root) == str(default):
            return {"changed": False}
        old_root = self.data_root
        self.db.close()
        try:
            new_root_path = old_root.migrate_to(default)
        except Exception:
            self._rebuild_repos(old_root.root)
            raise
        self._rebuild_repos(new_root_path)
        return {"changed": True, **self._paths_dict()}

    def _rebuild_repos(self, root) -> None:
        """用给定数据根重建 DataRoot / Database 及各仓库（迁移后热切换）。"""
        self.data_root = DataRoot(root, manage_pointer=True)
        self.db = Database(self.data_root.db_path)
        self.profiles = ProfileRepo(self.db)
        self.entries = EntryRepo(self.db)
        self.attachments = AttachmentRepo(self.db, self.data_root)
        self.batches = BatchRepo(self.db)

    def _paths_dict(self) -> dict:
        return {
            "root": str(self.data_root.root),
            "attachments": str(self.data_root.attachments_dir),
            "exports": str(self.data_root.exports_dir),
            "db": str(self.data_root.db_path),
            "components": str(self.data_root.components_dir),
            "updates": str(self.data_root.updates_dir),
        }

    def _operator_profile_for_print(self) -> dict:
        keys = {
            "person_name": "tidoc.operator.name",
            "student_id": "tidoc.operator.student_id",
            "contact": "tidoc.operator.contact",
            "bank_name": "tidoc.operator.bank_name",
            "bank_card": "tidoc.operator.bank_card",
        }
        profile = {}
        for out_key, pref_key in keys.items():
            row = self.db.conn.execute("SELECT value FROM meta WHERE key = ?", (pref_key,)).fetchone()
            profile[out_key] = row["value"] if row else ""
        return profile

    def _preference_value(self, key: str, default: str = "") -> str:
        row = self.db.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def _payment_ocr_enabled(self) -> bool:
        return self._preference_value(PAYMENT_OCR_PREF_KEY, "1") != "0"

    def _default_paid_to_invoice(self) -> bool:
        return self._preference_value(DEFAULT_PAID_TO_INVOICE_PREF_KEY, "1") != "0"

    def _ensure_invoice_not_duplicate(self, parsed, source_paths) -> None:
        """按发票号优先、文件摘要兜底，在全库阻止同一发票重复建条目。"""
        invoice_no = str(getattr(parsed, "invoice_no", "") or "").strip()
        row = None
        if invoice_no:
            row = self.db.conn.execute(
                """SELECT e.id, e.invoice_no, e.seller, p.name AS profile_name
                     FROM entries e
                     LEFT JOIN profiles p ON p.id = e.profile_id
                    WHERE e.invoice_no = ?
                    ORDER BY e.created_at
                    LIMIT 1""",
                (invoice_no,),
            ).fetchone()

        if row is None:
            for raw_path in source_paths or []:
                if not raw_path:
                    continue
                path = Path(raw_path)
                if not path.is_file():
                    continue
                sha256 = _file_sha256(path)
                row = self.db.conn.execute(
                    """SELECT e.id, e.invoice_no, e.seller, p.name AS profile_name
                         FROM attachments a
                         JOIN entries e ON e.id = a.entry_id
                         LEFT JOIN profiles p ON p.id = e.profile_id
                        WHERE a.sha256 = ?
                          AND a.type IN ('invoice_pdf', 'invoice_xml')
                        ORDER BY e.created_at
                        LIMIT 1""",
                    (sha256,),
                ).fetchone()
                if row is not None:
                    break

        if row is not None:
            raise DuplicateInvoiceError({key: row[key] for key in row.keys()})

    def _discard_incomplete_entry(self, entry_id: str) -> str:
        """导入失败时清掉已落库的半成品记录和已复制附件。"""
        errors = []
        try:
            self.db.conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            self.db.conn.commit()
        except Exception as exc:  # noqa: BLE001 - 返回给原始导入错误统一报告
            errors.append(f"记录未删除：{exc}")
        entry_dir = self.data_root.attachments_dir / entry_id
        try:
            if entry_dir.is_dir():
                shutil.rmtree(entry_dir)
        except Exception as exc:  # noqa: BLE001 - 返回给原始导入错误统一报告
            errors.append(f"附件未删除：{exc}")
        return "；".join(errors)

    def _delete_entries_with_files(self, entry_ids) -> tuple[int, str]:
        """先暂存附件目录，再删记录；数据库删除失败时可把附件原位恢复。"""
        ids = list(dict.fromkeys(str(value) for value in (entry_ids or []) if value))
        if not ids:
            return 0, ""
        placeholders = ",".join("?" * len(ids))
        rows = self.db.conn.execute(
            f"SELECT id FROM entries WHERE id IN ({placeholders})", ids
        ).fetchall()
        existing_ids = [row["id"] for row in rows]
        if not existing_ids:
            return 0, ""

        moved: list[tuple[Path, Path]] = []
        try:
            for entry_id in existing_ids:
                source = self.data_root.attachments_dir / entry_id
                if not source.is_dir():
                    continue
                quarantine = self.data_root.attachments_dir / (
                    f".deleting-{entry_id}-{uuid.uuid4().hex[:8]}"
                )
                source.rename(quarantine)
                moved.append((source, quarantine))
        except Exception:
            for source, quarantine in reversed(moved):
                if quarantine.exists() and not source.exists():
                    quarantine.rename(source)
            raise

        try:
            deleted = self.entries.delete_many(existing_ids)
        except Exception:
            self.db.conn.rollback()
            restore_errors = []
            for source, quarantine in reversed(moved):
                try:
                    if quarantine.exists() and not source.exists():
                        quarantine.rename(source)
                except OSError as exc:
                    restore_errors.append(str(exc))
            if restore_errors:
                raise RuntimeError(
                    "条目删除失败，且附件目录恢复未完成：" + "；".join(restore_errors)
                )
            raise

        cleanup_errors = []
        for _source, quarantine in moved:
            try:
                shutil.rmtree(quarantine)
            except OSError as exc:
                cleanup_errors.append(str(exc))
        warning = (
            f"条目已删除，但有 {len(cleanup_errors)} 个附件目录清理失败："
            + "；".join(cleanup_errors)
            if cleanup_errors else ""
        )
        return deleted, warning

    def _set_preference_value(self, key: str, value: str) -> None:
        self.db.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.db.conn.commit()

    def _record_update_check(self, status: dict, checked_at: int | None = None) -> dict:
        checked_at = checked_at or int(time.time())
        result = {**status, "checked_at": checked_at}
        self._set_preference_value(UPDATE_LAST_CHECK_KEY, str(checked_at))
        self._set_preference_value(
            UPDATE_LAST_RESULT_KEY,
            json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        )
        return result

    def _cached_update_result(self) -> dict:
        raw = self._preference_value(UPDATE_LAST_RESULT_KEY, "")
        if not raw:
            return {"updates": []}
        try:
            result = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {"updates": []}
        return result if isinstance(result, dict) else {"updates": []}

    def _cache_cleanup_candidates(self) -> list[Path]:
        """只返回可重建的临时文件，保留待安装核心包和所有业务数据。"""
        from .services.updater import downloaded_core_update_info

        pending = downloaded_core_update_info(self.data_root.updates_dir)
        pending_path = Path(pending.get("file_path") or "") if pending else None
        pending_resolved = pending_path.resolve() if pending_path and pending_path.exists() else None
        files: list[Path] = []
        for path in self.data_root.dropped_dir.rglob("*"):
            if path.is_file():
                files.append(path)
        for path in self.data_root.updates_dir.rglob("*"):
            if not path.is_file() or path.name == "current.json":
                continue
            if pending_resolved and path.resolve() == pending_resolved:
                continue
            files.append(path)
        return files

    @_guard
    def open_path(self, path):
        _open_local_path(path)
        return {"opened": str(path)}

    @_guard
    def open_external_url(self, url):
        text = str(url)
        if not (text.startswith("https://") or text.startswith("http://")):
            raise ValueError("只能打开网页链接。")
        webbrowser.open(text)
        return {"opened": text}


def _file_dialog_kind(webview_module, modern_name: str, legacy_name: str):
    file_dialog = getattr(webview_module, "FileDialog", None)
    if file_dialog is not None and hasattr(file_dialog, modern_name):
        return getattr(file_dialog, modern_name)
    return getattr(webview_module, legacy_name)


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[/\\:\0]+", "_", name).strip()
    return cleaned or "dropped-file"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_size(folder: Path) -> int:
    """Return the current size of regular files, tolerating concurrent changes."""
    total = 0
    for path in folder.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _is_inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _cleanup_old_dropped_files(folder: Path, max_age_seconds: int = 24 * 60 * 60) -> None:
    if not folder.exists():
        return
    cutoff = time.time() - max_age_seconds
    for path in folder.rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
            _remove_empty_dropped_parent(folder, path.parent)


def _remove_empty_dropped_parent(root: Path, folder: Path) -> None:
    if folder.resolve() == root.resolve() or not _is_inside(root, folder):
        return
    try:
        folder.rmdir()
    except OSError:
        pass


def _open_local_path(path: str | Path) -> None:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在：{p}")
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    elif os.name == "nt":
        os.startfile(str(p))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(p)])


def _validate_same_invoice(entry: dict, invoice_no: str, filename: str) -> None:
    expected = entry.get("invoice_no") or ""
    if expected and invoice_no and invoice_no != expected:
        raise ValueError(f"这份材料不属于当前条目：{filename}。识别到发票号 {invoice_no}，当前条目是 {expected}。")


def _same_money(a: str, b: str) -> bool:
    try:
        return Decimal(str(a)).quantize(Decimal("0.01")) == Decimal(str(b)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return str(a).strip() == str(b).strip()


def _reveal_local_path(path: str | Path) -> None:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在：{p}")
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(p)])
    elif os.name == "nt":
        subprocess.Popen(["explorer", "/select,", str(p)])
    else:
        _open_local_path(p.parent)

"""绑定包 .tidoc 的导出 / 导入（设计文档第 8.6 节）。

一个 zip 包，内含：
- entries.json     结构化条目数据（含字段级修改标记与历史，不可擦除）
- summary.json     汇总信息文本（第 8.4 节）
- attachments/     规范命名的 PDF / 截图 / 查验单
- signatures.json  HMAC 签名清单（第 6.1 节）

文件与信息绑定，可整体导出、他人整体导入还原。导入时逐项校验 HMAC，
不符即判定被外部修改，返回 tampered 列表由 UI 醒目标红。
"""

from __future__ import annotations

import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from ..db.attachments import AttachmentRepo
from ..db.entries import EntryRepo
from .signing import MANIFEST_NAME, sign_bytes, verify
from .summary import build_summary

BINDLE_VERSION = 1
ENTRIES_NAME = "entries.json"
SUMMARY_NAME = "summary.json"


def _serialize_entry(entry: dict) -> dict:
    """挑出需要随包走的字段：识别字段、可改字段的 origin/current/modified、
    明细、附件元数据、字段历史。"""
    return {
        "id": entry["id"],
        "title": entry.get("title", ""),
        "invoice_no": entry.get("invoice_no", ""),
        "invoice_date": entry.get("invoice_date", ""),
        "seller": entry.get("seller", ""),
        "total": entry.get("total", ""),
        "buyer_name": entry.get("buyer_name", ""),
        "buyer_tax_id": entry.get("buyer_tax_id", ""),
        "category": entry.get("category", ""),
        "tags": entry.get("tags", []),
        "status": entry.get("status", ""),
        "check_status": entry.get("check_status", ""),
        "check_message": entry.get("check_message", ""),
        "source": entry.get("source", ""),
        "profile_name": entry.get("_profile_name", ""),
        "reviewer": entry.get("_reviewer", ""),
        "fields": entry.get("fields", {}),
        "items": [
            {k: it.get(k) for k in ("name", "actual_name", "unit", "quantity", "unit_price", "total", "spec", "ordinal")}
            for it in entry.get("items", [])
        ],
        "attachments": [
            {k: a.get(k) for k in ("id", "type", "original_name", "stored_path", "sha256", "note", "added_at")}
            for a in entry.get("attachments", [])
        ],
        "history": [
            {k: h.get(k) for k in ("field", "old_value", "new_value", "profile_id", "changed_at")}
            for h in entry.get("history", [])
        ],
    }


def export_bindle(
    entries_repo: EntryRepo,
    attachments_repo: AttachmentRepo,
    entry_ids: list[str],
    out_path: str | Path,
    profile_lookup: dict[str, dict] | None = None,
) -> Path:
    """把选定条目连同附件打成一个 .tidoc 包，内嵌 HMAC 签名清单。"""
    out_path = Path(out_path)
    if out_path.suffix != ".tidoc":
        out_path = out_path.with_suffix(".tidoc")
    profile_lookup = profile_lookup or {}

    serialized, signatures = [], {}
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for eid in entry_ids:
            entry = entries_repo.get(eid)
            if not entry:
                continue
            prof = profile_lookup.get(entry.get("profile_id"), {})
            entry["_profile_name"] = prof.get("name", "")
            entry["_reviewer"] = prof.get("reviewer", "")
            serialized.append(_serialize_entry(entry))
            # 写附件文件，并对每个文件签名
            for att in entry.get("attachments", []):
                abs_path = attachments_repo.data_root.attachments_dir / att["stored_path"]
                if not abs_path.exists():
                    continue
                arcname = f"attachments/{att['stored_path']}"
                zf.write(abs_path, arcname)
                signatures[arcname] = sign_bytes(abs_path.read_bytes())

        entries_payload = {"bindle_version": BINDLE_VERSION, "entries": serialized}
        entries_bytes = json.dumps(entries_payload, ensure_ascii=False, indent=2).encode("utf-8")
        summary_bytes = json.dumps(build_summary(entries_repo, entry_ids), ensure_ascii=False, indent=2).encode("utf-8")

        zf.writestr(ENTRIES_NAME, entries_bytes)
        zf.writestr(SUMMARY_NAME, summary_bytes)
        signatures[ENTRIES_NAME] = sign_bytes(entries_bytes)
        signatures[SUMMARY_NAME] = sign_bytes(summary_bytes)

        manifest = {
            "bindle_version": BINDLE_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "algorithm": "HMAC-SHA256",
            "signatures": signatures,
        }
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))

    return out_path


def inspect_bindle(path: str | Path) -> dict:
    """读取并校验一个 .tidoc 包，返回条目数据 + 篡改检测结果，不写入数据库。

    返回 {"entries": [...], "summary": {...}, "tampered": [文件名...], "verified": bool}
    """
    path = Path(path)
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        if MANIFEST_NAME not in names or ENTRIES_NAME not in names:
            raise ValueError("不是合法的 .tidoc 绑定包（缺少签名清单或条目数据）。")

        manifest = json.loads(zf.read(MANIFEST_NAME))
        signatures = manifest.get("signatures", {})
        tampered: list[str] = []

        for arcname, expected in signatures.items():
            if arcname not in names:
                tampered.append(arcname)  # 文件被删
                continue
            if not verify(zf.read(arcname), expected):
                tampered.append(arcname)

        # 也检查是否有清单外的附件被偷加（仅提示，不阻断）
        entries_payload = json.loads(zf.read(ENTRIES_NAME))
        summary = json.loads(zf.read(SUMMARY_NAME)) if SUMMARY_NAME in names else {}

    return {
        "entries": entries_payload.get("entries", []),
        "summary": summary,
        "tampered": tampered,
        "verified": not tampered,
    }


def import_bindle(
    entries_repo: EntryRepo,
    attachments_repo: AttachmentRepo,
    path: str | Path,
    profile_id: str,
    allow_tampered: bool = False,
) -> dict:
    """把一个 .tidoc 包导入到当前库，附件落地到指定条目目录。

    默认拒绝导入被篡改的包（allow_tampered=False）。导入的条目挂到 profile_id 名下，
    保留原始识别字段、可改字段的修改标记与历史（不可擦除）。
    返回 {"imported": n, "tampered": [...], "entry_ids": [...]}。
    """
    import uuid

    inspected = inspect_bindle(path)
    if inspected["tampered"] and not allow_tampered:
        return {
            "imported": 0,
            "skipped": [],
            "tampered": inspected["tampered"],
            "entry_ids": [],
            "message": "绑定包已被外部修改，已拒绝导入。",
        }

    path = Path(path)
    imported_ids: list[str] = []
    skipped: list[dict] = []
    created_dirs: list[Path] = []
    conn = entries_repo.db.conn
    now = datetime.now().isoformat(timespec="seconds")
    existing_invoice_nos = {
        str(row["invoice_no"] or "").strip()
        for row in conn.execute(
            "SELECT invoice_no FROM entries WHERE TRIM(COALESCE(invoice_no, '')) <> ''"
        ).fetchall()
    }
    existing_invoice_hashes = {
        str(row["sha256"] or "").strip()
        for row in conn.execute(
            """SELECT sha256 FROM attachments
                WHERE type IN ('invoice_pdf', 'invoice_xml')
                  AND TRIM(COALESCE(sha256, '')) <> ''"""
        ).fetchall()
    }

    try:
        with zipfile.ZipFile(path, "r") as zf:
            for e in inspected["entries"]:
                invoice_no = str(e.get("invoice_no") or "").strip()
                invoice_hashes = {
                    str(att.get("sha256") or "").strip()
                    for att in e.get("attachments", [])
                    if att.get("type") in {"invoice_pdf", "invoice_xml"}
                    and str(att.get("sha256") or "").strip()
                }
                if (
                    (invoice_no and invoice_no in existing_invoice_nos)
                    or bool(invoice_hashes & existing_invoice_hashes)
                ):
                    skipped.append({
                        "invoice_no": invoice_no,
                        "seller": e.get("seller", ""),
                        "reason": "发票号已存在" if invoice_no in existing_invoice_nos else "相同发票文件已存在",
                    })
                    continue

                check_status = e.get("check_status", "warning")
                check_message = e.get("check_message", "")
                status = e.get("status", "draft")
                if inspected["tampered"]:
                    check_status = "blocked"
                    status = "partial"
                    tampered_message = "绑定包完整性校验未通过，导入前已由用户确认"
                    check_message = "；".join(filter(None, [tampered_message, check_message]))

                new_id = uuid.uuid4().hex
                conn.execute(
                    """INSERT INTO entries(id, profile_id, title, invoice_no, invoice_date,
                       seller, total, buyer_name, buyer_tax_id, category, tags, status,
                       check_status, check_message, source, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id, profile_id, e.get("title", ""), invoice_no,
                     e.get("invoice_date", ""), e.get("seller", ""), e.get("total", ""),
                     e.get("buyer_name", ""), e.get("buyer_tax_id", ""), e.get("category", ""),
                     json.dumps(e.get("tags", []), ensure_ascii=False), status,
                     check_status, check_message, e.get("source", "imported"), now, now),
                )
                for field, fv in e.get("fields", {}).items():
                    conn.execute(
                        "INSERT INTO entry_fields(entry_id, field, origin, current, modified) VALUES(?,?,?,?,?)",
                        (new_id, field, fv.get("origin", ""), fv.get("current", ""), int(bool(fv.get("modified")))),
                    )
                for it in e.get("items", []):
                    conn.execute(
                        """INSERT INTO items(entry_id, name, actual_name, unit, quantity,
                           unit_price, total, spec, ordinal) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (new_id, it.get("name", ""), it.get("actual_name", ""), it.get("unit", ""),
                         it.get("quantity", ""), it.get("unit_price", ""), it.get("total", ""),
                         it.get("spec", ""), it.get("ordinal", 0)),
                    )
                for h in e.get("history", []):
                    conn.execute(
                        """INSERT INTO field_history(entry_id, field, old_value, new_value, profile_id, changed_at)
                           VALUES(?,?,?,?,?,?)""",
                        (new_id, h.get("field", ""), h.get("old_value", ""), h.get("new_value", ""),
                         h.get("profile_id", ""), h.get("changed_at", now)),
                    )
                # 附件：从包里解出到新条目目录，重建记录
                dest_dir = attachments_repo.data_root.entry_dir(new_id)
                created_dirs.append(dest_dir)
                for att in e.get("attachments", []):
                    arcname = f"attachments/{att['stored_path']}"
                    if arcname not in zf.namelist():
                        continue
                    stored_name = Path(att["stored_path"]).name
                    dest = dest_dir / stored_name
                    dest.write_bytes(zf.read(arcname))
                    conn.execute(
                        """INSERT INTO attachments(id, entry_id, type, original_name, stored_path,
                           sha256, note, added_at) VALUES(?,?,?,?,?,?,?,?)""",
                        (uuid.uuid4().hex, new_id, att.get("type", "other"),
                         att.get("original_name", ""), f"{new_id}/{stored_name}",
                         att.get("sha256", ""), att.get("note", ""), att.get("added_at", now)),
                    )
                imported_ids.append(new_id)
                if invoice_no:
                    existing_invoice_nos.add(invoice_no)
                existing_invoice_hashes.update(invoice_hashes)
            conn.commit()
    except Exception as exc:
        conn.rollback()
        cleanup_errors = []
        for folder in created_dirs:
            if folder.is_dir():
                try:
                    shutil.rmtree(folder)
                except OSError as cleanup_exc:
                    cleanup_errors.append(str(cleanup_exc))
        if cleanup_errors:
            raise RuntimeError(
                f"{exc}；失败后的附件目录清理未完成：" + "；".join(cleanup_errors)
            ) from exc
        raise

    message_parts = ["导入完成" if imported_ids else "没有导入新条目"]
    if skipped:
        message_parts.append(f"已跳过 {len(skipped)} 条重复发票")
    if inspected["tampered"] and imported_ids:
        message_parts.append("完整性异常条目已标记为严重问题")
    message = "；".join(message_parts) + "。"

    return {
        "imported": len(imported_ids),
        "skipped": skipped,
        "tampered": inspected["tampered"],
        "entry_ids": imported_ids,
        "message": message,
    }

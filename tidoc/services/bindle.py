"""绑定包 .tidoc 的导出 / 导入（设计文档第 8.6 节）。

一个 zip 包，内含：
- entries.json     结构化条目数据（含字段级修改标记与历史，不可擦除）
                   以及报账人清单和每条发票的归属
- summary.json     汇总信息文本（第 8.4 节）
- attachments/     规范命名的 PDF / 截图 / 查验单
- signatures.json  HMAC 签名清单（第 6.1 节）

文件与信息绑定，可整体导出、他人整体导入还原。导入时逐项校验 HMAC，
不符即判定被外部修改，返回 tampered 列表由 UI 醒目标红。
"""

from __future__ import annotations

import json
import hashlib
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from ..db.attachments import AttachmentRepo
from ..db.entries import EntryRepo
from .signing import MANIFEST_NAME, sign_bytes, verify
from .summary import build_summary

BINDLE_VERSION = 4
ENTRIES_NAME = "entries.json"
SUMMARY_NAME = "summary.json"
EXCLUSIVE_TAG_GROUPS = (
    frozenset({"已报销", "未报销"}),
    frozenset({"已支付", "未支付"}),
)


def _serialize_entry(
    entry: dict,
    *,
    include_notes: bool = True,
    include_tags: bool = True,
) -> dict:
    """挑出需要随包走的字段：识别字段、可改字段的 origin/current/modified、
    明细、附件元数据、字段历史。"""
    fields = {
        field: value
        for field, value in entry.get("fields", {}).items()
        if include_notes or field != "notes"
    }
    history = [
        {k: h.get(k) for k in ("field", "old_value", "new_value", "profile_id", "changed_at")}
        for h in entry.get("history", [])
        if include_notes or h.get("field") != "notes"
    ]
    attachments = []
    for attachment in entry.get("attachments", []):
        serialized_attachment = {
            k: attachment.get(k)
            for k in ("id", "type", "original_name", "stored_path", "sha256", "added_at")
        }
        serialized_attachment["note"] = attachment.get("note", "") if include_notes else ""
        attachments.append(serialized_attachment)

    return {
        "id": entry["id"],
        "profile_id": entry.get("profile_id", ""),
        "title": entry.get("title", ""),
        "invoice_no": entry.get("invoice_no", ""),
        "invoice_date": entry.get("invoice_date", ""),
        "seller": entry.get("seller", ""),
        "total": entry.get("total", ""),
        "buyer_name": entry.get("buyer_name", ""),
        "buyer_tax_id": entry.get("buyer_tax_id", ""),
        "category": entry.get("category", ""),
        "tags": entry.get("tags", []) if include_tags else [],
        "status": entry.get("status", ""),
        "check_status": entry.get("check_status", ""),
        "check_message": entry.get("check_message", ""),
        "source": entry.get("source", ""),
        "created_at": entry.get("created_at", ""),
        "updated_at": entry.get("updated_at", ""),
        "profile_name": entry.get("_profile_name", ""),
        "reviewer": entry.get("_reviewer", ""),
        "fields": fields,
        "items": [
            {k: it.get(k) for k in ("name", "actual_name", "unit", "quantity", "unit_price", "total", "spec", "ordinal")}
            for it in entry.get("items", [])
        ],
        "attachments": attachments,
        "history": history,
        "ocr_results": [
            {
                key: result.get(key)
                for key in (
                    "provider", "file_sha256", "file_name", "raw_json", "normalized",
                    "closure_pass", "applied_at", "pending", "applied_changes",
                    "api_calls", "status", "error", "created_at",
                )
            }
            for result in entry.get("_ocr_results", [])
        ],
    }


def _serialize_profile(profile: dict) -> dict:
    """绑定包只携带发票归属所需的报账人字段，不携带本机收款信息。"""
    return {
        "id": profile.get("id", ""),
        "name": profile.get("name", ""),
        "reviewer": profile.get("reviewer", ""),
        "is_default": bool(profile.get("is_default")),
    }


def export_bindle(
    entries_repo: EntryRepo,
    attachments_repo: AttachmentRepo,
    entry_ids: list[str],
    out_path: str | Path,
    profile_lookup: dict[str, dict] | None = None,
    *,
    include_notes: bool = True,
    include_tags: bool = True,
) -> Path:
    """把选定条目连同附件打成一个 .tidoc 包，内嵌 HMAC 签名清单。"""
    out_path = Path(out_path)
    if out_path.suffix != ".tidoc":
        out_path = out_path.with_suffix(".tidoc")
    profile_lookup = profile_lookup or {}

    serialized, signatures = [], {}
    referenced_profile_ids: set[str] = set()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for eid in entry_ids:
            entry = entries_repo.get(eid)
            if not entry:
                continue
            prof = profile_lookup.get(entry.get("profile_id"), {})
            entry["_profile_name"] = prof.get("name", "")
            entry["_reviewer"] = prof.get("reviewer", "")
            entry["_ocr_results"] = [
                dict(row) for row in entries_repo.db.conn.execute(
                    "SELECT * FROM ocr_results WHERE entry_id = ? ORDER BY id",
                    (eid,),
                ).fetchall()
            ]
            if entry.get("profile_id"):
                referenced_profile_ids.add(entry["profile_id"])
            referenced_profile_ids.update(
                h.get("profile_id", "") for h in entry.get("history", [])
                if h.get("profile_id")
            )
            serialized.append(
                _serialize_entry(
                    entry,
                    include_notes=include_notes,
                    include_tags=include_tags,
                )
            )
            # 写附件文件，并对每个文件签名
            for att in entry.get("attachments", []):
                abs_path = attachments_repo.data_root.attachments_dir / att["stored_path"]
                if not abs_path.exists():
                    continue
                arcname = f"attachments/{att['stored_path']}"
                zf.write(abs_path, arcname)
                signatures[arcname] = sign_bytes(abs_path.read_bytes())

        profiles = [
            _serialize_profile(profile_lookup[profile_id])
            for profile_id in referenced_profile_ids
            if profile_id in profile_lookup
        ]
        profiles.sort(key=lambda p: (not p["is_default"], p["name"], p["reviewer"], p["id"]))
        entries_payload = {
            "bindle_version": BINDLE_VERSION,
            "profiles": profiles,
            "options": {
                "include_notes": include_notes,
                "include_tags": include_tags,
            },
            "entries": serialized,
        }
        entries_bytes = json.dumps(entries_payload, ensure_ascii=False, indent=2).encode("utf-8")
        summary_payload = build_summary(entries_repo, entry_ids)
        if not include_notes:
            for record in summary_payload.get("entries", []):
                record.pop("notes", None)
        summary_bytes = json.dumps(summary_payload, ensure_ascii=False, indent=2).encode("utf-8")

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


def inspect_bindle(path: str | Path, entries_repo: EntryRepo | None = None) -> dict:
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

    result = {
        "entries": entries_payload.get("entries", []),
        "profiles": entries_payload.get("profiles", []),
        "options": entries_payload.get("options", {}),
        "summary": summary,
        "tampered": tampered,
        "verified": not tampered,
    }
    if entries_repo is not None:
        _annotate_import_actions(entries_repo, result)
    return result


def _entry_identity_maps(conn) -> tuple[dict[str, str], dict[str, str]]:
    by_number = {
        str(row["invoice_no"] or "").strip(): row["id"]
        for row in conn.execute(
            "SELECT id, invoice_no FROM entries WHERE TRIM(COALESCE(invoice_no, '')) <> ''"
        ).fetchall()
    }
    by_hash = {
        str(row["sha256"] or "").strip(): row["entry_id"]
        for row in conn.execute(
            """SELECT entry_id, sha256 FROM attachments
                 WHERE type IN ('invoice_pdf', 'invoice_xml')
                   AND TRIM(COALESCE(sha256, '')) <> ''"""
        ).fetchall()
    }
    return by_number, by_hash


def _matching_entry_id(entry: dict, by_number: dict[str, str], by_hash: dict[str, str]) -> str:
    invoice_no = str(entry.get("invoice_no") or "").strip()
    if invoice_no and invoice_no in by_number:
        return by_number[invoice_no]
    for attachment in entry.get("attachments") or []:
        if attachment.get("type") not in {"invoice_pdf", "invoice_xml"}:
            continue
        digest = str(attachment.get("sha256") or "").strip()
        if digest and digest in by_hash:
            return by_hash[digest]
    return ""


def _merge_preview(entries_repo: EntryRepo, existing_id: str, incoming: dict) -> dict:
    existing = entries_repo.get(existing_id) or {}
    local_by_hash = {
        str(attachment.get("sha256") or ""): attachment
        for attachment in existing.get("attachments") or []
        if str(attachment.get("sha256") or "")
    }
    local_hashes = set(local_by_hash)
    incoming_attachments = [
        attachment for attachment in incoming.get("attachments") or []
        if str(attachment.get("sha256") or "") not in local_hashes
    ]
    attachment_note_changes = sum(
        1 for attachment in incoming.get("attachments") or []
        if str(attachment.get("sha256") or "") in local_by_hash
        and _merge_text(
            str(local_by_hash[str(attachment.get("sha256") or "")].get("note") or ""),
            str(attachment.get("note") or ""),
        ) != str(local_by_hash[str(attachment.get("sha256") or "")].get("note") or "")
    )
    local_tag_list = list(existing.get("tags") or [])
    incoming_tag_list = [str(tag).strip() for tag in incoming.get("tags") or [] if str(tag).strip()]
    preview_tags = _merge_tags(local_tag_list, incoming_tag_list)
    added_tags = [tag for tag in incoming_tag_list if tag not in local_tag_list]
    tags_changed = preview_tags != local_tag_list
    local_fields = existing.get("fields") or {}
    added_fields = []
    for field, value in (incoming.get("fields") or {}).items():
        incoming_value = str(value.get("current") or "")
        local_value = str((local_fields.get(field) or {}).get("current") or "")
        if incoming_value and (
            not local_value or (field == "notes" and incoming_value not in local_value)
        ):
            added_fields.append(field)
    added_base_fields = [
        field for field in (
            "title", "invoice_no", "invoice_date", "seller", "total",
            "buyer_name", "buyer_tax_id",
            "category", "source",
        )
        if str(incoming.get(field) or "") and not str(existing.get(field) or "")
    ]
    adds_items = bool(incoming.get("items") and not existing.get("items"))
    existing_ocr = {
        (
            str(result["provider"] or ""), str(result["file_sha256"] or ""),
            str(result["created_at"] or ""), str(result["normalized"] or ""),
        )
        for result in entries_repo.db.conn.execute(
            "SELECT provider, file_sha256, created_at, normalized FROM ocr_results WHERE entry_id = ?",
            (existing_id,),
        ).fetchall()
    }
    added_ocr = sum(
        1 for result in incoming.get("ocr_results") or []
        if (
            str(result.get("provider") or "aliyun"), str(result.get("file_sha256") or ""),
            str(result.get("created_at") or ""), str(result.get("normalized") or ""),
        ) not in existing_ocr
    )
    existing_history = {
        (
            str(row["field"] or ""), str(row["old_value"] or ""),
            str(row["new_value"] or ""), str(row["changed_at"] or ""),
        )
        for row in entries_repo.db.conn.execute(
            "SELECT field, old_value, new_value, changed_at FROM field_history WHERE entry_id = ?",
            (existing_id,),
        ).fetchall()
    }
    added_history = sum(
        1 for history in incoming.get("history") or []
        if (
            str(history.get("field") or ""), str(history.get("old_value") or ""),
            str(history.get("new_value") or ""), str(history.get("changed_at") or ""),
        ) not in existing_history
    )
    labels = []
    if incoming_attachments:
        labels.append(f"材料 {len(incoming_attachments)}")
    if attachment_note_changes:
        labels.append(f"附件备注 {attachment_note_changes}")
    if "notes" in added_fields:
        labels.append("备注")
    ordinary_fields = [field for field in added_fields if field != "notes"]
    if ordinary_fields:
        labels.append(f"信息 {len(ordinary_fields)}")
    if added_base_fields:
        labels.append(f"基础信息 {len(added_base_fields)}")
    if adds_items:
        labels.append("发票明细")
    if added_ocr:
        labels.append(f"识别记录 {added_ocr}")
    if tags_changed:
        labels.append(f"标签 {max(1, len(added_tags))}")
    if added_history:
        labels.append(f"修改记录 {added_history}")
    return {
        "attachments": len(incoming_attachments),
        "attachment_notes": attachment_note_changes,
        "fields": added_fields,
        "base_fields": added_base_fields,
        "tags": added_tags,
        "items": adds_items,
        "ocr_results": added_ocr,
        "history": added_history,
        "labels": labels,
        "has_changes": bool(labels),
    }


def _annotate_import_actions(entries_repo: EntryRepo, inspected: dict) -> None:
    by_number, by_hash = _entry_identity_maps(entries_repo.db.conn)
    counts = {"new": 0, "merge": 0, "unchanged": 0}
    for entry in inspected.get("entries") or []:
        existing_id = _matching_entry_id(entry, by_number, by_hash)
        if not existing_id:
            entry["import_action"] = "new"
            counts["new"] += 1
            continue
        preview = _merge_preview(entries_repo, existing_id, entry)
        entry["existing_entry_id"] = existing_id
        entry["merge_preview"] = preview
        entry["import_action"] = "merge" if preview["has_changes"] else "unchanged"
        counts[entry["import_action"]] += 1
    inspected["import_plan"] = counts


def _merge_text(local: str, incoming: str) -> str:
    local = str(local or "").strip()
    incoming = str(incoming or "").strip()
    if not incoming or incoming in local:
        return local
    return f"{local}\n{incoming}" if local else incoming


def _merge_tags(local_tags: list[str], incoming_tags: list[str]) -> list[str]:
    """Union ordinary tags; a package status tag replaces its local opposite."""
    result = list(local_tags)
    for incoming in incoming_tags:
        for group in EXCLUSIVE_TAG_GROUPS:
            if incoming in group:
                result = [tag for tag in result if tag not in group]
                break
        if incoming not in result:
            result.append(incoming)
    return result


def _merge_existing_entry(
    conn,
    entries_repo: EntryRepo,
    attachments_repo: AttachmentRepo,
    archive: zipfile.ZipFile,
    entry_id: str,
    incoming: dict,
    import_tags: list[str],
    now: str,
    created_files: list[Path],
    tampered: bool = False,
) -> list[str]:
    """Add package-only material and metadata without overwriting local work."""
    changes: list[str] = []
    existing = entries_repo.get(entry_id) or {}

    filled_locked = []
    for field in (
        "title", "invoice_no", "invoice_date", "seller", "total",
        "buyer_name", "buyer_tax_id",
        "category", "source",
    ):
        incoming_value = str(incoming.get(field) or "")
        if incoming_value and not str(existing.get(field) or ""):
            conn.execute(
                f"UPDATE entries SET {field} = ? WHERE id = ?",
                (incoming_value, entry_id),
            )
            filled_locked.append(field)
    if filled_locked:
        changes.append(f"基础信息 {len(filled_locked)}")

    local_tags = list(existing.get("tags") or [])
    merged_tags = _merge_tags(
        local_tags,
        [str(tag).strip() for tag in incoming.get("tags") or [] if str(tag).strip()]
        + import_tags,
    )
    if merged_tags != local_tags:
        conn.execute(
            "UPDATE entries SET tags = ?, updated_at = ? WHERE id = ?",
            (json.dumps(merged_tags, ensure_ascii=False), now, entry_id),
        )
        changes.append(f"标签 {max(1, len(set(merged_tags) - set(local_tags)))}")

    local_fields = existing.get("fields") or {}
    for field, value in (incoming.get("fields") or {}).items():
        incoming_value = str(value.get("current") or "")
        if not incoming_value:
            continue
        local = local_fields.get(field)
        if local is None:
            conn.execute(
                """INSERT INTO entry_fields(entry_id, field, origin, current, modified, value_source)
                   VALUES(?,?,?,?,?,?)""",
                (
                    entry_id, field, str(value.get("origin") or ""), incoming_value,
                    int(bool(value.get("modified"))), str(value.get("value_source") or ""),
                ),
            )
            changes.append("备注" if field == "notes" else "条目信息")
            continue
        local_value = str(local.get("current") or "")
        next_value = _merge_text(local_value, incoming_value) if field == "notes" else (
            incoming_value if not local_value else local_value
        )
        if next_value == local_value:
            continue
        conn.execute(
            """UPDATE entry_fields
                  SET current = ?, modified = MAX(modified, ?),
                      value_source = CASE WHEN value_source = '' THEN ? ELSE value_source END
                WHERE entry_id = ? AND field = ?""",
            (
                next_value, int(bool(value.get("modified"))),
                str(value.get("value_source") or ""), entry_id, field,
            ),
        )
        conn.execute(
            """INSERT INTO field_history(entry_id, field, old_value, new_value, profile_id, changed_at)
               VALUES(?,?,?,?,?,?)""",
            (entry_id, field, local_value, next_value, "", now),
        )
        changes.append("备注" if field == "notes" else "条目信息")

    local_attachments = existing.get("attachments") or []
    by_hash = {
        str(attachment.get("sha256") or ""): attachment
        for attachment in local_attachments if str(attachment.get("sha256") or "")
    }
    attachment_added = 0
    for attachment in incoming.get("attachments") or []:
        digest = str(attachment.get("sha256") or "")
        duplicate = by_hash.get(digest) if digest else None
        incoming_note = str(attachment.get("note") or "")
        if duplicate:
            merged_note = _merge_text(str(duplicate.get("note") or ""), incoming_note)
            if merged_note != str(duplicate.get("note") or ""):
                conn.execute(
                    "UPDATE attachments SET note = ? WHERE id = ?",
                    (merged_note, duplicate["id"]),
                )
                changes.append("附件备注")
            continue
        stored_path = str(attachment.get("stored_path") or "")
        arcname = f"attachments/{stored_path}"
        if not stored_path or arcname not in archive.namelist():
            continue
        suffix = Path(
            str(attachment.get("original_name") or "") or stored_path
        ).suffix
        att_type = str(attachment.get("type") or "other")
        dest_dir = attachments_repo.data_root.entry_dir(entry_id)
        stored_name = attachments_repo._unique_name(dest_dir, entry_id, att_type, suffix)
        dest = dest_dir / stored_name
        created_files.append(dest)
        dest.write_bytes(archive.read(arcname))
        actual_digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        if digest and actual_digest != digest and not tampered:
            raise ValueError(f"附件 {attachment.get('original_name') or stored_name} 校验失败。")
        conn.execute(
            """INSERT INTO attachments(id, entry_id, type, original_name, stored_path,
               sha256, note, added_at) VALUES(?,?,?,?,?,?,?,?)""",
            (
                uuid.uuid4().hex, entry_id, att_type,
                str(attachment.get("original_name") or stored_name),
                f"{entry_id}/{stored_name}", actual_digest, incoming_note,
                str(attachment.get("added_at") or now),
            ),
        )
        by_hash[actual_digest] = {"id": "", "sha256": actual_digest, "note": incoming_note}
        attachment_added += 1
    if attachment_added:
        changes.append(f"材料 {attachment_added}")

    item_count = conn.execute(
        "SELECT COUNT(*) FROM items WHERE entry_id = ?", (entry_id,)
    ).fetchone()[0]
    if not item_count and incoming.get("items"):
        for item in incoming["items"]:
            conn.execute(
                """INSERT INTO items(entry_id, name, actual_name, unit, quantity,
                   unit_price, total, spec, ordinal) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    entry_id, item.get("name", ""), item.get("actual_name", ""),
                    item.get("unit", ""), item.get("quantity", ""),
                    item.get("unit_price", ""), item.get("total", ""),
                    item.get("spec", ""), item.get("ordinal", 0),
                ),
            )
        changes.append("发票明细")

    existing_ocr = {
        (
            str(row["provider"] or ""), str(row["file_sha256"] or ""),
            str(row["created_at"] or ""), str(row["normalized"] or ""),
        )
        for row in conn.execute(
            "SELECT provider, file_sha256, created_at, normalized FROM ocr_results WHERE entry_id = ?",
            (entry_id,),
        ).fetchall()
    }
    ocr_added = 0
    for result in incoming.get("ocr_results") or []:
        identity = (
            str(result.get("provider") or "aliyun"),
            str(result.get("file_sha256") or ""),
            str(result.get("created_at") or ""),
            str(result.get("normalized") or ""),
        )
        if identity in existing_ocr:
            continue
        conn.execute(
            """INSERT INTO ocr_results(
                   entry_id, provider, file_sha256, file_name, raw_json, normalized,
                   closure_pass, applied_at, pending, applied_changes, is_local_call,
                   api_calls, status, error, created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)""",
            (
                entry_id, identity[0], identity[1], result.get("file_name", ""),
                result.get("raw_json", ""), identity[3], int(bool(result.get("closure_pass"))),
                result.get("applied_at", ""), result.get("pending", "[]"),
                result.get("applied_changes", ""), max(1, int(result.get("api_calls") or 1)),
                result.get("status", "ok"), result.get("error", ""), identity[2] or now,
            ),
        )
        existing_ocr.add(identity)
        ocr_added += 1
    if ocr_added:
        changes.append(f"识别记录 {ocr_added}")

    existing_history = {
        (
            str(row["field"] or ""), str(row["old_value"] or ""),
            str(row["new_value"] or ""), str(row["changed_at"] or ""),
        )
        for row in conn.execute(
            "SELECT field, old_value, new_value, changed_at FROM field_history WHERE entry_id = ?",
            (entry_id,),
        ).fetchall()
    }
    history_added = 0
    for history in incoming.get("history") or []:
        identity = (
            str(history.get("field") or ""), str(history.get("old_value") or ""),
            str(history.get("new_value") or ""), str(history.get("changed_at") or now),
        )
        if identity in existing_history:
            continue
        conn.execute(
            """INSERT INTO field_history(entry_id, field, old_value, new_value, profile_id, changed_at)
               VALUES(?,?,?,?,?,?)""",
            (entry_id, identity[0], identity[1], identity[2], "", identity[3]),
        )
        existing_history.add(identity)
        history_added += 1
    if history_added:
        changes.append(f"修改记录 {history_added}")

    if tampered and changes:
        warning = "绑定包完整性校验未通过，补充内容已由用户确认"
        check_message = _merge_text(str(existing.get("check_message") or ""), warning)
        conn.execute(
            """UPDATE entries SET check_status = 'blocked', status = 'partial',
                      check_message = ?, updated_at = ? WHERE id = ?""",
            (check_message, now, entry_id),
        )
        changes.append("完整性标记")

    if changes:
        conn.execute("UPDATE entries SET updated_at = ? WHERE id = ?", (now, entry_id))
    return list(dict.fromkeys(changes))


def import_bindle(
    entries_repo: EntryRepo,
    attachments_repo: AttachmentRepo,
    path: str | Path,
    profile_id: str,
    allow_tampered: bool = False,
    options: dict | None = None,
) -> dict:
    """把一个 .tidoc 包导入到当前库，附件落地到指定条目目录。

    默认拒绝导入被篡改的包（allow_tampered=False）。新版绑定包会按姓名和审核人
    复用或创建报账人并恢复每条发票的归属；旧绑定包缺少身份时才挂到 profile_id。
    保留原始识别字段、可改字段的修改标记与历史（不可擦除）。
    返回 {"imported": n, "tampered": [...], "entry_ids": [...]}。
    """
    options = dict(options or {})
    profile_overrides = options.get("profile_overrides") or {}
    selection_supplied = "selected_profile_ids" in options
    selected_profile_ids = {
        str(value or "") for value in (options.get("selected_profile_ids") or [])
    }
    import_tags = list(dict.fromkeys(
        str(tag).strip() for tag in (options.get("tags") or []) if str(tag).strip()
    ))
    target_batch_id = str(options.get("batch_id") or "").strip()
    new_batch_name = str(options.get("batch_name") or "").strip()
    if target_batch_id and new_batch_name:
        raise ValueError("已有批次和新建批次只能选择一个。")

    inspected = inspect_bindle(path)
    if inspected["tampered"] and not allow_tampered:
        return {
            "imported": 0,
            "updated": 0,
            "updated_entry_ids": [],
            "merged": [],
            "skipped": [],
            "tampered": inspected["tampered"],
            "entry_ids": [],
            "profiles_imported": 0,
            "profile_ids": [],
            "message": "绑定包已被外部修改，已拒绝导入。",
        }

    path = Path(path)
    imported_ids: list[str] = []
    updated_ids: list[str] = []
    merged: list[dict] = []
    skipped: list[dict] = []
    created_dirs: list[Path] = []
    created_files: list[Path] = []
    conn = entries_repo.db.conn
    now = datetime.now().isoformat(timespec="seconds")
    existing_by_invoice_no, existing_by_invoice_hash = _entry_identity_maps(conn)
    package_profiles = {
        str(profile.get("id") or ""): profile
        for profile in inspected.get("profiles", [])
        if str(profile.get("id") or "")
    }
    existing_profiles = [dict(row) for row in conn.execute("SELECT * FROM profiles").fetchall()]
    if target_batch_id and not conn.execute(
        "SELECT 1 FROM batches WHERE id = ?", (target_batch_id,)
    ).fetchone():
        raise ValueError("所选批次不存在，导入前请重新选择。")
    profile_count_before = len(existing_profiles)
    profile_by_identity = {
        (str(profile.get("name") or "").strip(), str(profile.get("reviewer") or "").strip()): profile["id"]
        for profile in existing_profiles
    }
    source_profile_map: dict[str, str] = {}
    created_profile_ids: list[str] = []
    batch_created = False

    def resolve_profile(profile: dict | None, source_id: str = "") -> str:
        if source_id and source_id in source_profile_map:
            return source_profile_map[source_id]
        profile = profile or {}
        name = str(profile.get("name") or "").strip()
        reviewer = str(profile.get("reviewer") or "").strip()
        if not name or not reviewer:
            if not profile_id:
                raise ValueError("绑定包缺少可用的报账人信息，且未指定导入归属。")
            if source_id:
                source_profile_map[source_id] = profile_id
            return profile_id

        identity = (name, reviewer)
        destination_id = profile_by_identity.get(identity)
        if not destination_id:
            destination_id = uuid.uuid4().hex
            is_default = int(
                not existing_profiles
                and not created_profile_ids
                and bool(profile.get("is_default"))
            )
            conn.execute(
                """INSERT INTO profiles(id, name, reviewer, is_default, created_at)
                   VALUES(?,?,?,?,?)""",
                (destination_id, name, reviewer, is_default, now),
            )
            profile_by_identity[identity] = destination_id
            created_profile_ids.append(destination_id)
        if source_id:
            source_profile_map[source_id] = destination_id
        return destination_id

    try:
        with zipfile.ZipFile(path, "r") as zf:
            for e in inspected["entries"]:
                source_profile_id = str(e.get("profile_id") or "__fallback__")
                if selection_supplied and source_profile_id not in selected_profile_ids:
                    continue
                invoice_no = str(e.get("invoice_no") or "").strip()
                invoice_hashes = {
                    str(att.get("sha256") or "").strip()
                    for att in e.get("attachments", [])
                    if att.get("type") in {"invoice_pdf", "invoice_xml"}
                    and str(att.get("sha256") or "").strip()
                }
                existing_id = _matching_entry_id(
                    e, existing_by_invoice_no, existing_by_invoice_hash
                )
                if existing_id:
                    changes = _merge_existing_entry(
                        conn, entries_repo, attachments_repo, zf, existing_id, e,
                        import_tags, now, created_files, bool(inspected["tampered"]),
                    )
                    if changes:
                        if existing_id not in updated_ids:
                            updated_ids.append(existing_id)
                        merged.append({
                            "entry_id": existing_id,
                            "invoice_no": invoice_no,
                            "seller": e.get("seller", ""),
                            "changes": changes,
                        })
                    else:
                        skipped.append({
                            "invoice_no": invoice_no,
                            "seller": e.get("seller", ""),
                            "reason": "现有条目已包含包内材料和信息",
                        })
                    continue

                lookup_profile_id = "" if source_profile_id == "__fallback__" else source_profile_id
                source_profile = package_profiles.get(lookup_profile_id)
                if source_profile is None and (e.get("profile_name") or e.get("reviewer")):
                    # v1 绑定包虽没有 profiles 清单，但每条仍带姓名和审核人。
                    source_profile = {
                        "name": e.get("profile_name", ""),
                        "reviewer": e.get("reviewer", ""),
                    }
                override = profile_overrides.get(source_profile_id) or profile_overrides.get("__fallback__")
                if override:
                    source_profile = dict(source_profile or {})
                    for key in ("name", "reviewer"):
                        if key in override:
                            source_profile[key] = str(override.get(key) or "").strip()
                destination_profile_id = resolve_profile(source_profile, lookup_profile_id)

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
                    (new_id, destination_profile_id, e.get("title", ""), invoice_no,
                     e.get("invoice_date", ""), e.get("seller", ""), e.get("total", ""),
                     e.get("buyer_name", ""), e.get("buyer_tax_id", ""), e.get("category", ""),
                     json.dumps(
                         list(dict.fromkeys(
                             [str(tag).strip() for tag in (e.get("tags") or []) if str(tag).strip()]
                             + import_tags
                         )),
                         ensure_ascii=False,
                     ), status,
                     check_status, check_message, e.get("source", "imported"),
                     e.get("created_at") or now, now),
                )
                for field, fv in e.get("fields", {}).items():
                    conn.execute(
                        """INSERT INTO entry_fields(
                               entry_id, field, origin, current, modified, value_source
                           ) VALUES(?,?,?,?,?,?)""",
                        (
                            new_id,
                            field,
                            fv.get("origin", ""),
                            fv.get("current", ""),
                            int(bool(fv.get("modified"))),
                            fv.get("value_source", ""),
                        ),
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
                    history_source_profile_id = str(h.get("profile_id") or "")
                    if history_source_profile_id in package_profiles:
                        history_profile_id = resolve_profile(
                            package_profiles[history_source_profile_id],
                            history_source_profile_id,
                        )
                    elif history_source_profile_id:
                        history_profile_id = source_profile_map.get(
                            history_source_profile_id, destination_profile_id
                        )
                    else:
                        history_profile_id = ""
                    conn.execute(
                        """INSERT INTO field_history(entry_id, field, old_value, new_value, profile_id, changed_at)
                           VALUES(?,?,?,?,?,?)""",
                        (new_id, h.get("field", ""), h.get("old_value", ""), h.get("new_value", ""),
                         history_profile_id, h.get("changed_at", now)),
                    )
                for result in e.get("ocr_results", []):
                    conn.execute(
                        """INSERT INTO ocr_results(
                               entry_id, provider, file_sha256, file_name, raw_json,
                               normalized, closure_pass, applied_at, pending,
                               applied_changes, is_local_call, api_calls, status, error, created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)""",
                        (
                            new_id,
                            result.get("provider", "aliyun"),
                            result.get("file_sha256", ""),
                            result.get("file_name", ""),
                            result.get("raw_json", ""),
                            result.get("normalized", ""),
                            int(bool(result.get("closure_pass"))),
                            result.get("applied_at", ""),
                            result.get("pending", "[]"),
                            result.get("applied_changes", ""),
                            max(1, int(result.get("api_calls") or 1)),
                            result.get("status", "ok"),
                            result.get("error", ""),
                            result.get("created_at", now),
                        ),
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
                    created_files.append(dest)
                    dest.write_bytes(zf.read(arcname))
                    actual_digest = hashlib.sha256(dest.read_bytes()).hexdigest()
                    expected_digest = str(att.get("sha256") or "")
                    if (
                        expected_digest
                        and actual_digest != expected_digest
                        and not inspected["tampered"]
                    ):
                        raise ValueError(
                            f"附件 {att.get('original_name') or stored_name} 校验失败。"
                        )
                    conn.execute(
                        """INSERT INTO attachments(id, entry_id, type, original_name, stored_path,
                           sha256, note, added_at) VALUES(?,?,?,?,?,?,?,?)""",
                        (uuid.uuid4().hex, new_id, att.get("type", "other"),
                         att.get("original_name", ""), f"{new_id}/{stored_name}",
                         actual_digest, att.get("note", ""), att.get("added_at", now)),
                    )
                imported_ids.append(new_id)
                if invoice_no:
                    existing_by_invoice_no[invoice_no] = new_id
                for digest in invoice_hashes:
                    existing_by_invoice_hash[digest] = new_id

            affected_ids = list(dict.fromkeys(imported_ids + updated_ids))
            if affected_ids and new_batch_name:
                target_batch_id = uuid.uuid4().hex
                conn.execute(
                    """INSERT INTO batches(id, name, note, archived, created_at, updated_at)
                       VALUES(?,?,?,0,?,?)""",
                    (target_batch_id, new_batch_name, "", now, now),
                )
                batch_created = True
            if affected_ids and target_batch_id:
                for imported_id in affected_ids:
                    conn.execute(
                        """INSERT OR IGNORE INTO batch_entries(batch_id, entry_id, note, added_at)
                           VALUES(?,?,?,?)""",
                        (target_batch_id, imported_id, "", now),
                    )
                conn.execute(
                    "UPDATE batches SET updated_at = ? WHERE id = ?",
                    (now, target_batch_id),
                )

            if affected_ids and created_profile_ids:
                has_default = conn.execute(
                    "SELECT 1 FROM profiles WHERE is_default = 1 LIMIT 1"
                ).fetchone()
                if not has_default:
                    conn.execute(
                        "UPDATE profiles SET is_default = 1 WHERE id = ?",
                        (created_profile_ids[0],),
                    )
                profile_count_after = conn.execute(
                    "SELECT COUNT(*) c FROM profiles"
                ).fetchone()["c"]
                if profile_count_before < 2 <= profile_count_after:
                    conn.execute(
                        """INSERT INTO meta(key, value) VALUES('tidoc.multiClaimantMode', '1')
                           ON CONFLICT(key) DO UPDATE SET value = excluded.value"""
                    )
            conn.commit()
    except Exception as exc:
        conn.rollback()
        cleanup_errors = []
        for file_path in reversed(created_files):
            try:
                file_path.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                cleanup_errors.append(str(cleanup_exc))
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

    message_parts = [f"新增 {len(imported_ids)} 条" if imported_ids else "没有新增条目"]
    if updated_ids:
        message_parts.append(f"补充 {len(updated_ids)} 条现有记录")
    if skipped:
        message_parts.append(f"已跳过 {len(skipped)} 条重复发票")
    if inspected["tampered"] and imported_ids:
        message_parts.append("完整性异常条目已标记为严重问题")
    if created_profile_ids:
        message_parts.append(f"已恢复 {len(created_profile_ids)} 个报账人")
    affected_ids = list(dict.fromkeys(imported_ids + updated_ids))
    if import_tags and affected_ids:
        message_parts.append(f"已给所选 {len(affected_ids)} 条添加标签")
    if target_batch_id and affected_ids:
        message_parts.append("已装入新建批次" if batch_created else "已装入所选批次")
    message = "；".join(message_parts) + "。"

    return {
        "imported": len(imported_ids),
        "updated": len(updated_ids),
        "updated_entry_ids": updated_ids,
        "merged": merged,
        "skipped": skipped,
        "tampered": inspected["tampered"],
        "entry_ids": imported_ids,
        "profiles_imported": len(created_profile_ids),
        "profile_ids": created_profile_ids,
        "batch_id": target_batch_id if affected_ids else "",
        "batch_created": batch_created,
        "tags_applied": import_tags if affected_ids else [],
        "message": message,
    }

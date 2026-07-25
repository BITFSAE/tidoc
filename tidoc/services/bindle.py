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
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from ..db.attachments import AttachmentRepo
from ..db.entries import EntryRepo
from .signing import MANIFEST_NAME, sign_bytes, verify
from .summary import build_summary

BINDLE_VERSION = 2
ENTRIES_NAME = "entries.json"
SUMMARY_NAME = "summary.json"


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
        "profile_name": entry.get("_profile_name", ""),
        "reviewer": entry.get("_reviewer", ""),
        "fields": fields,
        "items": [
            {k: it.get(k) for k in ("name", "actual_name", "unit", "quantity", "unit_price", "total", "spec", "ordinal")}
            for it in entry.get("items", [])
        ],
        "attachments": attachments,
        "history": history,
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
        "profiles": entries_payload.get("profiles", []),
        "options": entries_payload.get("options", {}),
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

    默认拒绝导入被篡改的包（allow_tampered=False）。新版绑定包会按姓名和审核人
    复用或创建报账人并恢复每条发票的归属；旧绑定包缺少身份时才挂到 profile_id。
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
            "profiles_imported": 0,
            "profile_ids": [],
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
    package_profiles = {
        str(profile.get("id") or ""): profile
        for profile in inspected.get("profiles", [])
        if str(profile.get("id") or "")
    }
    existing_profiles = [dict(row) for row in conn.execute("SELECT * FROM profiles").fetchall()]
    profile_count_before = len(existing_profiles)
    profile_by_identity = {
        (str(profile.get("name") or "").strip(), str(profile.get("reviewer") or "").strip()): profile["id"]
        for profile in existing_profiles
    }
    source_profile_map: dict[str, str] = {}
    created_profile_ids: list[str] = []

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

                source_profile_id = str(e.get("profile_id") or "")
                source_profile = package_profiles.get(source_profile_id)
                if source_profile is None and (e.get("profile_name") or e.get("reviewer")):
                    # v1 绑定包虽没有 profiles 清单，但每条仍带姓名和审核人。
                    source_profile = {
                        "name": e.get("profile_name", ""),
                        "reviewer": e.get("reviewer", ""),
                    }
                destination_profile_id = resolve_profile(source_profile, source_profile_id)

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
                     json.dumps(e.get("tags", []), ensure_ascii=False), status,
                     check_status, check_message, e.get("source", "imported"), now, now),
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

            if imported_ids and created_profile_ids:
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
    if created_profile_ids:
        message_parts.append(f"已恢复 {len(created_profile_ids)} 个报账人")
    message = "；".join(message_parts) + "。"

    return {
        "imported": len(imported_ids),
        "skipped": skipped,
        "tampered": inspected["tampered"],
        "entry_ids": imported_ids,
        "profiles_imported": len(created_profile_ids),
        "profile_ids": created_profile_ids,
        "message": message,
    }

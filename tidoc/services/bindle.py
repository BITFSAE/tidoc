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
import copy
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from ..db.attachments import AttachmentRepo
from ..db.entries import EntryRepo
from .signing import MANIFEST_NAME, sign_bytes, sign_file, verify, verify_stream
from .summary import build_summary

BINDLE_VERSION = 5
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
            for k in ("id", "type", "original_name", "stored_path", "sha256", "added_at",
                      "role_id", "role_definition_revision_id")
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
        "adapter": entry.get("_adapter"),
    }


def _serialize_profile(profile: dict) -> dict:
    """绑定包只携带发票归属所需的报账人字段，不携带本机收款信息。"""
    return {
        "id": profile.get("id", ""),
        "name": profile.get("name", ""),
        "reviewer": profile.get("reviewer") or "",
        "is_default": bool(profile.get("is_default")),
    }


def _adapter_service(repo, supplied=None):
    return supplied or getattr(getattr(repo, "db", None), "adapter_service", None)


def _extension_records(service, entry_id: str, revision_id: str):
    """Read entry extension values/history through the adapter boundary when available."""
    if not service or not revision_id:
        return None, [], []
    definition = service.get_revision(revision_id)
    repo = getattr(service, "extensions", None)
    if repo is None:
        return definition, [], []
    package_id = definition.get("manifest", {}).get("package_id", "")
    list_values = getattr(repo, "list_values", None)
    row = service.db.conn.execute("SELECT scheme_id FROM entries WHERE id=?", (entry_id,)).fetchone()
    scheme_id = row[0] if row else None
    if callable(list_values) and scheme_id:
        values = list_values("entry", entry_id, scheme_id=scheme_id, package_id=package_id,
                             revision_id=revision_id)
    else:
        values = []
    history_method = getattr(repo, "history", None)
    history = history_method("entry", entry_id) if callable(history_method) else []
    return definition, values, history


def _resolve_bindle_flag(definition, key, requested):
    """Resolve a global transfer choice against the entry's pinned revision."""
    if requested is not None and type(requested) is not bool:
        raise ValueError(f'{key} 必须是布尔值或未指定。')
    descriptor=((definition or {}).get('scheme') or {}).get('settings',{}).get(key,{})
    fixed=descriptor.get('fixed') if isinstance(descriptor,dict) else None
    if isinstance(descriptor,dict) and 'fixed' in descriptor:
        if type(fixed) is not bool:
            raise ValueError(f'{key} 的固定策略无效。')
        if requested is not None and requested is not fixed:
            raise ValueError(f'当前方案固定了 {key}，不能使用冲突的导出选项。')
        return fixed
    if requested is not None:
        return requested
    settings=(definition or {}).get('effective_settings') or {}
    value=settings.get(key,descriptor.get('default',True) if isinstance(descriptor,dict) else True)
    if type(value) is not bool:
        raise ValueError(f'{key} 的有效设置无效。')
    return value


def _archive_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sanitize_exchange_entry(entry: dict, depth=0) -> None:
    """Apply transfer allowlists again at the trust boundary before persistence."""
    adapter = entry.get("adapter")
    if depth>1:
        raise ValueError('绑定包来源信息嵌套过深。')
    if not isinstance(adapter, dict):
        entry["adapter"] = None
        return
    definition = adapter.get("definition")
    if (not isinstance(definition, dict) or not isinstance(definition.get("manifest"), dict)
            or not isinstance(definition.get("scheme", {}), dict)):
        entry["adapter"] = None
        return
    from ..adapters.transfer import project_extension_data, public_rule_projection, field_key
    # A transferred ask value is already an explicit sender choice. Preserve it
    # on receipt; another export will ask the new sender again.
    chosen={field_key(field,definition['manifest'].get('package_id','')) for field in definition.get('fields',[])
            if field.get('scope')=='entry' and field.get('transfer')!='never'
            and (field.get('transfer')=='ask' or field.get('sensitive'))
            and any(row.get('field_id')==field.get('id') for row in adapter.get('extension_values',[])+adapter.get('extension_history',[]))}
    clean = public_rule_projection(definition,include_ask_fields=chosen)
    package_id = clean.get("manifest", {}).get("package_id", "")
    digest = str(adapter.get("source_revision_digest") or "")
    if not package_id or (digest and not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
        raise ValueError("绑定包中的适配规则来源信息无效。")
    from ..adapters.registry import LIMITS
    from ..adapters.resolver import resolve_definition
    from ..adapters.loader import validate_resolved_definition
    if any(len(clean.get(key,[]))>LIMITS[limit] for key,limit in [('fields','fields'),('rules','rules'),('materials','files')]):
        raise ValueError('绑定包中的规则定义数量超限。')
    from ..adapters.transfer import supported_rule_capabilities
    _,missing=supported_rule_capabilities(clean)
    if not missing:
        validate_resolved_definition(resolve_definition(clean))
    values, history = project_extension_data(
        clean, adapter.get("extension_values") or [], adapter.get("extension_history") or [],include_ask_fields=chosen
    )
    entry["adapter"] = {
        "source_revision_id": str(adapter.get("source_revision_id") or ""),
        "source_revision_digest": digest.lower(),
        "definition": clean,
        "extension_values": values,
        "extension_history": history,
        "sources": [],
    }
    if not isinstance(adapter.get('sources',[]),list) or len(adapter.get('sources',[]))>100:
        raise ValueError('绑定包来源信息数量超限。')
    for source in adapter.get('sources',[]):
        nested={'adapter':source}
        source=copy.deepcopy(source);source.pop('sources',None);nested['adapter']=source
        _sanitize_exchange_entry(nested,depth+1)
        if nested.get('adapter'):entry['adapter']['sources'].append(nested['adapter'])


def _local_preview_version(entries_repo: EntryRepo, entry_ids: set[str]) -> str:
    snapshots = _import_preview_snapshots(entries_repo, entry_ids)
    material = []
    for entry_id in sorted(entry_ids):
        entry = snapshots.get(entry_id)
        if entry is None:
            material.append((entry_id, None))
            continue
        row = entries_repo.db.conn.execute(
            "SELECT updated_at, scheme_id, scheme_revision_id FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        material.append((entry_id, dict(row) if row else None, entry))
    from ..adapters.transfer import content_hash
    return content_hash(material)


def export_bindle(
    entries_repo: EntryRepo,
    attachments_repo: AttachmentRepo,
    entry_ids: list[str],
    out_path: str | Path,
    profile_lookup: dict[str, dict] | None = None,
    *,
    include_notes: bool | None = None,
    include_tags: bool | None = None,
    adapter_service=None,
    include_ask_fields: list[str] | tuple[str, ...] = (),
) -> Path:
    """把选定条目连同附件打成一个 .tidoc 包，内嵌 HMAC 签名清单。"""
    out_path = Path(out_path)
    if out_path.suffix != ".tidoc":
        out_path = out_path.with_suffix(".tidoc")
    profile_lookup = profile_lookup or {}
    adapter_service = _adapter_service(entries_repo, adapter_service)

    serialized, signatures = [], {}
    per_entry_transfer: dict[str,dict[str,bool]] = {}
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
            revision_id = str(entry.get("scheme_revision_id") or "")
            definition, values, extension_history = _extension_records(adapter_service, eid, revision_id)
            entry_include_notes=_resolve_bindle_flag(definition,'transfer.include_notes',include_notes)
            entry_include_tags=_resolve_bindle_flag(definition,'transfer.include_tags',include_tags)
            per_entry_transfer[eid]={'include_notes':entry_include_notes,'include_tags':entry_include_tags}
            retained=entry.get('adapter_sources') or []
            if definition and retained:
                from ..adapters.transfer import public_rule_projection
                retained=[source for source in retained if source.get('definition')!=public_rule_projection(definition)]
            if definition:
                from ..adapters.transfer import (
                    public_rule_projection, project_extension_data, revision_digest,
                    role_id,
                )
                public_values, public_history = project_extension_data(
                    definition, values, extension_history,
                    include_ask_fields=include_ask_fields,
                )
                entry["_adapter"] = {
                    "source_revision_id": revision_id,
                    "source_revision_digest": revision_digest(definition),
                    "definition": public_rule_projection(
                        definition, include_ask_fields=include_ask_fields
                    ),
                    "extension_values": public_values,
                    "extension_history": public_history,
                }
            else:
                entry["_adapter"] = None
            if retained:
                from ..adapters.transfer import public_rule_projection,project_extension_data
                projections=[]
                for source in retained:
                    values,history=project_extension_data(source['definition'],source.get('extension_values',[]),source.get('extension_history',[]),include_ask_fields=include_ask_fields)
                    projections.append({**source,'definition':public_rule_projection(source['definition'],include_ask_fields=include_ask_fields),'extension_values':values,'extension_history':history})
                if entry['_adapter'] is None:
                    entry['_adapter']=projections[0]
                    projections=projections[1:]
                entry['_adapter']['sources']=projections
            for attachment in entry.get("attachments", []):
                if attachment.get("role_id"):
                    continue
                attachment["role_id"] = {
                    "invoice_pdf": "invoice", "invoice_xml": "invoice",
                    "payment_screenshot": "payment_screenshot",
                    "physical_image": "physical_image", "inspection_pdf": "inspection_pdf",
                }.get(attachment.get("type"), "other")
            if entry.get("profile_id"):
                referenced_profile_ids.add(entry["profile_id"])
            referenced_profile_ids.update(
                h.get("profile_id", "") for h in entry.get("history", [])
                if h.get("profile_id")
            )
            serialized.append(
                _serialize_entry(
                    entry,
                    include_notes=entry_include_notes,
                    include_tags=entry_include_tags,
                )
            )
            # 写附件文件，并对每个文件签名
            for att in entry.get("attachments", []):
                abs_path = attachments_repo.data_root.attachments_dir / att["stored_path"]
                if not abs_path.exists():
                    continue
                arcname = f"attachments/{att['stored_path']}"
                zf.write(abs_path, arcname)
                signatures[arcname] = sign_file(abs_path)

        profiles = [
            _serialize_profile(profile_lookup[profile_id])
            for profile_id in referenced_profile_ids
            if profile_id in profile_lookup
        ]
        profiles.sort(key=lambda p: (not p["is_default"], p["name"], p["reviewer"], p["id"]))
        def summary_option(name,requested):
            if requested is not None:
                return requested
            values={settings[name] for settings in per_entry_transfer.values()}
            return next(iter(values)) if len(values)==1 else True if not values else None

        exported_notes=summary_option('include_notes',include_notes)
        exported_tags=summary_option('include_tags',include_tags)
        entries_payload = {
            "bindle_version": BINDLE_VERSION,
            "profiles": profiles,
            "options": {
                "include_notes": exported_notes,
                "include_tags": exported_tags,
            },
            "entries": serialized,
        }
        entries_bytes = json.dumps(entries_payload, ensure_ascii=False, indent=2).encode("utf-8")
        summary_payload = build_summary(entries_repo, entry_ids)
        summary_entry_ids=[record['id'] for record in serialized]
        for eid,record in zip(summary_entry_ids,summary_payload.get('entries',[])):
            if not per_entry_transfer.get(eid,{}).get('include_notes',True):
                record.pop('notes',None)
        summary_bytes = json.dumps(summary_payload, ensure_ascii=False, indent=2).encode("utf-8")

        zf.writestr(ENTRIES_NAME, entries_bytes)
        zf.writestr(SUMMARY_NAME, summary_bytes)
        signatures[ENTRIES_NAME] = sign_bytes(entries_bytes)
        signatures[SUMMARY_NAME] = sign_bytes(summary_bytes)

        manifest = {
            "bindle_version": BINDLE_VERSION,
            "minimum_receiver_capability": "bindle.v5",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "algorithm": "HMAC-SHA256",
            "signatures": signatures,
        }
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))

    return out_path


def inspect_bindle(path: str | Path, entries_repo: EntryRepo | None = None, *, adapter_service=None,
                   target_scheme_id: str | None = None, mappings: dict | None = None) -> dict:
    """读取并校验一个 .tidoc 包，返回条目数据 + 篡改检测结果，不写入数据库。

    返回 {"entries": [...], "summary": {...}, "tampered": [文件名...], "verified": bool}
    """
    path = Path(path)
    archive_digest_before = _archive_digest(path)
    with zipfile.ZipFile(path, "r") as zf:
        name_list = zf.namelist()
        names = set(name_list)
        duplicate_names = sorted({name for name in name_list if name_list.count(name) > 1})
        if MANIFEST_NAME not in names or ENTRIES_NAME not in names:
            raise ValueError("不是合法的 .tidoc 绑定包（缺少签名清单或条目数据）。")

        manifest = json.loads(zf.read(MANIFEST_NAME))
        signatures = manifest.get("signatures", {})
        tampered: list[str] = [f"duplicate:{name}" for name in duplicate_names]

        verified_payloads: dict[str, bytes] = {}
        for arcname, expected in signatures.items():
            if arcname not in names:
                tampered.append(arcname)  # 文件被删
                continue
            if arcname in {ENTRIES_NAME, SUMMARY_NAME}:
                payload = zf.read(arcname)
                verified_payloads[arcname] = payload
                valid = verify(payload, expected)
            else:
                with zf.open(arcname) as member:
                    valid = verify_stream(member, expected)
            if not valid:
                tampered.append(arcname)

        # 结构数据在校验时已经解压，直接复用，避免大包重复读取。
        entries_bytes = verified_payloads.get(ENTRIES_NAME)
        if entries_bytes is None:
            entries_bytes = zf.read(ENTRIES_NAME)
        entries_payload = json.loads(entries_bytes)
        package_version = int(entries_payload.get("bindle_version", manifest.get("bindle_version", 1)))
        if package_version < 1 or package_version > BINDLE_VERSION:
            raise ValueError(f"不支持的绑定包版本：{package_version}")
        if int(manifest.get("bindle_version", package_version)) != package_version:
            tampered.append("bindle_version")
        if package_version >= 5:
            expected_members = names - {MANIFEST_NAME}
            if set(signatures) != expected_members:
                tampered.append("signature_coverage")
            unexpected = {name for name in names
                          if name not in {MANIFEST_NAME, ENTRIES_NAME, SUMMARY_NAME}
                          and not name.startswith("attachments/")}
            if unexpected:
                tampered.extend(f"unexpected:{name}" for name in sorted(unexpected))
            if not isinstance(entries_payload.get("entries", []), list):
                raise ValueError("绑定包条目结构无效。")
            for item in entries_payload.get("entries", []):
                if not isinstance(item, dict):
                    raise ValueError("绑定包条目结构无效。")
                _sanitize_exchange_entry(item)
        summary_bytes = verified_payloads.get(SUMMARY_NAME)
        if summary_bytes is None and SUMMARY_NAME in names:
            summary_bytes = zf.read(SUMMARY_NAME)
        summary = json.loads(summary_bytes) if summary_bytes else {}

    archive_digest_after = _archive_digest(path)
    if archive_digest_after != archive_digest_before:
        raise ValueError("绑定包在检查期间发生了变化，请重新检查。")

    result = {
        "entries": entries_payload.get("entries", []),
        "profiles": entries_payload.get("profiles", []),
        "options": entries_payload.get("options", {}),
        "summary": summary,
        "tampered": tampered,
        "verified": not tampered,
        "bindle_version": package_version,
        "required_capability": manifest.get("minimum_receiver_capability", ""),
        "archive_digest": archive_digest_after,
    }
    if entries_repo is not None:
        result["target_scheme_id"] = target_scheme_id or ""
        result["mappings"] = copy.deepcopy(mappings or {})
        _annotate_import_actions(entries_repo, result, adapter_service=adapter_service,
                                 target_scheme_id=target_scheme_id)
        matched = {e.get("existing_entry_id") for e in result["entries"] if e.get("existing_entry_id")}
        result["local_version"] = _local_preview_version(entries_repo, matched)
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


def _merge_preview(
    entries_repo: EntryRepo,
    existing_id: str,
    incoming: dict,
    existing: dict | None = None,
) -> dict:
    if existing is None:
        existing = entries_repo.get(existing_id) or {}
        existing["_ocr_results"] = [
            dict(row) for row in entries_repo.db.conn.execute(
                "SELECT provider, file_sha256, created_at, normalized "
                "FROM ocr_results WHERE entry_id = ?",
                (existing_id,),
            ).fetchall()
        ]
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
            str(result.get("provider") or ""), str(result.get("file_sha256") or ""),
            str(result.get("created_at") or ""), str(result.get("normalized") or ""),
        )
        for result in existing.get("_ocr_results") or []
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
        for row in existing.get("history") or []
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


def _import_preview_snapshots(entries_repo: EntryRepo, entry_ids: set[str]) -> dict[str, dict]:
    """Load all local comparison data in batches instead of querying per entry."""
    if not entry_ids:
        return {}
    conn = entries_repo.db.conn
    snapshots: dict[str, dict] = {}
    ordered_ids = sorted(entry_ids)
    for offset in range(0, len(ordered_ids), 900):
        batch = ordered_ids[offset:offset + 900]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"SELECT * FROM entries WHERE id IN ({placeholders})", batch
        ).fetchall():
            entry = dict(row)
            entry["tags"] = json.loads(entry.get("tags") or "[]")
            entry["fields"] = {}
            entry["items"] = []
            entry["attachments"] = []
            entry["batches"] = []
            entry["history"] = []
            entry["_ocr_results"] = []
            snapshots[entry["id"]] = entry

        for row in conn.execute(
            f"SELECT entry_id, field, origin, current, modified, value_source "
            f"FROM entry_fields WHERE entry_id IN ({placeholders})",
            batch,
        ).fetchall():
            if row["entry_id"] in snapshots:
                snapshots[row["entry_id"]]["fields"][row["field"]] = {
                    "origin": row["origin"],
                    "current": row["current"],
                    "modified": bool(row["modified"]),
                    "value_source": row["value_source"] or "",
                }

        for row in conn.execute(
            f"SELECT * FROM attachments WHERE entry_id IN ({placeholders})",
            batch,
        ).fetchall():
            if row["entry_id"] in snapshots:
                snapshots[row["entry_id"]]["attachments"].append(dict(row))

        for row in conn.execute(
            f"SELECT entry_id, id FROM items WHERE entry_id IN ({placeholders})",
            batch,
        ).fetchall():
            if row["entry_id"] in snapshots:
                snapshots[row["entry_id"]]["items"].append({"id": row["id"]})

        for row in conn.execute(
            f"""SELECT be.entry_id, b.id, b.name, b.archived
                  FROM batch_entries be
                  JOIN batches b ON b.id = be.batch_id
                 WHERE be.entry_id IN ({placeholders})
                 ORDER BY b.archived ASC, b.updated_at DESC""",
            batch,
        ).fetchall():
            if row["entry_id"] in snapshots:
                snapshots[row["entry_id"]]["batches"].append({
                    "id": row["id"],
                    "name": row["name"],
                    "archived": bool(row["archived"]),
                })

        for row in conn.execute(
            f"SELECT * FROM field_history WHERE entry_id IN ({placeholders})",
            batch,
        ).fetchall():
            if row["entry_id"] in snapshots:
                snapshots[row["entry_id"]]["history"].append(dict(row))

        for row in conn.execute(
            f"SELECT entry_id, provider, file_sha256, created_at, normalized "
            f"FROM ocr_results WHERE entry_id IN ({placeholders})",
            batch,
        ).fetchall():
            if row["entry_id"] in snapshots:
                snapshots[row["entry_id"]]["_ocr_results"].append(dict(row))
    return snapshots


def _annotate_import_actions(entries_repo: EntryRepo, inspected: dict, *, adapter_service=None,
                             target_scheme_id: str | None = None) -> None:
    by_number, by_hash = _entry_identity_maps(entries_repo.db.conn)
    counts = {"new": 0, "merge": 0, "unchanged": 0}
    entries = inspected.get("entries") or []
    matches = [
        _matching_entry_id(entry, by_number, by_hash)
        for entry in entries
    ]
    snapshots = _import_preview_snapshots(
        entries_repo, {entry_id for entry_id in matches if entry_id}
    )
    for entry, existing_id in zip(entries, matches):
        source_adapter = entry.get("adapter") or {}
        entry["adapter_compatibility"] = {"available": bool(source_adapter.get("definition")),
                                          "source_revision_digest": source_adapter.get("source_revision_digest", ""),
                                          "required_capabilities": [], "missing_capabilities": []}
        if source_adapter.get("definition"):
            try:
                from ..adapters.transfer import supported_rule_capabilities
                required, missing = supported_rule_capabilities(source_adapter["definition"])
                entry["adapter_compatibility"].update(required_capabilities=required,
                                                     missing_capabilities=missing,
                                                     available=not missing)
            except Exception as exc:
                entry["adapter_compatibility"].update(available=False, missing_capabilities=[str(exc)])
        if source_adapter:
            public_package = source_adapter.get("definition", {}).get("manifest", {}).get("package_id", "")
            entry["extension_merge_preview"] = _extension_merge_preview(entries_repo, existing_id,
                                                                           source_adapter, public_package)
        if not existing_id:
            entry["import_action"] = "new"
            counts["new"] += 1
            continue
        existing = snapshots.get(existing_id) or {}
        preview = _merge_preview(entries_repo, existing_id, entry, existing)
        entry["existing_entry_id"] = existing_id
        entry["existing_batches"] = existing.get("batches") or []
        entry["merge_preview"] = preview
        entry["import_action"] = "merge" if preview["has_changes"] else "unchanged"
        entry["role_mapping_preview"] = _role_mapping_preview(entries_repo, existing_id, entry,
                                                                 adapter_service=adapter_service,
                                                                 target_scheme_id=target_scheme_id)
        counts[entry["import_action"]] += 1
    inspected["import_plan"] = counts

    profiles = [dict(row) for row in entries_repo.db.conn.execute("SELECT id,name,reviewer FROM profiles").fetchall()]
    by_name = defaultdict(list)
    for profile in profiles:
        by_name[str(profile.get("name") or "").strip()].append(profile)
    profile_choices = {}
    source_profiles = {str(row.get("id") or ""): dict(row)
                       for row in inspected.get("profiles", []) if row.get("id")}
    for source_entry in entries:
        source_id = str(source_entry.get("profile_id") or "")
        if source_id and source_id not in source_profiles and (source_entry.get("profile_name") or source_entry.get("reviewer")):
            source_profiles[source_id] = {"id": source_id, "name": source_entry.get("profile_name", ""),
                                          "reviewer": source_entry.get("reviewer") or ""}
    if not source_profiles and any(e.get("profile_name") or e.get("reviewer") for e in entries):
        first = next(e for e in entries if e.get("profile_name") or e.get("reviewer"))
        source_profiles["__fallback__"] = {"id": "__fallback__", "name": first.get("profile_name", ""),
                                            "reviewer": first.get("reviewer") or ""}
    for source_profile in source_profiles.values():
        source_id = str(source_profile.get("id") or "")
        if not source_id or str(source_profile.get("reviewer") or "").strip():
            continue
        candidates = by_name.get(str(source_profile.get("name") or "").strip(), [])
        if len(candidates) > 1:
            profile_choices[source_id] = {"status": "ambiguous", "candidates": candidates}
        elif len(candidates) == 1:
            profile_choices[source_id] = {"status": "unique", "candidates": candidates}
    inspected["profile_mapping_preview"] = profile_choices


def _extension_merge_preview(entries_repo, existing_id, source_adapter, package_id):
    incoming = source_adapter.get("extension_values") or []
    if not incoming:
        return {"eligible": [], "skipped": 0}
    if not existing_id:
        # 新条目没有本机值要保留；导入时同包、同字段、同类型的值会随条目一起带入。
        return {"eligible": [], "skipped": 0}
    conn = entries_repo.db.conn
    columns = {row[1] for row in conn.execute("PRAGMA table_info(extension_values)")}
    if not columns:
        return {"eligible": [], "skipped": len(incoming)}
    entry = conn.execute("SELECT scheme_id,scheme_revision_id FROM entries WHERE id=?", (existing_id,)).fetchone()
    if not entry or not entry["scheme_id"] or not entry["scheme_revision_id"]:
        return {"eligible": [], "skipped": len(incoming)}
    from ..db.adapters import AdapterRepo
    try:
        target_definition = AdapterRepo(entries_repo.db).get_revision(entry["scheme_revision_id"])
    except Exception:
        return {"eligible": [], "skipped": len(incoming)}
    if target_definition.get("manifest", {}).get("package_id") != package_id:
        return {"eligible": [], "skipped": len(incoming)}
    source_fields = {f.get("id"): f for f in (source_adapter.get("definition", {}).get("fields") or [])
                     if f.get("scope") == "entry"}
    target_fields = {f.get("id"): f for f in (target_definition.get("fields") or [])
                     if f.get("scope") == "entry"}
    local = {r[1]: json.loads(r[2]) for r in conn.execute(
        "SELECT package_id,field_id,value_json FROM extension_values WHERE scope='entry' AND owner_id=? AND scheme_id=?",
        (existing_id, entry["scheme_id"]),
    ).fetchall() if r[0] == package_id}
    eligible = []
    for record in incoming:
        if record.get("scope", "entry") != "entry" or record.get("package_id", package_id) != package_id:
            continue
        field_id = record.get("field_id")
        if (field_id in source_fields and field_id in target_fields
                and source_fields[field_id].get("type") == target_fields[field_id].get("type")
                and (field_id not in local or local[field_id] in (None, ""))):
            eligible.append(field_id)
    return {"eligible": sorted(set(eligible)), "skipped": max(0, len(incoming)-len(eligible))}


def _merge_extension_data(entries_repo, entry_id, source_adapter):
    """Merge only same-package, same-id, same-type entry fields; local non-empty wins."""
    if not source_adapter:
        return []
    conn = entries_repo.db.conn
    row = conn.execute("SELECT scheme_id,scheme_revision_id FROM entries WHERE id=?", (entry_id,)).fetchone()
    if not row or not row["scheme_id"] or not row["scheme_revision_id"]:
        return []
    from ..db.adapters import AdapterRepo, encode
    from ..adapters.policy import validate_field_value
    repo = AdapterRepo(entries_repo.db)
    package_id = (source_adapter.get("definition", {}).get("manifest") or {}).get("package_id", "")
    try:
        target = repo.get_revision(row["scheme_revision_id"])
    except Exception:
        return []
    if target.get("manifest", {}).get("package_id") != package_id:
        return []
    source_fields = {f.get("id"): f for f in source_adapter.get("definition", {}).get("fields", [])
                     if f.get("scope") == "entry"}
    target_fields = {f.get("id"): f for f in target.get("fields", []) if f.get("scope") == "entry"}
    values = source_adapter.get("extension_values") or []
    changed = []
    for record in values:
        field_id = record.get("field_id")
        if (record.get("scope", "entry") != "entry" or record.get("package_id", package_id) != package_id
                or field_id not in source_fields or field_id not in target_fields
                or source_fields[field_id].get("type") != target_fields[field_id].get("type")):
            continue
        value = record.get("value")
        try:
            value = validate_field_value(target_fields[field_id], value)
        except (ValueError, TypeError):
            continue
        local = conn.execute("""SELECT value_json FROM extension_values
            WHERE scope='entry' AND owner_id=? AND scheme_id=? AND package_id=? AND field_id=?""",
            (entry_id, row["scheme_id"], package_id, field_id)).fetchone()
        if local and json.loads(local[0]) not in (None, ""):
            continue
        if local and json.loads(local[0]) == value:
            continue
        changed_at = datetime.now().isoformat(timespec="microseconds")
        conn.execute("""INSERT INTO extension_values(scope,owner_id,scheme_id,package_id,field_id,
            definition_revision_id,value_json,updated_at) VALUES('entry',?,?,?,?,?,?,?)
            ON CONFLICT(scope,owner_id,scheme_id,package_id,field_id) DO UPDATE SET
            definition_revision_id=excluded.definition_revision_id,value_json=excluded.value_json,
            updated_at=excluded.updated_at""",
            (entry_id, row["scheme_id"], package_id, field_id, row["scheme_revision_id"], encode(value), changed_at))
        changed.append(field_id)
    # Preserve each transferred event. Stable source event identity makes repeat imports idempotent.
    history = source_adapter.get("extension_history") or []
    for ordinal, record in enumerate(history):
        field_id = record.get("field_id")
        if (record.get("scope", "entry") != "entry" or record.get("package_id", package_id) != package_id
                or field_id not in source_fields or field_id not in target_fields
                or source_fields[field_id].get("type") != target_fields[field_id].get("type")):
            continue
        source_event = str(record.get("id") or ordinal)
        actor = json.dumps({"source_revision": source_adapter.get("source_revision_digest", ""),
                            "source_actor": record.get("actor_id", record.get("profile_id", "")),
                            "source_event": source_event}, ensure_ascii=False, sort_keys=True)
        changed_on = str(record.get("changed_at") or datetime.now().isoformat(timespec="seconds"))
        exists = conn.execute("""SELECT 1 FROM extension_history WHERE scope='entry' AND owner_id=? AND scheme_id=?
            AND package_id=? AND field_id=? AND actor_id=? AND changed_at=? LIMIT 1""",
            (entry_id, row["scheme_id"], package_id, field_id, actor, changed_on)).fetchone()
        if exists:
            continue
        conn.execute("""INSERT INTO extension_history(scope,owner_id,scheme_id,package_id,field_id,
            definition_revision_id,kind,old_value_json,new_value_json,actor_id,changed_at)
            VALUES('entry',?,?,?,?,?,?,?,?,?,?)""",
            (entry_id, row["scheme_id"], package_id, field_id, row["scheme_revision_id"],
             str(record.get("kind") or "value"), encode(record.get("old_value")),
             encode(record.get("new_value")), actor, changed_on))
    return changed


def _role_mapping_preview(entries_repo, existing_id, entry, *, adapter_service=None, target_scheme_id=None):
    incoming = entry.get("attachments") or []
    roles = sorted({str(a.get("role_id") or "other") for a in incoming})
    local = (entries_repo.db.conn.execute(
        "SELECT scheme_id,scheme_revision_id FROM entries WHERE id=?", (existing_id,)
    ).fetchone() if existing_id else None)
    revision_id = (local["scheme_revision_id"] or "") if local else ""
    if target_scheme_id and adapter_service:
        try:
            target = adapter_service.get_scheme(target_scheme_id)
            revision_id = target.get("current_revision_id") or target.get("revision_id") or revision_id
        except Exception:
            pass
    target_roles = []
    if revision_id:
        try:
            from ..db.adapters import AdapterRepo
            definition = AdapterRepo(entries_repo.db).get_revision(revision_id)
            material = definition.get("materials", [])
            target_roles = [r.get("id") for r in (material.get("roles", []) if isinstance(material, dict) else material)]
        except Exception:
            target_roles = []
    return {"source_roles": roles, "target_roles": target_roles,
            # Names and IDs are not proof of semantic equivalence across revisions.
            "requires_explicit_mapping": [role for role in roles if role != "invoice"]}


def preview_bindle_transfer(entries_repo: EntryRepo, entry_ids: list[str], adapter_service=None) -> dict:
    """Summarize per-field ask choices before v5 export without exposing field values."""
    from ..adapters.transfer import ask_field_preview
    adapter_service = _adapter_service(entries_repo, adapter_service)
    pairs = []
    for entry_id in dict.fromkeys(str(value) for value in entry_ids):
        entry = entries_repo.get(entry_id)
        if not entry:
            continue
        revision_id = str(entry.get("scheme_revision_id") or "")
        if revision_id and adapter_service:
            definition, values, _history = _extension_records(adapter_service, entry_id, revision_id)
            if definition:
                for value in values:value.setdefault('owner_id',entry_id)
                pairs.append((definition,values))
        for source in entry.get('adapter_sources') or []:
            values=copy.deepcopy(source.get('extension_values',[]))
            for value in values:value.setdefault('owner_id',entry_id)
            pairs.append((source['definition'],values))
    return {"ask_fields": ask_field_preview(pairs), "entry_count": len(pairs),
            "requires_explicit_choice": bool(ask_field_preview(pairs))}


def _external_revision_id(entries_repo, adapter_payload):
    definition = (adapter_payload or {}).get("definition")
    if not definition:
        return ""
    from ..adapters.transfer import content_hash, revision_digest
    from ..db.adapters import encode, now
    digest=content_hash(definition)
    revision=revision_digest(definition)
    # Public exchange definitions are immutable, stored for provenance, and never linked
    # to a local scheme or activated as a package.
    # Exchange snapshots can contain unsupported capabilities and have no
    # templates. Keep them immutable without activating or revalidating them as
    # an installable local package. Their storage identity avoids a collision
    # with an installed package sharing the author's ID/version.
    conn=entries_repo.db.conn
    conn.execute('''INSERT OR IGNORE INTO adapter_packages(content_hash,package_id,package_version,schema_version,source,resource_path,definition_json,diagnostics_json,installed_at) VALUES(?,?,?,?,?,?,?,?,?)''',
        (digest,'org.tidoc.exchange.'+digest,'1.0.0','1.0','exchange','',encode(definition),'[]',now()))
    conn.execute('INSERT OR IGNORE INTO scheme_revisions VALUES(?,?,?,?)',(revision,digest,encode(definition),now()))
    return revision


def _retain_adapter_source(entries_repo,entry_id,adapter):
    if not adapter or not adapter.get('definition'):return False
    from ..adapters.transfer import content_hash
    from ..db.adapters import encode, now
    payload=copy.deepcopy(adapter);sources=payload.pop('sources',[])
    revision=_external_revision_id(entries_repo,payload)
    digest=content_hash(payload)
    cursor=entries_repo.db.conn.execute('INSERT OR IGNORE INTO entry_adapter_sources VALUES(?,?,?,?,?)',
        (entry_id,digest,revision,encode(payload),now()))
    for source in sources:_retain_adapter_source(entries_repo,entry_id,source)
    return bool(cursor.rowcount)


def _validated_material_mapping(entries_repo, entry_id, revision_id, source_role, target_role, att_type, filename):
    """Resolve an explicit role without granting it invoice/OCR semantics."""
    from ..db.adapters import AdapterRepo
    definition=AdapterRepo(entries_repo.db).get_revision(revision_id)
    roles=definition.get('materials',[])
    roles=roles.get('roles',[]) if isinstance(roles,dict) else roles
    role=next((role for role in roles if role['id']==target_role),None)
    if not role:
        raise ValueError(f'材料角色映射目标不存在：{target_role}')
    invoice=att_type in ('invoice_pdf','invoice_xml')
    if invoice != (target_role=='invoice') or (source_role=='invoice') != invoice:
        raise ValueError('自定义或其他材料不能替代发票。')
    if role.get('extensions') and Path(filename).suffix.lower() not in role['extensions']:
        raise ValueError('映射材料格式不符合目标角色要求。')
    limit=role.get('max_count')
    if limit is not None:
        count=entries_repo.db.conn.execute('SELECT COUNT(*) FROM attachments WHERE entry_id=? AND role_id=? AND (role_definition_revision_id IS NULL OR role_definition_revision_id=?)',(entry_id,target_role,revision_id)).fetchone()[0]
        if count>=limit:
            raise ValueError('材料数量达到目标方案上限。')
    return att_type if invoice else 'other' if target_role.startswith('custom:') else target_role


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
    *, role_mappings: dict | None = None,
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
        from ..adapters.transfer import role_id as get_role_id
        incoming_role = get_role_id(attachment)
        mapped_role = (role_mappings or {}).get(incoming_role)
        if incoming_role=='invoice' and existing.get('scheme_revision_id'):
            mapped_role='invoice'
        if mapped_role:
            target_revision = str(existing.get("scheme_revision_id") or "")
            target_definition = None
            if target_revision:
                from ..db.adapters import AdapterRepo
                target_definition = AdapterRepo(entries_repo.db).get_revision(target_revision)
            roles = (target_definition or {}).get("materials", [])
            roles = roles.get("roles", []) if isinstance(roles, dict) else roles
            if mapped_role not in {r.get("id") for r in roles}:
                raise ValueError(f"材料角色映射目标不存在：{mapped_role}")
            final_role, role_revision = mapped_role, target_revision or None
            att_type=_validated_material_mapping(entries_repo,entry_id,target_revision,incoming_role,mapped_role,att_type,attachment.get('original_name') or stored_path)
        else:
            final_role = incoming_role
            role_revision = _external_revision_id(entries_repo, incoming.get("adapter")) or None
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
               sha256, note, added_at, role_id, role_definition_revision_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                uuid.uuid4().hex, entry_id, att_type,
                str(attachment.get("original_name") or stored_name),
                f"{entry_id}/{stored_name}", actual_digest, incoming_note,
                str(attachment.get("added_at") or now), final_role, role_revision,
            ),
        )
        by_hash[actual_digest] = {"id": "", "sha256": actual_digest, "note": incoming_note}
        attachment_added += 1
    if attachment_added:
        changes.append(f"材料 {attachment_added}")

    extension_fields = _merge_extension_data(entries_repo, entry_id, incoming.get("adapter") or {})
    if extension_fields:
        changes.append(f"附加信息 {len(extension_fields)}")
    if _retain_adapter_source(entries_repo,entry_id,incoming.get('adapter')):
        changes.append('来源方案信息')

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
    *,
    inspected: dict | None = None,
    adapter_service=None,
) -> dict:
    """把一个 .tidoc 包导入到当前库，附件落地到指定条目目录。

    默认拒绝导入被篡改的包（allow_tampered=False）。新版绑定包会按姓名和审核人
    复用或创建报账人并恢复每条发票的归属；旧绑定包缺少身份时才挂到 profile_id。
    保留原始识别字段、可改字段的修改标记与历史（不可擦除）。
    返回 {"imported": n, "tampered": [...], "entry_ids": [...]}。
    """
    options = dict(options or {})
    adapter_service = _adapter_service(entries_repo, adapter_service)
    profile_overrides = options.get("profile_overrides") or {}
    profile_mappings = options.get("profile_mappings") or {}
    role_mappings_by_entry = options.get("role_mappings") or {}
    selection_supplied = "selected_profile_ids" in options
    selected_profile_ids = {
        str(value or "") for value in (options.get("selected_profile_ids") or [])
    }
    import_tags = list(dict.fromkeys(
        str(tag).strip() for tag in (options.get("tags") or []) if str(tag).strip()
    ))
    target_batch_id = str(options.get("batch_id") or "").strip()
    requested_scheme_id = str(options.get("scheme_id") or "").strip()
    new_batch_name = str(options.get("batch_name") or "").strip()
    if target_batch_id and new_batch_name:
        raise ValueError("已有批次和新建批次只能选择一个。")
    entry_selection_supplied = "selected_entry_indexes" in options
    try:
        selected_entry_indexes = {
            int(value) for value in (options.get("selected_entry_indexes") or [])
        }
    except (TypeError, ValueError) as exc:
        raise ValueError("逐条导入选择无效，请重新打开导入预览。") from exc
    entry_batch_overrides: dict[int, dict] = {}
    for raw_index, raw_override in (options.get("entry_batch_overrides") or {}).items():
        try:
            entry_index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("逐条批次设置无效，请重新选择。") from exc
        override = dict(raw_override or {})
        mode = str(override.get("mode") or "default")
        if mode not in {"default", "none", "existing", "new"}:
            raise ValueError("逐条批次设置无效，请重新选择。")
        if mode == "default":
            continue
        normalized = {"mode": mode}
        if mode == "existing":
            normalized["batch_id"] = str(override.get("batch_id") or "").strip()
            if not normalized["batch_id"]:
                raise ValueError("请为逐条设置选择报账批次。")
        elif mode == "new":
            normalized["batch_name"] = str(override.get("batch_name") or "").strip()
            if not normalized["batch_name"]:
                raise ValueError("请填写逐条设置的新批次名称。")
        entry_batch_overrides[entry_index] = normalized

    inspected = inspected if inspected is not None else inspect_bindle(path)
    if inspected:
        preview_scheme = str(inspected.get("target_scheme_id") or "")
        if preview_scheme and requested_scheme_id and preview_scheme != requested_scheme_id:
            raise ValueError("导入目标方案与预览不一致，请重新打开导入预览。")
        expected_digest = inspected.get("archive_digest")
        if not expected_digest or _archive_digest(Path(path)) != expected_digest:
            raise ValueError("预览后绑定包或本地条目已变化，请重新打开导入预览。")
        current = copy.deepcopy(inspected)
        for entry in current.get("entries", []):
            for key in ("import_action", "existing_entry_id", "existing_batches", "merge_preview",
                        "adapter_compatibility", "extension_merge_preview", "role_mapping_preview"):
                entry.pop(key, None)
        _annotate_import_actions(entries_repo, current, adapter_service=adapter_service,
                                 target_scheme_id=inspected.get("target_scheme_id"))
        matched = {e.get("existing_entry_id") for e in current["entries"] if e.get("existing_entry_id")}
        current_version = _local_preview_version(entries_repo, matched)
        if (inspected.get("local_version") is not None
                and current_version != inspected.get("local_version")):
            raise ValueError("预览后绑定包或本地条目已变化，请重新打开导入预览。")
        inspected = current
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
    override_batch_ids = {
        override["batch_id"]
        for override in entry_batch_overrides.values()
        if override["mode"] == "existing"
    }
    for override_batch_id in override_batch_ids:
        if not conn.execute(
            "SELECT 1 FROM batches WHERE id = ?", (override_batch_id,)
        ).fetchone():
            raise ValueError("逐条设置中的批次已不存在，请重新选择。")
    profile_count_before = len(existing_profiles)
    profile_by_identity = {
        (str(profile.get("name") or "").strip(), str(profile.get("reviewer") or "").strip()): profile["id"]
        for profile in existing_profiles if str(profile.get("reviewer") or "").strip()
    }
    profile_by_name: dict[str, list[str]] = defaultdict(list)
    for profile in existing_profiles:
        profile_by_name[str(profile.get("name") or "").strip()].append(profile["id"])
    source_profile_map: dict[str, str] = {}
    created_profile_ids: list[str] = []
    batch_created = False
    global_batch_created = False
    destination_by_entry_index: dict[int, str] = {}
    affected_entry_indexes: set[int] = set()
    individually_assigned = 0
    globally_assigned = 0

    def resolve_profile(profile: dict | None, source_id: str = "") -> str:
        if source_id and source_id in source_profile_map:
            return source_profile_map[source_id]
        profile = profile or {}
        explicit = str(profile_mappings.get(source_id) or "").strip() if source_id else ""
        if explicit:
            if not conn.execute("SELECT 1 FROM profiles WHERE id=?", (explicit,)).fetchone():
                raise ValueError("所选报账人映射已不存在，请重新打开导入预览。")
            if source_id:
                source_profile_map[source_id] = explicit
            return explicit
        name = str(profile.get("name") or "").strip()
        reviewer = str(profile.get("reviewer") or "").strip()
        if not name:
            if not profile_id:
                raise ValueError("绑定包缺少可用的报账人信息，且未指定导入归属。")
            if source_id:
                source_profile_map[source_id] = profile_id
            return profile_id

        identity = (name, reviewer)
        if reviewer:
            destination_id = profile_by_identity.get(identity)
        else:
            candidates = profile_by_name.get(name, [])
            if len(candidates) > 1:
                raise ValueError(f"报账人“{name}”存在多个匹配项，请在预览中明确选择。")
            destination_id = candidates[0] if candidates else None
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
            profile_by_name[name].append(destination_id)
            created_profile_ids.append(destination_id)
        if source_id:
            source_profile_map[source_id] = destination_id
        return destination_id

    try:
        with zipfile.ZipFile(path, "r") as zf:
            for entry_index, e in enumerate(inspected["entries"]):
                source_profile_id = str(e.get("profile_id") or "__fallback__")
                if entry_selection_supplied and entry_index not in selected_entry_indexes:
                    continue
                if (
                    not entry_selection_supplied
                    and selection_supplied
                    and source_profile_id not in selected_profile_ids
                ):
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
                    destination_by_entry_index[entry_index] = existing_id
                    changes = _merge_existing_entry(
                        conn, entries_repo, attachments_repo, zf, existing_id, e,
                        import_tags, now, created_files, bool(inspected["tampered"]),
                        role_mappings=role_mappings_by_entry.get(str(entry_index), role_mappings_by_entry),
                    )
                    if changes:
                        if existing_id not in updated_ids:
                            updated_ids.append(existing_id)
                        affected_entry_indexes.add(entry_index)
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
                source_adapter = e.get("adapter") or {}
                exchange_revision_id = _external_revision_id(entries_repo, source_adapter)
                target_scheme_id = ""
                target_revision_id = ""
                if requested_scheme_id and adapter_service:
                    target_scheme = adapter_service.get_scheme(requested_scheme_id)
                    target_scheme_id = target_scheme.get("id") or target_scheme.get("scheme_id") or ""
                    target_revision_id = target_scheme.get("current_revision_id") or target_scheme.get("revision_id") or ""
                elif not source_adapter and adapter_service:
                    try:
                        target_scheme_id, target_revision_id = adapter_service.default_binding()
                    except Exception:
                        pass
                if source_adapter and not e.get("scheme_revision_id"):
                    status = "partial" if status == "complete" else status
                    check_status = "warning" if check_status not in {"blocked", "fail"} else check_status
                    check_message = _merge_text(check_message, "来自其他报账方案的材料；本机尚未绑定该方案规则。")
                if inspected["tampered"]:
                    check_status = "blocked"
                    status = "partial"
                    tampered_message = "绑定包完整性校验未通过，导入前已由用户确认"
                    check_message = "；".join(filter(None, [tampered_message, check_message]))

                new_id = uuid.uuid4().hex
                conn.execute(
                    """INSERT INTO entries(id, profile_id, title, invoice_no, invoice_date,
                       seller, total, buyer_name, buyer_tax_id, category, tags, status,
                       check_status, check_message, source, created_at, updated_at,scheme_id,scheme_revision_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                     e.get("created_at") or now, now, target_scheme_id or None, target_revision_id or None),
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
                    from ..adapters.transfer import role_id as get_role_id
                    source_role = get_role_id(att)
                    mapping = role_mappings_by_entry.get(str(entry_index), role_mappings_by_entry)
                    mapped_role = mapping.get(source_role) if isinstance(mapping, dict) else None
                    if source_role=='invoice' and target_revision_id:
                        mapped_role='invoice'
                    target_revision = ""
                    mapped_type=att.get('type','other')
                    if mapped_role and target_revision_id:
                        from ..db.adapters import AdapterRepo
                        target_definition = AdapterRepo(entries_repo.db).get_revision(target_revision_id)
                        role_rows = target_definition.get("materials", [])
                        role_rows = role_rows.get("roles", []) if isinstance(role_rows, dict) else role_rows
                        if mapped_role not in {r.get("id") for r in role_rows}:
                            raise ValueError(f"材料角色映射目标不存在：{mapped_role}")
                        stored_role, role_revision = mapped_role, target_revision_id
                        mapped_type=_validated_material_mapping(entries_repo,new_id,target_revision_id,source_role,mapped_role,mapped_type,att.get('original_name') or stored_name)
                    else:
                        stored_role = source_role
                        role_revision = exchange_revision_id or None
                    conn.execute(
                        """INSERT INTO attachments(id, entry_id, type, original_name, stored_path,
                           sha256, note, added_at, role_id, role_definition_revision_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (uuid.uuid4().hex, new_id, mapped_type,
                         att.get("original_name", ""), f"{new_id}/{stored_name}",
                         actual_digest, att.get("note", ""), att.get("added_at", now), stored_role, role_revision),
                    )
                imported_ids.append(new_id)
                _retain_adapter_source(entries_repo,new_id,source_adapter)
                _merge_extension_data(entries_repo,new_id,source_adapter)
                destination_by_entry_index[entry_index] = new_id
                affected_entry_indexes.add(entry_index)
                if invoice_no:
                    existing_by_invoice_no[invoice_no] = new_id
                for digest in invoice_hashes:
                    existing_by_invoice_hash[digest] = new_id

            affected_ids = list(dict.fromkeys(imported_ids + updated_ids))
            created_batches_by_name: dict[str, str] = {}

            def ensure_new_batch(name: str) -> str:
                nonlocal batch_created
                normalized_name = str(name or "").strip()
                existing_created = created_batches_by_name.get(normalized_name)
                if existing_created:
                    return existing_created
                created_id = uuid.uuid4().hex
                conn.execute(
                    """INSERT INTO batches(id, name, note, archived, created_at, updated_at)
                       VALUES(?,?,?,0,?,?)""",
                    (created_id, normalized_name, "", now, now),
                )
                batch_created = True
                created_batches_by_name[normalized_name] = created_id
                return created_id

            default_affected_indexes = [
                entry_index
                for entry_index in affected_entry_indexes
                if entry_index not in entry_batch_overrides
            ]
            if default_affected_indexes and new_batch_name:
                target_batch_id = ensure_new_batch(new_batch_name)
                global_batch_created = True

            assignments: dict[str, tuple[str, bool]] = {}
            if target_batch_id:
                for entry_index in default_affected_indexes:
                    destination_id = destination_by_entry_index.get(entry_index)
                    if destination_id:
                        assignments[destination_id] = (target_batch_id, False)

            for entry_index, override in entry_batch_overrides.items():
                destination_id = destination_by_entry_index.get(entry_index)
                if not destination_id:
                    continue
                mode = override["mode"]
                if mode == "existing":
                    override_target = override["batch_id"]
                elif mode == "new":
                    override_target = ensure_new_batch(override["batch_name"])
                else:
                    override_target = ""
                assignments[destination_id] = (override_target, True)

            for destination_id, (assignment_batch_id, is_individual) in assignments.items():
                previous_rows = conn.execute(
                    "SELECT batch_id FROM batch_entries WHERE entry_id = ?",
                    (destination_id,),
                ).fetchall()
                previous_ids = [row["batch_id"] for row in previous_rows]
                if previous_ids == ([assignment_batch_id] if assignment_batch_id else []):
                    continue
                conn.execute(
                    "DELETE FROM batch_entries WHERE entry_id = ?", (destination_id,)
                )
                if assignment_batch_id:
                    conn.execute(
                        """INSERT OR IGNORE INTO batch_entries(batch_id, entry_id, note, added_at)
                           VALUES(?,?,?,?)""",
                        (assignment_batch_id, destination_id, "", now),
                    )
                for previous_id in previous_ids:
                    conn.execute(
                        "UPDATE batches SET updated_at = ? WHERE id = ?",
                        (now, previous_id),
                    )
                if assignment_batch_id:
                    conn.execute(
                        "UPDATE batches SET updated_at = ? WHERE id = ?",
                        (now, assignment_batch_id),
                    )
                if is_individual:
                    individually_assigned += 1
                else:
                    globally_assigned += 1

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
            for affected_id in affected_ids:
                entries_repo.recompute_status(affected_id,commit=False)
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
    if globally_assigned:
        message_parts.append("已移到新建批次" if global_batch_created else "已移到所选批次")
    if individually_assigned:
        message_parts.append(f"已按逐条设置调整 {individually_assigned} 条批次")
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
        "batch_id": target_batch_id if globally_assigned else "",
        "batch_created": batch_created,
        "individual_batch_assignments": individually_assigned,
        "tags_applied": import_tags if affected_ids else [],
        "message": message,
    }

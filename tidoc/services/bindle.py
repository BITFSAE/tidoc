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
import uuid
import zipfile
import copy
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from ..db.attachments import AttachmentRepo
from ..db.entries import EntryRepo
from ..db.manual_changes import item_snapshot
from ..engine.check_lines import INSPECTION_CHECK_PREFIX, PAYMENT_CHECK_PREFIX, split_message
from .entry_merge import (
    BATCH_NOTE_PREFIX,
    BASE_FIELDS,
    COMPARABLE_BASE_FIELDS,
    EXCLUSIVE_TAG_GROUPS,
    MERGE_HISTORY_PREFIX,
    attachment_group_key,
    build_entry_snapshot,
    known_ancestors,
    load_exchange_bases,
    merge_tags as _merge_tags,
    merge_text as _merge_text,
    plan_entry_merge,
    record_exchange_base,
    snapshot_digest,
    validate_entry_decisions,
    _payment_recognition_upgrades,
)
from .inspection_pdf import cached_inspection_info, inspection_summary, read_inspection_pdf
from .signing import MANIFEST_NAME, sign_bytes, sign_file, verify, verify_stream
from .summary import build_summary

BINDLE_VERSION = 5
ENTRIES_NAME = "entries.json"
SUMMARY_NAME = "summary.json"



PAYMENT_RECOGNITION_KEYS = ("recognition_version", "recognition_status", "recognized_value", "recognition_message")
_PAYMENT_RECOGNITION_STATUSES = {"recognized", "review", "unrecognized", "error"}


def _imported_payment_recognition(attachment: dict, att_type: str) -> tuple[str, str, str, str]:
    """包里的付款截图识别结论；格式不对或已不是付款截图时丢弃，按未识别处理。"""
    import re
    empty = ("", "", "", "")
    if att_type != "payment_screenshot":
        return empty
    version, status, value, message = (str(attachment.get(key) or "") for key in PAYMENT_RECOGNITION_KEYS)
    if status not in _PAYMENT_RECOGNITION_STATUSES or not version or len(version) > 80:
        return empty
    if value and not re.fullmatch(r"(0|[1-9][0-9]{0,11})\.[0-9]{2}", value):
        return empty
    # 标成导入的结果：同一识别规则下视为已识别，不因对方用了别的服务商而重新上传计费。
    return version.split(":")[0] + ":imported", status, value, message[:300]

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
        if include_notes or not (h.get("field") == "notes" or str(h.get("field") or "").endswith("]notes"))
    ]
    attachments = []
    for attachment in entry.get("attachments", []):
        serialized_attachment = {
            k: attachment.get(k)
            for k in ("id", "type", "original_name", "stored_path", "sha256", "added_at",
                      "role_id", "role_definition_revision_id")
        }
        serialized_attachment["note"] = attachment.get("note", "") if include_notes else ""
        # 付款截图的 AI 识别结论随包走，对方不用为同一张截图再付费识别。
        if attachment.get("type") == "payment_screenshot" and attachment.get("recognition_status"):
            for key in PAYMENT_RECOGNITION_KEYS:
                serialized_attachment[key] = attachment.get(key) or ""
        # 查验单正文摘要随包走，便于接收方在冲突对比时直接看到第几次查验与查验时间。
        if attachment.get("type") == "inspection_pdf":
            info = cached_inspection_info(
                attachment.get("recognition_version"),
                attachment.get("recognized_value"),
                attachment.get("sha256"),
            )
            if info is not None:
                serialized_attachment["inspection"] = inspection_summary(info)
        attachments.append(serialized_attachment)

    payload = {
        "id": entry["id"],
        "profile_id": entry.get("profile_id", ""),
        "title": entry.get("title", ""),
        "invoice_no": entry.get("invoice_no", ""),
        "invoice_date": entry.get("invoice_date", ""),
        "seller": entry.get("seller", ""),
        "seller_tax_id": entry.get("seller_tax_id", ""),
        "recognition_data": entry.get("recognition_data", ""),
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
    if include_notes and str(entry.get("_batch_note") or "").strip():
        payload["batch_note"] = str(entry.get("_batch_note") or "").strip()
    if not include_notes or not include_tags:
        payload["transfer_options"] = {
            "include_notes": bool(include_notes),
            "include_tags": bool(include_tags),
        }
    return payload


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


def _has_archive_member(archive: zipfile.ZipFile, arcname: str) -> bool:
    """Constant-time membership test; namelist() would rebuild the whole list on every call."""
    try:
        archive.getinfo(arcname)
    except KeyError:
        return False
    return True


def _extract_archive_member(archive: zipfile.ZipFile, arcname: str, dest: Path) -> str:
    """Stream one member to disk and return its sha256 without holding the file in memory."""
    digest = hashlib.sha256()
    with archive.open(arcname) as source, dest.open("wb") as target:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
            target.write(block)
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
    export_snapshots: list[tuple[str, dict]] = []
    try:
        batch_note_by_entry = {
            row["entry_id"]: str(row["note"] or "").strip()
            for row in entries_repo.db.conn.execute(
                "SELECT entry_id, note FROM batch_entries WHERE TRIM(COALESCE(note, '')) <> ''"
            ).fetchall()
        }
    except Exception:
        batch_note_by_entry = {}
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for eid in entry_ids:
            entry = entries_repo.get(eid)
            if not entry:
                continue
            prof = profile_lookup.get(entry.get("profile_id"), {})
            entry["_profile_name"] = prof.get("name", "")
            entry["_reviewer"] = prof.get("reviewer", "")
            entry["_batch_note"] = batch_note_by_entry.get(eid, "")
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
            serialized_entry = _serialize_entry(
                entry,
                include_notes=entry_include_notes,
                include_tags=entry_include_tags,
            )
            snap = build_entry_snapshot(serialized_entry)
            snap_hash = snapshot_digest(snap)
            previous_ancestors = known_ancestors(entries_repo.db.conn, eid)
            serialized_entry["exchange_ancestors"] = list(
                dict.fromkeys([snap_hash, *previous_ancestors])
            )[:20]
            export_snapshots.append((eid, snap))
            serialized.append(serialized_entry)
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

    if export_snapshots:
        now = datetime.now().isoformat(timespec="seconds")
        conn = entries_repo.db.conn
        if hasattr(conn, "commit"):
            try:
                for eid, snap in export_snapshots:
                    record_exchange_base(conn, eid, snap, "export", now)
                conn.commit()
            except Exception:
                if hasattr(conn, "rollback"):
                    conn.rollback()

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
        duplicate_names = sorted(name for name, count in Counter(name_list).items() if count > 1)
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
                                 target_scheme_id=target_scheme_id, archive_path=path)
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


def _entry_transfer_flags(inspected_options: dict | None, entry: dict) -> tuple[bool | None, bool | None]:
    entry_opts = entry.get("transfer_options") if isinstance(entry.get("transfer_options"), dict) else {}
    pkg_opts = inspected_options if isinstance(inspected_options, dict) else {}
    include_notes = entry_opts["include_notes"] if "include_notes" in entry_opts else pkg_opts.get("include_notes")
    include_tags = entry_opts["include_tags"] if "include_tags" in entry_opts else pkg_opts.get("include_tags")
    return include_notes, include_tags


def _fill_archive_inspection_hints(archive_path: Path | None, existing: dict, incoming: dict) -> None:
    """Lazily read inspection PDF metadata from an older bindle when comparing differing files."""
    if not archive_path:
        return
    local_insp = {
        str(att.get("sha256") or "")
        for att in existing.get("attachments") or []
        if att.get("type") == "inspection_pdf" and str(att.get("sha256") or "")
    }
    missing = [
        att for att in incoming.get("attachments") or []
        if att.get("type") == "inspection_pdf"
        and not isinstance(att.get("inspection"), dict)
        and str(att.get("sha256") or "") not in local_insp
        and str(att.get("stored_path") or "")
    ]
    if not missing or not local_insp:
        return
    import io
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            for att in missing:
                arcname = f"attachments/{att['stored_path']}"
                if not _has_archive_member(zf, arcname):
                    continue
                data = zf.read(arcname)
                att["inspection"] = inspection_summary(read_inspection_pdf(io.BytesIO(data)))
    except (OSError, zipfile.BadZipFile, ValueError):
        pass


def _merge_preview(
    entries_repo: EntryRepo,
    existing_id: str,
    incoming: dict,
    existing: dict | None = None,
    *,
    local_bases: dict[str, dict] | None = None,
    extension_preview: dict | None = None,
    include_notes: bool | None = True,
    include_tags: bool | None = True,
) -> dict:
    if existing is None:
        existing = _import_preview_snapshots(entries_repo, {existing_id}).get(existing_id) or {}
    if local_bases is None:
        local_bases = load_exchange_bases(entries_repo.db.conn, {existing_id}).get(existing_id, {})
    return plan_entry_merge(
        existing,
        incoming,
        local_bases=local_bases,
        local_batch_note=str(existing.get("_batch_note") or ""),
        local_extension_values=existing.get("_extension_values"),
        extension_preview=extension_preview,
        include_notes=include_notes,
        include_tags=include_tags,
    )


def _import_preview_snapshots(entries_repo: EntryRepo, entry_ids: set[str]) -> dict[str, dict]:
    """Load all local comparison data in batches instead of querying per entry."""
    if not entry_ids:
        return {}
    conn = entries_repo.db.conn
    snapshots: dict[str, dict] = {}
    ordered_ids = sorted(entry_ids)
    ext_table = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='extension_values'"
    ).fetchone())
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
            entry["_batch_note"] = ""
            entry["_extension_values"] = []
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
            f"SELECT * FROM attachments WHERE entry_id IN ({placeholders}) ORDER BY added_at, id",
            batch,
        ).fetchall():
            if row["entry_id"] in snapshots:
                snapshots[row["entry_id"]]["attachments"].append(dict(row))

        for row in conn.execute(
            f"""SELECT entry_id, id, name, actual_name, unit, quantity,
                       unit_price, total, spec, ordinal
                  FROM items WHERE entry_id IN ({placeholders})
                 ORDER BY ordinal, id""",
            batch,
        ).fetchall():
            if row["entry_id"] in snapshots:
                snapshots[row["entry_id"]]["items"].append(dict(row))

        for row in conn.execute(
            f"""SELECT be.entry_id, b.id, b.name, b.archived, be.note AS batch_note
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
                if row["batch_note"] and not snapshots[row["entry_id"]]["_batch_note"]:
                    snapshots[row["entry_id"]]["_batch_note"] = str(row["batch_note"])

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

        if ext_table:
            for row in conn.execute(
                f"""SELECT owner_id, scheme_id, package_id, field_id, value_json
                      FROM extension_values
                     WHERE scope = 'entry' AND owner_id IN ({placeholders})""",
                batch,
            ).fetchall():
                snap = snapshots.get(row["owner_id"])
                if snap and row["scheme_id"] == snap.get("scheme_id"):
                    try:
                        value = json.loads(row["value_json"])
                    except (TypeError, ValueError):
                        continue
                    snap["_extension_values"].append({
                        "scope": "entry",
                        "package_id": row["package_id"],
                        "field_id": row["field_id"],
                        "value": value,
                    })
    return snapshots


def _annotate_import_actions(entries_repo: EntryRepo, inspected: dict, *, adapter_service=None,
                             target_scheme_id: str | None = None, archive_path: Path | None = None) -> None:
    by_number, by_hash = _entry_identity_maps(entries_repo.db.conn)
    counts = {"new": 0, "merge": 0, "conflict": 0, "unchanged": 0}
    entries = inspected.get("entries") or []
    matches = [
        _matching_entry_id(entry, by_number, by_hash)
        for entry in entries
    ]
    matched_ids = {entry_id for entry_id in matches if entry_id}
    snapshots = _import_preview_snapshots(entries_repo, matched_ids)
    exchange_bases = load_exchange_bases(entries_repo.db.conn, matched_ids)
    target_revision_id = ""
    if target_scheme_id and adapter_service:
        target = adapter_service.get_scheme(target_scheme_id)
        target_revision_id = target.get("current_revision_id") or target.get("revision_id") or ""
    profiles = [dict(row) for row in entries_repo.db.conn.execute("SELECT id,name,reviewer FROM profiles").fetchall()]
    profile_by_id = {profile["id"]: profile for profile in profiles}
    package_options = inspected.get("options") if isinstance(inspected.get("options"), dict) else {}
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
            except (ValueError, TypeError, KeyError) as exc:
                entry["adapter_compatibility"].update(available=False, missing_capabilities=[str(exc)])
        if source_adapter:
            public_package = source_adapter.get("definition", {}).get("manifest", {}).get("package_id", "")
            entry["extension_merge_preview"] = _extension_merge_preview(entries_repo, existing_id,
                                                                           source_adapter, public_package)
        # A merge keeps the existing entry's rules, regardless of the new-entry scheme.
        revision_id = (snapshots.get(existing_id) or {}).get("scheme_revision_id") if existing_id else target_revision_id
        entry["role_mapping_preview"] = _role_mapping_preview(entries_repo, entry, revision_id)
        if not existing_id:
            entry["import_action"] = "new"
            counts["new"] += 1
            continue
        existing = snapshots.get(existing_id) or {}
        _fill_archive_inspection_hints(archive_path, existing, entry)
        inc_notes, inc_tags = _entry_transfer_flags(package_options, entry)
        preview = _merge_preview(
            entries_repo,
            existing_id,
            entry,
            existing,
            local_bases=exchange_bases.get(existing_id, {}),
            extension_preview=entry.get("extension_merge_preview"),
            include_notes=inc_notes,
            include_tags=inc_tags,
        )
        entry["existing_entry_id"] = existing_id
        entry["existing_batches"] = existing.get("batches") or []
        entry["existing_profile"] = profile_by_id.get(existing.get("profile_id")) or {}
        entry["merge_preview"] = preview
        if preview["has_conflicts"]:
            entry["import_action"] = "conflict"
        elif preview["has_changes"]:
            entry["import_action"] = "merge"
        else:
            entry["import_action"] = "unchanged"
        counts[entry["import_action"]] += 1
    inspected["import_plan"] = counts

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
    except (ValueError, KeyError):
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
    conflicts = []
    same_count = 0
    for record in incoming:
        if record.get("scope", "entry") != "entry" or record.get("package_id", package_id) != package_id:
            continue
        field_id = record.get("field_id")
        if (field_id not in source_fields or field_id not in target_fields
                or source_fields[field_id].get("type") != target_fields[field_id].get("type")):
            continue
        if field_id not in local or local[field_id] in (None, ""):
            eligible.append(field_id)
        elif local[field_id] == record.get("value"):
            same_count += 1
        elif record.get("value") not in (None, ""):
            conflicts.append({
                "package_id": package_id,
                "field_id": field_id,
                "label": target_fields[field_id].get("label") or field_id,
                "local_value": local[field_id],
                "incoming_value": record.get("value"),
            })
    result = {
        "eligible": sorted(set(eligible)),
        "skipped": max(0, len(incoming) - len(eligible) - len(conflicts) - same_count),
    }
    if conflicts:
        result["conflicts"] = conflicts
    return result


def _merge_extension_data(entries_repo, entry_id, source_adapter, *, overwrite_fields: set[tuple[str, str]] | None = None):
    """Merge same-package, same-id, same-type entry fields; overwrite only approved fields."""
    if not source_adapter:
        return []
    overwrite_fields = overwrite_fields or set()
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
    except (ValueError, KeyError):
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
        local_val = json.loads(local[0]) if local else None
        if local and local_val not in (None, "") and (package_id, field_id) not in overwrite_fields:
            continue
        if local and local_val == value:
            continue
        changed_at = datetime.now().isoformat(timespec="microseconds")
        conn.execute("""INSERT INTO extension_values(scope,owner_id,scheme_id,package_id,field_id,
            definition_revision_id,value_json,updated_at) VALUES('entry',?,?,?,?,?,?,?)
            ON CONFLICT(scope,owner_id,scheme_id,package_id,field_id) DO UPDATE SET
            definition_revision_id=excluded.definition_revision_id,value_json=excluded.value_json,
            updated_at=excluded.updated_at""",
            (entry_id, row["scheme_id"], package_id, field_id, row["scheme_revision_id"], encode(value), changed_at))
        if local_val not in (None, "") and (package_id, field_id) in overwrite_fields:
            conn.execute("""INSERT INTO extension_history(scope,owner_id,scheme_id,package_id,field_id,
                definition_revision_id,kind,old_value_json,new_value_json,actor_id,changed_at)
                VALUES('entry',?,?,?,?,?,?,?,?,?,?)""",
                (entry_id, row["scheme_id"], package_id, field_id, row["scheme_revision_id"],
                 "value", encode(local_val), encode(value), MERGE_HISTORY_PREFIX, changed_at))
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


def _role_mapping_preview(entries_repo, entry, revision_id):
    from ..adapters.transfer import builtin_material_mapping, role_id
    incoming = entry.get("attachments") or []
    roles = sorted({role_id(a) for a in incoming})
    definition = {}
    if revision_id:
        try:
            definition = entries_repo._definition(revision_id)
        except (ValueError, KeyError):
            definition = {}
    material = definition.get("materials", [])
    target_roles = [r.get("id") for r in (material.get("roles", []) if isinstance(material, dict) else material)]
    automatic_roles = {role for role in roles if all(
        builtin_material_mapping(a, definition) == role for a in incoming if role_id(a) == role)}
    return {"source_roles": roles, "target_roles": target_roles,
            # Custom role names and IDs do not prove equivalence across revisions.
            "requires_explicit_mapping": [role for role in roles if role != "invoice" and role not in automatic_roles]}


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


def _import_material_mapping(entries_repo, entry_id, revision_id, attachment, mappings, external_revision_id):
    """Use a local built-in role when compatible; retain unmatched source material."""
    from ..adapters.transfer import builtin_material_mapping, role_id
    from ..db.adapters import AdapterRepo

    source_role = role_id(attachment)
    att_type = str(attachment.get("type") or "other")
    mapped_role = (mappings or {}).get(source_role)
    explicit = bool(mapped_role)
    if revision_id:
        definition = AdapterRepo(entries_repo.db).get_revision(revision_id)
        if source_role == "invoice":
            mapped_role = "invoice"
            explicit = True
        elif source_role not in (mappings or {}):
            mapped_role = builtin_material_mapping(attachment, definition)
        if mapped_role:
            try:
                mapped_type = _validated_material_mapping(
                    entries_repo, entry_id, revision_id, source_role, mapped_role, att_type,
                    attachment.get("original_name") or attachment.get("stored_path") or "")
            except ValueError:
                if explicit:
                    raise
            else:
                return mapped_role, revision_id, mapped_type
    elif mapped_role:
        raise ValueError("请先为条目选择报账方案。")
    return source_role, external_revision_id or None, att_type


def _validated_recognition_data(value) -> str:
    if not value:
        return ""
    from ..engine.models import ParsedInvoice
    try:
        data = json.loads(value)
        return json.dumps(ParsedInvoice.from_dict(data).to_dict(), ensure_ascii=False)
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
        raise ValueError("绑定包中的发票识别记录无法读取，请重新导出。") from exc


def _sync_title_profile_id(entries_repo: EntryRepo, conn, entry_id: str, new_title: str, buyer_tax_id: str) -> None:
    row = conn.execute("SELECT scheme_revision_id FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if not row or not row["scheme_revision_id"]:
        return
    try:
        definition = entries_repo._definition(row["scheme_revision_id"]) or {}
    except (ValueError, KeyError):
        return
    candidates = [
        t for t in definition.get("scheme", {}).get("titles", [])
        if t.get("name") == new_title
        and (not t.get("tax_id") or not buyer_tax_id or t.get("tax_id") == buyer_tax_id)
    ]
    title_profile_id = candidates[0]["id"] if len(candidates) == 1 else ""
    conn.execute("UPDATE entries SET title_profile_id = ? WHERE id = ?", (title_profile_id, entry_id))


def _non_material_check_lines(message: str) -> list[str]:
    return [
        part for part in split_message(message)
        if not part.startswith(PAYMENT_CHECK_PREFIX) and not part.startswith(INSPECTION_CHECK_PREFIX)
    ]


def _material_check_lines(message: str) -> list[str]:
    return [
        part for part in split_message(message)
        if part.startswith(PAYMENT_CHECK_PREFIX) or part.startswith(INSPECTION_CHECK_PREFIX)
    ]


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
    *,
    role_mappings: dict | None = None,
    merge_plan: dict | None = None,
    merge_decisions: dict[str, str] | None = None,
    removed_files: list[Path] | None = None,
    package_options: dict | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Apply a planned three-way / two-way merge inside the caller's transaction.

    Old attachment files removed during replacement are queued into `removed_files`
    and must be unlinked by the caller ONLY after `conn.commit()` succeeds.
    """
    changes: list[str] = []
    if removed_files is None:
        removed_files = []
    existing = _import_preview_snapshots(entries_repo, {entry_id}).get(entry_id) or {}
    inc_notes, inc_tags = _entry_transfer_flags(package_options, incoming)
    if merge_plan is None:
        ext_preview = None
        source_adapter = incoming.get("adapter") or {}
        if source_adapter:
            pkg_id = source_adapter.get("definition", {}).get("manifest", {}).get("package_id", "")
            ext_preview = _extension_merge_preview(entries_repo, entry_id, source_adapter, pkg_id)
        merge_plan = _merge_preview(
            entries_repo,
            entry_id,
            incoming,
            existing,
            extension_preview=ext_preview,
            include_notes=inc_notes,
            include_tags=inc_tags,
        )
    decisions = validate_entry_decisions(merge_plan, merge_decisions)
    plan_items = {item["key"]: item for item in merge_plan.get("items") or []}
    invoice_core_updated = False

    # --- 1. Base / locked fields ---
    base_filled = 0
    base_replaced = 0
    new_title = str(existing.get("title") or "")
    new_buyer_tax = str(existing.get("buyer_tax_id") or "")
    for field in COMPARABLE_BASE_FIELDS:
        item = plan_items.get(f"base:{field}")
        if not item:
            continue
        choice = decisions.get(item["key"], "keep_local")
        if choice != "use_incoming":
            continue
        old_val = str(existing.get(field) or "")
        inc_val = str(incoming.get(field) or "")
        if inc_val == old_val:
            continue
        conn.execute(f"UPDATE entries SET {field} = ? WHERE id = ?", (inc_val, entry_id))
        if old_val:
            conn.execute(
                """INSERT INTO field_history(entry_id, field, old_value, new_value, profile_id, changed_at)
                   VALUES(?,?,?,?,?,?)""",
                (entry_id, f"{MERGE_HISTORY_PREFIX}{field}", old_val, inc_val, "", now),
            )
            base_replaced += 1
        else:
            base_filled += 1
        if field == "title":
            new_title = inc_val
        elif field == "buyer_tax_id":
            new_buyer_tax = inc_val
        if field in {"title", "invoice_no", "invoice_date", "seller", "seller_tax_id", "total", "buyer_name", "buyer_tax_id"}:
            invoice_core_updated = True
    if new_title != str(existing.get("title") or "") or new_buyer_tax != str(existing.get("buyer_tax_id") or ""):
        _sync_title_profile_id(entries_repo, conn, entry_id, new_title, new_buyer_tax)

    # Fill empty recognition_data / source (or refresh when invoice core is updated from incoming)
    for meta_field in ("recognition_data", "source"):
        inc_val = (
            _validated_recognition_data(incoming.get(meta_field))
            if meta_field == "recognition_data"
            else str(incoming.get(meta_field) or "")
        )
        old_val = str(existing.get(meta_field) or "")
        if inc_val and (not old_val or (invoice_core_updated and inc_val != old_val)):
            conn.execute(f"UPDATE entries SET {meta_field} = ? WHERE id = ?", (inc_val, entry_id))
            if not old_val:
                base_filled += 1
    if base_replaced:
        changes.append(f"更新基础信息 {base_replaced}")
    if base_filled:
        changes.append(f"基础信息 {base_filled}")

    # --- 2. Tags ---
    local_tags = list(existing.get("tags") or [])
    inc_tag_list = [str(tag).strip() for tag in (incoming.get("tags") or []) if str(tag).strip()]
    tags_item = plan_items.get("tags")
    tags_choice = decisions.get("tags", "keep_local") if tags_item else "keep_local"
    if tags_choice == "use_incoming":
        next_tags = _merge_tags(inc_tag_list, import_tags)
    elif tags_choice == "merge":
        next_tags = _merge_tags(local_tags, inc_tag_list + import_tags)
    else:
        next_tags = _merge_tags(local_tags, import_tags)
    if next_tags != local_tags:
        conn.execute(
            "UPDATE entries SET tags = ?, updated_at = ? WHERE id = ?",
            (json.dumps(next_tags, ensure_ascii=False), now, entry_id),
        )
        changes.append(f"标签 {max(1, len(set(next_tags) - set(local_tags)))}")

    # --- 3. Editable fields (paid_amount, actual_item_name, notes, ...) ---
    local_fields = existing.get("fields") or {}
    inc_fields = incoming.get("fields") or {}
    for key, item in plan_items.items():
        if item["kind"] not in {"field", "notes"}:
            continue
        field = item["field"]
        choice = decisions.get(key, "keep_local")
        if choice == "keep_local":
            continue
        value = inc_fields.get(field) or {}
        incoming_value = str(value.get("current") or "")
        local = local_fields.get(field)
        local_value = str((local or {}).get("current") or "")
        if choice == "merge" and field == "notes":
            next_value = _merge_text(local_value, incoming_value)
        else:
            next_value = incoming_value
        if next_value == local_value:
            continue
        if local is None:
            conn.execute(
                """INSERT INTO entry_fields(entry_id, field, origin, current, modified, value_source)
                   VALUES(?,?,?,?,?,?)""",
                (
                    entry_id, field, str(value.get("origin") or ""), next_value,
                    int(bool(value.get("modified"))), str(value.get("value_source") or ""),
                ),
            )
            changes.append("备注" if field == "notes" else "条目信息")
            continue
        new_source = str(value.get("value_source") or "") if choice == "use_incoming" else (
            str(local.get("value_source") or "") or str(value.get("value_source") or "")
        )
        new_modified = int(bool(local.get("modified")) or bool(value.get("modified")) or (bool(local_value) and choice == "use_incoming"))
        conn.execute(
            """UPDATE entry_fields
                  SET current = ?, modified = ?, value_source = ?
                WHERE entry_id = ? AND field = ?""",
            (next_value, new_modified, new_source, entry_id, field),
        )
        history_field = f"{MERGE_HISTORY_PREFIX}{field}" if (local_value and choice == "use_incoming") else field
        conn.execute(
            """INSERT INTO field_history(entry_id, field, old_value, new_value, profile_id, changed_at)
               VALUES(?,?,?,?,?,?)""",
            (entry_id, history_field, local_value, next_value, "", now),
        )
        changes.append("备注" if field == "notes" else ("更新条目信息" if local_value else "条目信息"))

    # --- 4. Attachments (add / replace / three_way + same-sha notes & payment AI) ---
    local_attachments = existing.get("attachments") or []
    by_hash = {
        str(attachment.get("sha256") or ""): attachment
        for attachment in local_attachments if str(attachment.get("sha256") or "")
    }
    local_by_group: dict[str, list[dict]] = defaultdict(list)
    for att in local_attachments:
        if str(att.get("sha256") or ""):
            local_by_group[attachment_group_key(att)].append(att)
    inc_by_group: dict[str, list[dict]] = defaultdict(list)
    for att in incoming.get("attachments") or []:
        inc_by_group[attachment_group_key(att)].append(att)

    attachment_added = 0
    attachment_replaced = 0
    attachment_removed = 0

    # First handle same-sha256 metadata upgrades (payment AI recognition & notes) on surviving local attachments
    def _upgrade_duplicate(duplicate: dict, inc_att: dict) -> None:
        if _payment_recognition_upgrades(duplicate, inc_att) and duplicate.get("id"):
            recognition = _imported_payment_recognition(inc_att, str(duplicate.get("type") or ""))
            if recognition[1]:
                conn.execute(
                    """UPDATE attachments SET recognition_version = ?, recognition_status = ?,
                       recognized_value = ?, recognition_message = ? WHERE id = ?""",
                    (*recognition, duplicate["id"]),
                )
                changes.append("付款截图识别结果")
        if inc_notes is not False and duplicate.get("id"):
            merged_note = _merge_text(str(duplicate.get("note") or ""), str(inc_att.get("note") or ""))
            if merged_note != str(duplicate.get("note") or ""):
                conn.execute("UPDATE attachments SET note = ? WHERE id = ?", (merged_note, duplicate["id"]))
                changes.append("附件备注")

    if merge_plan.get("mode") != "ancestor":
        for inc_att in incoming.get("attachments") or []:
            digest = str(inc_att.get("sha256") or "")
            if digest and digest in by_hash:
                _upgrade_duplicate(by_hash[digest], inc_att)

    for key, item in plan_items.items():
        if item["kind"] != "attachment_group":
            continue
        group_key = item["group_key"]
        choice = decisions.get(key, "keep_local")
        if choice == "keep_local":
            continue
        group_local = local_by_group.get(group_key, [])
        group_inc = inc_by_group.get(group_key, [])
        inc_shas = {str(att.get("sha256") or "") for att in group_inc if str(att.get("sha256") or "")}

        if choice == "replace":
            target_keep_shas = inc_shas
        elif choice == "three_way":
            target_keep_shas = set(item.get("three_way_shas") or [])
        else:  # "merge"
            target_keep_shas = {str(att.get("sha256") or "") for att in group_local} | inc_shas

        # Validate all required replacement members exist in the archive BEFORE deleting any local row.
        for attachment in group_inc:
            digest = str(attachment.get("sha256") or "")
            if digest and digest not in target_keep_shas:
                continue
            if digest and digest in by_hash:
                continue
            stored_path = str(attachment.get("stored_path") or "")
            arcname = f"attachments/{stored_path}"
            if not stored_path or not _has_archive_member(archive, arcname):
                if choice in {"replace", "three_way"}:
                    missing_name = str(attachment.get("original_name") or stored_path or item["label"])
                    raise ValueError(f"绑定包缺少附件文件（{missing_name}），已取消替换以保护本机材料。")

        # Delete local rows in this group that are not in target_keep_shas BEFORE extracting new ones
        # so max_count constraints on replacement roles pass cleanly.
        removed_names: list[str] = []
        for loc_att in group_local:
            loc_sha = str(loc_att.get("sha256") or "")
            if loc_sha not in target_keep_shas and loc_att.get("id"):
                conn.execute("DELETE FROM attachments WHERE id = ?", (loc_att["id"],))
                stored = str(loc_att.get("stored_path") or "")
                if stored:
                    removed_files.append(attachments_repo.data_root.attachments_dir / stored)
                removed_names.append(str(loc_att.get("original_name") or stored))
                by_hash.pop(loc_sha, None)

        added_names: list[str] = []
        for attachment in group_inc:
            digest = str(attachment.get("sha256") or "")
            if digest and digest not in target_keep_shas:
                continue
            if digest and digest in by_hash:
                continue
            stored_path = str(attachment.get("stored_path") or "")
            arcname = f"attachments/{stored_path}"
            if not stored_path or not _has_archive_member(archive, arcname):
                continue
            suffix = Path(str(attachment.get("original_name") or "") or stored_path).suffix
            att_type = str(attachment.get("type") or "other")
            final_role, role_revision, att_type = _import_material_mapping(
                entries_repo, entry_id, existing.get("scheme_revision_id"), attachment,
                role_mappings, _external_revision_id(entries_repo, incoming.get("adapter")))
            dest_dir = attachments_repo.data_root.entry_dir(entry_id)
            stored_name = attachments_repo._unique_name(dest_dir, entry_id, att_type, suffix)
            dest = dest_dir / stored_name
            created_files.append(dest)
            actual_digest = _extract_archive_member(archive, arcname, dest)
            if digest and actual_digest != digest and not tampered:
                raise ValueError(f"附件 {attachment.get('original_name') or stored_name} 校验失败。")
            recognition = _imported_payment_recognition(attachment, att_type) if actual_digest == digest else ("", "", "", "")
            incoming_note = str(attachment.get("note") or "") if inc_notes is not False else ""
            conn.execute(
                """INSERT INTO attachments(id, entry_id, type, original_name, stored_path,
                   sha256, note, added_at, role_id, role_definition_revision_id,
                   recognition_version, recognition_status, recognized_value, recognition_message)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    uuid.uuid4().hex, entry_id, att_type,
                    str(attachment.get("original_name") or stored_name),
                    f"{entry_id}/{stored_name}", actual_digest, incoming_note,
                    str(attachment.get("added_at") or now), final_role, role_revision, *recognition,
                ),
            )
            by_hash[actual_digest] = {"id": "", "sha256": actual_digest, "note": incoming_note}
            added_names.append(str(attachment.get("original_name") or stored_name))

        if removed_names and added_names:
            attachment_replaced += len(added_names)
            conn.execute(
                """INSERT INTO field_history(entry_id, field, old_value, new_value, profile_id, changed_at)
                   VALUES(?,?,?,?,?,?)""",
                (entry_id, f"{MERGE_HISTORY_PREFIX}{item['label']}", "、".join(removed_names), "、".join(added_names), "", now),
            )
            if group_key in {"invoice_pdf", "invoice_xml"}:
                invoice_core_updated = True
        elif removed_names:
            attachment_removed += len(removed_names)
            conn.execute(
                """INSERT INTO field_history(entry_id, field, old_value, new_value, profile_id, changed_at)
                   VALUES(?,?,?,?,?,?)""",
                (entry_id, f"{MERGE_HISTORY_PREFIX}{item['label']}", "、".join(removed_names), "", "", now),
            )
        elif added_names:
            attachment_added += len(added_names)
            if group_key in {"invoice_pdf", "invoice_xml"}:
                invoice_core_updated = True

    if attachment_replaced:
        changes.append(f"替换材料 {attachment_replaced}")
    if attachment_removed:
        changes.append(f"移除材料 {attachment_removed}")
    if attachment_added:
        changes.append(f"材料 {attachment_added}")

    # --- 5. Extension fields ---
    overwrite_ext = {
        (item["package_id"], item["field_id"])
        for key, item in plan_items.items()
        if item["kind"] == "extension" and decisions.get(key) == "use_incoming"
    }
    extension_fields = _merge_extension_data(
        entries_repo, entry_id, incoming.get("adapter") or {}, overwrite_fields=overwrite_ext
    )
    if extension_fields:
        changes.append(f"附加信息 {len(extension_fields)}")
    # 保留交接来源属于内部记录，不把材料和信息无变化的条目计作补充。
    _retain_adapter_source(entries_repo, entry_id, incoming.get('adapter'))

    # --- 6. Line items ---
    items_plan = plan_items.get("items")
    if items_plan and decisions.get("items") == "use_incoming":
        before_items = existing.get("items") or []
        conn.execute("DELETE FROM items WHERE entry_id = ?", (entry_id,))
        for ordinal, item in enumerate(incoming.get("items") or []):
            conn.execute(
                """INSERT INTO items(entry_id, name, actual_name, unit, quantity,
                   unit_price, total, spec, ordinal) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    entry_id, item.get("name", ""), item.get("actual_name", ""),
                    item.get("unit", ""), item.get("quantity", ""),
                    item.get("unit_price", ""), item.get("total", ""),
                    item.get("spec", ""), item.get("ordinal", ordinal),
                ),
            )
        if before_items:
            conn.execute(
                """INSERT INTO field_history(entry_id, field, old_value, new_value, profile_id, changed_at)
                   VALUES(?,?,?,?,?,?)""",
                (entry_id, f"{MERGE_HISTORY_PREFIX}items", item_snapshot(before_items), item_snapshot(incoming.get("items") or []), "", now),
            )
            changes.append("更新发票明细")
        else:
            changes.append("发票明细")
        invoice_core_updated = True

    # --- 7. OCR results (union) ---
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

    # --- 8. Field history (union) ---
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

    # --- 9. Non-material check reminders (preserve/sync across machines) ---
    if merge_plan.get("mode") != "ancestor":
        cur_row = conn.execute(
            "SELECT check_status, check_message FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        cur_status = str(cur_row["check_status"] or "pass") if cur_row else "pass"
        cur_message = str(cur_row["check_message"] or "") if cur_row else ""
        material_lines = _material_check_lines(cur_message)
        loc_non_mat = _non_material_check_lines(cur_message)
        inc_non_mat = _non_material_check_lines(str(incoming.get("check_message") or ""))
        if invoice_core_updated:
            from ..engine import check_invoice
            updated_row = conn.execute("SELECT title FROM entries WHERE id = ?", (entry_id,)).fetchone()
            parsed = entries_repo.parsed_for_check(entry_id)
            rechecked = check_invoice(
                parsed,
                expected_title=(updated_row["title"] if updated_row else "") or "",
                context=entries_repo.policy_context(entry_id),
            )
            rechecked_non_mat = _non_material_check_lines(rechecked.message)
            next_message = "；".join([*rechecked_non_mat, *material_lines])
            next_status = "blocked" if (cur_status == "blocked" or rechecked.status == "blocked") else (
                "warning" if next_message else "pass"
            )
            if next_message != cur_message or next_status != cur_status:
                conn.execute(
                    "UPDATE entries SET check_status = ?, check_message = ? WHERE id = ?",
                    (next_status, next_message, entry_id),
                )
        else:
            extra_lines = [line for line in inc_non_mat if line not in loc_non_mat]
            if extra_lines:
                next_message = "；".join([*loc_non_mat, *extra_lines, *material_lines])
                next_status = "blocked" if cur_status == "blocked" else "warning"
                conn.execute(
                    "UPDATE entries SET check_status = ?, check_message = ? WHERE id = ?",
                    (next_status, next_message, entry_id),
                )
                changes.append(f"识别提醒 {len(extra_lines)}")

    if tampered and changes:
        warning = "绑定包完整性校验未通过，补充内容已由用户确认"
        cur_msg = str(conn.execute("SELECT check_message FROM entries WHERE id = ?", (entry_id,)).fetchone()["check_message"] or "")
        check_message = _merge_text(cur_msg, warning)
        conn.execute(
            """UPDATE entries SET check_status = 'blocked', status = 'partial',
                      check_message = ?, updated_at = ? WHERE id = ?""",
            (check_message, now, entry_id),
        )
        changes.append("完整性标记")

    if changes:
        conn.execute("UPDATE entries SET updated_at = ? WHERE id = ?", (now, entry_id))
    return list(dict.fromkeys(changes)), decisions


def import_bindle(entries_repo, attachments_repo, path, profile_id, allow_tampered=False,
                  options=None, *, inspected=None, adapter_service=None):
    """Import through the isolated write transaction; keep the public service entry point."""
    from .bindle_import import import_bindle as run
    return run(entries_repo, attachments_repo, path, profile_id, allow_tampered, options,
               inspected=inspected, adapter_service=adapter_service)

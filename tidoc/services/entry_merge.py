"""Decoupled three-way / two-way merge planner and snapshot tracker for entries.

This module owns entry merge semantics independently of ZIP bindle I/O so both
offline `.tidoc` packages and future remote shared-space sync share the exact
same snapshot hashing, common-ancestor lookup, conflict planning and rules.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..db.entries import PAYMENT_AI_RECOGNITION_VERSION
from ..db.manual_changes import _number as _norm_number, item_values, same_value
from ..engine.check_lines import (
    INSPECTION_CHECK_PREFIX,
    PAYMENT_CHECK_PREFIX,
    split_message,
)
from ..services.inspection_pdf import cached_inspection_info, inspection_summary

MERGE_HISTORY_PREFIX = "[合并更新]"
BATCH_NOTE_PREFIX = "[批次催办] "
SNAPSHOT_VERSION = 1
MAX_STORED_BASES_PER_ENTRY = 30

EXCLUSIVE_TAG_GROUPS = (
    frozenset({"已报销", "未报销"}),
    frozenset({"已支付", "未支付"}),
)

BASE_FIELDS = (
    "title",
    "invoice_no",
    "invoice_date",
    "seller",
    "seller_tax_id",
    "total",
    "buyer_name",
    "buyer_tax_id",
    "category",
    "source",
)

COMPARABLE_BASE_FIELDS = (
    "title",
    "invoice_no",
    "invoice_date",
    "seller",
    "seller_tax_id",
    "total",
    "buyer_name",
    "buyer_tax_id",
    "category",
)

BASE_FIELD_LABELS = {
    "title": "抬头",
    "invoice_no": "发票号码",
    "invoice_date": "开票日期",
    "seller": "销售方",
    "seller_tax_id": "销售方税号",
    "total": "价税合计",
    "buyer_name": "购买方",
    "buyer_tax_id": "购买方税号",
    "category": "分类",
    "source": "数据来源",
}

EDITABLE_FIELD_LABELS = {
    "paid_amount": "实付金额",
    "actual_item_name": "实际物资名称",
    "notes": "条目备注",
}

ATTACHMENT_GROUP_ORDER = (
    "invoice_pdf",
    "invoice_xml",
    "payment_screenshot",
    "inspection_pdf",
    "physical_image",
    "other",
)

ATTACHMENT_GROUP_LABELS = {
    "invoice_pdf": "发票 PDF",
    "invoice_xml": "发票 XML",
    "payment_screenshot": "付款截图",
    "inspection_pdf": "查验单",
    "physical_image": "实物图",
    "other": "其他材料",
}

SINGLE_FILE_GROUPS = frozenset({"invoice_pdf", "invoice_xml", "inspection_pdf"})

PAYMENT_STATUS_RANK = {
    "": 0,
    "pending": 0,
    "error": 1,
    "unrecognized": 2,
    "review": 3,
    "recognized": 4,
}


def _text_contains(local: str, incoming: str) -> bool:
    """Check line-aligned containment so '补充' does not falsely match '请补充'."""
    local = str(local or "").strip()
    incoming = str(incoming or "").strip()
    if not incoming:
        return True
    if not local:
        return False
    if incoming == local:
        return True
    inc_lines = [line.strip() for line in incoming.splitlines() if line.strip()]
    loc_lines = [line.strip() for line in local.splitlines() if line.strip()]
    if not inc_lines:
        return True
    if len(inc_lines) == 1:
        return inc_lines[0] in loc_lines
    span = len(inc_lines)
    return any(loc_lines[i:i + span] == inc_lines for i in range(len(loc_lines) - span + 1))


def merge_text(local: str, incoming: str) -> str:
    local = str(local or "").strip()
    incoming = str(incoming or "").strip()
    if not incoming or _text_contains(local, incoming):
        return local
    return f"{local}\n{incoming}" if local else incoming


def merge_tags(local_tags: list[str], incoming_tags: list[str]) -> list[str]:
    """Union ordinary tags; an incoming status tag replaces its local opposite."""
    result = [str(tag).strip() for tag in (local_tags or []) if str(tag).strip()]
    for raw in incoming_tags or []:
        incoming = str(raw).strip()
        if not incoming:
            continue
        for group in EXCLUSIVE_TAG_GROUPS:
            if incoming in group:
                result = [tag for tag in result if tag not in group]
                break
        if incoming not in result:
            result.append(incoming)
    return result


def attachment_group_key(attachment: dict) -> str:
    att_type = str(attachment.get("type") or "other")
    if att_type in {"invoice_pdf", "invoice_xml", "payment_screenshot", "inspection_pdf", "physical_image"}:
        return att_type
    role_id = str(attachment.get("role_id") or "")
    if role_id.startswith("custom:"):
        return role_id
    return "other"


def attachment_group_label(group_key: str) -> str:
    if group_key in ATTACHMENT_GROUP_LABELS:
        return ATTACHMENT_GROUP_LABELS[group_key]
    if group_key.startswith("custom:"):
        return f"自定义材料（{group_key[7:]}）"
    return group_key


def _attachment_inspection_info(attachment: dict) -> dict | None:
    if isinstance(attachment.get("inspection"), dict):
        return inspection_summary(attachment["inspection"])
    if str(attachment.get("type") or "") != "inspection_pdf":
        return None
    cached = cached_inspection_info(
        attachment.get("recognition_version"),
        attachment.get("recognized_value"),
        attachment.get("sha256"),
    )
    if cached is not None:
        return inspection_summary(cached)
    return None


def describe_attachment(attachment: dict) -> dict:
    """Compact UI-friendly descriptor for one attachment in merge comparisons."""
    att_type = str(attachment.get("type") or "other")
    name = str(attachment.get("original_name") or attachment.get("stored_path") or "材料")
    meta_parts: list[str] = []
    if att_type == "inspection_pdf":
        info = _attachment_inspection_info(attachment)
        if info:
            if info.get("count"):
                scope = "当日" if info.get("count_scope") == "day" else ""
                meta_parts.append(f"{scope}第 {info['count']} 次查验")
            else:
                meta_parts.append("未读出查验次数")
            if info.get("checked_at"):
                meta_parts.append(info["checked_at"])
    elif att_type == "payment_screenshot":
        status = str(attachment.get("recognition_status") or "")
        amount = str(attachment.get("recognized_value") or "")
        msg = str(attachment.get("recognition_message") or "")
        status_label = {
            "recognized": "已识别",
            "review": "需核对",
            "unrecognized": "未识别金额",
            "error": "识别失败",
        }.get(status, "")
        if amount:
            meta_parts.append(f"¥{amount}")
        if status_label:
            meta_parts.append(status_label)
        if msg and status != "recognized":
            meta_parts.append(msg[:40])
    note = str(attachment.get("note") or "").strip()
    if note:
        meta_parts.append(f"备注：{note[:30]}")
    return {
        "name": name,
        "sha256": str(attachment.get("sha256") or ""),
        "type": att_type,
        "role_id": str(attachment.get("role_id") or ""),
        "meta": " · ".join(meta_parts),
    }


def _extract_extension_map(entry: dict, extension_values: list[dict] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    records = extension_values
    if records is None:
        adapter = entry.get("adapter") or {}
        if isinstance(adapter, dict):
            records = adapter.get("extension_values") or []
        else:
            records = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        if record.get("scope", "entry") != "entry":
            continue
        field_id = str(record.get("field_id") or "").strip()
        if not field_id:
            continue
        package_id = str(record.get("package_id") or "").strip()
        if not package_id:
            adapter = entry.get("adapter") or {}
            package_id = str((adapter.get("definition") or {}).get("manifest", {}).get("package_id") or "")
        value = record.get("value")
        if value in (None, ""):
            continue
        out[f"{package_id}::{field_id}"] = value
    return out


def _canonical_scalar(field: str, value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return ""
    if field in {"total", "paid_amount"}:
        return _norm_number(text, amount=True)
    return text


def build_entry_snapshot(
    entry: dict,
    *,
    batch_note: str | None = None,
    extension_values: list[dict] | None = None,
) -> dict:
    """Build a deterministic, transport-agnostic snapshot of an entry's content."""
    base_fields = {
        field: normalized
        for field in COMPARABLE_BASE_FIELDS
        if (normalized := _canonical_scalar(field, entry.get(field)))
    }
    fields: dict[str, str] = {}
    for field, fv in sorted((entry.get("fields") or {}).items()):
        current = _canonical_scalar(str(field), (fv or {}).get("current"))
        if current:
            fields[str(field)] = current
    attachments_by_group: dict[str, list[str]] = {}
    attachment_notes: dict[str, str] = {}
    for att in entry.get("attachments") or []:
        sha = str(att.get("sha256") or "").strip()
        if not sha:
            continue
        group = attachment_group_key(att)
        bucket = attachments_by_group.setdefault(group, [])
        if sha not in bucket:
            bucket.append(sha)
        note = str(att.get("note") or "").strip()
        if note:
            attachment_notes[sha] = note
    for group in attachments_by_group:
        attachments_by_group[group].sort()
    tags = sorted({str(tag).strip() for tag in (entry.get("tags") or []) if str(tag).strip()})
    raw_batch_note = entry.get("batch_note") if batch_note is None else batch_note
    resolved_batch_note = str(raw_batch_note or "").strip()
    snapshot: dict[str, Any] = {
        "snapshot_version": SNAPSHOT_VERSION,
        "base_fields": base_fields,
        "fields": fields,
        "items": item_values(entry.get("items") or []),
        "attachments": dict(sorted(attachments_by_group.items())),
        "attachment_notes": dict(sorted(attachment_notes.items())),
        "tags": tags,
        "extension_values": dict(sorted(_extract_extension_map(entry, extension_values).items())),
    }
    if resolved_batch_note:
        snapshot["batch_note"] = resolved_batch_note
    return snapshot


def snapshot_digest(snapshot: dict) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def record_exchange_base(
    conn,
    entry_id: str,
    snapshot: dict,
    direction: str,
    now: str,
) -> str:
    """Persist one exchange snapshot for future three-way merge ancestor lookup."""
    digest = snapshot_digest(snapshot)
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    conn.execute(
        """INSERT INTO entry_exchange_bases(entry_id, snapshot_hash, snapshot_json, direction, recorded_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(entry_id, snapshot_hash) DO UPDATE SET
               recorded_at = excluded.recorded_at,
               direction = excluded.direction""",
        (entry_id, digest, encoded, direction, now),
    )
    conn.execute(
        """DELETE FROM entry_exchange_bases
            WHERE entry_id = ?
              AND id NOT IN (
                  SELECT id FROM entry_exchange_bases
                   WHERE entry_id = ?
                   ORDER BY recorded_at DESC, id DESC
                   LIMIT ?
              )""",
        (entry_id, entry_id, MAX_STORED_BASES_PER_ENTRY),
    )
    return digest


def known_ancestors(conn, entry_id: str, limit: int = 20) -> list[str]:
    try:
        rows = conn.execute(
            """SELECT snapshot_hash FROM entry_exchange_bases
                WHERE entry_id = ?
                ORDER BY recorded_at DESC, id DESC
                LIMIT ?""",
            (entry_id, limit),
        ).fetchall()
    except Exception:
        return []
    return [str(row["snapshot_hash"]) for row in rows if row and row["snapshot_hash"]]


def load_exchange_bases(conn, entry_ids: set[str]) -> dict[str, dict[str, dict]]:
    """Batch-load recorded exchange snapshots keyed by entry_id -> snapshot_hash -> snapshot."""
    if not entry_ids:
        return {}
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entry_exchange_bases'"
        ).fetchone()
    except Exception:
        return {}
    if not has_table:
        return {}
    out: dict[str, dict[str, dict]] = {}
    ordered = sorted(entry_ids)
    for offset in range(0, len(ordered), 900):
        batch = ordered[offset:offset + 900]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""SELECT entry_id, snapshot_hash, snapshot_json
                  FROM entry_exchange_bases
                 WHERE entry_id IN ({placeholders})
                 ORDER BY recorded_at DESC, id DESC""",
            batch,
        ).fetchall()
        for row in rows:
            try:
                parsed = json.loads(row["snapshot_json"])
            except (TypeError, ValueError):
                continue
            out.setdefault(row["entry_id"], {})[str(row["snapshot_hash"])] = parsed
    return out


def find_common_base(
    local_snapshot: dict,
    incoming_snapshot: dict,
    local_bases: dict[str, dict],
    incoming_ancestors: list[str] | None,
) -> tuple[str, str, dict | None]:
    """Resolve merge mode and common base snapshot.

    Returns (mode, base_hash, base_snapshot):
      - ("three_way", local_hash, local_snapshot) if local is an explicit ancestor of incoming (fast-forward)
      - ("ancestor", incoming_hash, incoming_snapshot) if incoming is already a recorded ancestor of local
      - ("three_way", ancestor_hash, base_snapshot) if a recorded common ancestor matches incoming_ancestors
      - ("two_way", "", None) if no common ancestor exists
    """
    local_hash = snapshot_digest(local_snapshot)
    incoming_hash = snapshot_digest(incoming_snapshot)
    ancestors = [str(h).strip() for h in (incoming_ancestors or []) if str(h).strip()]
    strict_parents = [h for h in ancestors if h != incoming_hash]
    if local_hash == incoming_hash:
        return "three_way", local_hash, local_snapshot
    if local_hash in ancestors:
        return "three_way", local_hash, local_snapshot
    # Check strict parent ancestors BEFORE incoming_hash so a deliberate revert
    # (e.g. A -> B -> A with ancestors=[A, B]) resolves base=B instead of being
    # mistaken for a stale resend of A.
    for candidate in strict_parents:
        if candidate in local_bases:
            return "three_way", candidate, local_bases[candidate]
    if incoming_hash in local_bases:
        return "ancestor", incoming_hash, local_bases[incoming_hash]
    return "two_way", "", None


def _payment_recognition_upgrades(local_att: dict, incoming_att: dict) -> bool:
    if str(local_att.get("type") or "") != "payment_screenshot":
        return False
    inc_status = str(incoming_att.get("recognition_status") or "")
    inc_version = str(incoming_att.get("recognition_version") or "")
    if not inc_status or not inc_version:
        return False
    loc_status = str(local_att.get("recognition_status") or "")
    loc_version = str(local_att.get("recognition_version") or "")
    if not loc_status:
        return True
    inc_current = inc_version.startswith(PAYMENT_AI_RECOGNITION_VERSION)
    loc_current = loc_version.startswith(PAYMENT_AI_RECOGNITION_VERSION)
    if inc_current and not loc_current:
        return True
    if loc_current and not inc_current:
        return False
    if PAYMENT_STATUS_RANK.get(inc_status, 0) > PAYMENT_STATUS_RANK.get(loc_status, 0):
        return True
    if (
        inc_status == loc_status
        and str(incoming_att.get("recognized_value") or "")
        and not str(local_att.get("recognized_value") or "")
    ):
        return True
    return False


def _items_summary(items: list[dict]) -> str:
    if not items:
        return "无明细"
    first = str(items[0].get("actual_name") or items[0].get("name") or "").strip()
    if len(items) == 1:
        return f"1 项（{first or '未命名'}）"
    return f"{len(items)} 项（{first or '未命名'} 等）"


def _field_source_hint(field_data: dict | None) -> str:
    if not field_data:
        return ""
    source = str(field_data.get("value_source") or "")
    if source == "manual" or (field_data.get("modified") and source not in {"payment_ai", "payment_ocr"}):
        return "人工修改"
    if source in {"payment_ai", "payment_ocr"}:
        return "截图识别"
    return ""


def _non_material_check_lines(message: str) -> list[str]:
    return [
        part for part in split_message(message)
        if not part.startswith(PAYMENT_CHECK_PREFIX) and not part.startswith(INSPECTION_CHECK_PREFIX)
    ]


def _choice_option(value: str, label: str) -> dict[str, str]:
    return {"value": value, "label": label}


def _sorted_group_keys(keys: set[str]) -> list[str]:
    order_index = {key: idx for idx, key in enumerate(ATTACHMENT_GROUP_ORDER)}
    return sorted(keys, key=lambda key: (order_index.get(key, 99), key))


def plan_entry_merge(
    local_entry: dict,
    incoming_entry: dict,
    *,
    local_bases: dict[str, dict] | None = None,
    local_batch_note: str = "",
    local_extension_values: list[dict] | None = None,
    extension_preview: dict | None = None,
    include_notes: bool | None = True,
    include_tags: bool | None = True,
) -> dict:
    """Compute a complete, structured merge plan for one existing entry.

    Every mergeable unit carries a stable `key`, `status` (`auto_add`,
    `auto_update`, `conflict`), `default_choice`, allowed `choices`, and
    UI-ready summaries so both preview rendering and transactional execution
    consume the exact same plan.
    """
    local_bases = local_bases or {}
    local_batches = list(local_entry.get("batches") or [])
    archived_batches = [b.get("name") or "已归档批次" for b in local_batches if b.get("archived")]
    archived = bool(archived_batches)

    local_snapshot = build_entry_snapshot(
        local_entry,
        batch_note=local_batch_note,
        extension_values=local_extension_values,
    )
    incoming_batch_note = str(incoming_entry.get("batch_note") or "").strip() if include_notes is not False else ""
    incoming_snapshot = build_entry_snapshot(
        incoming_entry,
        batch_note=incoming_batch_note,
    )
    incoming_ancestors = incoming_entry.get("exchange_ancestors") or []
    mode, base_hash, base_snapshot = find_common_base(
        local_snapshot, incoming_snapshot, local_bases, incoming_ancestors
    )

    items: list[dict] = []

    # Always inspect non-snapshot additive metadata (OCR, history, payment AI results, check reminders)
    local_attachments = list(local_entry.get("attachments") or [])
    incoming_attachments = list(incoming_entry.get("attachments") or [])
    local_by_sha = {
        str(att.get("sha256") or ""): att
        for att in local_attachments
        if str(att.get("sha256") or "")
    }
    incoming_by_sha = {
        str(att.get("sha256") or ""): att
        for att in incoming_attachments
        if str(att.get("sha256") or "")
    }

    # --- 1. Attachment groups (unless incoming is already an older ancestor) ---
    if mode != "ancestor":
        local_groups: dict[str, list[dict]] = {}
        for att in local_attachments:
            if str(att.get("sha256") or ""):
                local_groups.setdefault(attachment_group_key(att), []).append(att)
        inc_groups: dict[str, list[dict]] = {}
        for att in incoming_attachments:
            if str(att.get("sha256") or ""):
                inc_groups.setdefault(attachment_group_key(att), []).append(att)
        base_groups: dict[str, list[str]] = (
            (base_snapshot.get("attachments") or {}) if base_snapshot is not None else {}
        )
        all_groups = _sorted_group_keys(set(local_groups) | set(inc_groups) | set(base_groups))
        for group_key in all_groups:
            loc_list = local_groups.get(group_key, [])
            inc_list = inc_groups.get(group_key, [])
            loc_shas = {str(att.get("sha256") or "") for att in loc_list}
            inc_shas = {str(att.get("sha256") or "") for att in inc_list}
            if loc_shas == inc_shas:
                continue
            label = attachment_group_label(group_key)
            is_single = group_key in SINGLE_FILE_GROUPS
            loc_desc = [describe_attachment(att) for att in loc_list]
            inc_desc = [describe_attachment(att) for att in inc_list]

            if base_snapshot is not None:
                base_shas = set(base_groups.get(group_key, []))
                loc_added = loc_shas - base_shas
                loc_removed = base_shas - loc_shas
                inc_added = inc_shas - base_shas
                inc_removed = base_shas - inc_shas
                loc_changed = bool(loc_added or loc_removed)
                inc_changed = bool(inc_added or inc_removed)
                if not inc_changed:
                    continue
                if not loc_changed:
                    if not inc_removed:
                        items.append({
                            "key": f"att:{group_key}",
                            "kind": "attachment_group",
                            "group_key": group_key,
                            "label": label,
                            "status": "auto_add",
                            "default_choice": "merge",
                            "choices": [
                                _choice_option("merge", "添加包内材料"),
                                _choice_option("keep_local", "不添加"),
                            ],
                            "local_files": loc_desc,
                            "incoming_files": inc_desc,
                            "added_count": len(inc_added),
                            "removed_count": 0,
                            "change_summary": f"新增 {len(inc_added)} 份",
                            "archived_protected": False,
                        })
                    elif inc_added:
                        status = "conflict" if archived else "auto_update"
                        default_choice = "keep_local" if archived else "replace"
                        choices = [
                            _choice_option("replace", "用包内替换本机"),
                            _choice_option("keep_local", "保留本机"),
                            _choice_option("merge", "保留两份" if is_single else "合并保留全部"),
                        ]
                        summary = (
                            f"对方已更换（替换 {len(inc_removed)} 份为 {len(inc_added)} 份）"
                            if len(inc_removed) != len(inc_added)
                            else f"对方已更换（替换 {len(inc_added)} 份）"
                        )
                        items.append({
                            "key": f"att:{group_key}",
                            "kind": "attachment_group",
                            "group_key": group_key,
                            "label": label,
                            "status": status,
                            "default_choice": default_choice,
                            "choices": choices,
                            "local_files": loc_desc,
                            "incoming_files": inc_desc,
                            "added_count": len(inc_added),
                            "removed_count": len(inc_removed),
                            "change_summary": summary,
                            "archived_protected": archived,
                        })
                    else:
                        # Remote deleted all/some files without replacing: always ask explicitly.
                        items.append({
                            "key": f"att:{group_key}",
                            "kind": "attachment_group",
                            "group_key": group_key,
                            "label": label,
                            "status": "conflict",
                            "default_choice": "keep_local",
                            "choices": [
                                _choice_option("keep_local", "保留本机"),
                                _choice_option("replace", "同步移除"),
                            ],
                            "local_files": loc_desc,
                            "incoming_files": inc_desc,
                            "added_count": 0,
                            "removed_count": len(inc_removed),
                            "change_summary": f"对方已移除 {len(inc_removed)} 份",
                            "archived_protected": archived,
                        })
                else:
                    # Both sides modified this attachment group.
                    if not is_single and not loc_removed and not inc_removed:
                        # Pure disjoint additions on both sides: safe union.
                        new_only = inc_shas - loc_shas
                        if new_only:
                            items.append({
                                "key": f"att:{group_key}",
                                "kind": "attachment_group",
                                "group_key": group_key,
                                "label": label,
                                "status": "auto_add",
                                "default_choice": "merge",
                                "choices": [
                                    _choice_option("merge", "合并保留双方新增"),
                                    _choice_option("keep_local", "仅保留本机"),
                                    _choice_option("replace", "仅用包内"),
                                ],
                                "local_files": loc_desc,
                                "incoming_files": inc_desc,
                                "added_count": len(new_only),
                                "removed_count": 0,
                                "change_summary": f"双方各新增，将补充 {len(new_only)} 份",
                                "archived_protected": False,
                            })
                    else:
                        three_way_shas = (base_shas - loc_removed - inc_removed) | loc_added | inc_added
                        choices = []
                        if not is_single and three_way_shas not in (loc_shas, inc_shas, loc_shas | inc_shas):
                            choices.append(_choice_option("three_way", f"智能合并（保留 {len(three_way_shas)} 份）"))
                        choices.extend([
                            _choice_option("keep_local", "保留本机"),
                            _choice_option("replace", "用包内替换本机"),
                            _choice_option("merge", "保留两份" if is_single else "合并保留全部"),
                        ])
                        items.append({
                            "key": f"att:{group_key}",
                            "kind": "attachment_group",
                            "group_key": group_key,
                            "label": label,
                            "status": "conflict",
                            "default_choice": "keep_local",
                            "choices": choices,
                            "local_files": loc_desc,
                            "incoming_files": inc_desc,
                            "three_way_shas": sorted(three_way_shas),
                            "added_count": len(inc_shas - loc_shas),
                            "removed_count": len(loc_shas - inc_shas),
                            "change_summary": "双方均有调整，请确认",
                            "archived_protected": archived,
                        })
            else:
                # Two-way comparison (no shared ancestor).
                if not loc_shas and inc_shas:
                    items.append({
                        "key": f"att:{group_key}",
                        "kind": "attachment_group",
                        "group_key": group_key,
                        "label": label,
                        "status": "auto_add",
                        "default_choice": "merge",
                        "choices": [
                            _choice_option("merge", "添加包内材料"),
                            _choice_option("keep_local", "不添加"),
                        ],
                        "local_files": loc_desc,
                        "incoming_files": inc_desc,
                        "added_count": len(inc_shas),
                        "removed_count": 0,
                        "change_summary": f"新增 {len(inc_shas)} 份",
                        "archived_protected": False,
                    })
                elif inc_shas <= loc_shas:
                    # Local already contains everything in incoming (or more).
                    continue
                elif loc_shas < inc_shas:
                    # Incoming is a strict superset of local: safe additive supplement.
                    extra = inc_shas - loc_shas
                    items.append({
                        "key": f"att:{group_key}",
                        "kind": "attachment_group",
                        "group_key": group_key,
                        "label": label,
                        "status": "auto_add",
                        "default_choice": "merge",
                        "choices": [
                            _choice_option("merge", "补充新增材料"),
                            _choice_option("keep_local", "保留本机"),
                        ],
                        "local_files": loc_desc,
                        "incoming_files": inc_desc,
                        "added_count": len(extra),
                        "removed_count": 0,
                        "change_summary": f"新增 {len(extra)} 份",
                        "archived_protected": False,
                    })
                else:
                    # Divergent attachment sets without a common ancestor: let user decide replace/merge/keep.
                    choices = (
                        [
                            _choice_option("keep_local", "保留本机"),
                            _choice_option("replace", "用包内替换本机"),
                            _choice_option("merge", "保留两份"),
                        ]
                        if is_single
                        else [
                            _choice_option("keep_local", "保留本机"),
                            _choice_option("replace", "用包内替换本机"),
                            _choice_option("merge", "合并保留全部"),
                        ]
                    )
                    items.append({
                        "key": f"att:{group_key}",
                        "kind": "attachment_group",
                        "group_key": group_key,
                        "label": label,
                        "status": "conflict",
                        "default_choice": "keep_local",
                        "choices": choices,
                        "local_files": loc_desc,
                        "incoming_files": inc_desc,
                        "added_count": len(inc_shas - loc_shas),
                        "removed_count": len(loc_shas - inc_shas),
                        "change_summary": f"与本机不同（本机 {len(loc_shas)} 份 / 包内 {len(inc_shas)} 份）",
                        "archived_protected": archived,
                    })

        # --- 2. Editable fields (paid_amount, actual_item_name, notes, ...) ---
        local_fields = local_entry.get("fields") or {}
        inc_fields = incoming_entry.get("fields") or {}
        base_fields_map = (base_snapshot.get("fields") or {}) if base_snapshot is not None else {}
        field_keys = list(dict.fromkeys([
            "paid_amount", "actual_item_name", "notes",
            *sorted(set(local_fields) | set(inc_fields) | set(base_fields_map)),
        ]))
        for field in field_keys:
            if field == "notes" and include_notes is False:
                continue
            loc_data = local_fields.get(field) or {}
            inc_data = inc_fields.get(field) or {}
            loc_val = str(loc_data.get("current") or "").strip()
            inc_val = str(inc_data.get("current") or "").strip()
            if same_value(field, loc_val, inc_val):
                continue
            label = EDITABLE_FIELD_LABELS.get(field, field)
            loc_hint = _field_source_hint(loc_data)
            inc_hint = _field_source_hint(inc_data)

            if field == "notes":
                if base_snapshot is not None:
                    base_val = str(base_fields_map.get("notes") or "").strip()
                    if same_value("notes", inc_val, base_val):
                        continue
                    if same_value("notes", loc_val, base_val):
                        if not loc_val:
                            items.append({
                                "key": "field:notes",
                                "kind": "notes",
                                "field": "notes",
                                "label": label,
                                "status": "auto_add",
                                "default_choice": "use_incoming",
                                "choices": [
                                    _choice_option("use_incoming", "采用包内"),
                                    _choice_option("keep_local", "保留本机"),
                                ],
                                "local_value": loc_val,
                                "incoming_value": inc_val,
                                "change_summary": "补充备注",
                                "archived_protected": False,
                            })
                        else:
                            status = "conflict" if (archived or not inc_val) else "auto_update"
                            default_choice = "keep_local" if status == "conflict" else "use_incoming"
                            choices = [
                                _choice_option("use_incoming", "同步清空" if not inc_val else "用包内替换"),
                                _choice_option("keep_local", "保留本机"),
                            ]
                            if inc_val:
                                choices.insert(1, _choice_option("merge", "合并两条备注"))
                            items.append({
                                "key": "field:notes",
                                "kind": "notes",
                                "field": "notes",
                                "label": label,
                                "status": status,
                                "default_choice": default_choice,
                                "choices": choices,
                                "local_value": loc_val,
                                "incoming_value": inc_val,
                                "change_summary": "对方已清空备注" if not inc_val else "对方已更新备注",
                                "archived_protected": archived,
                            })
                    else:
                        if not inc_val or inc_val in loc_val:
                            continue
                        items.append({
                            "key": "field:notes",
                            "kind": "notes",
                            "field": "notes",
                            "label": label,
                            "status": "conflict",
                            "default_choice": "merge",
                            "choices": [
                                _choice_option("merge", "合并两条备注"),
                                _choice_option("keep_local", "保留本机"),
                                _choice_option("use_incoming", "用包内替换"),
                            ],
                            "local_value": loc_val,
                            "incoming_value": inc_val,
                            "change_summary": "双方均修改了备注",
                            "archived_protected": archived,
                        })
                else:
                    if not inc_val or inc_val in loc_val:
                        continue
                    items.append({
                        "key": "field:notes",
                        "kind": "notes",
                        "field": "notes",
                        "label": label,
                        "status": "auto_add",
                        "default_choice": "merge" if loc_val else "use_incoming",
                        "choices": (
                            [
                                _choice_option("merge", "合并两条备注"),
                                _choice_option("use_incoming", "用包内替换"),
                                _choice_option("keep_local", "保留本机"),
                            ]
                            if loc_val
                            else [
                                _choice_option("use_incoming", "补充备注"),
                                _choice_option("keep_local", "不补充"),
                            ]
                        ),
                        "local_value": loc_val,
                        "incoming_value": inc_val,
                        "change_summary": "合并备注" if loc_val else "补充备注",
                        "archived_protected": False,
                    })
                continue

            # Non-note editable fields (paid_amount, actual_item_name, ...)
            if base_snapshot is not None:
                base_val = str(base_fields_map.get(field) or "").strip()
                if same_value(field, inc_val, base_val):
                    continue
                if same_value(field, loc_val, base_val):
                    if not loc_val and inc_val:
                        items.append({
                            "key": f"field:{field}",
                            "kind": "field",
                            "field": field,
                            "label": label,
                            "status": "auto_add",
                            "default_choice": "use_incoming",
                            "choices": [
                                _choice_option("use_incoming", "补充包内值"),
                                _choice_option("keep_local", "保留空值"),
                            ],
                            "local_value": loc_val,
                            "incoming_value": inc_val,
                            "local_hint": loc_hint,
                            "incoming_hint": inc_hint,
                            "change_summary": f"补充为 {inc_val}",
                            "archived_protected": False,
                        })
                    else:
                        status = "conflict" if (archived or not inc_val) else "auto_update"
                        default_choice = "keep_local" if status == "conflict" else "use_incoming"
                        items.append({
                            "key": f"field:{field}",
                            "kind": "field",
                            "field": field,
                            "label": label,
                            "status": status,
                            "default_choice": default_choice,
                            "choices": [
                                _choice_option("use_incoming", "同步清空" if not inc_val else "采用包内"),
                                _choice_option("keep_local", "保留本机"),
                            ],
                            "local_value": loc_val,
                            "incoming_value": inc_val,
                            "local_hint": loc_hint,
                            "incoming_hint": inc_hint,
                            "change_summary": f"对方已清空" if not inc_val else f"对方改为 {inc_val}",
                            "archived_protected": archived,
                        })
                else:
                    items.append({
                        "key": f"field:{field}",
                        "kind": "field",
                        "field": field,
                        "label": label,
                        "status": "conflict",
                        "default_choice": "keep_local",
                        "choices": [
                            _choice_option("keep_local", "保留本机"),
                            _choice_option("use_incoming", "采用包内"),
                        ],
                        "local_value": loc_val,
                        "incoming_value": inc_val,
                        "local_hint": loc_hint,
                        "incoming_hint": inc_hint,
                        "change_summary": "双方均有修改",
                        "archived_protected": archived,
                    })
            else:
                if not loc_val and inc_val:
                    items.append({
                        "key": f"field:{field}",
                        "kind": "field",
                        "field": field,
                        "label": label,
                        "status": "auto_add",
                        "default_choice": "use_incoming",
                        "choices": [
                            _choice_option("use_incoming", "补充包内值"),
                            _choice_option("keep_local", "保留空值"),
                        ],
                        "local_value": loc_val,
                        "incoming_value": inc_val,
                        "local_hint": loc_hint,
                        "incoming_hint": inc_hint,
                        "change_summary": f"补充为 {inc_val}",
                        "archived_protected": False,
                    })
                elif loc_val and inc_val:
                    hint_note = "（包内已人工修改）" if (inc_hint == "人工修改" and loc_hint != "人工修改") else ""
                    items.append({
                        "key": f"field:{field}",
                        "kind": "field",
                        "field": field,
                        "label": label,
                        "status": "conflict",
                        "default_choice": "keep_local",
                        "choices": [
                            _choice_option("keep_local", "保留本机"),
                            _choice_option("use_incoming", "采用包内"),
                        ],
                        "local_value": loc_val,
                        "incoming_value": inc_val,
                        "local_hint": loc_hint,
                        "incoming_hint": inc_hint,
                        "change_summary": f"与本机不同{hint_note}",
                        "archived_protected": archived,
                    })

        # --- 3. Base (invoice/header) fields ---
        base_base_fields = (base_snapshot.get("base_fields") or {}) if base_snapshot is not None else {}
        for field in COMPARABLE_BASE_FIELDS:
            loc_val = str(local_entry.get(field) or "").strip()
            inc_val = str(incoming_entry.get(field) or "").strip()
            if same_value(field, loc_val, inc_val):
                continue
            label = BASE_FIELD_LABELS.get(field, field)
            if base_snapshot is not None:
                base_val = str(base_base_fields.get(field) or "").strip()
                if same_value(field, inc_val, base_val):
                    continue
                if same_value(field, loc_val, base_val):
                    if not loc_val and inc_val:
                        items.append({
                            "key": f"base:{field}",
                            "kind": "base_field",
                            "field": field,
                            "label": label,
                            "status": "auto_add",
                            "default_choice": "use_incoming",
                            "choices": [
                                _choice_option("use_incoming", "补充包内值"),
                                _choice_option("keep_local", "保留空值"),
                            ],
                            "local_value": loc_val,
                            "incoming_value": inc_val,
                            "change_summary": f"补充为 {inc_val}",
                            "archived_protected": False,
                        })
                    else:
                        status = "conflict" if (archived or not inc_val) else "auto_update"
                        default_choice = "keep_local" if status == "conflict" else "use_incoming"
                        items.append({
                            "key": f"base:{field}",
                            "kind": "base_field",
                            "field": field,
                            "label": label,
                            "status": status,
                            "default_choice": default_choice,
                            "choices": [
                                _choice_option("use_incoming", "同步清空" if not inc_val else "采用包内"),
                                _choice_option("keep_local", "保留本机"),
                            ],
                            "local_value": loc_val,
                            "incoming_value": inc_val,
                            "change_summary": "对方已清空" if not inc_val else f"对方改为 {inc_val}",
                            "archived_protected": archived,
                        })
                else:
                    items.append({
                        "key": f"base:{field}",
                        "kind": "base_field",
                        "field": field,
                        "label": label,
                        "status": "conflict",
                        "default_choice": "keep_local",
                        "choices": [
                            _choice_option("keep_local", "保留本机"),
                            _choice_option("use_incoming", "采用包内"),
                        ],
                        "local_value": loc_val,
                        "incoming_value": inc_val,
                        "change_summary": "双方均有修改",
                        "archived_protected": archived,
                    })
            else:
                if not loc_val and inc_val:
                    items.append({
                        "key": f"base:{field}",
                        "kind": "base_field",
                        "field": field,
                        "label": label,
                        "status": "auto_add",
                        "default_choice": "use_incoming",
                        "choices": [
                            _choice_option("use_incoming", "补充包内值"),
                            _choice_option("keep_local", "保留空值"),
                        ],
                        "local_value": loc_val,
                        "incoming_value": inc_val,
                        "change_summary": f"补充为 {inc_val}",
                        "archived_protected": False,
                    })
                elif loc_val and inc_val:
                    items.append({
                        "key": f"base:{field}",
                        "kind": "base_field",
                        "field": field,
                        "label": label,
                        "status": "conflict",
                        "default_choice": "keep_local",
                        "choices": [
                            _choice_option("keep_local", "保留本机"),
                            _choice_option("use_incoming", "采用包内"),
                        ],
                        "local_value": loc_val,
                        "incoming_value": inc_val,
                        "change_summary": "与本机不同",
                        "archived_protected": archived,
                    })

        # --- 4. Line items ---
        loc_items_norm = item_values(local_entry.get("items") or [])
        inc_items_norm = item_values(incoming_entry.get("items") or [])
        if loc_items_norm != inc_items_norm:
            loc_summary = _items_summary(local_entry.get("items") or [])
            inc_summary = _items_summary(incoming_entry.get("items") or [])
            if base_snapshot is not None:
                base_items_norm = base_snapshot.get("items") or []
                if inc_items_norm != base_items_norm:
                    if loc_items_norm == base_items_norm:
                        if not loc_items_norm and inc_items_norm:
                            items.append({
                                "key": "items",
                                "kind": "items",
                                "label": "发票明细",
                                "status": "auto_add",
                                "default_choice": "use_incoming",
                                "choices": [
                                    _choice_option("use_incoming", "补齐明细"),
                                    _choice_option("keep_local", "保留本机"),
                                ],
                                "local_value": loc_summary,
                                "incoming_value": inc_summary,
                                "change_summary": f"补齐 {len(inc_items_norm)} 项明细",
                                "archived_protected": False,
                            })
                        else:
                            status = "conflict" if (archived or not inc_items_norm) else "auto_update"
                            default_choice = "keep_local" if status == "conflict" else "use_incoming"
                            items.append({
                                "key": "items",
                                "kind": "items",
                                "label": "发票明细",
                                "status": status,
                                "default_choice": default_choice,
                                "choices": [
                                    _choice_option("use_incoming", "采用包内明细"),
                                    _choice_option("keep_local", "保留本机明细"),
                                ],
                                "local_value": loc_summary,
                                "incoming_value": inc_summary,
                                "change_summary": "对方已更新明细",
                                "archived_protected": archived,
                            })
                    else:
                        items.append({
                            "key": "items",
                            "kind": "items",
                            "label": "发票明细",
                            "status": "conflict",
                            "default_choice": "keep_local",
                            "choices": [
                                _choice_option("keep_local", "保留本机明细"),
                                _choice_option("use_incoming", "采用包内明细"),
                            ],
                            "local_value": loc_summary,
                            "incoming_value": inc_summary,
                            "change_summary": "双方均修改了明细",
                            "archived_protected": archived,
                        })
            else:
                if not loc_items_norm and inc_items_norm:
                    items.append({
                        "key": "items",
                        "kind": "items",
                        "label": "发票明细",
                        "status": "auto_add",
                        "default_choice": "use_incoming",
                        "choices": [
                            _choice_option("use_incoming", "补齐明细"),
                            _choice_option("keep_local", "保留本机"),
                        ],
                        "local_value": loc_summary,
                        "incoming_value": inc_summary,
                        "change_summary": f"补齐 {len(inc_items_norm)} 项明细",
                        "archived_protected": False,
                    })
                elif loc_items_norm and inc_items_norm:
                    items.append({
                        "key": "items",
                        "kind": "items",
                        "label": "发票明细",
                        "status": "conflict",
                        "default_choice": "keep_local",
                        "choices": [
                            _choice_option("keep_local", "保留本机明细"),
                            _choice_option("use_incoming", "采用包内明细"),
                        ],
                        "local_value": loc_summary,
                        "incoming_value": inc_summary,
                        "change_summary": "与本机明细不同",
                        "archived_protected": archived,
                    })

        # --- 5. Batch催办批注 (batch_note) ---
        if include_notes is not False and incoming_batch_note:
            local_notes_text = str((local_fields.get("notes") or {}).get("current") or "").strip()
            already_present = (
                (local_batches and incoming_batch_note in local_batch_note)
                or (not local_batches and incoming_batch_note in local_notes_text)
            )
            if not already_present:
                base_bnote = str(base_snapshot.get("batch_note") or "").strip() if base_snapshot is not None else ""
                if not (base_snapshot is not None and incoming_batch_note == base_bnote and local_batch_note != base_bnote):
                    items.append({
                        "key": "batch_note",
                        "kind": "batch_note",
                        "label": "催办批注",
                        "status": "auto_add",
                        "default_choice": "merge",
                        "choices": [
                            _choice_option("merge", "合入批注"),
                            _choice_option("keep_local", "忽略"),
                        ],
                        "local_value": local_batch_note if local_batches else local_notes_text,
                        "incoming_value": incoming_batch_note,
                        "change_summary": "带入催办批注" if local_batches else "带入催办批注（写入备注）",
                        "archived_protected": False,
                    })

        # --- 6. Tags ---
        if include_tags is not False:
            local_tag_list = [str(tag).strip() for tag in (local_entry.get("tags") or []) if str(tag).strip()]
            inc_tag_list = [str(tag).strip() for tag in (incoming_entry.get("tags") or []) if str(tag).strip()]
            if base_snapshot is not None:
                base_tags = set(base_snapshot.get("tags") or [])
                loc_tag_set = set(local_tag_list)
                inc_tag_set = set(inc_tag_list)
                if inc_tag_set != base_tags and loc_tag_set != inc_tag_set:
                    if loc_tag_set == base_tags:
                        added_only = inc_tag_set > loc_tag_set
                        status = "auto_add" if added_only else ("conflict" if archived else "auto_update")
                        default_choice = "keep_local" if status == "conflict" else ("merge" if added_only else "use_incoming")
                        items.append({
                            "key": "tags",
                            "kind": "tags",
                            "label": "标签",
                            "status": status,
                            "default_choice": default_choice,
                            "choices": [
                                _choice_option("use_incoming", "采用包内标签"),
                                _choice_option("merge", "合并标签"),
                                _choice_option("keep_local", "保留本机标签"),
                            ],
                            "local_value": "、".join(local_tag_list) or "无",
                            "incoming_value": "、".join(inc_tag_list) or "无",
                            "change_summary": f"新增 {len(inc_tag_set - loc_tag_set)} 个标签" if added_only else "对方已更新标签",
                            "archived_protected": archived and not added_only,
                        })
                    else:
                        merged_tag_list = merge_tags(local_tag_list, inc_tag_list)
                        if merged_tag_list != local_tag_list:
                            items.append({
                                "key": "tags",
                                "kind": "tags",
                                "label": "标签",
                                "status": "auto_add",
                                "default_choice": "merge",
                                "choices": [
                                    _choice_option("merge", "合并标签"),
                                    _choice_option("use_incoming", "采用包内标签"),
                                    _choice_option("keep_local", "保留本机标签"),
                                ],
                                "local_value": "、".join(local_tag_list) or "无",
                                "incoming_value": "、".join(inc_tag_list) or "无",
                                "change_summary": f"合并标签（+{max(1, len(set(merged_tag_list) - loc_tag_set))}）",
                                "archived_protected": False,
                            })
            else:
                merged_tag_list = merge_tags(local_tag_list, inc_tag_list)
                if merged_tag_list != local_tag_list:
                    items.append({
                        "key": "tags",
                        "kind": "tags",
                        "label": "标签",
                        "status": "auto_add",
                        "default_choice": "merge",
                        "choices": [
                            _choice_option("merge", "合并标签"),
                            _choice_option("use_incoming", "采用包内标签"),
                            _choice_option("keep_local", "保留本机标签"),
                        ],
                        "local_value": "、".join(local_tag_list) or "无",
                        "incoming_value": "、".join(inc_tag_list) or "无",
                        "change_summary": f"标签 {max(1, len(set(merged_tag_list) - set(local_tag_list)))}",
                        "archived_protected": False,
                    })

        # --- 7. Extension fields (same-package adapter fields) ---
        if extension_preview:
            for conflict_field in extension_preview.get("conflicts") or []:
                fid = conflict_field["field_id"]
                pkg = conflict_field["package_id"]
                flabel = conflict_field.get("label") or fid
                loc_v = conflict_field.get("local_value")
                inc_v = conflict_field.get("incoming_value")
                base_ext_map = (base_snapshot.get("extension_values") or {}) if base_snapshot is not None else {}
                ext_key = f"{pkg}::{fid}"
                if base_snapshot is not None:
                    base_v = base_ext_map.get(ext_key)
                    if inc_v == base_v:
                        continue
                    if loc_v == base_v and not archived:
                        items.append({
                            "key": f"ext:{ext_key}",
                            "kind": "extension",
                            "package_id": pkg,
                            "field_id": fid,
                            "label": flabel,
                            "status": "auto_update",
                            "default_choice": "use_incoming",
                            "choices": [
                                _choice_option("use_incoming", "采用包内"),
                                _choice_option("keep_local", "保留本机"),
                            ],
                            "local_value": str(loc_v if loc_v is not None else ""),
                            "incoming_value": str(inc_v if inc_v is not None else ""),
                            "change_summary": f"对方改为 {inc_v}",
                            "archived_protected": False,
                        })
                        continue
                items.append({
                    "key": f"ext:{ext_key}",
                    "kind": "extension",
                    "package_id": pkg,
                    "field_id": fid,
                    "label": flabel,
                    "status": "conflict",
                    "default_choice": "keep_local",
                    "choices": [
                        _choice_option("keep_local", "保留本机"),
                        _choice_option("use_incoming", "采用包内"),
                    ],
                    "local_value": str(loc_v if loc_v is not None else ""),
                    "incoming_value": str(inc_v if inc_v is not None else ""),
                    "change_summary": "双方均有修改" if base_snapshot is not None else "与本机不同",
                    "archived_protected": archived,
                })

    # --- 8. Always-safe additive metadata (skipped on stale ancestor resends) ---
    attachment_note_changes = 0
    payment_ai_upgrades = 0
    if mode != "ancestor":
        for sha, inc_att in incoming_by_sha.items():
            loc_att = local_by_sha.get(sha)
            if not loc_att:
                continue
            if include_notes is not False:
                merged_note = merge_text(str(loc_att.get("note") or ""), str(inc_att.get("note") or ""))
                if merged_note != str(loc_att.get("note") or ""):
                    attachment_note_changes += 1
            if _payment_recognition_upgrades(loc_att, inc_att):
                payment_ai_upgrades += 1

    # Archived-entry protection: never mutate an archived entry silently by default.
    if archived:
        for item in items:
            if item["default_choice"] != "keep_local":
                item["status"] = "conflict"
                item["default_choice"] = "keep_local"
                item["archived_protected"] = True

    existing_ocr = {
        (
            str(result.get("provider") or ""), str(result.get("file_sha256") or ""),
            str(result.get("created_at") or ""), str(result.get("normalized") or ""),
        )
        for result in local_entry.get("_ocr_results") or []
    }
    added_ocr = sum(
        1 for result in incoming_entry.get("ocr_results") or []
        if (
            str(result.get("provider") or "aliyun"), str(result.get("file_sha256") or ""),
            str(result.get("created_at") or ""), str(result.get("normalized") or ""),
        ) not in existing_ocr
    )
    existing_history = {
        (
            str(row.get("field") or ""), str(row.get("old_value") or ""),
            str(row.get("new_value") or ""), str(row.get("changed_at") or ""),
        )
        for row in local_entry.get("history") or []
    }
    added_history = sum(
        1 for history in incoming_entry.get("history") or []
        if (
            str(history.get("field") or ""), str(history.get("old_value") or ""),
            str(history.get("new_value") or ""), str(history.get("changed_at") or ""),
        ) not in existing_history
    )
    eligible_ext = list((extension_preview or {}).get("eligible") or [])
    extra_reminders = [
        line for line in _non_material_check_lines(str(incoming_entry.get("check_message") or ""))
        if line not in _non_material_check_lines(str(local_entry.get("check_message") or ""))
    ]
    # Also check recognition_data fill when empty locally
    fills_recognition_data = bool(
        str(incoming_entry.get("recognition_data") or "").strip()
        and not str(local_entry.get("recognition_data") or "").strip()
    )
    fills_source = bool(
        str(incoming_entry.get("source") or "").strip()
        and not str(local_entry.get("source") or "").strip()
    )

    # Build concise labels for list row display
    labels: list[str] = []
    att_added_total = 0
    att_replaced_groups = 0
    field_updated_count = 0
    base_filled_count = 0
    conflict_count = 0
    replacement_count = 0

    for item in items:
        if item["status"] == "conflict":
            conflict_count += 1
        if item["kind"] == "attachment_group":
            if item["default_choice"] == "replace":
                att_replaced_groups += 1
                replacement_count += 1
            elif item["status"] == "auto_add":
                att_added_total += int(item.get("added_count") or 0)
        elif item["kind"] == "field":
            if item["status"] in {"auto_add", "auto_update"}:
                field_updated_count += 1
                if item["status"] == "auto_update":
                    replacement_count += 1
        elif item["kind"] == "base_field":
            if item["status"] in {"auto_add", "auto_update"}:
                base_filled_count += 1
                if item["status"] == "auto_update":
                    replacement_count += 1
        elif item["kind"] == "notes" and item["status"] in {"auto_add", "auto_update"}:
            labels.append("备注")
        elif item["kind"] == "batch_note":
            labels.append("催办批注")
        elif item["kind"] == "items" and item["status"] in {"auto_add", "auto_update"}:
            labels.append("发票明细")
        elif item["kind"] == "tags" and item["status"] in {"auto_add", "auto_update"}:
            labels.append(item["change_summary"])
        elif item["kind"] == "extension" and item["status"] == "auto_update":
            field_updated_count += 1
            replacement_count += 1

    ordered_labels: list[str] = []
    if att_replaced_groups:
        ordered_labels.append(f"替换材料 {att_replaced_groups} 类")
    if att_added_total:
        ordered_labels.append(f"材料 {att_added_total}")
    if attachment_note_changes:
        ordered_labels.append(f"附件备注 {attachment_note_changes}")
    if payment_ai_upgrades:
        ordered_labels.append(f"付款截图识别 {payment_ai_upgrades}")
    if "备注" in labels:
        ordered_labels.append("备注")
    if "催办批注" in labels:
        ordered_labels.append("催办批注")
    if field_updated_count:
        ordered_labels.append(f"信息 {field_updated_count}")
    if base_filled_count or fills_recognition_data or fills_source:
        ordered_labels.append(f"基础信息 {base_filled_count + int(fills_recognition_data) + int(fills_source)}")
    if "发票明细" in labels:
        ordered_labels.append("发票明细")
    if eligible_ext:
        ordered_labels.append(f"附加信息 {len(eligible_ext)}")
    if added_ocr:
        ordered_labels.append(f"识别记录 {added_ocr}")
    for tag_label in labels:
        if "标签" in tag_label:
            ordered_labels.append(tag_label)
    if added_history:
        ordered_labels.append(f"修改记录 {added_history}")
    if extra_reminders and mode != "ancestor":
        ordered_labels.append(f"识别提醒 {len(extra_reminders)}")
    if conflict_count:
        ordered_labels.append(f"待确认 {conflict_count} 项")

    has_auto_changes = bool(
        any(item["status"] in {"auto_add", "auto_update"} for item in items)
        or attachment_note_changes
        or payment_ai_upgrades
        or added_ocr
        or added_history
        or eligible_ext
        or fills_recognition_data
        or fills_source
        or (extra_reminders and mode != "ancestor")
    )
    has_changes = bool(has_auto_changes or conflict_count)

    return {
        "mode": mode,
        "base_hash": base_hash,
        "archived": archived,
        "archived_batches": archived_batches,
        "items": items,
        "has_changes": has_changes,
        "has_conflicts": conflict_count > 0,
        "has_replacements": replacement_count > 0,
        "conflict_count": conflict_count,
        "replacement_count": replacement_count,
        "labels": ordered_labels,
        # Legacy summary counters preserved for compatibility
        "attachments": att_added_total,
        "attachment_notes": attachment_note_changes,
        "payment_ai_upgrades": payment_ai_upgrades,
        "fields": [item["field"] for item in items if item["kind"] in {"field", "notes"} and item["status"] != "conflict"],
        "base_fields": [
            *[item["field"] for item in items if item["kind"] == "base_field" and item["status"] != "conflict"],
            *(["recognition_data"] if fills_recognition_data else []),
            *(["source"] if fills_source else []),
        ],
        "items_changed": any(item["kind"] == "items" and item["status"] != "conflict" for item in items),
        "ocr_results": added_ocr,
        "history": added_history,
        "extra_reminders": extra_reminders if mode != "ancestor" else [],
    }


def validate_entry_decisions(merge_plan: dict, raw_decisions: dict | None) -> dict[str, str]:
    """Validate per-item merge choices against the plan's allowed choices."""
    if not raw_decisions:
        return {item["key"]: item["default_choice"] for item in merge_plan.get("items") or []}
    if not isinstance(raw_decisions, dict):
        raise ValueError("条目合并选项格式无效，请重新打开导入预览。")
    allowed_by_key = {
        item["key"]: {choice["value"] for choice in item.get("choices") or []}
        for item in merge_plan.get("items") or []
    }
    resolved = {item["key"]: item["default_choice"] for item in merge_plan.get("items") or []}
    for key, value in raw_decisions.items():
        if key not in allowed_by_key:
            continue
        choice = str(value or "").strip()
        if choice not in allowed_by_key[key]:
            raise ValueError(f"条目合并选项无效：{key}={choice}")
        resolved[key] = choice
    return resolved

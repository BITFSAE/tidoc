"""Binding-package import transaction: entry restoration, batches and rollback.

Archive inspection and exchange helpers live in bindle.py. This module owns writes.
"""
from __future__ import annotations
import copy
import json
import shutil
import uuid
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from ..db.entries import EntryRepo
from ..db.attachments import AttachmentRepo
from . import bindle as storage


class _EntryRestorer:
    """Restore one entry inside the caller's database and file transaction."""

    def __init__(
        self, *, entries_repo, attachments_repo, requested_scheme_id, adapter_service,
        inspected, import_tags, package_profiles, resolve_profile, source_profile_map,
        created_files, created_dirs, role_mappings_by_entry, now,
    ):
        self.entries_repo = entries_repo
        self.attachments_repo = attachments_repo
        self.requested_scheme_id = requested_scheme_id
        self.adapter_service = adapter_service
        self.inspected = inspected
        self.import_tags = import_tags
        self.package_profiles = package_profiles
        self.resolve_profile = resolve_profile
        self.source_profile_map = source_profile_map
        self.created_files = created_files
        self.created_dirs = created_dirs
        self.role_mappings_by_entry = role_mappings_by_entry
        self.now = now
        self.conn = entries_repo.db.conn

    def restore(self, e, entry_index, destination_profile_id, invoice_no, zf):
        check_status = e.get("check_status", "warning")
        check_message = e.get("check_message", "")
        status = e.get("status", "draft")
        source_adapter = e.get("adapter") or {}
        exchange_revision_id = storage._external_revision_id(self.entries_repo, source_adapter)
        target_scheme_id = ""
        target_revision_id = ""
        if self.requested_scheme_id and self.adapter_service:
            target_scheme = self.adapter_service.get_scheme(self.requested_scheme_id)
            target_scheme_id = target_scheme.get("id") or target_scheme.get("scheme_id") or ""
            target_revision_id = target_scheme.get("current_revision_id") or target_scheme.get("revision_id") or ""
        elif not source_adapter and self.adapter_service:
            try:
                target_scheme_id, target_revision_id = self.adapter_service.default_binding()
            except ValueError:
                pass
        if source_adapter and not e.get("scheme_revision_id"):
            status = "partial" if status == "complete" else status
            check_status = "warning" if check_status not in {"blocked", "fail"} else check_status
            check_message = storage._merge_text(check_message, "来自其他报账方案的材料；本机尚未绑定该方案规则。")
        if self.inspected["tampered"]:
            check_status = "blocked"
            status = "partial"
            tampered_message = "绑定包完整性校验未通过，导入前已由用户确认"
            check_message = "；".join(filter(None, [tampered_message, check_message]))

        new_id = uuid.uuid4().hex
        self.conn.execute(
            """INSERT INTO entries(id, profile_id, title, invoice_no, invoice_date,
               seller, total, buyer_name, buyer_tax_id, category, tags, status,
               check_status, check_message, source, created_at, updated_at,scheme_id,scheme_revision_id,
               seller_tax_id,recognition_data)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id, destination_profile_id, e.get("title", ""), invoice_no,
             e.get("invoice_date", ""), e.get("seller", ""), e.get("total", ""),
             e.get("buyer_name", ""), e.get("buyer_tax_id", ""), e.get("category", ""),
             json.dumps(
                 list(dict.fromkeys(
                     [str(tag).strip() for tag in (e.get("tags") or []) if str(tag).strip()]
                     + self.import_tags
                 )),
                 ensure_ascii=False,
             ), status,
             check_status, check_message, e.get("source", "imported"),
             e.get("created_at") or self.now, self.now, target_scheme_id or None, target_revision_id or None,
             e.get("seller_tax_id", ""), storage._validated_recognition_data(e.get("recognition_data"))),
        )
        for field, fv in e.get("fields", {}).items():
            self.conn.execute(
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
            self.conn.execute(
                """INSERT INTO items(entry_id, name, actual_name, unit, quantity,
                   unit_price, total, spec, ordinal) VALUES(?,?,?,?,?,?,?,?,?)""",
                (new_id, it.get("name", ""), it.get("actual_name", ""), it.get("unit", ""),
                 it.get("quantity", ""), it.get("unit_price", ""), it.get("total", ""),
                 it.get("spec", ""), it.get("ordinal", 0)),
            )
        for h in e.get("history", []):
            history_source_profile_id = str(h.get("profile_id") or "")
            if history_source_profile_id in self.package_profiles:
                history_profile_id = self.resolve_profile(
                    self.package_profiles[history_source_profile_id],
                    history_source_profile_id,
                )
            elif history_source_profile_id:
                history_profile_id = self.source_profile_map.get(
                    history_source_profile_id, destination_profile_id
                )
            else:
                history_profile_id = ""
            self.conn.execute(
                """INSERT INTO field_history(entry_id, field, old_value, new_value, profile_id, changed_at)
                   VALUES(?,?,?,?,?,?)""",
                (new_id, h.get("field", ""), h.get("old_value", ""), h.get("new_value", ""),
                 history_profile_id, h.get("changed_at", self.now)),
            )
        for result in e.get("ocr_results", []):
            self.conn.execute(
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
                    result.get("created_at", self.now),
                ),
            )
        # 附件：从包里解出到新条目目录，重建记录
        dest_dir = self.attachments_repo.data_root.entry_dir(new_id)
        self.created_dirs.append(dest_dir)
        for att in e.get("attachments", []):
            arcname = f"attachments/{att['stored_path']}"
            if not storage._has_archive_member(zf, arcname):
                continue
            stored_name = Path(att["stored_path"]).name
            dest = dest_dir / stored_name
            self.created_files.append(dest)
            actual_digest = storage._extract_archive_member(zf, arcname, dest)
            expected_digest = str(att.get("sha256") or "")
            if (
                expected_digest
                and actual_digest != expected_digest
                and not self.inspected["tampered"]
            ):
                raise ValueError(
                    f"附件 {att.get('original_name') or stored_name} 校验失败。"
                )
            from ..adapters.transfer import role_id as get_role_id
            source_role = get_role_id(att)
            mapping = self.role_mappings_by_entry.get(str(entry_index), self.role_mappings_by_entry)
            mapped_role = mapping.get(source_role) if isinstance(mapping, dict) else None
            if source_role=='invoice' and target_revision_id:
                mapped_role='invoice'
            target_revision = ""
            mapped_type=att.get('type','other')
            if mapped_role and target_revision_id:
                from ..db.adapters import AdapterRepo
                target_definition = AdapterRepo(self.entries_repo.db).get_revision(target_revision_id)
                role_rows = target_definition.get("materials", [])
                role_rows = role_rows.get("roles", []) if isinstance(role_rows, dict) else role_rows
                if mapped_role not in {r.get("id") for r in role_rows}:
                    raise ValueError(f"材料角色映射目标不存在：{mapped_role}")
                stored_role, role_revision = mapped_role, target_revision_id
                mapped_type=storage._validated_material_mapping(self.entries_repo,new_id,target_revision_id,source_role,mapped_role,mapped_type,att.get('original_name') or stored_name)
            else:
                stored_role = source_role
                role_revision = exchange_revision_id or None
            self.conn.execute(
                """INSERT INTO attachments(id, entry_id, type, original_name, stored_path,
                   sha256, note, added_at, role_id, role_definition_revision_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, new_id, mapped_type,
                 att.get("original_name", ""), f"{new_id}/{stored_name}",
                 actual_digest, att.get("note", ""), att.get("added_at", self.now), stored_role, role_revision),
            )
        storage._retain_adapter_source(self.entries_repo, new_id, source_adapter)
        storage._merge_extension_data(self.entries_repo, new_id, source_adapter)
        return new_id


def _assign_batches(conn, now, affected_entry_indexes, entry_batch_overrides,
                    destination_by_entry_index, target_batch_id, new_batch_name):
    batch_created = global_batch_created = False
    individually_assigned = globally_assigned = 0
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

    return target_batch_id, batch_created, global_batch_created, individually_assigned, globally_assigned


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
    adapter_service = storage._adapter_service(entries_repo, adapter_service)
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

    inspected = inspected if inspected is not None else storage.inspect_bindle(path)
    if inspected:
        preview_scheme = str(inspected.get("target_scheme_id") or "")
        if preview_scheme and requested_scheme_id and preview_scheme != requested_scheme_id:
            raise ValueError("导入目标方案与预览不一致，请重新打开导入预览。")
        expected_digest = inspected.get("archive_digest")
        if not expected_digest or storage._archive_digest(Path(path)) != expected_digest:
            raise ValueError("预览后绑定包或本地条目已变化，请重新打开导入预览。")
        current = copy.deepcopy(inspected)
        for entry in current.get("entries", []):
            for key in ("import_action", "existing_entry_id", "existing_batches", "merge_preview",
                        "adapter_compatibility", "extension_merge_preview", "role_mapping_preview"):
                entry.pop(key, None)
        storage._annotate_import_actions(entries_repo, current, adapter_service=adapter_service,
                                 target_scheme_id=inspected.get("target_scheme_id"))
        matched = {e.get("existing_entry_id") for e in current["entries"] if e.get("existing_entry_id")}
        current_version = storage._local_preview_version(entries_repo, matched)
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
    existing_by_invoice_no, existing_by_invoice_hash = storage._entry_identity_maps(conn)
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

    restorer = _EntryRestorer(
        entries_repo=entries_repo,
        attachments_repo=attachments_repo,
        requested_scheme_id=requested_scheme_id,
        adapter_service=adapter_service,
        inspected=inspected,
        import_tags=import_tags,
        package_profiles=package_profiles,
        resolve_profile=resolve_profile,
        source_profile_map=source_profile_map,
        created_files=created_files,
        created_dirs=created_dirs,
        role_mappings_by_entry=role_mappings_by_entry,
        now=now,
    )
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
                existing_id = storage._matching_entry_id(
                    e, existing_by_invoice_no, existing_by_invoice_hash
                )
                if existing_id:
                    destination_by_entry_index[entry_index] = existing_id
                    changes = storage._merge_existing_entry(
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

                new_id = restorer.restore(e, entry_index, destination_profile_id, invoice_no, zf)
                imported_ids.append(new_id)
                destination_by_entry_index[entry_index] = new_id
                affected_entry_indexes.add(entry_index)
                if invoice_no:
                    existing_by_invoice_no[invoice_no] = new_id
                for digest in invoice_hashes:
                    existing_by_invoice_hash[digest] = new_id

            affected_ids = list(dict.fromkeys(imported_ids + updated_ids))
            (target_batch_id, batch_created, global_batch_created,
             individually_assigned, globally_assigned) = _assign_batches(
                conn, now, affected_entry_indexes, entry_batch_overrides,
                destination_by_entry_index, target_batch_id, new_batch_name,
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
            for affected_id in affected_ids:
                entries_repo.recompute_status(affected_id,commit=False)
            conn.commit()
    except BaseException as exc:  # Roll back writes and extracted files on cancellation.
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
        if cleanup_errors and not isinstance(exc, Exception):
            exc.add_note("失败后的附件目录清理未完成：" + "；".join(cleanup_errors))
        elif cleanup_errors:
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

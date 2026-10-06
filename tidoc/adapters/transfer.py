"""Allowlisted public projections used by v5 material bindles."""
from __future__ import annotations

import copy
import hashlib
import json
from collections import defaultdict
from typing import Iterable, Mapping

TRANSFER_CAPABILITY = "bindle.v5"
PUBLIC_SETTINGS = frozenset({"profile.reviewer_required", "entry.default_paid_to_invoice"})
_BUILTIN_ROLES = {
    "invoice_pdf": "invoice", "invoice_xml": "invoice", "invoice": "invoice",
    "payment_screenshot": "payment_screenshot", "physical_image": "physical_image",
    "inspection_pdf": "inspection_pdf", "other": "other",
}


def canonical_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def content_hash(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def revision_digest(definition: dict) -> str:
    from .resolver import revision_hash
    return revision_hash(definition)


def _fields(definition):
    fields = definition.get("fields", [])
    fields = fields.get("fields", []) if isinstance(fields, dict) else fields
    return fields if isinstance(fields, list) else []


def _roles(definition):
    roles = definition.get("materials", [])
    roles = roles.get("roles", []) if isinstance(roles, dict) else roles
    return roles if isinstance(roles, list) else []


def _rules(definition):
    rules = definition.get("rules", [])
    rules = rules.get("rules", []) if isinstance(rules, dict) else rules
    return rules if isinstance(rules, list) else []


def role_id(attachment: Mapping) -> str:
    value = attachment.get("role_id")
    if value:
        return str(value)
    return _BUILTIN_ROLES.get(str(attachment.get("type") or ""), "other")


def _copy_keys(value, keys):
    return {key: copy.deepcopy(value[key]) for key in keys if key in value}


def field_key(field: Mapping, package_id: str = "") -> str:
    return f"{field.get('package_id') or package_id}:{field.get('scope', 'entry')}:{field['id']}"


def field_can_transfer(field: Mapping, package_id: str, include_ask_fields: Iterable[str] = ()) -> bool:
    if field.get("scope") != "entry":
        return False
    policy = field.get("transfer", "ask" if field.get("sensitive") else "include")
    if policy == "never":
        return False
    if policy == "include" and not field.get("sensitive"):
        return True
    # An explicit per-field choice is required for ask and sensitive values.
    return field_key(field, package_id) in set(include_ask_fields)


def public_rule_projection(definition: dict, *, include_ask_fields: Iterable[str] = ()) -> dict:
    """Return only definitions needed to interpret entry completion on another device."""
    manifest_source = definition.get("manifest", {})
    if not isinstance(manifest_source, Mapping):
        manifest_source = {}
    manifest = _copy_keys(manifest_source,
                          ("format", "schema_version", "package_id", "package_version", "name", "description", "author"))
    required = manifest_source.get("requires", {})
    if not isinstance(required, Mapping):
        required = {}
    capabilities = [str(cap) for cap in required.get("capabilities", [])
                    if not str(cap).startswith(("output.", "template.", "payee."))]
    manifest["requires"] = {"adapter_api": required.get("adapter_api", 1),
                            "capabilities": sorted(set(capabilities))}
    package_id = manifest.get("package_id", "")
    projected_fields = []
    for field in _fields(definition):
        if not isinstance(field, Mapping) or field.get("scope") != "entry" or not field.get("id"):
            continue
        clean = _copy_keys(field, ("id", "scope", "label", "type", "max_length", "min_length", "minimum", "maximum",
                                  "min", "max", "options", "required_at", "presentation", "sensitive", "transfer",
                                  "help", "description", "visible_when"))
        if "default" in field and field_can_transfer(field, package_id, include_ask_fields):
            clean["default"] = copy.deepcopy(field["default"])
        projected_fields.append(clean)
    scheme_source = definition.get("scheme", {})
    if not isinstance(scheme_source, Mapping):
        scheme_source = {}
    scheme = _copy_keys(scheme_source, ("titles", "default_title_id"))
    # Organization fields are intentionally private/local even if their definitions are public.
    scheme["organization"] = {}
    scheme["settings"] = {key: copy.deepcopy(value) for key, value in scheme_source.get("settings", {}).items()
                          if key in PUBLIC_SETTINGS}
    clean_roles = []
    for role in _roles(definition):
        if not isinstance(role, Mapping) or not role.get("id"):
            continue
        clean_roles.append(_copy_keys(role, ("id", "label", "description", "extensions", "min_count", "max_count",
                                              "order", "quick_action", "reclassifiable", "presentation")))
    clean_rules = [_copy_keys(rule, ("id", "stage", "when", "require", "message", "severity", "suggested_action"))
                   for rule in _rules(definition) if isinstance(rule, Mapping)
                   and rule.get("stage", "complete") == "complete"]
    return {"manifest": manifest, "scheme": scheme, "fields": projected_fields,
            "materials": clean_roles, "rules": clean_rules, "outputs": [],
            "effective_settings": {key: copy.deepcopy(value)
                                   for key, value in definition.get("effective_settings", {}).items()
                                   if key in PUBLIC_SETTINGS}}


def project_extension_data(definition: dict, values: list[dict], history: list[dict], *,
                           include_ask_fields: Iterable[str] = ()) -> tuple[list[dict], list[dict]]:
    """Apply identical transfer policy to current values and every historical value."""
    package_id = definition.get("manifest", {}).get("package_id", "")
    allowed = {field["id"] for field in _fields(definition)
               if isinstance(field, Mapping) and field.get("id")
               and field_can_transfer(field, package_id, include_ask_fields)}

    def project(records, keys):
        result = []
        for row in records:
            if (row.get("scope", "entry") != "entry" or row.get("field_id") not in allowed
                    or row.get("package_id", package_id) != package_id):
                continue
            result.append(_copy_keys(row, keys))
        return result

    return (project(values, ("scope", "package_id", "field_id", "value", "value_type", "definition_revision_id",
                             "version", "updated_at")),
            project(history, ("id", "scope", "package_id", "field_id", "old_value", "new_value", "profile_id",
                              "actor_id", "kind", "changed_at", "definition_revision_id", "operation", "action")))


def ask_field_preview(definitions_and_values: Iterable[tuple[dict, list[dict]]]) -> list[dict]:
    counts = defaultdict(set)
    fields_by_key = {}
    for definition, values in definitions_and_values:
        package_id = definition.get("manifest", {}).get("package_id", "")
        for field in _fields(definition):
            if field.get("scope") != "entry" or field.get("transfer") == "never":
                continue
            if field.get("transfer") != "ask" and not field.get("sensitive"):
                continue
            key = field_key(field, package_id)
            fields_by_key[key] = {"key": key, "package_id": package_id, "scope": "entry", "field_id": field["id"],
                                  "label": field.get("label", field["id"]), "sensitive": bool(field.get("sensitive"))}
            for index, value in enumerate(values):
                if (value.get("scope", "entry") == "entry" and value.get("field_id") == field["id"]
                        and value.get("package_id", package_id) == package_id):
                    if value.get("value") not in (None, ""):
                        counts[key].add(value.get("owner_id", index))
    return [dict(fields_by_key[key], count=len(counts[key])) for key in sorted(fields_by_key) if counts[key]]


def supported_rule_capabilities(definition: dict) -> tuple[list[str], list[str]]:
    from . import registry
    supported = getattr(registry, "CAPABILITIES", getattr(registry, "capabilities", ()))
    if callable(supported):
        supported = supported()
    required = definition.get("manifest", {}).get("requires", {})
    required_caps = list(required.get("capabilities", []))
    missing = sorted(set(required_caps) - set(supported))
    if required.get("adapter_api", 1) != 1:
        missing.append(f"adapter-api.{required['adapter_api']}")
    return required_caps, missing

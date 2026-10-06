#!/usr/bin/env python3
"""Repeatable synthetic benchmark for adapter-aware and legacy entry listing.

Uses only an in-memory SQLite database and generated metadata. It never reads
invoice files, user databases, attachment directories, or personal data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import sqlite3
import sys
import time
from pathlib import Path
from uuid import uuid4

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tidoc import __version__ as TIDOC_VERSION
from tidoc.adapters.resolver import canonical_json, revision_hash
from tidoc.db.database import Database
from tidoc.db.entries import EntryRepo

DEFAULT_OUTPUT = Path("/tmp/tidoc-adapter-benchmark.json")
ATTACHMENT_TYPES = (
    ("invoice", "invoice_pdf"),
    ("payment_screenshot", "payment_screenshot"),
    ("physical_image", "physical_image"),
    ("inspection_pdf", "inspection_pdf"),
    ("other", "other"),
)


def _definition(scheme_index: int, rules_per_scheme: int) -> dict:
    package_id = f"org.tidoc.synthetic.bench{scheme_index}"
    roles = []
    for index, (role_id, _attachment_type) in enumerate(ATTACHMENT_TYPES):
        roles.append({
            "id": role_id,
            "label": role_id,
            "extensions": [],
            "min_count": 1 if role_id == "invoice" else 0,
            "max_count": 100,
            "order": index,
            "quick_action": False,
            "reclassifiable": role_id != "invoice",
            "presentation": "visible",
        })
    rules = [{
        "id": f"bench_rule_{index:02d}",
        "stage": "complete",
        "when": {"field": "invoice.total", "op": "gte", "value": "0.00"},
        "require": [{"material": "invoice", "min_count": 1}],
        "message": f"Synthetic policy rule {index:02d}.",
        "severity": "required",
    } for index in range(rules_per_scheme)]
    return {
        "manifest": {
            "format": "tidoc-team-adapter",
            "schema_version": "1.0",
            "package_id": package_id,
            "package_version": "1.0.0",
            "name": f"Synthetic scheme {scheme_index}",
            "requires": {"adapter_api": 1, "capabilities": ["material-roles.v1", "rules.v1"]},
        },
        "scheme": {"organization": {"name": f"Synthetic {scheme_index}"}, "titles": [], "settings": {}},
        "fields": [],
        "materials": roles,
        "rules": rules,
        "outputs": [],
        "effective_settings": {"profile.reviewer_required": False},
    }


def create_dataset(
    entry_count: int = 10_000,
    attachments_per_entry: int = 5,
    scheme_count: int = 5,
    rules_per_scheme: int = 20,
):
    """Create a self-contained in-memory dataset using batched SQL inserts.

    Returns (Database, EntryRepo, scheme_ids). Database lifetime is owned by
    the caller. Parameters are injectable so performance tests can use a
    smaller workload without duplicating setup logic.
    """
    if entry_count < 1 or attachments_per_entry < 1 or scheme_count < 1 or rules_per_scheme < 0:
        raise ValueError("workload sizes must be positive (rules may be zero)")
    db = Database(":memory:")
    conn = db.conn
    timestamp = "2026-01-01T00:00:00"
    profile_id = "synthetic-profile"
    conn.execute(
        "INSERT INTO profiles(id,name,reviewer,is_default,created_at) VALUES(?,?,?,?,?)",
        (profile_id, "Synthetic claimant", "", 1, timestamp),
    )

    schemes = []
    for index in range(scheme_count):
        definition = _definition(index, rules_per_scheme)
        definition_json = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(definition_json.encode("utf-8")).hexdigest()
        revision_id = revision_hash(definition)
        scheme_id = f"synthetic-scheme-{index}"
        conn.execute(
            """INSERT INTO adapter_packages(content_hash,package_id,package_version,schema_version,
               source,resource_path,definition_json,diagnostics_json,installed_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (content_hash, definition["manifest"]["package_id"], "1.0.0", "1.0", "benchmark",
             "synthetic-memory-only", definition_json, "[]", timestamp),
        )
        conn.execute(
            "INSERT INTO scheme_revisions(revision_id,content_hash,definition_json,created_at) VALUES(?,?,?,?)",
            (revision_id, content_hash, definition_json, timestamp),
        )
        conn.execute(
            """INSERT INTO schemes(id,name,current_revision_id,overrides_json,is_default,disabled,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (scheme_id, definition["manifest"]["name"], revision_id, "{}", int(index == 0), 0, timestamp, timestamp),
        )
        conn.execute(
            "INSERT INTO scheme_revision_links(scheme_id,revision_id,created_at) VALUES(?,?,?)",
            (scheme_id, revision_id, timestamp),
        )
        schemes.append((scheme_id, revision_id))

    conn.commit()
    entry_rows = []
    field_rows = []
    for index in range(entry_count):
        scheme_id, revision_id = schemes[index % scheme_count]
        entry_id = f"synthetic-entry-{index:05d}"
        entry_rows.append((
            entry_id, profile_id, "Synthetic title", f"SYN-{index:05d}", "2026-01-01",
            "Synthetic seller", "10.00", "Synthetic buyer", "", "", "[]", "partial",
            "pass", "", "benchmark", scheme_id, revision_id, "", "", timestamp, timestamp,
        ))
        field_rows.extend((
            (entry_id, "paid_amount", "10.00", "10.00", 0),
            (entry_id, "actual_item_name", "Synthetic item", "Synthetic item", 0),
            (entry_id, "notes", "", "", 0),
        ))
    conn.executemany(
        """INSERT INTO entries(id,profile_id,title,invoice_no,invoice_date,seller,total,buyer_name,
           buyer_tax_id,category,tags,status,check_status,check_message,source,scheme_id,
           scheme_revision_id,title_profile_id,status_engine_version,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        entry_rows,
    )
    conn.executemany(
        "INSERT INTO entry_fields(entry_id,field,origin,current,modified) VALUES(?,?,?,?,?)",
        field_rows,
    )

    attachment_rows = []
    for index in range(entry_count):
        entry_id = f"synthetic-entry-{index:05d}"
        for attachment_index in range(attachments_per_entry):
            role_id, attachment_type = ATTACHMENT_TYPES[attachment_index % len(ATTACHMENT_TYPES)]
            attachment_rows.append((
                f"synthetic-attachment-{index:05d}-{attachment_index:02d}",
                entry_id, attachment_type, f"synthetic-{attachment_index}.bin", "", "0" * 64,
                role_id, None, timestamp,
            ))
    conn.executemany(
        """INSERT INTO attachments(id,entry_id,type,original_name,stored_path,sha256,
           role_id,role_definition_revision_id,added_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        attachment_rows,
    )
    conn.commit()
    return db, EntryRepo(db), [scheme_id for scheme_id, _ in schemes]


def _set_listing_mode(db: Database, adapter_bound: bool) -> None:
    if adapter_bound:
        db.conn.execute(
            """UPDATE entries SET scheme_id=(
                   SELECT s.id FROM schemes s WHERE s.id='synthetic-scheme-' ||
                       CAST(CAST(substr(entries.id, length('synthetic-entry-') + 1) AS INTEGER) % ? AS TEXT)
               ), scheme_revision_id=(
                   SELECT s.current_revision_id FROM schemes s WHERE s.id='synthetic-scheme-' ||
                       CAST(CAST(substr(entries.id, length('synthetic-entry-') + 1) AS INTEGER) % ? AS TEXT)
               )""",
            (len(db.conn.execute("SELECT id FROM schemes WHERE id LIKE 'synthetic-scheme-%'").fetchall()),) * 2,
        )
    else:
        # The legacy path is represented by pre-adapter entries with no frozen
        # scheme/revision binding; material status then uses legacy meta settings.
        db.conn.execute("UPDATE entries SET scheme_id=NULL,scheme_revision_id=NULL")
    db.conn.commit()


def _timed_list(repo: EntryRepo) -> float:
    # Clear only the derived policy cache so each sample includes evaluation of
    # the five distinct scheme revisions and their twenty rules.
    repo._policy_cache.clear()
    started = time.perf_counter()
    rows = repo.list()
    elapsed = time.perf_counter() - started
    if len(rows) == 0:
        raise RuntimeError("synthetic list unexpectedly returned no entries")
    return elapsed


def _median_seconds(db: Database, repo: EntryRepo, adapter_bound: bool, repeats: int) -> float:
    _set_listing_mode(db, adapter_bound)
    _timed_list(repo)  # warm interpreter, SQLite pages and adapter-definition cache
    samples = []
    for _ in range(repeats):
        samples.append(_timed_list(repo))
    return statistics.median(samples)


def run_benchmark(
    entry_count: int = 10_000,
    attachments_per_entry: int = 5,
    scheme_count: int = 5,
    rules_per_scheme: int = 20,
    repeats: int = 5,
) -> dict:
    if repeats < 1:
        raise ValueError("repeats must be at least one")
    db, repo, _scheme_ids = create_dataset(
        entry_count=entry_count,
        attachments_per_entry=attachments_per_entry,
        scheme_count=scheme_count,
        rules_per_scheme=rules_per_scheme,
    )
    try:
        adapter_samples = []
        legacy_samples = []
        # Alternate order to reduce systematic thermal/load bias.
        for adapter_bound in (True, False):
            _set_listing_mode(db, adapter_bound)
            _timed_list(repo)
        for index in range(repeats):
            modes = (True, False) if index % 2 == 0 else (False, True)
            for adapter_bound in modes:
                _set_listing_mode(db, adapter_bound)
                elapsed = _timed_list(repo)
                (adapter_samples if adapter_bound else legacy_samples).append(elapsed)
        adapter_median = statistics.median(adapter_samples)
        legacy_median = statistics.median(legacy_samples)
        return {
            "metadata": {
                "tidoc_version": TIDOC_VERSION,
                "python_version": platform.python_version(),
                "sqlite_version": sqlite3.sqlite_version,
                "operating_system": platform.system(),
                "os_release": platform.release(),
                "machine": platform.machine(),
                "processor": platform.processor() or platform.machine(),
                "cpu_count": __import__("os").cpu_count(),
            },
            "workload": {
                "entries": entry_count,
                "attachments_per_entry": attachments_per_entry,
                "attachment_metadata_rows": entry_count * attachments_per_entry,
                "schemes": scheme_count,
                "rules_per_scheme": rules_per_scheme,
                "total_rules": scheme_count * rules_per_scheme,
                "repeats_per_mode": repeats,
                "database": "SQLite :memory:",
                "input_data": "synthetic; no invoice files or private paths",
            },
            "measurements": {
                "adapter_entry_repo_list_median_ms": round(adapter_median * 1000, 3),
                "legacy_material_entry_repo_list_median_ms": round(legacy_median * 1000, 3),
                "adapter_to_legacy_ratio": round(adapter_median / legacy_median, 4) if legacy_median else None,
                "adapter_samples_ms": [round(v * 1000, 3) for v in adapter_samples],
                "legacy_samples_ms": [round(v * 1000, 3) for v in legacy_samples],
            },
            "limitations": [
                "Single-machine microbenchmark with an in-memory SQLite database; it does not model disk I/O or a user's real attachment store.",
                "Synthetic rules and attachment metadata measure list hydration and policy overhead, not invoice parsing or document rendering.",
                "The legacy comparison uses unbound entries and the current legacy material-requirements implementation.",
            ],
        }
    finally:
        db.conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", type=int, default=10_000)
    parser.add_argument("--attachments-per-entry", type=int, default=5)
    parser.add_argument("--schemes", type=int, default=5)
    parser.add_argument("--rules-per-scheme", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    result = run_benchmark(args.entries, args.attachments_per_entry, args.schemes,
                           args.rules_per_scheme, args.repeats)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

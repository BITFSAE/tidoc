from __future__ import annotations

import tempfile
import unittest

from scripts.benchmark_adapters import create_dataset
from tidoc.adapters.service import AdapterService
from tidoc.db.database import Database
from tidoc.db.entries import EntryRepo


class AdapterPerformanceTests(unittest.TestCase):
    def test_list_query_count_is_batched_not_per_entry(self):
        counts = []
        for entries in (100, 800):
            db, repo, _schemes = create_dataset(
                entry_count=entries,
                attachments_per_entry=5,
                scheme_count=5,
                rules_per_scheme=20,
            )
            statements = []
            try:
                repo._revision_cache.clear()
                repo._policy_cache.clear()
                db.conn.set_trace_callback(statements.append)
                result = repo.list()
                self.assertEqual(len(result), entries)
                selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
                counts.append(len(selects))
            finally:
                db.conn.set_trace_callback(None)
                db.conn.close()
        # Both workloads fit within EntryRepo.QUERY_BATCH_SIZE: list prefetches
        # related rows in a fixed set of bulk statements instead of N per-card queries.
        self.assertLessEqual(counts[1], counts[0] + 1, counts)

    def test_default_scheme_switch_does_not_scan_entries(self):
        with tempfile.TemporaryDirectory(prefix="tidoc-default-switch-") as temp_root:
            db = Database(":memory:")
            try:
                service = AdapterService(db, temp_root)
                service.bootstrap()
                schemes = service.list_schemes()
                self.assertGreaterEqual(len(schemes), 2)
                target = next(s for s in schemes if not s["is_default"])
                statements = []
                db.conn.set_trace_callback(statements.append)
                service.set_default_scheme(target["id"])
                db.conn.set_trace_callback(None)
                entry_queries = [
                    sql for sql in statements
                    if "entries" in sql.lower()
                ]
                self.assertEqual([], entry_queries, statements)
            finally:
                db.conn.set_trace_callback(None)
                db.conn.close()


if __name__ == "__main__":
    unittest.main()

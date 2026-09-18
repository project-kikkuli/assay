from __future__ import annotations

import unittest

from experiments.cell import performance


class PerformanceEvidenceTests(unittest.TestCase):
    def test_plan_tree_keeps_cost_work_and_buffers(self) -> None:
        tree = performance.plan_tree(
            {
                "Node Type": "Index Scan",
                "Index Name": "cell_perf_owner_created_id",
                "Total Cost": 12.5,
                "Actual Rows": 20,
                "Actual Loops": 1,
                "Shared Hit Blocks": 4,
                "Shared Read Blocks": 1,
                "Rows Removed by Filter": 7,
                "Plans": [],
            }
        )
        self.assertEqual(tree["node_type"], "Index Scan")
        self.assertEqual(tree["total_cost"], 12.5)
        self.assertEqual(tree["shared_read_blocks"], 1)
        self.assertEqual(tree["rows_removed_by_filter"], 7)

    def test_report_requires_both_scales_and_exact_evidence(self) -> None:
        report = {
            "status": "passed",
            "provenance": {"source_unchanged": True},
            "scales": {},
        }
        with self.assertRaises(performance.PerformanceError):
            performance.validate_report(report)

    def test_page_query_keeps_owner_scope_and_order(self) -> None:
        self.assertIn("owner_id = public.cell_kernel_current_user_id()", performance.PAGE_SQL)
        self.assertIn("ORDER BY created_at DESC, id DESC", performance.PAGE_SQL)
        self.assertIn("owner_id = public.cell_kernel_current_user_id()", performance.COUNT_SQL)

    def test_source_inventory_is_local_and_explicit(self) -> None:
        self.assertEqual(
            performance.SOURCE_FILES,
            ("performance.py", "test_performance.py", "kernel.py", "fixture.py"),
        )
        self.assertNotIn("/tmp/", str(performance.OUTPUT))


if __name__ == "__main__":
    unittest.main()

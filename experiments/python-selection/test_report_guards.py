#!/usr/bin/env python3
"""Stdlib-only regression checks for report validity guards."""

import json
import tempfile
import unittest
from pathlib import Path

from run_selection import classify_selection, read_timing, summary_counts


class ReportGuardTests(unittest.TestCase):
    def test_counts_parse_all_pytest_kinds(self) -> None:
        self.assertEqual(
            summary_counts("2 passed, 1 failed, 3 skipped, 4 xfailed, 5 xpassed, 6 deselected"),
            {"passed": 2, "failed": 1, "skipped": 3, "xfailed": 4, "xpassed": 5, "deselected": 6},
        )

    def test_freshness_rejects_missing_and_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timing.json"
            self.assertEqual(read_timing(path)["validity"], "unknown_missing_timing")
            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(read_timing(path)["validity"], "unknown_malformed_timing")
            path.write_text(json.dumps({"selected_tests": 1, "exit_status": 0,
                                        "phases_s": {"process_observed": 0.1}}), encoding="utf-8")
            self.assertEqual(read_timing(path)["validity"], "fresh_timing")

    def test_malformed_shapes_and_nonfinite_timings_are_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timing.json"
            for value in ([], None, {"selected_tests": True},
                          {"selected_tests": 1, "exit_status": 0,
                           "phases_s": {"process_observed": float("nan")}}):
                path.write_text(json.dumps(value))
                self.assertEqual(read_timing(path)["validity"], "unknown_malformed_timing")

    def test_exit_and_count_contradictions_are_unknown(self):
        for count, exit_status in ((2, 0), (1, 5)):
            observed = classify_selection({"status": "passed"},
                {"validity": "fresh_timing", "selected_tests": count, "exit_status": exit_status},
                {"passed": 1})
            self.assertEqual(observed["status"], "unknown_contradictory_counts")

    def test_zero_and_empty_reports_are_not_green(self) -> None:
        base = {"status": "passed"}
        zero = classify_selection(dict(base), {"validity": "fresh_timing", "selected_tests": 0}, {"deselected": 4})
        empty = classify_selection(dict(base), {"validity": "fresh_timing", "selected_tests": 1}, {})
        self.assertEqual(zero["status"], "unknown_zero_tests")
        self.assertEqual(empty["status"], "unknown_empty_counts")


if __name__ == "__main__":
    unittest.main()

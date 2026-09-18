#!/usr/bin/env python3
"""DB-free regression tests for benchmark report admission guards."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from measure import (
    all_measured_passed,
    classify_status,
    normalize_output,
    pytest_summary_validity,
)
from profile_vitest import load_fresh_positive_report


class ReportGuardTests(unittest.TestCase):
    def test_python_empty_summary_is_unknown(self) -> None:
        self.assertEqual(pytest_summary_validity({}), "unknown_empty_summary")
        self.assertEqual(classify_status(0, False, "unknown_empty_summary"), "unknown")

    def test_empty_measurement_list_cannot_be_green(self) -> None:
        self.assertFalse(all_measured_passed([]))

    def test_timeout_bytes_are_normalized(self) -> None:
        self.assertEqual(normalize_output(b"partial\xff"), "partial\ufffd")

    def test_vitest_missing_stale_empty_and_malformed_are_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            missing, validity = load_fresh_positive_report(report, time.time_ns())
            self.assertIsNone(missing)
            self.assertEqual(validity, "unknown_missing_or_stale_report")

            report.write_text(json.dumps({"numTotalTests": 0}), encoding="utf-8")
            empty, validity = load_fresh_positive_report(report, 0)
            self.assertIsNone(empty)
            self.assertEqual(validity, "unknown_empty_report")

            report.write_text("not-json", encoding="utf-8")
            malformed, validity = load_fresh_positive_report(report, 0)
            self.assertIsNone(malformed)
            self.assertEqual(validity, "unknown_malformed_report")

    def test_vitest_positive_report_is_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            report.write_text(json.dumps({"numTotalTests": 1}), encoding="utf-8")
            data, validity = load_fresh_positive_report(report, 0)
            self.assertEqual(data, {"numTotalTests": 1})
            self.assertEqual(validity, "fresh_positive_report")


if __name__ == "__main__":
    unittest.main()

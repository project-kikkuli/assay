from pathlib import Path
import unittest
from unittest.mock import patch

from assay.demo import demo


def report(cached=False, status="verified", key="original"):
    return {"status": status, "duration_ms": 1, "tasks": [{"id": "x", "cached": cached, "key": key}], "warnings": []}


class DemoClaimsTests(unittest.TestCase):
    def reports(self):
        return [report(), *[report(cached=True) for _ in range(5)], report(), report(cached=True), report(key="changed")]

    def test_changed_fixture_must_actually_pass(self):
        responses = self.reports()
        responses[-1]["status"] = "rejected"
        with patch("assay.demo.run", side_effect=responses), patch("assay.demo.measure_queries", return_value=[]):
            result = demo(Path.cwd())
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["invalidation"]["changed_status"], "rejected")

    def test_warm_reexecution_is_counted_and_not_claimed_as_reuse(self):
        responses = self.reports()
        responses[1]["tasks"][0]["cached"] = False
        with patch("assay.demo.run", side_effect=responses), patch("assay.demo.measure_queries", return_value=[]):
            result = demo(Path.cwd())
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["benchmarks"][0]["work"]["executed_commands_per_sample"], [1, 0, 0, 0, 0])

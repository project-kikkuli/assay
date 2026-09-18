import unittest
from assay.inspect import explain


class InspectTests(unittest.TestCase):
    def test_overview_and_task_drilldown(self):
        report = {"name": "Example", "status": "verified", "tasks": [{"id": "contract", "status": "verified", "cached": True, "reason": "same inputs"}]}
        self.assertIn("--task contract", explain(report))
        self.assertIn("same inputs", explain(report, task="contract"))
        with self.assertRaises(ValueError):
            explain(report, task="missing")

    def test_scenario_displays_changed_fields_and_violation(self):
        report = {"status": "rejected", "scenarios": [{"name": "stale lease", "invariant": "fencing", "source": "queue.py", "events": [{"step": 2, "action": "complete", "before": [{"id": 1, "status": "leased"}], "after": [{"id": 1, "status": "done"}], "result": True, "expected": False}], "failures": [{"rule": "fencing"}]}]}
        text = explain(report, scenario=0)
        self.assertIn("'leased' -> 'done'", text)
        self.assertIn("Expected: False; observed: True", text)
        self.assertIn("VIOLATION", text)
        with self.assertRaises(ValueError):
            explain(report, scenario=-1)

    def test_benchmark_is_raw_measured_evidence(self):
        report = {"status": "verified", "benchmarks": [{"query": "SELECT id", "samples_ms": [1, 2]}]}
        self.assertIn("SELECT id", explain(report, benchmark=0))
        with self.assertRaises(ValueError):
            explain(report, benchmark=1)

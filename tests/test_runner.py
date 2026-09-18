import json
from pathlib import Path
import tempfile
import unittest

from assay.runner import audit, run


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = self.root / "assay.json"
        (self.root / "check.py").write_text("print('checked')\n")
        self.task = {"id": "unit", "command": ["{python}", "check.py"], "inputs": ["check.py"]}
        self.write()

    def write(self, tasks=None):
        self.manifest.write_text(json.dumps({"version": 1, "tasks": tasks if tasks is not None else [self.task]}))

    def execute(self, **kwargs):
        return run(self.manifest, **kwargs)

    def test_cold_and_warm(self):
        cold, warm = self.execute(), self.execute()
        self.assertEqual(cold["status"], "verified")
        self.assertFalse(cold["tasks"][0]["cached"])
        self.assertTrue(warm["tasks"][0]["cached"])
        self.assertEqual(cold["candidate"], warm["candidate"])
        self.assertEqual(cold["tasks"][0]["key"], warm["tasks"][0]["key"])

    def test_source_mutation_invalidates(self):
        first = self.execute()
        (self.root / "check.py").write_text("print('different')\n")
        changed = self.execute()
        self.assertFalse(changed["tasks"][0]["cached"])
        self.assertNotEqual(first["tasks"][0]["key"], changed["tasks"][0]["key"])

    def test_policy_mutation_invalidates(self):
        self.execute()
        self.task["description"] = "Changed obligation"
        self.write()
        self.assertFalse(self.execute()["tasks"][0]["cached"])

    def test_environment_mutation_invalidates(self):
        self.execute()
        self.task["env"] = {"FEATURE": "on"}
        self.write()
        self.assertFalse(self.execute()["tasks"][0]["cached"])

    def test_glob_inventory_addition_invalidates(self):
        self.task["inputs"] = ["*.py"]
        self.write()
        self.execute()
        (self.root / "new.py").write_text("NEW=1\n")
        self.assertFalse(self.execute()["tasks"][0]["cached"])

    def test_explicit_scope_reuses_unaffected_task(self):
        self.execute()
        (self.root / "unrelated.txt").write_text("No declared dependency")
        self.assertTrue(self.execute()["tasks"][0]["cached"])

    def test_conservative_scope_invalidates_unrelated_changes(self):
        self.task.pop("inputs")
        self.write()
        self.execute()
        (self.root / "unrelated.txt").write_text("Conservatively relevant")
        self.assertFalse(self.execute()["tasks"][0]["cached"])

    def test_deleted_input_unresolved(self):
        self.execute()
        (self.root / "check.py").unlink()
        result = self.execute()
        self.assertEqual(result["status"], "unresolved")
        self.assertFalse(result["tasks"][0]["cached"])

    def test_failure_not_cached(self):
        (self.root / "check.py").write_text("raise AssertionError('bad invariant')\n")
        self.assertEqual(self.execute()["status"], "rejected")
        again = self.execute()
        self.assertEqual(again["status"], "rejected")
        self.assertFalse(again["tasks"][0]["cached"])

    def test_dependency_failure_never_becomes_successful_skip(self):
        self.task["command"] = ["{python}", "-c", "raise AssertionError('no')"]
        dependent = {"id": "rollup", "command": ["{python}", "-c", "print('must not run')"], "needs": ["unit"]}
        self.write([self.task, dependent])
        result = self.execute()
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["tasks"][1]["status"], "unresolved")
        self.assertEqual(result["tasks"][1]["stdout"], "")

    def test_dependency_key_invalidates_consumer(self):
        (self.root / "consumer.py").write_text("print('consumer')\n")
        dependent = {"id": "consumer", "command": ["{python}", "consumer.py"], "inputs": ["consumer.py"], "needs": ["unit"]}
        self.write([self.task, dependent])
        self.execute()
        self.assertTrue(self.execute()["tasks"][1]["cached"])
        (self.root / "check.py").write_text("print('upstream changed')\n")
        self.assertFalse(self.execute()["tasks"][1]["cached"])

    def test_cache_corruption_is_miss(self):
        first = self.execute()
        path = self.root / ".assay/cache" / (first["tasks"][0]["key"] + ".json")
        path.write_text("{truncated")
        second = self.execute()
        self.assertEqual(second["status"], "verified")
        self.assertFalse(second["tasks"][0]["cached"])

    def test_cache_digest_mismatch_is_miss(self):
        first = self.execute()
        path = self.root / ".assay/cache" / (first["tasks"][0]["key"] + ".json")
        value = json.loads(path.read_text())
        value["evidence"]["stdout"] = "tampered"
        path.write_text(json.dumps(value))
        self.assertFalse(self.execute()["tasks"][0]["cached"])

    def test_timeout_unresolved(self):
        self.task.update(command=["{python}", "-c", "while True: pass"], timeout_s=0.03)
        self.write()
        self.assertEqual(self.execute()["status"], "unresolved")

    def test_missing_executable_unresolved(self):
        self.task["command"] = ["assay-command-that-does-not-exist"]
        self.write()
        self.assertEqual(self.execute()["status"], "unresolved")

    def test_source_drift_unresolved(self):
        (self.root / "check.py").write_text("from pathlib import Path\nPath('check.py').write_text('print(123)')\n")
        self.assertEqual(self.execute()["status"], "unresolved")

    def test_symlink_input_unresolved(self):
        (self.root / "alias.py").symlink_to(self.root / "check.py")
        self.task["inputs"] = ["alias.py"]
        self.write()
        self.assertEqual(self.execute()["status"], "unresolved")

    def test_final_integrity_error_cannot_leave_verified_verdict(self):
        (self.root / "check.py").write_text("from pathlib import Path\nPath('new-link').symlink_to('check.py')\n")
        result = self.execute()
        self.assertEqual(result["tasks"][0]["status"], "verified")
        self.assertEqual(result["status"], "unresolved")

    def test_executable_permission_change_invalidates_success(self):
        script = self.root / "check.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        self.task.update(command=["./check.sh"], inputs=["check.sh"])
        self.write()
        self.assertEqual(self.execute()["status"], "verified")
        self.assertTrue(self.execute()["tasks"][0]["cached"])
        script.chmod(0o644)
        result = self.execute()
        self.assertEqual(result["status"], "unresolved")
        self.assertFalse(result["tasks"][0]["cached"])

    def test_manifest_validation(self):
        cases = [
            {"version": 1, "tasks": []},
            {"version": 1, "tasks": [self.task, self.task]},
            {"version": 1, "tasks": [{**self.task, "inputs": []}]},
            {"version": 1, "tasks": [{**self.task, "inputs": ["../outside"]}]},
            {"version": 1, "tasks": [{**self.task, "needs": ["missing"]}]},
            {"version": 1, "tasks": [{**self.task, "needs": ["unit"]}]},
            {"version": 1, "tasks": [{**self.task, "timeout_s": -1}]},
            {"version": 1, "tasks": [{**self.task, "timeout_s": True}]},
            {"version": 1, "tasks": [{**self.task, "needs": "unit"}]},
            {"version": 1, "tasks": [{**self.task, "dependson": ["missing"]}]},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.manifest.write_text(json.dumps(case))
                self.assertEqual(self.execute()["status"], "unresolved")

    def test_audit_retains_failure_followed_by_pass(self):
        (self.root / "check.py").write_text("from pathlib import Path\np=Path('.assay/toggle')\np.parent.mkdir(exist_ok=True)\nif not p.exists():\n p.write_text('seen')\n raise AssertionError('first failure')\n")
        result = audit(self.manifest, repeats=2)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["unstable_tasks"], {"unit": ["rejected", "verified"]})
        self.assertFalse(any(t["cached"] for r in result["repetitions"] for t in r["tasks"]))


if __name__ == "__main__":
    unittest.main()

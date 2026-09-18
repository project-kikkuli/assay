import copy
import unittest

from run import CHALLENGES, challenges_valid
from behavior import OBLIGATIONS, complete


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.records = [{"kind": "challenge", "candidate": name, "status": status,
                         "cleanup": "removed", "candidate_unchanged": True,
                         "runtime": {"container_controls": {"passed": True},
                                     "cleanup": {"status": "removed"}}}
                        for name, status in CHALLENGES.items()]
        for record in self.records:
            if record["candidate"] in {"attack_hang.py", "attack_output_flood.py"}:
                record["runtime"]["failure_kind"] = ("deadline_exceeded" if record["candidate"] == "attack_hang.py"
                                                        else "output_cap_exceeded")

    def test_exact_expected_results(self):
        self.assertTrue(challenges_valid(self.records))

    def test_empty_or_duplicate_business_evidence_rejects(self):
        records = [{"obligation": name, "passed": True} for name in OBLIGATIONS]
        self.assertTrue(complete(records))
        self.assertFalse(complete([]))
        self.assertFalse(complete(records[:-1]))
        self.assertFalse(complete(records[:-1] + [records[0]]))

    def test_missing_duplicate_unknown_and_failed_cleanup_reject(self):
        self.assertFalse(challenges_valid([]))
        self.assertFalse(challenges_valid(self.records[:-1]))
        self.assertFalse(challenges_valid(self.records[:-1] + [self.records[0]]))
        for field, value in (("status", "unknown"), ("cleanup", "failed"),
                             ("candidate_unchanged", False), ("runtime", {})):
            records = copy.deepcopy(self.records)
            records[0][field] = value
            self.assertFalse(challenges_valid(records))

    def test_resource_probe_requires_its_actual_cause(self):
        records = copy.deepcopy(self.records)
        next(r for r in records if r["candidate"] == "attack_hang.py")["runtime"]["failure_kind"] = "process_ended"
        self.assertFalse(challenges_valid(records))


if __name__ == "__main__":
    unittest.main()

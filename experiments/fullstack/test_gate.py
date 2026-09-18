import copy
import unittest

from gate import REVISION, decide


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            {"check": "upstream", "status": "passed", "tests": {"passed": 62}},
            {"check": "oracle", "status": "passed", "tests": {"passed": 7}},
            {"check": "browser", "status": "passed", "tests": {"expected": 62, "actual_passed": 62}},
            {"check": "build", "status": "passed"},
            {"check": "typecheck", "status": "passed"},
            *({"check": name, "status": "passed"} for name in ("mypy", "ty", "ruff", "python_format")),
        ]
        self.boundary = {"source_sha256": "source", "revision": REVISION, "source_unchanged": True, "oracle_unchanged": True, "claims": {
            "invalid_title_is_client_error": True, "invalid_title_does_not_modify_storage": True,
            "invalid_offset_is_client_error": True, "all_owned_items_reachable": True,
            "stored_title_does_not_execute": True,
        }}

    def test_positive_evidence(self):
        self.assertEqual(decide(self.records, self.boundary, 0, "source"), "supported")

    def test_missing_checks_and_empty_claims_are_unknown(self):
        self.assertEqual(decide(self.records[:-1], self.boundary, 0, "source"), "unresolved")
        self.assertEqual(decide(self.records, {**self.boundary, "claims": {}}, 0, "source"), "unresolved")

    def test_missing_tests_cannot_pass(self):
        records = copy.deepcopy(self.records)
        records[1]["tests"]["passed"] = 0
        self.assertEqual(decide(records, self.boundary, 0, "source"), "unresolved")

    def test_negative_claim_and_timeout(self):
        self.boundary["claims"]["all_owned_items_reachable"] = False
        self.assertEqual(decide(self.records, self.boundary, 1, "source"), "rejected")
        self.assertEqual(decide(self.records, self.boundary, None, "source"), "unresolved")

    def test_policy_drift(self):
        self.boundary["oracle_unchanged"] = False
        self.assertEqual(decide(self.records, self.boundary, 0, "source"), "unresolved")

    def test_counts_cannot_contradict_a_pass(self):
        for index, key in ((0, "failed"), (1, "errors"), (2, "unexpected")):
            with self.subTest(index=index):
                records = copy.deepcopy(self.records)
                records[index]["tests"][key] = 1
                self.assertEqual(decide(records, self.boundary, 0, "source"), "unresolved")

    def test_expected_count_alone_is_not_execution(self):
        self.records[2]["tests"]["actual_passed"] = 0
        self.assertEqual(decide(self.records, self.boundary, 0, "source"), "unresolved")

    def test_duplicate_and_different_candidate_evidence(self):
        self.assertEqual(decide(self.records + [self.records[0]], self.boundary, 0, "source"), "unresolved")
        self.assertEqual(decide(self.records, self.boundary, 0, "other-source"), "unresolved")

import copy
import unittest

from qualify import composition_valid


class QualificationTests(unittest.TestCase):
    def setUp(self):
        browser = {"status": "passed", "inventory": {"item_tests": 9, "auth_setup_tests": 1, "total": 10},
                   "cases": [{"status": "passed"} for _ in range(10)],
                   "cross_tenant": {"passed": True}, "admin_positive_control": {"passed": True}}
        self.report = {"status": "passed", "source_unchanged": True,
                       "records": [{"mode": mode, "status": "passed", "cleanup": "removed",
                                    "browser": copy.deepcopy(browser)} for mode in ("baseline", "cell")]}
        self.report["records"][1]["shutdown"] = {
            "status": "passed", "actor": {"calls": [{"request_id": "x"}],
              "cleanup": {"status": "removed"}, "container_controls": {"passed": True}, "source_identity_stable": True}}

    def test_complete_composition(self):
        self.assertTrue(composition_valid(self.report))

    def test_browser_success_without_actor_execution_rejects(self):
        self.report["records"][1]["shutdown"]["actor"]["calls"] = []
        self.assertFalse(composition_valid(self.report))

    def test_missing_case_cleanup_or_identity_rejects(self):
        for alter in (
            lambda r: r["records"].pop(),
            lambda r: r["records"][0]["browser"]["cases"].pop(),
            lambda r: r["records"][1].update(cleanup="failed"),
            lambda r: r.update(source_unchanged=False),
        ):
            report = copy.deepcopy(self.report)
            alter(report)
            self.assertFalse(composition_valid(report))

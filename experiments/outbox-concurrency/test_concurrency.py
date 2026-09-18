import signal
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import COMMITTED_STATE, PENDING_STATE, validate_report


def blocking_case(source):
    events = [
        {"child": 0, "phase": "connected", "backend_pid": 101},
        {"child": 1, "phase": "connected", "backend_pid": 102},
        {"child": 0, "phase": "waiting_for_row_lock"},
        {"child": 1, "phase": "waiting_for_row_lock"},
    ]
    if source == "consumer.ts":
        events.extend(
            [
                {"child": 0, "phase": "precheck_done", "seen": False},
                {"child": 1, "phase": "precheck_done", "seen": False},
            ]
        )
        outcomes = [
            {"outcome": "committed"},
            {"outcome": "rolled_back_unique_violation"},
        ]
    else:
        outcomes = [{"outcome": "committed"}, {"outcome": "dedup_skipped"}]
    return {
        "source": source,
        "pre_release_events": events,
        "post_release_events": (
            [
                {"child": 0, "phase": "postlock_check", "seen": False},
                {"child": 1, "phase": "postlock_check", "seen": True},
            ]
            if source == "repaired_consumer.ts"
            else []
        ),
        "coordinator_pid": 100,
        "child_backend_pids": [101, 102],
        "blocked": {"101": [100], "102": [101], "100": []},
        "blocking_graph": {"101": [100], "102": [101], "100": []},
        "outcomes": outcomes,
        "state": COMMITTED_STATE,
    }


def crash_case(barrier):
    return {
        "barrier": barrier,
        "barrier_events": [{"child": 0, "phase": barrier}],
        "killed_returncode": -signal.SIGKILL,
        "state_before_kill": PENDING_STATE,
        "state_after_kill": PENDING_STATE
        if barrier == "before_commit"
        else COMMITTED_STATE,
        "state_before_recovery": PENDING_STATE
        if barrier == "before_commit"
        else COMMITTED_STATE,
        "recovery_events": [{"child": 0, "phase": "outcome"}],
        "recovery_outcome": {
            "outcome": "committed" if barrier == "before_commit" else "dedup_skipped"
        },
        "final_state": COMMITTED_STATE,
    }


def valid_report():
    return {
        "source_unchanged": True,
        "concurrency": [
            blocking_case("consumer.ts"),
            blocking_case("repaired_consumer.ts"),
        ],
        "crash": [crash_case("before_commit"), crash_case("after_commit")],
    }


class ConcurrencyValidationTests(unittest.TestCase):
    def test_valid_report_passes(self):
        validate_report(valid_report())

    def test_empty_concurrency_is_not_green(self):
        report = valid_report()
        report["concurrency"] = []
        with self.assertRaises(AssertionError):
            validate_report(report)

    def test_empty_crash_is_not_green(self):
        report = valid_report()
        report["crash"] = []
        with self.assertRaises(AssertionError):
            validate_report(report)

    def test_two_commits_are_not_green(self):
        report = valid_report()
        report["concurrency"][0]["outcomes"][1]["outcome"] = "committed"
        with self.assertRaises(AssertionError):
            validate_report(report)

    def test_repaired_state_is_checked(self):
        report = valid_report()
        report["concurrency"][1]["state"] = PENDING_STATE
        with self.assertRaises(AssertionError):
            validate_report(report)

    def test_empty_blocker_graph_is_not_green(self):
        report = valid_report()
        report["concurrency"][0]["blocking_graph"] = {}
        report["concurrency"][0]["blocked"] = {}
        with self.assertRaises(AssertionError):
            validate_report(report)

    def test_unrelated_blocker_is_not_green(self):
        report = valid_report()
        unrelated = {"101": [999], "102": [999], "999": []}
        report["concurrency"][0]["blocked"] = unrelated
        report["concurrency"][0]["blocking_graph"] = unrelated
        with self.assertRaises(AssertionError):
            validate_report(report)

    def test_missing_barrier_event_is_not_green(self):
        report = valid_report()
        report["crash"][0]["barrier_events"] = []
        with self.assertRaises(AssertionError):
            validate_report(report)

    def test_before_commit_kill_cannot_have_persisted_effect(self):
        report = valid_report()
        report["crash"][0]["state_after_kill"] = COMMITTED_STATE
        with self.assertRaises(AssertionError):
            validate_report(report)

    def test_wrong_kill_signal_is_not_green(self):
        report = valid_report()
        report["crash"][1]["killed_returncode"] = -signal.SIGTERM
        with self.assertRaises(AssertionError):
            validate_report(report)

    def test_source_identity_must_be_true(self):
        report = valid_report()
        report["source_unchanged"] = False
        with self.assertRaises(AssertionError):
            validate_report(report)


if __name__ == "__main__":
    unittest.main()

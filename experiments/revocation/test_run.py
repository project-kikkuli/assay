import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.revocation import run


class RevocationEvidenceTests(unittest.TestCase):
    def test_cli_honors_repeat_output(self):
        with (
            patch("sys.argv", ["run.py", "--output", "replay.json"]),
            patch.object(run, "run_repeats", return_value={"status": "passed"}) as repeats,
            patch("builtins.print"),
        ):
            self.assertEqual(run.main(), 0)
        repeats.assert_called_once_with(Path("replay.json"))

    def variant_evidence(self, variant: str) -> dict:
        run_id = "abc123"
        new_secret = f"rotated-{variant}-{run_id}"
        leaked = variant == "baseline"
        return {
            "variant": variant,
            "positive_read": {
                "rows": [
                    {"secret_id": 1, "secret": f"initial-secret-{variant}"}
                ]
            },
            "transaction_facts": {
                "admin": {"isolation": "read committed", "pid": 10},
                "reader": {"isolation": "read committed", "pid": 11},
            },
            "barrier": {
                "verified": True,
                "mechanism": "pg_stat_activity + pg_blocking_pids",
                "admin_pid": 10,
                "reader_pid": 11,
                "state": {
                    "pid": 11,
                    "state": "active",
                    "wait_event_type": "Lock",
                    "blocking_pids": [10],
                    "query": run.target_query(variant, run_id),
                },
            },
            "membership_update_boundary": {
                "membership_lock_column_update_granted": True,
                "secret_lock_column_update_granted": True,
                "protected_updates_denied": True,
            },
            "reader_rows_after_commit": (
                [{"secret_id": 1, "secret": new_secret}] if leaked else []
            ),
            "new_secret": new_secret,
            "expected_leak": leaked,
            "leaked_rotated_secret": leaked,
            "contention_ms": {
                "to_verified_barrier": 1.0,
                "reader_query": 2.0,
                "whole_schedule": 3.0,
            },
        }

    def test_require_is_explicit_and_optimization_safe(self):
        with self.assertRaises(run.EvidenceError):
            run.require(False, "expected failure")

    def test_resource_names_are_unique_and_identifier_safe(self):
        names = run.resource_names()
        self.assertEqual(len(set(names.values())), len(names))
        for value in names.values():
            self.assertRegex(value, r"^[a-z][a-z0-9_]+$")
            self.assertEqual(run.ident(value), f'"{value}"')

    def test_invalid_variant_evidence_is_rejected(self):
        with self.assertRaises(run.EvidenceError):
            run.validate_variant({"variant": "baseline"}, "baseline")

    def test_rows_not_boolean_marker_drive_outcome(self):
        evidence = self.variant_evidence("select_for_share")
        evidence["leaked_rotated_secret"] = True
        with self.assertRaises(run.EvidenceError):
            run.validate_variant(evidence, "select_for_share", "abc123")

    def test_barrier_and_contention_require_exact_evidence(self):
        evidence = self.variant_evidence("baseline")
        evidence["barrier"]["state"]["query"] = "SELECT membership"
        with self.assertRaises(run.EvidenceError):
            run.validate_variant(evidence, "baseline", "abc123")

        evidence = self.variant_evidence("baseline")
        evidence["contention_ms"] = {"whole_schedule": 0.0}
        with self.assertRaises(run.EvidenceError):
            run.validate_variant(evidence, "baseline", "abc123")


if __name__ == "__main__":
    unittest.main()

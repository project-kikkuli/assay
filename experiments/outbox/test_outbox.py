import json
import hashlib
import unittest
from pathlib import Path

from checks import require
from model import (
    MAX_OUTSTANDING,
    State,
    actions,
    finish,
    outstanding_deliveries,
    search,
    violation,
)


class ModelRegressionTests(unittest.TestCase):
    def test_actor_state_is_preserved_and_both_actors_are_enabled(self):
        state = State(
            accepted=frozenset({"cmd-1"}),
            outbox=(("cmd-1", "pending"),),
            producer=("cmd-2", "begun"),
            consumer=("cmd-1", "begun"),
        )
        self.assertIn("accept:cmd-2@commit", actions(state, "normal"))
        self.assertIn("consumer:cmd-1@atomic_commit", actions(state, "normal"))
        self.assertEqual(
            finish(state, "accept:cmd-2@commit", "normal").consumer, state.consumer
        )
        self.assertEqual(
            finish(state, "consumer:cmd-1@atomic_commit", "normal").producer,
            state.producer,
        )

    def test_actor_crash_and_restart_preserve_the_other_actor(self):
        state = State(producer=("cmd-1", "begun"), consumer=("cmd-2", "begun"))
        crashed = finish(state, "crash:producer:cmd-1@before_commit", "normal")
        self.assertIsNone(crashed.producer)
        self.assertEqual(crashed.consumer, state.consumer)
        restarted = finish(crashed, "restart:producer", "normal")
        self.assertEqual(restarted.crashed, frozenset())
        self.assertEqual(restarted.consumer, state.consumer)

    def test_invariants_are_checked_at_committed_boundaries(self):
        state = finish(State(), "begin_accept:cmd-1", "non-atomic-outbox")
        state = finish(state, "accept:cmd-1@command_commit", "non-atomic-outbox")
        self.assertIn("no durable outbox event", violation(state, "non-atomic-outbox"))

        state = finish(State(), "begin_accept:cmd-1", "processed-before-ledger")
        state = finish(state, "accept:cmd-1@commit", "processed-before-ledger")
        state = finish(state, "enqueue:cmd-1", "processed-before-ledger")
        state = finish(state, "deliver:cmd-1@index=0", "processed-before-ledger")
        state = finish(
            state, "consumer:cmd-1@mark_processed_commit", "processed-before-ledger"
        )
        self.assertIn(
            "without ledger effect", violation(state, "processed-before-ledger")
        )

        ghost = State(processed=frozenset({"evt:ghost"}), ledger=(("ghost", 1),))
        self.assertIn("no durable outbox event", violation(ghost, "normal"))

    def test_crash_returns_held_delivery_without_exceeding_total_bound(self):
        state = State(
            accepted=frozenset({"cmd-1", "cmd-2"}),
            outbox=(("cmd-1", "pending"), ("cmd-2", "pending")),
            queue=("cmd-2", "cmd-2"),
            consumer=("cmd-1", "begun"),
        )
        self.assertNotIn("enqueue:cmd-1", actions(state, "normal"))
        crashed = finish(state, "crash:consumer:cmd-1@before_consumer_commit", "normal")
        self.assertEqual(len(crashed.queue), 3)
        self.assertEqual(outstanding_deliveries(crashed), MAX_OUTSTANDING)

    def test_explorer_reports_reachable_queue_and_outstanding_maxima(self):
        result = search("normal")
        self.assertFalse(result["found_counterexample"])
        self.assertEqual(result["max_queue"], MAX_OUTSTANDING)
        self.assertEqual(result["max_outstanding"], MAX_OUTSTANDING)


class EvidenceRegressionTests(unittest.TestCase):
    def test_require_helper_rejects_false_inputs(self):
        with self.assertRaises(RuntimeError):
            require(False, "intentional test failure")

    def test_normal_driver_requires_commit_then_deduplication(self):
        actual = json.loads(
            (Path(__file__).parent / "results" / "actual-replay.json").read_text()
        )
        normal = actual["actual_variants"]["normal"]
        self.assertEqual(normal["first_consume"]["stdout"]["outcome"], "committed")
        self.assertEqual(normal["second_consume"]["stdout"]["outcome"], "dedup_skipped")
        self.assertEqual(
            normal["invariant"]["outbox"],
            [{"command_id": "cmd-1", "status": "published"}],
        )
        self.assertEqual(normal["invariant"]["processed"], ["evt:cmd-1"])
        self.assertEqual(
            normal["invariant"]["ledger_counts"], [{"command_id": "cmd-1", "count": 1}]
        )

    def test_ghost_and_broken_retry_evidence(self):
        actual = json.loads(
            (Path(__file__).parent / "results" / "actual-replay.json").read_text()
        )
        ghost = actual["actual_variants"]["ghost-delivery"]
        self.assertEqual(
            ghost["ghost_consume"]["stdout"]["outcome"], "rejected_missing_outbox"
        )
        replay = actual["actual_replay"]
        self.assertEqual(
            replay["retry_after_marked_consume"]["stdout"]["outcome"], "dedup_skipped"
        )
        self.assertEqual(replay["invariant"]["ledger_counts"], [])

    def test_run_record_has_source_hashes_and_tool_versions(self):
        actual = json.loads(
            (Path(__file__).parent / "results" / "actual-replay.json").read_text()
        )
        provenance = actual["provenance"]
        self.assertEqual(
            actual["model_version"], "outbox-model-v2-total-outstanding-bound"
        )
        root = Path(__file__).parent
        expected_sources = {
            "model.py",
            "run_actual.py",
            "checks.py",
            "consumer.ts",
            "schema.sql",
            "package.json",
            "package-lock.json",
            "test_outbox.py",
        }
        self.assertEqual(set(provenance["source_sha256"]), expected_sources)
        for name, digest in provenance["source_sha256"].items():
            self.assertEqual(
                digest, hashlib.sha256((root / name).read_bytes()).hexdigest()
            )
        self.assertTrue(provenance["tools"]["python"])
        self.assertTrue(provenance["tools"]["pg_dependency"])

    def test_discovered_trace_has_real_sql_ts_replay(self):
        root = Path(__file__).parent / "results"
        model = json.loads((root / "model-processed-before-ledger.json").read_text())
        actual = json.loads((root / "actual-replay.json").read_text())["actual_replay"]
        self.assertEqual(actual["model_trace"], model["trace"])
        self.assertFalse(actual["invariant"]["holds"])
        self.assertEqual(actual["invariant"]["processed_without_ledger"], ["evt:cmd-1"])
        self.assertEqual(
            actual["first_consume"]["stdout"]["outcome"],
            "simulated_crash_after_processed",
        )


if __name__ == "__main__":
    unittest.main()

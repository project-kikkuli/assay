import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import lab


def capture(function, *args, **kwargs):
    output = io.StringIO()
    with redirect_stdout(output):
        result = function(*args, **kwargs)
    return output.getvalue(), result


class ReaderTests(unittest.TestCase):
    def test_default_is_recorded_and_not_assertion_narrative(self):
        output, code = capture(lab.main, [])
        self.assertEqual(code, 0)
        self.assertIn("RECORDED JSON", output)
        self.assertNotIn("zero actual-assertion failures", output)
        for target in lab.SHOW_CHOICES:
            self.assertIn(f"show {target}", output)

    def test_default_summary_uses_recorded_outcomes_and_costs(self):
        output, _ = capture(lab.main, [])
        gate, gate_error = lab.record("gate")
        policy, policy_error = lab.record("policy")
        scale, scale_error = lab.record("scale")
        self.assertEqual(gate_error, "")
        self.assertEqual(policy_error, "")
        self.assertEqual(scale_error, "")
        self.assertIsNotNone(gate)
        self.assertIsNotNone(policy)
        self.assertIsNotNone(scale)

        gate_times = [run["seconds"] for run in gate["runs"]]
        symbolic_count = sum(
            row["semantic"] in {"proved", "counterexample"} for row in policy["results"]
        )
        direct_all = scale["direct_vitest_profile"]["all"]
        self.assertIn(f"{len(gate['runs'])} runs", output)
        self.assertIn(lab.numeric_range(gate_times, "s"), output)
        self.assertIn(f"{symbolic_count} symbolic checks", output)
        self.assertIn(f"{direct_all['tests']} tests", output)
        self.assertIn(lab.seconds(direct_all["duration_s"]), output)

    def test_gate_holdout_pointer_parses_recorded_claim(self):
        holdout = (
            "The added seven-check oracle catches **3/12**, not the 7/8 "
            "seen on its initial author-shared challenges."
        )
        with mock.patch.object(lab, "read_text", return_value=(holdout, "")):
            output, _ = capture(lab.show_gate)
        self.assertIn("matrix oracle catches 3/12", output)
        self.assertIn("initial author-shared 7/8", output)
        self.assertIn("challenge set, not holdout proof", output)

    def test_gate_view_includes_latest_checks_sources_and_hash_prefixes(self):
        output, _ = capture(lab.show_gate)
        self.assertIn("latest run: actual counts per check", output)
        self.assertIn("latest run: slowest stage wall times", output)
        self.assertIn("gate=experiments/fullstack/gate.py", output)
        self.assertIn("lifecycle=experiments/fullstack/lifecycle_tests.py", output)
        gate, error = lab.record("gate")
        self.assertEqual(error, "")
        latest = gate["runs"][-1]
        self.assertIn(latest["built_artifact_sha256"][:12], output)
        self.assertIn(latest["boundary"]["source_sha256"][:12], output)

    def test_gate_view_keeps_hosted_negative_result_visible(self):
        output, _ = capture(lab.show_gate)
        self.assertIn("hosted / 4 browser workers", output)
        self.assertIn("hosted / 2 browser workers", output)
        self.assertIn("No transport fix is established", output)
        self.assertIn("rejected", output)

    def test_policy_view_summarizes_repeated_checks(self):
        data = {
            "admission": "accept",
            "assertions_passed": True,
            "results": [
                {
                    "name": "warm_candidate_implies",
                    "semantic": "proved",
                    "elapsed_ms": float(index),
                }
                for index in range(1, 31)
            ],
            "failures": [],
        }
        with mock.patch.object(lab, "record", return_value=(data, "")):
            output, _ = capture(lab.show_policy)
        self.assertIn("count", output)
        self.assertIn("30", output)
        self.assertIn("15.500ms", output)
        self.assertIn("1.000–30.000ms", output)
        self.assertEqual(output.count("warm_candidate_implies"), 1)

    def test_scale_view_includes_direct_and_selected_python_records(self):
        output, _ = capture(lab.show_scale)
        data, error = lab.record("scale")
        self.assertEqual(error, "")
        direct = data["direct_vitest_profile"]["all"]
        selected = data["selected_python_validation"]
        self.assertIn(f"tests={direct['tests']}", output)
        self.assertIn(f"cost={lab.precise_seconds(direct['duration_s'])}", output)
        self.assertIn("15/15", selected["after"])
        self.assertIn(
            f"cost={lab.precise_seconds(selected['selected_test_s'])}", output
        )

    def test_missing_malformed_and_wrong_type_are_not_green(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.json").write_text("{bad")
            (root / "list.json").write_text("[]")
            with mock.patch.object(lab, "ROOT", root):
                missing, missing_error = lab.load_object(Path("missing.json"))
                malformed, malformed_error = lab.load_object(Path("bad.json"))
                wrong_type, type_error = lab.load_object(Path("list.json"))
        self.assertIsNone(missing)
        self.assertEqual(missing_error, "unavailable")
        self.assertIsNone(malformed)
        self.assertIn("malformed", malformed_error)
        self.assertIsNone(wrong_type)
        self.assertIn("expected JSON object", type_error)

    def test_contradictory_record_is_unsupported(self):
        data = {
            "verdict": "supported",
            "runs": [{"verdict": "rejected", "seconds": 1.0}],
            "source_unchanged": True,
            "policy_unchanged": True,
        }
        self.assertIn("non-supported", lab.validate_gate(data))

    def test_gate_aggregate_accepts_history_and_drift_failure(self):
        history = {
            "verdict": "rejected",
            "runs": [
                {"verdict": "rejected", "seconds": 1.0},
                {"verdict": "supported", "seconds": 1.0},
            ],
        }
        drift = {
            "verdict": "unresolved",
            "runs": [{"verdict": "supported", "seconds": 1.0}],
            "source_unchanged": False,
            "policy_unchanged": True,
        }
        self.assertEqual(lab.validate_gate(history), "")
        self.assertEqual(lab.validate_gate(drift), "")
        for verdict in ("unknown", "failed"):
            self.assertEqual(
                lab.validate_gate(
                    {"verdict": verdict, "runs": [{"verdict": verdict, "seconds": 1.0}]}
                ),
                "",
            )

    def test_nested_schema_error_is_unsupported(self):
        with mock.patch.object(
            lab,
            "read_json",
            return_value=({"claims": {}, "observations": {"null_update": "bad"}}, ""),
        ):
            data, error = lab.load_object(Path("nested.json"), lab.validate_boundaries)
        self.assertIsNone(data)
        self.assertIn("unsupported", error)

    def test_actual_outbox_data_treats_ghost_as_blocked_positive(self):
        data, error = lab.record("outbox_actual")
        self.assertEqual(error, "")
        self.assertIsNotNone(data)
        output, _ = capture(lab.show_outbox)
        self.assertIn("ghost delivery", output)
        self.assertIn("expected blocked positive", output)
        self.assertNotIn("broken ghost", output)

    def test_isolation_role_guc_rows_are_observations(self):
        output, _ = capture(lab.show_isolation)
        self.assertIn("app GUC spoof case", output)
        self.assertIn("guc_spoof_visible", output)
        self.assertIn("classification", output)
        self.assertIn("equivalent", output)
        self.assertNotIn("same-role GUC spoof is a counterexample", output)


class ReplayTests(unittest.TestCase):
    def test_prepare_forwards_only_explicit_options_with_current_interpreter(self):
        self.assertEqual(
            lab.prepare_argv(),
            [__import__("sys").executable, "tools/prepare_lab.py"],
        )
        self.assertEqual(
            lab.prepare_argv("/subject", services_external=True, stop=True),
            [
                __import__("sys").executable,
                "tools/prepare_lab.py",
                "--subject",
                "/subject",
                "--services-external",
                "--stop",
            ],
        )

    def test_prepare_returns_child_status_without_running_by_default(self):
        with mock.patch.object(
            subprocess, "run", return_value=mock.Mock(returncode=9)
        ) as run:
            code = lab.run_prepare()
        self.assertEqual(code, 9)
        run.assert_called_once_with(
            [__import__("sys").executable, "tools/prepare_lab.py"],
            cwd=lab.ROOT,
            check=False,
        )

    def test_replay_uses_correct_sources_and_pinned_db_python(self):
        self.assertIn(
            "experiments/compatibility/verifier.py", lab.replay_argv("compatibility")
        )
        self.assertIn("experiments/outbox/run_actual.py", lab.replay_argv("outbox"))
        self.assertEqual(lab.replay_argv("legacy"), ["./demo"])
        self.assertIn("3.14.6", lab.replay_argv("isolation"))
        self.assertIn("psycopg[binary]==3.3.4", lab.replay_argv("isolation"))
        self.assertEqual(lab.replay_argv("policy")[0], __import__("sys").executable)
        for target in ("isolation", "outbox", "compatibility"):
            self.assertNotIn("ASSAY_PG_DSN", lab.ALLOWED_ENV[target])

    def test_default_subject_uses_state_and_ignores_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root, subject = Path(directory), Path(directory) / "subject"
            subject.mkdir()
            state = root / "state.json"
            state.write_text(json.dumps({"ready": True, "subject": str(subject)}))
            with (
                mock.patch.object(lab, "ROOT", root),
                mock.patch.object(lab, "STATE_PATH", Path("state.json")),
            ):
                resolved, error = lab.replay_subject("default")
        self.assertEqual(error, "")
        self.assertEqual(resolved, str(subject.resolve()))

    def test_state_not_ready_is_rejected_but_explicit_subject_is_trusted(self):
        with tempfile.TemporaryDirectory() as directory:
            root, subject = Path(directory), Path(directory) / "subject"
            subject.mkdir()
            state = root / "state.json"
            state.write_text(json.dumps({"ready": False, "subject": str(subject)}))
            with (
                mock.patch.object(lab, "ROOT", root),
                mock.patch.object(lab, "STATE_PATH", Path("state.json")),
            ):
                resolved, error = lab.replay_subject("default")
                explicit, explicit_error = lab.replay_subject(str(subject))
        self.assertIsNone(resolved)
        self.assertIn("ready=true", error)
        self.assertEqual(explicit_error, "")
        self.assertEqual(explicit, str(subject.resolve()))

    def test_dry_run_does_not_execute_and_real_status_is_returned(self):
        with mock.patch.object(subprocess, "run") as run:
            output, code = capture(lab.run_replay, "policy", dry_run=True)
        run.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn("experiments/cedar/run.py", output)
        with mock.patch.object(
            subprocess, "run", return_value=mock.Mock(returncode=7)
        ) as run:
            code = lab.run_replay("policy")
        run.assert_called_once()
        self.assertEqual(code, 7)

    def test_all_replay_dry_run_arguments_are_available(self):
        for target in lab.REPLAY_CHOICES:
            argv = lab.replay_argv(target, subject="/tmp/subject")
            self.assertTrue(argv)
            expected = "./demo" if target == "legacy" else str(lab.SOURCES[target])
            self.assertIn(expected, argv)
        for target in ("gate", "isolation", "outbox", "compatibility"):
            self.assertIn(
                "--no-project", lab.replay_argv(target, subject="/tmp/subject")
            )

    def test_gate_without_state_is_not_silent(self):
        with mock.patch.object(lab, "read_json", return_value=(None, "unavailable")):
            self.assertEqual(lab.run_replay("gate", subject="default", dry_run=True), 2)


if __name__ == "__main__":
    unittest.main()

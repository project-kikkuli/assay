import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import run


class ConfinementUnitTests(unittest.TestCase):
    def _record(self, *, network="none", mounts=None):
        return {
            "Config": {"User": "65532:65532"},
            "HostConfig": {
                "NetworkMode": network,
                "ReadonlyRootfs": True,
                "PidsLimit": 64,
                "Memory": 128 * 1024 * 1024,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "NanoCpus": 1_000_000_000,
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=16m"},
            },
            "Mounts": mounts
            or [
                {
                    "Type": "bind",
                    "Source": "/owned/candidate",
                    "Destination": "/candidate",
                    "RW": False,
                },
                {"Type": "tmpfs", "Destination": "/tmp", "RW": True},
            ],
        }

    def _outcomes(self):
        outcomes = [
            {
                "name": "healthy",
                "accepted": True,
                "container_controls_passed": True,
                "host_cache_unchanged": True,
                "canary_visible": False,
                "container_cleanup": "removed",
            }
        ]
        for name in run.EXPECTED_ATTACKS:
            outcomes.append(
                {
                    "name": name,
                    "accepted": False,
                    "container_controls_passed": True,
                    "host_cache_unchanged": True,
                    "canary_visible": False,
                    "container_cleanup": "removed",
                    "reason": {
                        "hang": "candidate_unknown_timeout",
                        "silent": "rejected_invalid_json_or_empty",
                        "output_flood": "rejected_output_limit",
                    }.get(name, "rejected_wrong_business_response"),
                }
            )
        outcomes.append(
            {
                "name": "exposed_canary_control",
                "accepted": False,
                "container_controls_passed": True,
                "host_cache_unchanged": True,
                "canary_visible": True,
                "container_cleanup": "removed",
                "boundary_control_passed": False,
            }
        )
        outcomes.append(
            {
                "name": "exposed_network_control",
                "accepted": False,
                "container_controls_passed": True,
                "host_cache_unchanged": True,
                "canary_visible": False,
                "container_cleanup": "removed",
                "boundary_control_passed": False,
                "network_server_connected": True,
            }
        )
        return outcomes

    def test_digest_is_immutable_for_supported_platforms(self):
        for digest in run.IMAGE_DIGESTS.values():
            self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")

    def test_host_protocol_is_not_candidate_attestation(self):
        self.assertEqual(run.EXPECTED_RESULT, 5)
        self.assertNotEqual({"accepted": True}, {"result": run.EXPECTED_RESULT})

    def test_request_is_json_protocol(self):
        encoded = json.dumps(run.EXPECTED_REQUEST)
        self.assertEqual(json.loads(encoded)["protocol"], "confinement.v1")

    def test_candidate_sources_are_files_not_imported(self):
        for path in (Path(run.ROOT) / "candidates").glob("*.py"):
            self.assertTrue(path.read_bytes())

    def test_response_requires_exact_integer_and_rejects_duplicate_keys(self):
        response, reason = run.parse_response('{"result": 5.0}')
        self.assertEqual(reason, None)
        self.assertFalse(run.exact_business_response(response))
        response, reason = run.parse_response('{"result": 5, "result": 5}')
        self.assertIsNone(response)
        self.assertEqual(reason, "rejected_invalid_json_or_empty")

    def test_container_controls_observe_wrong_network_and_mount(self):
        record = self._record(
            network="default",
            mounts=[
                {
                    "Type": "bind",
                    "Source": "/owned/candidate",
                    "Destination": "/candidate",
                    "RW": True,
                },
                {
                    "Type": "bind",
                    "Source": "/owned/extra",
                    "Destination": "/extra",
                    "RW": False,
                },
            ],
        )
        checked = run.check_container_controls(record, Path("/owned/candidate"), False)
        self.assertFalse(checked["passed"])
        self.assertIn("network_not_none", checked["failures"])
        self.assertIn("unexpected_host_mounts", checked["failures"])

    def test_positive_network_control_requires_observed_owned_server(self):
        record = self._record(network="owned-network")
        checked = run.check_container_controls(
            record, Path("/owned/candidate"), False, "owned-network"
        )
        self.assertTrue(checked["passed"])

    def test_unknown_network_cleanup_is_not_success(self):
        failed_remove = subprocess.CompletedProcess([], 1, "", "permission denied")
        still_present = subprocess.CompletedProcess([], 0, '{"Name":"owned"}', "")
        with mock.patch("run.subprocess.run", side_effect=[failed_remove, still_present]):
            self.assertTrue(run.cleanup_network("a" * 64).startswith("unknown"))

    def test_admission_rejects_unknown_owned_resource_cleanup(self):
        checked = run.check_admission(
            self._outcomes(),
            resource_cleanup={"network_server": "removed", "network": "unknown_cleanup"},
        )
        self.assertFalse(checked["passed"])
        self.assertIn("cleanup:network:unknown_cleanup", checked["failures"])

    def test_fresh_non_green_evidence_overwrites_stale_green(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "evidence.json"
            run.write_evidence({"admission": "green"}, path)
            run.write_evidence(run.fresh_non_green("runner_exception"), path)
            self.assertEqual(json.loads(path.read_text())["admission"], "not_green")

    def test_unexpected_attack_timeout_fails_classification(self):
        outcomes = self._outcomes()
        next(item for item in outcomes if item["name"] == "wrong_result")["reason"] = (
            "candidate_unknown_timeout"
        )
        checked = run.check_admission(outcomes)
        self.assertFalse(checked["passed"])
        self.assertIn("classification:wrong_result", checked["failures"])

    def test_pipe_eof_allows_process_to_exit_without_kill(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "print('healthy')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        collected = run.collect_process(process, time.monotonic() + 1)
        process.stdout.close()
        process.stderr.close()
        self.assertFalse(collected["timed_out"])
        self.assertEqual(collected["returncode"], 0)
        self.assertEqual(collected["stdout"], b"healthy\n")

    def test_admission_requires_canary_and_cache_invariants(self):
        outcomes = self._outcomes()
        outcomes_by_name = {item["name"]: item for item in outcomes}
        outcomes_by_name["read_canary"]["canary_visible"] = True
        outcomes_by_name["edit_oracle"]["host_cache_unchanged"] = False
        checked = run.check_admission(list(outcomes_by_name.values()))
        self.assertFalse(checked["passed"])
        self.assertIn("control:read_canary", checked["failures"])
        self.assertIn("control:edit_oracle", checked["failures"])

    def test_admission_rejects_missing_or_empty_cases(self):
        self.assertFalse(run.check_admission([])["passed"])
        outcomes = self._outcomes()
        outcomes = [item for item in outcomes if item["name"] != "healthy"]
        checked = run.check_admission(outcomes)
        self.assertFalse(checked["passed"])
        self.assertIn("missing_healthy", checked["failures"])
        self.assertIn("missing_healthy", run.check_admission([])["failures"])

    def test_expected_timeout_passes_only_after_cleanup(self):
        outcomes = self._outcomes()
        self.assertTrue(run.check_admission(outcomes)["passed"])
        for item in outcomes:
            if item["name"] == "hang":
                item["container_cleanup"] = "unknown_cleanup_failure"
        checked = run.check_admission(outcomes)
        self.assertFalse(checked["passed"])
        self.assertIn("control:hang", checked["failures"])


if __name__ == "__main__":
    unittest.main()

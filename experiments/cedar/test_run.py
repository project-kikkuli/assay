"""Admission controls without Docker, networking, or third-party packages."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run


class AdmissionTests(unittest.TestCase):
    def test_timeout_bytes_are_safe_and_never_proof(self):
        self.assertEqual(
            run.normalize_text(b"pset1 implies pset2\n"), "pset1 implies pset2"
        )
        self.assertEqual(
            run.semantic_result("smt_timeout", "unknown", "pset1 implies pset2"),
            "unknown",
        )

    def test_only_exact_unambiguous_summary_is_proof(self):
        self.assertEqual(
            run.semantic_result("smt_good", "ok", "noise\npset1 implies pset2\n"),
            "proved",
        )
        self.assertEqual(
            run.semantic_result("smt_bad", "ok", "pset1 does not imply pset2"),
            "counterexample",
        )
        for output in (
            "pset1 implies pset2 extra",
            "",
            "pset1 implies pset2\npset1 does not imply pset2",
        ):
            with self.subTest(output=output):
                self.assertEqual(
                    run.semantic_result("smt_ambiguous", "ok", output), "unknown"
                )

    def test_runner_uses_resolved_image_reference(self):
        identity = "sha256:" + "a" * 64
        command = run.docker_args([], image_ref=identity, cidfile=Path("/scratch/cid"))
        self.assertEqual(command[-1], identity)
        self.assertIn("none", command)
        self.assertIn("--read-only", command)

    def test_cleanup_refuses_unowned_or_invalid_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            cidfile = Path(directory) / "id"
            cidfile.write_text("--all")
            with patch.object(run.subprocess, "run") as execute:
                with self.assertRaisesRegex(RuntimeError, "invalid Docker"):
                    run.cleanup_container(cidfile)
                execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()

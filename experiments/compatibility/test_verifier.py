"""DB-free unit tests for verifier helpers.

Run with: python -m unittest test_verifier -v
"""

import json
import unittest
from pathlib import Path
from unittest import mock

import verifier


class HelperTests(unittest.TestCase):
    def test_schema_sections_present(self):
        sections = verifier.schema_sections(verifier.SCHEMA_SQL)
        for name in verifier.REQUIRED_SECTIONS:
            self.assertIn(name, sections)

    def test_unknown_classifier(self):
        self.assertTrue(verifier.is_unknown_result({"ok": False, "unknown": True}))
        self.assertFalse(verifier.is_unknown_result({"ok": False, "sqlstate": "42703"}))
        self.assertFalse(verifier.is_unknown_result({"ok": True}))

    def test_summarize_error(self):
        self.assertEqual(
            verifier.summarize_error({"ok": False, "message": "rowcount 0"}),
            "rowcount 0",
        )
        self.assertEqual(
            verifier.summarize_error({"ok": False, "sqlstate": "42703"}), "42703"
        )
        self.assertTrue(
            verifier.summarize_error(
                {"ok": False, "unknown": True, "message": "x"}
            ).startswith("UNKNOWN")
        )

    def test_missing_client_is_unknown_not_green(self):
        with mock.patch.object(verifier, "DIST_JS", Path("/nonexistent-assay-dist.js")):
            result = verifier.node_call("postgres", "read", "some-id")
        self.assertTrue(verifier.is_unknown_result(result))
        self.assertFalse(result.get("ok"))

    def test_malformed_json_is_unknown_not_green(self):
        fake_process = mock.Mock(returncode=0)
        with mock.patch.object(verifier, "DIST_JS", Path(__file__)):
            with mock.patch.object(
                verifier,
                "run_process",
                return_value=(fake_process, ("not json {{{", ""), False),
            ):
                result = verifier.node_call("postgres", "read", "some-id")
        self.assertTrue(verifier.is_unknown_result(result))
        self.assertFalse(result.get("ok"))

    def test_expected_matrix_shape(self):
        self.assertEqual(
            verifier.EXPECTED_MATRIX["expand"], (True, False, False, False)
        )
        self.assertEqual(
            verifier.EXPECTED_GATE,
            {phase: (phase == "bridge") for phase in verifier.EXPECTED_MATRIX},
        )


if __name__ == "__main__":
    unittest.main()

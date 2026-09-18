import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import lifecycle


class LifecycleEvidenceTests(unittest.TestCase):
    def test_setup_failure_replaces_old_success(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            output.write_text('{"expected_outcomes_observed": true}')
            with patch("sys.argv", ["lifecycle.py", "--subject", directory, "--output", str(output)]), \
                    patch.object(lifecycle, "replay", side_effect=RuntimeError("setup failed")):
                self.assertEqual(lifecycle.main(), 1)
            report = json.loads(output.read_text())
            self.assertFalse(report["expected_outcomes_observed"])
            self.assertEqual(report["status"], "unresolved")
            self.assertEqual(report["matrix"], [])

    def test_interruption_also_invalidates_old_success(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            output.write_text('{"expected_outcomes_observed": true}')
            with patch("sys.argv", ["lifecycle.py", "--subject", directory, "--output", str(output)]), \
                    patch.object(lifecycle, "replay", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    lifecycle.main()
            self.assertFalse(json.loads(output.read_text())["expected_outcomes_observed"])


if __name__ == "__main__":
    unittest.main()

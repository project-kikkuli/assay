import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run


class HarnessTests(unittest.TestCase):
    def test_missing_report_is_not_evidence(self):
        self.assertEqual(run.junit_counts(Path("nonexistent-junit.xml"))["passed"], 0)

    def test_junit_counts_failures_and_errors_not_just_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "junit.xml"
            path.write_text('<testsuites><testsuite><testcase/><testcase><failure/></testcase><testcase><error/></testcase><testcase><skipped/></testcase></testsuite></testsuites>')
            self.assertEqual(run.junit_counts(path), {"collected": 4, "passed": 1, "failed": 1, "errors": 1, "skipped": 1})

    def test_child_environment_does_not_inherit_credentials(self):
        with patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "private", "SENTRY_DSN": "private", "DATABASE_URL": "private"}):
            env = run.clean_env()
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("SENTRY_DSN", env)
        self.assertNotIn("DATABASE_URL", env)

    def test_remote_database_refused_before_connect(self):
        with patch.object(run.psycopg, "connect") as connect:
            with self.assertRaises(ValueError), run.database("postgresql://example.com/production"):
                self.fail("must not connect")
            connect.assert_not_called()

    def test_dsn_escaping(self):
        self.assertEqual(run.database_url("host=localhost dbname=fixture user=u password='a@b:c'"), "postgresql://u:a%40b%3Ac@localhost:5432/fixture")

    def test_output_scrubs_machine_paths(self):
        value = run.sanitize('/Users/somebody/private/file /private/tmp/scratch/file', Path('/tmp/source'))
        self.assertNotIn("somebody", value)
        self.assertNotIn("/private/tmp", value)
        self.assertIn("$SCRATCH/file", value)


if __name__ == "__main__":
    unittest.main()

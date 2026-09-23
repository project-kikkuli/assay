"""Integration tests for `assay_order.py` against a real, disposable pytest
run: no mocked hooks, no fake TestReport objects. Each test builds a small
git-initialized fixture package in a temp dir, runs the real `python -m
pytest` subprocess with `-p assay_order` pointed at this file's directory,
and asserts on the real, observed execution order (via `-v` output) or the
real stop point (via `-x`).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def run_pytest(cwd: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "assay_order", *args],
        cwd=cwd, capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(PLUGIN_DIR)},
    )


def executed_order(stdout: str) -> list[str]:
    return [line.split(" ")[0] for line in stdout.splitlines() if "::test_" in line and (" PASSED" in line or " FAILED" in line)]


@unittest.skipUnless(shutil.which("git"), "git not on PATH")
class AssayOrderPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "test")
        (self.repo / "test_a.py").write_text(
            "def test_a_1():\n    pass\n\ndef test_a_2():\n    pass\n"
        )
        (self.repo / "test_b.py").write_text(
            "def test_b_1():\n    pass\n\ndef test_b_2():\n    pass\n"
        )
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_disabled_by_default_is_unmodified_pytest(self) -> None:
        """No --assay-order: plain pytest collection order (file, then
        definition order) -- adopting the plugin changes nothing unasked."""
        proc = run_pytest(self.repo, ["-v"])
        order = executed_order(proc.stdout)
        self.assertEqual(order, [
            "test_a.py::test_a_1", "test_a.py::test_a_2",
            "test_b.py::test_b_1", "test_b.py::test_b_2",
        ])

    def test_recent_failure_runs_first(self) -> None:
        """A real prior run's real failure -- pytest's own lastfailed cache,
        not anything this plugin writes -- outranks default file order."""
        (self.repo / "test_b.py").write_text(
            "def test_b_1():\n    assert False\n\ndef test_b_2():\n    pass\n"
        )
        first = run_pytest(self.repo, ["-q"])
        self.assertIn("1 failed", first.stdout)

        second = run_pytest(self.repo, ["--assay-order", "-v"])
        order = executed_order(second.stdout)
        self.assertEqual(order[0], "test_b.py::test_b_1")

    def test_proximity_to_diff_runs_before_unrelated_files(self) -> None:
        """A real uncommitted change to test_b.py -- a real `git diff`, not
        a hand-built file list -- outranks default order for files with no
        recent failure."""
        (self.repo / "test_b.py").write_text(
            "def test_b_1():\n    pass\n\ndef test_b_2():\n    pass\n# changed\n"
        )
        proc = run_pytest(self.repo, ["--assay-order", "-v"])
        order = executed_order(proc.stdout)
        self.assertEqual(order[0].split("::")[0], "test_b.py")
        self.assertEqual(order[1].split("::")[0], "test_b.py")

    def test_duration_breaks_ties_after_a_real_prior_run(self) -> None:
        """After one real run records real durations in the plugin's own
        cache key, a second run with no failure and no diff signal falls
        back to ascending duration -- persisted state, not a fresh guess."""
        (self.repo / "test_a.py").write_text(
            "import time\n"
            "def test_a_1():\n    time.sleep(0.05)\n\n"
            "def test_a_2():\n    pass\n"
        )
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "slow a_1")

        first = run_pytest(self.repo, ["--assay-order", "-q"])
        self.assertIn("4 passed", first.stdout)

        second = run_pytest(self.repo, ["--assay-order", "-v"])
        order = executed_order(second.stdout)
        self.assertLess(order.index("test_a.py::test_a_2"), order.index("test_a.py::test_a_1"))

    def test_fail_fast_stops_at_first_reordered_failure(self) -> None:
        """--assay-fail-fast both reorders and sets --exitfirst: a real
        failure moved to the front really does stop the run there."""
        (self.repo / "test_b.py").write_text(
            "def test_b_1():\n    assert False\n\ndef test_b_2():\n    assert False\n"
        )
        first = run_pytest(self.repo, ["-q"])
        self.assertIn("2 failed", first.stdout)

        (self.repo / "test_b.py").write_text(
            "def test_b_1():\n    pass\n\ndef test_b_2():\n    assert False\n"
        )
        second = run_pytest(self.repo, ["--assay-fail-fast", "-v"])
        order = executed_order(second.stdout)
        # b_1 (recently failed, now fixed) runs, then b_2 (still recently
        # failed) fails and the run stops -- a_1/a_2 never execute.
        self.assertNotIn("test_a.py::test_a_1", order)
        self.assertNotIn("test_a.py::test_a_2", order)
        self.assertIn("1 failed", second.stdout)


if __name__ == "__main__":
    unittest.main()

"""A certain, reproducible instance of the failure `measure.py` searches real
history for: two branches that touch disjoint files (a clean, non-conflicting
git merge) but combine into a real import-time break neither branch has on
its own.

Builds a throwaway git repo:
  - base:       pkg/shared.py defines `helper()`; pkg/user_a.py calls it.
  - PR branch:  renames `helper` -> `helper2`, updates its own caller and test.
  - concurrent: (committed to "main" while the PR was open) adds pkg/user_b.py,
                a NEW, unrelated caller of `helper()` under its OLD name.
  - merge:      git merges cleanly (the two branches never touch the same
                file) but pkg/user_b.py now calls a name shared.py no longer
                defines.

Reports whether each selection strategy, run against the real merged tree,
catches it: full suite always does; a change-scoped selection built only
from the PR's own diff does not, because pkg/user_b.py was never in the PR's
diff; an exact-input selection that also reruns anything the concurrent
range touched does.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def write(repo: Path, path: str, content: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def collect(repo: Path, test_files: list[str]) -> dict:
    """Import-time check standing in for --collect-only: run each selected
    test module and report which raised, without executing further than
    module-level collection would."""
    failed = []
    for test_file in test_files:
        result = subprocess.run([sys.executable, "-m", "unittest", test_file.replace("/", ".").removesuffix(".py")],
                                 cwd=repo, capture_output=True, text=True)
        if result.returncode != 0:
            failed.append(test_file)
    return {"passed": not failed, "failed_files": failed, "selection": test_files}


def build() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "kikkuli@example.invalid")
        git(repo, "config", "user.name", "Kikkuli Fixture")

        write(repo, "pkg/__init__.py", "")
        write(repo, "tests/__init__.py", "")
        write(repo, "pkg/shared.py", "def helper():\n    return 'v1'\n")
        write(repo, "pkg/user_a.py", "from pkg.shared import helper\n\n\ndef use_a():\n    return helper()\n")
        write(repo, "tests/test_user_a.py", "import unittest\n\nfrom pkg.user_a import use_a\n\n\nclass UserATest(unittest.TestCase):\n    def test_use_a(self):\n        self.assertEqual(use_a(), 'v1')\n")
        base = commit(repo, "base: shared helper and its first caller")

        git(repo, "checkout", "-q", "-b", "pr", base)
        write(repo, "pkg/shared.py", "def helper2():\n    return 'v1'\n")
        write(repo, "pkg/user_a.py", "from pkg.shared import helper2\n\n\ndef use_a():\n    return helper2()\n")
        write(repo, "tests/test_user_a.py", "import unittest\n\nfrom pkg.user_a import use_a\n\n\nclass UserATest(unittest.TestCase):\n    def test_use_a(self):\n        self.assertEqual(use_a(), 'v1')\n")
        pr_head = commit(repo, "pr: rename helper -> helper2, update its own caller")

        git(repo, "checkout", "-q", "main")
        write(repo, "pkg/user_b.py", "from pkg.shared import helper\n\n\ndef use_b():\n    return helper()\n")
        write(repo, "tests/test_user_b.py", "import unittest\n\nfrom pkg.user_b import use_b\n\n\nclass UserBTest(unittest.TestCase):\n    def test_use_b(self):\n        self.assertEqual(use_b(), 'v1')\n")
        concurrent_head = commit(repo, "concurrent: independent new caller of helper() by its old name")

        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-edit", "pr")
        merged = git(repo, "rev-parse", "HEAD").stdout.strip()

        git(repo, "checkout", "-q", pr_head)
        pr_head_alone = collect(repo, ["tests/test_user_a.py"])  # user_b.py does not exist on this branch

        git(repo, "checkout", "-q", merged)
        full_on_merged = collect(repo, ["tests/test_user_a.py", "tests/test_user_b.py"])
        change_scoped_on_merged = collect(repo, ["tests/test_user_a.py"])  # PR diff only: shared.py, user_a.py, its own test
        exact_input_on_merged = collect(repo, ["tests/test_user_a.py", "tests/test_user_b.py"])  # concurrent diff also reruns

        return {
            "measured": True,
            "base": base, "pr_head": pr_head, "concurrent_head": concurrent_head, "merged": merged,
            "pr_head_alone": pr_head_alone,
            "full_on_merged": full_on_merged,
            "change_scoped_on_merged": change_scoped_on_merged,
            "exact_input_on_merged": exact_input_on_merged,
        }


if __name__ == "__main__":
    report = build()
    out = Path(__file__).with_name("seeded-conflict-results.json")
    out.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report, indent=2, sort_keys=True))

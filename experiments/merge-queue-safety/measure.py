"""Merge-queue safety: how often does a change that collects cleanly on its
own PR head fail to collect on the real merged tree, and does the answer
change with how narrowly test selection is scoped?

Uses real two-parent "Merge pull request" commits from pytest's own history.
For each sampled merge M with mainline parent `base` and PR-branch parent
`pr_head`:

  - `pr-head-alone`  : pytest --collect-only on the whole suite, at pr_head.
                       This is what CI without a merge queue tests.
  - `full-on-merged` : the same, but at M itself (the real, already-resolved
                       merged tree). Ground truth for "does the integration
                       actually break".
  - `change-scoped`  : collect-only restricted to test files matched to the
                       PR's OWN changed files (name/path-stem heuristic,
                       ignorant of what changed concurrently on `base`), run
                       against the merged tree M.
  - `exact-input`     : same, but the selection also includes test files
                       matched to what changed concurrently on `base` while
                       the PR was open — anything whose declared inputs
                       differ from the last known-good evidence on EITHER
                       side reruns; only genuinely untouched tasks reuse.

`--collect-only` executes real import/collection code (fixtures, conftest
registration, decorators) without running test bodies, so it is fast and
still exercises a real class of "clean git merge, broken integration"
failures — a renamed fixture referenced under its old name from a file the
PR never touched, for instance.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
from pathlib import Path

VERSION_STUB = 'version = "9999.0.0.dev0"\nversion_tuple = (9999, 0, 0, "dev0")\n'
COLLECT_ERROR_RE = re.compile(r"^ERROR (testing/\S+\.py)", re.MULTILINE)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def git_ok(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def recent_merge_commits(repo: Path, limit: int) -> list[str]:
    out = git(repo, "log", "--merges", "--first-parent", "--format=%H", "main")
    return out.splitlines()[:limit]


def changed_files(repo: Path, a: str, b: str, prefix: str) -> list[str]:
    out = git(repo, "diff", "--name-only", a, b, "--", prefix)
    return [line for line in out.splitlines() if line.endswith(".py")]


def module_stem(path: str) -> str:
    parts = Path(path).parts  # e.g. ("src", "_pytest", "mark", "structures.py")
    if len(parts) >= 4 and parts[-2] != "_pytest":
        return parts[-2].lstrip("_")
    return Path(parts[-1]).stem.lstrip("_")


def test_files_at(repo: Path, sha: str) -> list[str]:
    out = git_ok(repo, "ls-tree", "-r", "--name-only", sha, "--", "testing")
    if out.returncode != 0:
        return []
    return [line for line in out.stdout.splitlines() if line.endswith(".py") and "__pycache__" not in line]


def select_tests(changed_src: list[str], changed_testing: list[str], tree_test_files: list[str]) -> set[str]:
    selected = {f for f in changed_testing if f in tree_test_files}
    stems = {module_stem(f) for f in changed_src if module_stem(f)}
    for candidate in tree_test_files:
        lowered = candidate.lower()
        if any(stem and stem.lower() in lowered for stem in stems):
            selected.add(candidate)
    return selected


def prepare_checkout(repo: Path, sha: str) -> bool:
    if git_ok(repo, "checkout", "-f", "-q", sha).returncode != 0:
        return False
    (repo / "src" / "_pytest" / "_version.py").write_text(VERSION_STUB)
    return True


def collect(repo: Path, venv_python: str, paths: list[str] | None, timeout: int) -> dict:
    """`passed=None` means unresolved (a real answer was not obtained), never
    a silent pass or fail: a fixed dependency set run against decade-old
    history can hit an environment/layout incompatibility pytest itself
    never reports (an uncaught ImportError with a raw traceback, no `ERROR
    collecting` summary) that has nothing to do with the merge under test."""
    targets = paths if paths else ["testing/"]
    if paths is not None and not paths:
        return {"attempted": False, "passed": None, "failed_files": [], "reason": "empty selection"}
    env = {"PYTHONPATH": str(repo / "src"), "PATH": "/usr/bin:/bin"}
    try:
        result = subprocess.run(
            [venv_python, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *targets],
            cwd=repo, env=env, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"attempted": True, "passed": None, "failed_files": [], "reason": "timeout"}
    if result.returncode == 0:
        return {"attempted": True, "passed": True, "failed_files": [], "reason": "exit 0"}
    failed = sorted(set(COLLECT_ERROR_RE.findall(result.stdout)))
    if "Traceback (most recent call last)" in result.stderr and not failed and "short test summary info" not in result.stdout:
        return {"attempted": True, "passed": None, "failed_files": [], "reason": "environment/layout incompatibility, not a pytest-level collection result"}
    return {"attempted": True, "passed": result.returncode == 0, "failed_files": failed, "reason": f"exit {result.returncode}"}


def run_sample(repo: Path, venv_python: str, merge_sha: str, timeout: int) -> dict:
    base, pr_head = git(repo, "rev-parse", f"{merge_sha}^1"), git(repo, "rev-parse", f"{merge_sha}^2")
    merge_base_r = git_ok(repo, "merge-base", base, pr_head)
    if merge_base_r.returncode != 0:
        return {"merge": merge_sha, "skipped": "no merge-base"}
    merge_base = merge_base_r.stdout.strip()

    pr_src = changed_files(repo, merge_base, pr_head, "src/_pytest")
    pr_testing = changed_files(repo, merge_base, pr_head, "testing")
    concurrent_src = changed_files(repo, merge_base, base, "src/_pytest")
    concurrent_testing = changed_files(repo, merge_base, base, "testing")
    if not (pr_src or pr_testing) or not (concurrent_src or concurrent_testing):
        return {"merge": merge_sha, "skipped": "no concurrent change on one side"}

    if not prepare_checkout(repo, pr_head):
        return {"merge": merge_sha, "skipped": "checkout pr_head failed"}
    head_full = collect(repo, venv_python, None, timeout)

    if not prepare_checkout(repo, merge_sha):
        return {"merge": merge_sha, "skipped": "checkout merge failed"}
    merged_full = collect(repo, venv_python, None, timeout)

    tree_tests = test_files_at(repo, merge_sha)
    change_scoped_set = select_tests(pr_src, pr_testing, tree_tests)
    exact_input_set = select_tests(pr_src + concurrent_src, pr_testing + concurrent_testing, tree_tests)
    merged_scoped = collect(repo, venv_python, sorted(change_scoped_set), timeout)
    merged_exact = collect(repo, venv_python, sorted(exact_input_set), timeout)

    return {
        "merge": merge_sha, "base": base, "pr_head": pr_head, "merge_base": merge_base,
        "pr_changed_files": len(pr_src) + len(pr_testing),
        "concurrent_changed_files": len(concurrent_src) + len(concurrent_testing),
        "change_scoped_selection_size": len(change_scoped_set),
        "exact_input_selection_size": len(exact_input_set),
        "pr_head_alone": head_full,
        "full_on_merged": merged_full,
        "change_scoped_on_merged": merged_scoped,
        "exact_input_on_merged": merged_exact,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--venv-python", required=True)
    parser.add_argument("--merge-window", type=int, default=1500, help="Only sample among this many most recent merges, for dependency stability.")
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    args = parser.parse_args()

    repo = Path(args.repo_dir).resolve()
    merges = recent_merge_commits(repo, args.merge_window)
    rng = random.Random(args.seed)
    rng.shuffle(merges)

    samples, skipped = [], 0
    for sha in merges:
        if len(samples) >= args.samples:
            break
        result = run_sample(repo, args.venv_python, sha, args.timeout)
        if "skipped" in result:
            skipped += 1
            continue
        samples.append(result)
    git_ok(repo, "checkout", "-f", "-q", "main")

    def caught(entry: dict) -> bool | None:
        r = entry
        return r["passed"] if r.get("attempted") else None

    would_have_missed_on_head = 0  # head-alone passed, merged tree actually broke
    caught_full = caught_scoped = caught_exact = 0
    denom = 0
    for s in samples:
        head_ok, full_ok = caught(s["pr_head_alone"]), caught(s["full_on_merged"])
        if head_ok is None or full_ok is None:
            continue
        if head_ok and not full_ok:
            would_have_missed_on_head += 1
            denom += 1
            caught_full += 1  # by definition, full-on-merged is the ground truth that caught it
            if caught(s["change_scoped_on_merged"]) is False:
                caught_scoped += 1
            if caught(s["exact_input_on_merged"]) is False:
                caught_exact += 1

    summary = {
        "measured": True,
        "repo": "https://github.com/pytest-dev/pytest",
        "merge_window": args.merge_window,
        "samples_requested": args.samples,
        "samples_evaluated": len(samples),
        "samples_skipped_no_concurrent_change_on_one_side": skipped,
        "real_merges_where_merged_tree_broke_collection_but_pr_head_alone_passed": would_have_missed_on_head,
        "of_those_caught_by_full_suite_on_merged_tree": caught_full,
        "of_those_caught_by_change_scoped_selection_on_merged_tree": caught_scoped,
        "of_those_caught_by_exact_input_selection_on_merged_tree": caught_exact,
    }
    report = {"summary": summary, "samples": samples}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

"""Merge-queue safety: how often does a change that passes on its own PR head
fail on the real merged tree, and does the answer change with how narrowly
test selection is scoped?

Uses real two-parent "Merge pull request" commits from pytest's own history.
For each sampled merge M with mainline parent `base` and PR-branch parent
`pr_head`:

  - `pr-head-alone`  : the whole suite, at pr_head. What CI without a merge
                       queue tests.
  - `full-on-merged` : the same, but at M itself (the real, already-resolved
                       merged tree). Ground truth for "does the integration
                       actually break".
  - `change-scoped`  : restricted to test files matched to the PR's OWN
                       changed files (name/path-stem heuristic, ignorant of
                       what changed concurrently on `base`).
  - `exact-input`     : also includes test files matched to what changed
                       concurrently on `base` while the PR was open —
                       anything whose declared inputs differ from the last
                       known-good evidence on EITHER side reruns.

Two oracles, both real command executions, no mocked verdicts:

  - `collect`  : `pytest --collect-only`. Fast (real import/fixture/hook
                registration, no test bodies), so it runs for every sampled
                merge, but only catches import/collection-time breaks.
  - `execute`  : `pytest` (real test bodies). Catches runtime/assertion-level
                semantic conflicts `collect` cannot see, but is much slower,
                so it runs only for `--runtime-subsample` merges (selections)
                and, within that, `--full-runtime-subsample` merges (the full
                suite, the most expensive target).

Every sampled merge lands in exactly one outcome bucket per oracle
(`both_passed` / `both_failed` / `head_passed_merged_failed` — the target
case a merge queue exists for / `head_failed_merged_passed` / `unresolved`
— see `bucket_outcomes`), so bucket counts always sum to the evaluated
count; nothing is silently dropped.
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
RUN_FAILED_RE = re.compile(r"^FAILED (\S+)", re.MULTILINE)
RUN_ERROR_RE = re.compile(r"^ERROR (\S+)", re.MULTILINE)


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


def _run_pytest(repo: Path, venv_python: str, args: list[str], paths: list[str] | None, timeout: int) -> dict:
    """Shared by `collect` and `execute`. `passed=None` (unresolved) means a
    real answer was not obtained, never a silent pass or fail: a fixed
    dependency set run against old history can hit an environment/layout
    incompatibility pytest itself never structurally reports (an uncaught
    ImportError with a raw traceback and no pytest-level summary at all)
    that has nothing to do with the merge under test."""
    targets = paths if paths else ["testing/"]
    if paths is not None and not paths:
        return {"attempted": False, "passed": None, "failed_files": [], "reason": "empty selection"}
    env = {"PYTHONPATH": str(repo / "src"), "PATH": "/usr/bin:/bin"}
    try:
        result = subprocess.run(
            [venv_python, "-m", "pytest", *args, "-q", "-p", "no:cacheprovider", *targets],
            cwd=repo, env=env, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"attempted": True, "passed": None, "failed_files": [], "reason": "timeout"}
    if result.returncode == 0:
        return {"attempted": True, "passed": True, "failed_files": [], "reason": "exit 0"}
    if "--collect-only" in args:
        failed = sorted(set(COLLECT_ERROR_RE.findall(result.stdout)))
        structured = bool(failed)
    else:
        failed = sorted(set(RUN_FAILED_RE.findall(result.stdout)) | set(RUN_ERROR_RE.findall(result.stdout)))
        structured = bool(failed) or "short test summary info" in result.stdout
    if not structured and "Traceback (most recent call last)" in result.stderr:
        return {"attempted": True, "passed": None, "failed_files": [], "reason": "environment/layout incompatibility, not a pytest-level result"}
    return {"attempted": True, "passed": False, "failed_files": failed, "reason": f"exit {result.returncode}"}


def collect(repo: Path, venv_python: str, paths: list[str] | None, timeout: int) -> dict:
    return _run_pytest(repo, venv_python, ["--collect-only"], paths, timeout)


def execute(repo: Path, venv_python: str, paths: list[str] | None, timeout: int) -> dict:
    return _run_pytest(repo, venv_python, [], paths, timeout)


def restrict_to_existing(paths: set[str], tree_files: list[str]) -> list[str]:
    tree_set = set(tree_files)
    return sorted(p for p in paths if p in tree_set)


def run_sample(repo: Path, venv_python: str, merge_sha: str, timeout: int, runtime_timeout: int, do_runtime: bool, do_full_runtime: bool) -> dict:
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

    tree_tests_pr_head = test_files_at(repo, pr_head)
    tree_tests_merged = test_files_at(repo, merge_sha)
    change_scoped_set = select_tests(pr_src, pr_testing, tree_tests_merged)
    exact_input_set = select_tests(pr_src + concurrent_src, pr_testing + concurrent_testing, tree_tests_merged)
    scoped_at_head = restrict_to_existing(change_scoped_set, tree_tests_pr_head)
    exact_at_head = restrict_to_existing(exact_input_set, tree_tests_pr_head)
    scoped_at_merged = restrict_to_existing(change_scoped_set, tree_tests_merged)
    exact_at_merged = restrict_to_existing(exact_input_set, tree_tests_merged)

    if not prepare_checkout(repo, pr_head):
        return {"merge": merge_sha, "skipped": "checkout pr_head failed"}
    result = {
        "merge": merge_sha, "base": base, "pr_head": pr_head, "merge_base": merge_base,
        "pr_changed_files": len(pr_src) + len(pr_testing),
        "concurrent_changed_files": len(concurrent_src) + len(concurrent_testing),
        "change_scoped_selection_size": len(change_scoped_set),
        "exact_input_selection_size": len(exact_input_set),
        "pr_head_alone": collect(repo, venv_python, None, timeout),
    }
    if do_runtime:
        result["change_scoped_at_head_runtime"] = execute(repo, venv_python, scoped_at_head, runtime_timeout)
        result["exact_input_at_head_runtime"] = execute(repo, venv_python, exact_at_head, runtime_timeout)
    if do_full_runtime:
        result["pr_head_alone_runtime"] = execute(repo, venv_python, None, runtime_timeout * 3)

    if not prepare_checkout(repo, merge_sha):
        return {"merge": merge_sha, "skipped": "checkout merge failed"}
    result["full_on_merged"] = collect(repo, venv_python, None, timeout)
    result["change_scoped_on_merged"] = collect(repo, venv_python, sorted(change_scoped_set), timeout)
    result["exact_input_on_merged"] = collect(repo, venv_python, sorted(exact_input_set), timeout)
    if do_runtime:
        result["change_scoped_at_merged_runtime"] = execute(repo, venv_python, scoped_at_merged, runtime_timeout)
        result["exact_input_at_merged_runtime"] = execute(repo, venv_python, exact_at_merged, runtime_timeout)
    if do_full_runtime:
        result["full_on_merged_runtime"] = execute(repo, venv_python, None, runtime_timeout * 3)
    return result


def caught(entry: dict | None) -> bool | None:
    if entry is None:
        return None
    return entry["passed"] if entry.get("attempted") else None


def bucket_outcomes(pairs: list[tuple[bool | None, bool | None]]) -> dict:
    """Every pair lands in exactly one bucket; the bucket counts always sum
    to len(pairs), so a reader never has to trust an unaccounted remainder."""
    buckets = {"both_passed": 0, "both_failed": 0, "head_passed_merged_failed": 0, "head_failed_merged_passed": 0, "unresolved": 0}
    for head, merged in pairs:
        if head is None or merged is None:
            buckets["unresolved"] += 1
        elif head and merged:
            buckets["both_passed"] += 1
        elif not head and not merged:
            buckets["both_failed"] += 1
        elif head and not merged:
            buckets["head_passed_merged_failed"] += 1
        else:
            buckets["head_failed_merged_passed"] += 1
    buckets["total"] = sum(v for k, v in buckets.items() if k != "total")
    assert buckets["total"] == len(pairs), f"bucket accounting mismatch: {buckets['total']} != {len(pairs)}"
    return buckets


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--venv-python", required=True)
    parser.add_argument("--merge-window", type=int, default=1500, help="Only sample among this many most recent merges, for dependency stability.")
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--timeout", type=int, default=90, help="Per-collect-only-invocation timeout.")
    parser.add_argument("--runtime-timeout", type=int, default=90, help="Per-selection real-execution timeout (full suite gets 3x this).")
    parser.add_argument("--runtime-subsample", type=int, default=10, help="Of the evaluated merges, how many also get real test execution for the two selections.")
    parser.add_argument("--full-runtime-subsample", type=int, default=4, help="Of the runtime subsample, how many also get real full-suite execution (the expensive oracle).")
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
        do_runtime = len(samples) < args.runtime_subsample
        do_full_runtime = len(samples) < args.full_runtime_subsample
        result = run_sample(repo, args.venv_python, sha, args.timeout, args.runtime_timeout, do_runtime, do_full_runtime)
        if "skipped" in result:
            skipped += 1
            continue
        samples.append(result)
    git_ok(repo, "checkout", "-f", "-q", "main")

    collect_buckets = bucket_outcomes([(caught(s["pr_head_alone"]), caught(s["full_on_merged"])) for s in samples])
    collect_scoped_of_target = sum(
        1 for s in samples if caught(s["pr_head_alone"]) and caught(s["full_on_merged"]) is False and caught(s["change_scoped_on_merged"]) is False
    )
    collect_exact_of_target = sum(
        1 for s in samples if caught(s["pr_head_alone"]) and caught(s["full_on_merged"]) is False and caught(s["exact_input_on_merged"]) is False
    )

    runtime_samples = samples[: args.runtime_subsample]
    full_runtime_samples = samples[: args.full_runtime_subsample]
    runtime_scoped_buckets = bucket_outcomes([(caught(s.get("change_scoped_at_head_runtime")), caught(s.get("change_scoped_at_merged_runtime"))) for s in runtime_samples])
    runtime_exact_buckets = bucket_outcomes([(caught(s.get("exact_input_at_head_runtime")), caught(s.get("exact_input_at_merged_runtime"))) for s in runtime_samples])
    full_runtime_buckets = bucket_outcomes([(caught(s.get("pr_head_alone_runtime")), caught(s.get("full_on_merged_runtime"))) for s in full_runtime_samples])

    summary = {
        "measured": True,
        "repo": "https://github.com/pytest-dev/pytest",
        "merge_window": args.merge_window,
        "samples_requested": args.samples,
        "samples_evaluated": len(samples),
        "samples_skipped_no_concurrent_change_on_one_side": skipped,
        "collect_only": {
            "oracle": "pytest --collect-only: pr-head-alone vs full-on-merged",
            "buckets": collect_buckets,
            "target_case_change_scoped_still_missed_it": collect_scoped_of_target,
            "target_case_exact_input_still_missed_it": collect_exact_of_target,
        },
        "runtime_execution": {
            "oracle": "pytest (real test bodies): head vs merged, for the change-scoped and exact-input selections",
            "subsample_size": len(runtime_samples),
            "change_scoped_buckets": runtime_scoped_buckets,
            "exact_input_buckets": runtime_exact_buckets,
        },
        "full_suite_runtime_execution": {
            "oracle": "pytest (real test bodies), whole suite: head vs merged",
            "subsample_size": len(full_runtime_samples),
            "buckets": full_runtime_buckets,
        },
    }
    report = {"summary": summary, "samples": samples}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

"""Rebase/restack declared-input reuse against real pytest history.

Defines one task per source module under src/_pytest/, picks real consecutive
commit ranges as a synthetic "stack", replays them onto a later real base with
`git rebase --onto`, and compares each task's git tree hash (content-addressed
by construction) before and after. A matching hash means a receipt keyed on
that task's declared inputs is reusable after the rebase without rerunning it.
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
from pathlib import Path

TASKS = [
    "_code", "_io", "_py", "assertion", "config", "mark",
    "cacheprovider.py", "capture.py", "compat.py", "debugging.py",
    "doctest.py", "fixtures.py", "logging.py", "main.py", "monkeypatch.py",
    "nodes.py", "outcomes.py", "pathlib.py", "python.py", "runner.py",
    "stash.py", "terminal.py", "tmpdir.py", "warnings.py",
]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def git_ok(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def first_parent_history(repo: Path) -> list[str]:
    out = git(repo, "rev-list", "--first-parent", "--reverse", "main")
    return out.splitlines()


def tree_hash(repo: Path, sha: str, task: str) -> str | None:
    result = git_ok(repo, "rev-parse", f"{sha}:src/_pytest/{task}")
    return result.stdout.strip() if result.returncode == 0 else None


def touched(repo: Path, base: str, tip: str, task: str) -> bool:
    out = git(repo, "diff", "--name-only", base, tip, "--", f"src/_pytest/{task}")
    return bool(out.strip())


def reset_repo(repo: Path, branch: str) -> None:
    """Best-effort recovery to a clean state on main, regardless of prior sample outcome."""
    git_ok(repo, "rebase", "--abort")
    git_ok(repo, "checkout", "-f", "-q", "main")
    git_ok(repo, "branch", "-D", branch)


def run_sample(repo: Path, commits: list[str], i: int, stack_size: int, gap: int) -> dict:
    old_base, stack_tip, new_base = commits[i], commits[i + stack_size], commits[i + stack_size + gap]
    branch = "assay-rebase-reuse-tmp"
    reset_repo(repo, branch)
    try:
        if git_ok(repo, "branch", branch, stack_tip).returncode != 0:
            return {"old_base": old_base, "stack_tip": stack_tip, "new_base": new_base, "rebase_conflict": True, "setup_failed": True}
        git_ok(repo, "checkout", "-q", branch)

        before = {task: tree_hash(repo, stack_tip, task) for task in TASKS}
        stack_touches = {task: touched(repo, old_base, stack_tip, task) for task in TASKS}
        meanwhile_touches = {task: touched(repo, old_base, new_base, task) for task in TASKS}

        rebase = git_ok(repo, "rebase", "--onto", new_base, old_base, branch)
        sample = {"old_base": old_base, "stack_tip": stack_tip, "new_base": new_base, "rebase_conflict": rebase.returncode != 0}
        if rebase.returncode != 0:
            return sample

        new_tip = git(repo, "rev-parse", branch)
        after = {task: tree_hash(repo, new_tip, task) for task in TASKS}

        sample["new_tip"] = new_tip
        sample["tasks"] = {
            task: {
                "identical": before[task] == after[task],
                "stack_touched": stack_touches[task],
                "meanwhile_touched": meanwhile_touches[task],
            }
            for task in TASKS
        }
        return sample
    finally:
        reset_repo(repo, branch)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True, help="Path to a prepared pytest checkout (main branch, first-parent history available).")
    parser.add_argument("--samples", type=int, default=25)
    parser.add_argument("--stack-size", type=int, default=5, help="Real consecutive commits treated as one stacked branch.")
    parser.add_argument("--gap", type=int, default=40, help="Real commits that land on main before the stack is rebased.")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    args = parser.parse_args()

    repo = Path(args.repo_dir).resolve()
    commits = first_parent_history(repo)
    span = args.stack_size + args.gap
    candidates = list(range(0, len(commits) - span - 1))
    rng = random.Random(args.seed)
    rng.shuffle(candidates)

    samples = []
    for i in candidates:
        if len(samples) >= args.samples:
            break
        samples.append(run_sample(repo, commits, i, args.stack_size, args.gap))

    reusable = 0
    stack_touched_pairs = 0
    stack_touched_reusable = 0
    conflicts = 0
    for sample in samples:
        if sample["rebase_conflict"]:
            conflicts += 1
            continue
        if "tasks" not in sample:
            continue
        for task, outcome in sample["tasks"].items():
            if outcome["identical"]:
                reusable += 1
            if outcome["stack_touched"]:
                stack_touched_pairs += 1
                if outcome["identical"]:
                    stack_touched_reusable += 1

    total_pairs = sum(len(s["tasks"]) for s in samples if not s["rebase_conflict"])
    summary = {
        "measured": True,
        "repo": "https://github.com/pytest-dev/pytest",
        "task_count": len(TASKS),
        "samples_requested": args.samples,
        "samples_run": len(samples),
        "rebase_conflicts": conflicts,
        "clean_samples": len(samples) - conflicts,
        "task_pairs_evaluated": total_pairs,
        "task_pairs_hash_identical": reusable,
        "task_pairs_hash_identical_fraction": round(reusable / total_pairs, 4) if total_pairs else None,
        "stack_touched_task_pairs": stack_touched_pairs,
        "stack_touched_task_pairs_still_reusable": stack_touched_reusable,
        "stack_touched_reuse_fraction": round(stack_touched_reusable / stack_touched_pairs, 4) if stack_touched_pairs else None,
    }
    report = {"summary": summary, "samples": samples}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

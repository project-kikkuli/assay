"""Cross-branch (not just cross-push) task-evidence reuse against real pytest history.

`rebase-reuse` measures how much one branch's own content-addressed evidence
survives that branch's own rebase. This measures a different, horizontal
question: when M agents each open a branch off the SAME real base commit,
how much of a shared receipt store's evidence (keyed by task id + real git
tree hash) is reusable ACROSS those sibling branches as they land one at a
time, versus a per-branch cache that only ever knows about its own pushes.

Each sample takes a real base commit B from pytest's actual first-parent
history and the next M real commits after it as M independent single-commit
sibling branches (each commit's own real changed-file set, from
`git diff-tree`, standing in for that sibling's real diff — the same reuse
of real commit bytes under a different concurrency assumption that
`rebase-reuse` already uses). Siblings are "merged" in their real
chronological order. Tasks are derived per file from the repository's own
real directory structure (directory-level under `src/_pytest/*` and
`testing/*`, whole-bucket for `doc/`, `changelog/`, etc. — the same
granularity `rebase-reuse` uses for `src/_pytest`), so every real commit
that touches the tree contributes at least one task, not just the narrow
slice one prior experiment happened to pick.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def first_parent_history(repo: Path) -> list[str]:
    return git(repo, "rev-list", "--first-parent", "--reverse", "main").splitlines()


def changed_files(repo: Path, sha: str) -> list[str]:
    out = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha)
    return [f for f in out.splitlines() if f]


def task_id(path: str) -> str:
    parts = path.split("/")
    if parts[0] == "src" and len(parts) >= 2 and parts[1] == "_pytest" and len(parts) >= 3:
        return "/".join(parts[:3])
    if parts[0] == "testing" and len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0]


def task_tree_hash(repo: Path, sha: str, task: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{sha}:{task}"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def changed_tasks(files: list[str]) -> set[str]:
    return {task_id(f) for f in files}


def run_window(repo: Path, base: str, siblings: list[str]) -> dict:
    """siblings are already in real chronological (assumed merge) order."""
    sibling_touched = [changed_tasks(changed_files(repo, sha)) for sha in siblings]
    all_tasks = sorted(set().union(*sibling_touched)) if sibling_touched else []
    base_hashes = {task: task_tree_hash(repo, base, task) for task in all_tasks}
    sibling_hashes = [
        {task: task_tree_hash(repo, sha, task) for task in touched}
        for sha, touched in zip(siblings, sibling_touched)
    ]

    # Shared store: a (task, tree_hash) pair only needs computing the first time it is
    # ever seen anywhere — pre-seeded with the base's own hashes for the tasks any
    # sibling touches, since the base was already verified once before any sibling
    # opened. Everything after that first sighting, whichever sibling produced it, is
    # a real hit against that already-verified content.
    seen: dict[str, set[str]] = {task: {h} for task, h in base_hashes.items() if h}
    shared_recomputes = 0
    cross_sibling_hits = 0
    own_edit_recomputes = 0
    collision_with_earlier_sibling = 0
    already_touched_tasks: set[str] = set()
    for touched, hashes in zip(sibling_touched, sibling_hashes):
        for task in touched:
            own_edit_recomputes += 1
            h = hashes.get(task)
            if h is not None and h in seen.get(task, set()):
                cross_sibling_hits += 1  # base or an earlier sibling already produced this exact content
            else:
                shared_recomputes += 1
                if h is not None:
                    seen.setdefault(task, set()).add(h)
            if task in already_touched_tasks:
                collision_with_earlier_sibling += 1
        already_touched_tasks |= touched

    return {
        "base": base,
        "siblings": siblings,
        "distinct_tasks_touched_in_window": len(all_tasks),
        "sibling_touched_task_counts": [len(t) for t in sibling_touched],
        "per_branch_cache_recomputes": own_edit_recomputes,
        "shared_store_recomputes": shared_recomputes,
        "shared_store_cross_sibling_hits": cross_sibling_hits,
        "collisions_with_earlier_sibling": collision_with_earlier_sibling,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True, help="Path to a prepared pytest checkout (main branch, first-parent history available).")
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--window-sizes", type=int, nargs="+", default=[2, 4, 8])
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    args = parser.parse_args()

    repo = Path(args.repo_dir).resolve()
    commits = first_parent_history(repo)
    rng = random.Random(args.seed)

    by_window: dict[int, list[dict]] = {}
    for window in args.window_sizes:
        candidates = list(range(0, len(commits) - window - 1))
        rng.shuffle(candidates)
        samples = []
        for i in candidates:
            if len(samples) >= args.samples:
                break
            base = commits[i]
            siblings = commits[i + 1:i + 1 + window]
            samples.append(run_window(repo, base, siblings))
        by_window[window] = samples

    summary = {"measured": True, "repo": "https://github.com/pytest-dev/pytest", "samples_per_window": args.samples, "windows": {}}
    for window, samples in by_window.items():
        per_branch = sum(s["per_branch_cache_recomputes"] for s in samples)
        shared = sum(s["shared_store_recomputes"] for s in samples)
        cross_hits = sum(s["shared_store_cross_sibling_hits"] for s in samples)
        collisions = sum(s["collisions_with_earlier_sibling"] for s in samples)
        disjoint_windows = sum(1 for s in samples if s["collisions_with_earlier_sibling"] == 0)
        summary["windows"][str(window)] = {
            "samples": len(samples),
            "per_branch_cache_recomputes_total": per_branch,
            "shared_store_recomputes_total": shared,
            "shared_store_cross_sibling_hits_total": cross_hits,
            "shared_store_additional_saved_fraction_vs_per_branch": round(1 - shared / per_branch, 4) if per_branch else None,
            "windows_with_a_real_task_collision": sum(1 for s in samples if s["collisions_with_earlier_sibling"] > 0),
            "task_collision_events_total": collisions,
            "windows_fully_disjoint": disjoint_windows,
            "windows_fully_disjoint_fraction": round(disjoint_windows / len(samples), 4) if samples else None,
            "mean_touched_tasks_per_sibling": round(statistics.mean(c for s in samples for c in s["sibling_touched_task_counts"]), 3) if samples else None,
            "mean_distinct_tasks_per_window": round(statistics.mean(s["distinct_tasks_touched_in_window"] for s in samples), 3) if samples else None,
        }

    report = {"summary": summary, "windows": {str(k): v for k, v in by_window.items()}}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

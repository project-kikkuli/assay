"""Measure real convergence time (push a fix -> green again) for a stacked
branch chain, with and without `layer_cache`'s real per-task receipt cache.

Scenario, built entirely from real `itsdangerous` history: a 2-layer stack
(`layer1` -> `layer2`, real consecutive commits) is already green — its
receipts are real, warmed by actually running every task at every layer once.
The author then pushes a fix to the BOTTOM layer: `git rebase --onto` inserts
`gap` further real historical commits underneath `layer1` (the same real
technique `rebase-reuse` uses, reframed here as "what landed underneath your
stack before you were done"), so `layer2` must be replayed on the corrected
base. The question is how fast the stack gets back to green: rerun
everything from the changed layer up (`run_stack_naive`), or run only the
real tasks whose declared-input content actually changed
(`run_stack_cached`, reusing the warm cache from the first pass)?

Both paths run real `pytest` subprocesses against a real checkout; nothing
here is simulated. A rebase that conflicts is recorded, not retried or
discarded — the same honesty `rebase-reuse` already established for this
technique.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from layer_cache import (  # noqa: E402
    LayerCache, checkout, git, git_ok, run_stack_cached, run_stack_naive,
)


def first_parent_history(repo: Path, recent: int) -> list[str]:
    """The most recent `recent` first-parent commits. Restricted to a recent
    window because this package's history includes a src-layout migration —
    sampling the whole history would draw layers whose test/source paths
    don't match `layer_cache`'s real path assumptions, which is a fact about
    this repo's layout history, not something to paper over with a broader
    path search."""
    all_commits = git(repo, "log", "--first-parent", "--reverse", "--pretty=%H", "main").splitlines()
    return all_commits[-recent:]


def reset_repo(repo: Path, branch: str) -> None:
    git_ok(repo, "rebase", "--abort")
    git_ok(repo, "checkout", "-f", "-q", "main")
    git_ok(repo, "branch", "-D", branch)


def run_sample(repo: Path, python: str, commits: list[str], i: int, gap: int, seed_tag: str) -> dict:
    old_base, layer1, layer2 = commits[i], commits[i + 1], commits[i + 2]
    new_base = commits[i + 2 + gap]
    branch = f"assay-stack-cache-tmp-{seed_tag}"
    reset_repo(repo, branch)
    try:
        if git_ok(repo, "branch", branch, layer2).returncode != 0:
            return {"old_base": old_base, "layer1": layer1, "layer2": layer2, "new_base": new_base, "gap": gap, "setup_failed": True}
        git_ok(repo, "checkout", "-q", branch)

        with tempfile.TemporaryDirectory() as tmp:
            cache = LayerCache(Path(tmp) / "cache.json")
            warm = run_stack_cached(repo, python, [layer1, layer2], cache)

            rebase = git_ok(repo, "rebase", "--onto", new_base, old_base, branch)
            sample = {"old_base": old_base, "layer1": layer1, "layer2": layer2, "new_base": new_base, "gap": gap,
                       "rebase_conflict": rebase.returncode != 0, "warm_wall_s": round(warm["wall_s"], 4)}
            if rebase.returncode != 0:
                return sample

            new_layer1 = git(repo, "rev-parse", f"{branch}~1")
            new_layer2 = git(repo, "rev-parse", branch)
            new_stack = [new_layer1, new_layer2]

            cached = run_stack_cached(repo, python, new_stack, cache)
            naive = run_stack_naive(repo, python, new_stack)

            sample.update({
                "cached_wall_s": round(cached["wall_s"], 4),
                "cached_tasks_run": cached["tasks_run"] - cached["tasks_reused"],
                "cached_tasks_reused": cached["tasks_reused"],
                "cached_tasks_total": cached["tasks_run"],
                "naive_wall_s": round(naive["wall_s"], 4),
                "naive_layers_run": len(naive["events"]),
                "speedup_x": round(naive["wall_s"] / cached["wall_s"], 3) if cached["wall_s"] > 0 else None,
                "time_saved_s": round(naive["wall_s"] - cached["wall_s"], 4),
            })
            return sample
    finally:
        reset_repo(repo, branch)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--python", required=True, help="Interpreter with itsdangerous installed editable, pytest and freezegun.")
    parser.add_argument("--samples", type=int, default=15)
    parser.add_argument("--gaps", type=int, nargs="+", default=[1, 5, 15])
    parser.add_argument("--seed", type=int, default=23)
    parser.add_argument("--recent-commits", type=int, default=150)
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    args = parser.parse_args()

    repo = Path(args.repo_dir).resolve()
    commits = first_parent_history(repo, args.recent_commits)
    starting_branch = "main"

    by_gap: dict[int, list[dict]] = {}
    try:
        for gap in args.gaps:
            span = 2 + gap
            candidates = list(range(0, len(commits) - span - 1))
            rng = random.Random(args.seed + gap)
            rng.shuffle(candidates)
            samples = []
            for i in candidates:
                if len(samples) >= args.samples:
                    break
                samples.append(run_sample(repo, args.python, commits, i, gap, seed_tag=f"g{gap}"))
            by_gap[gap] = samples
    finally:
        checkout(repo, starting_branch)

    summary = {"measured": True, "repo": "https://github.com/pallets/itsdangerous", "samples_per_gap": args.samples, "gaps": {}}
    for gap, samples in by_gap.items():
        clean = [s for s in samples if not s.get("rebase_conflict") and not s.get("setup_failed")]
        conflicts = sum(1 for s in samples if s.get("rebase_conflict"))
        setup_failed = sum(1 for s in samples if s.get("setup_failed"))
        if clean:
            speedups = [s["speedup_x"] for s in clean if s["speedup_x"] is not None]
            saved = [s["time_saved_s"] for s in clean]
            reused = sum(s["cached_tasks_reused"] for s in clean)
            total_tasks = sum(s["cached_tasks_total"] for s in clean)
            summary["gaps"][str(gap)] = {
                "samples_run": len(samples),
                "clean_samples": len(clean),
                "rebase_conflicts": conflicts,
                "setup_failed": setup_failed,
                "mean_naive_wall_s": round(statistics.mean(s["naive_wall_s"] for s in clean), 4),
                "mean_cached_wall_s": round(statistics.mean(s["cached_wall_s"] for s in clean), 4),
                "mean_speedup_x": round(statistics.mean(speedups), 3) if speedups else None,
                "median_speedup_x": round(statistics.median(speedups), 3) if speedups else None,
                "mean_time_saved_s": round(statistics.mean(saved), 4),
                "tasks_reused_total": reused,
                "tasks_total": total_tasks,
                "tasks_reused_fraction": round(reused / total_tasks, 4) if total_tasks else None,
            }
        else:
            summary["gaps"][str(gap)] = {"samples_run": len(samples), "clean_samples": 0, "rebase_conflicts": conflicts, "setup_failed": setup_failed}

    report = {"summary": summary, "gaps": {str(k): v for k, v in by_gap.items()}}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

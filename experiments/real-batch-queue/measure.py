"""Drive the real batch queue against a real backlog of real `itsdangerous`
commits and measure real per-PR convergence time (queue-open to verdict-
known) for serial vs real parallel batching, at two real worker-count
regimes standing in for low vs high effective arrival pressure.

"Realistic arrival rate" is operationalized here as burst size / worker
count, not a literal timed arrival schedule: the whole real backlog is
already queued (as it would be under many concurrent agents), and the
question is how fast real parallel batching clears it versus one real
worker taking PRs one at a time. Every duration is a real wall-clock
timestamp from a real `ThreadPoolExecutor` dispatching real subprocess
`pytest` runs against real `git worktree` checkouts — nothing here is
pre-measured and replayed.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from batch_queue import RealBatchQueue, git  # noqa: E402


def recent_first_parent_commits(repo: Path, count: int) -> list[str]:
    out = git(repo, "log", "--first-parent", "-n", str(count), "--pretty=%H", "main")
    return list(reversed(out.splitlines()))


def build_prs(commits: list[str], mutated_count: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    mutated_indices = set(rng.sample(range(len(commits)), k=min(mutated_count, len(commits))))
    return [{"index": i, "sha": sha, "mutated": i in mutated_indices} for i, sha in enumerate(commits)]


def summarize_run(run: dict, prs: list[dict]) -> dict:
    per_pr_finish = {}
    per_pr_passed = {}
    mismatches = 0
    for b in run["batches"]:
        for v in b["verdicts"]:
            per_pr_finish[v["index"]] = b["finish_s"]
            per_pr_passed[v["index"]] = v["passed"]
    for pr in prs:
        expected_broken = pr["mutated"]
        observed_pass = per_pr_passed.get(pr["index"])
        if observed_pass is None:
            continue
        if expected_broken and observed_pass:
            mismatches += 1  # a real mutation that somehow still passed — worth flagging, not hiding
        if not expected_broken and not observed_pass:
            mismatches += 1  # an unmutated PR that somehow failed for real
    finishes = list(per_pr_finish.values())
    return {
        "makespan_s": run["makespan_s"],
        "total_real_cpu_s": run["total_real_cpu_s"],
        "total_jobs": run["total_jobs"],
        "mean_convergence_s": round(statistics.mean(finishes), 4) if finishes else None,
        "p95_convergence_s": round(sorted(finishes)[max(0, int(0.95 * len(finishes)) - 1)], 4) if finishes else None,
        "prs_resolved": len(per_pr_finish),
        "verdict_mismatches": mismatches,
        "rejected_count": sum(1 for p in per_pr_passed.values() if not p),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--commits", type=int, default=24)
    parser.add_argument("--mutated-count", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=29)
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    args = parser.parse_args()

    repo = Path(args.repo_dir).resolve()
    tip = git(repo, "rev-parse", "main")
    commits = recent_first_parent_commits(repo, args.commits)
    prs = build_prs(commits, args.mutated_count, args.seed)

    with tempfile.TemporaryDirectory() as tmp:
        base_dir = Path(tmp)

        # Regime A: one real worker, batch size 1 — real serial baseline.
        serial_pool = RealBatchQueue(repo, base_dir, workers=1, python=args.python, tip_sha=tip)
        try:
            serial_run = serial_pool.run(prs, batch_size=1)
        finally:
            serial_pool.close()

        # Regime B: W real workers, real batching with real bisection.
        batch_pool = RealBatchQueue(repo, base_dir, workers=args.workers, python=args.python, tip_sha=tip)
        try:
            batched_run = batch_pool.run(prs, batch_size=args.batch_size)
        finally:
            batch_pool.close()

        # Regime C: W real workers, batch size 1 (parallel but unbatched) — isolates
        # the batching win from the plain parallelism win.
        parallel_pool = RealBatchQueue(repo, base_dir, workers=args.workers, python=args.python, tip_sha=tip)
        try:
            parallel_run = parallel_pool.run(prs, batch_size=1)
        finally:
            parallel_pool.close()

    summary = {
        "measured": True,
        "repo": "https://github.com/pallets/itsdangerous",
        "commits": len(commits),
        "mutated_count": args.mutated_count,
        "batch_size": args.batch_size,
        "workers": args.workers,
        "regimes": {
            "serial_1worker_batch1": summarize_run(serial_run, prs),
            "parallel_Nworkers_batch1": summarize_run(parallel_run, prs),
            "parallel_Nworkers_batchN": summarize_run(batched_run, prs),
        },
    }
    report = {"summary": summary, "runs": {"serial": serial_run, "parallel_unbatched": parallel_run, "parallel_batched": batched_run}}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

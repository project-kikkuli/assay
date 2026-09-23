"""Merge-queue throughput at simulated agent-driven PR volumes.

Measures real per-commit test-suite duration and a real repeated-run flake
rate against a real repository (`itsdangerous`'s actual `main` history and
its actual `pytest` suite), then simulates three queue policies over that
history: serial FIFO, batch-of-N with bisection on failure, and a
W-worker speculative queue that discards and reruns work invalidated by an
earlier rejection. The simulation layer is real-input-driven but not itself
a real execution: durations come from actually checking out and running the
suite at real commits; which attempts are treated as "broken" is a seeded
synthetic parameter swept explicitly, because this repository's own history
never contains a broken commit on `main` by construction. Every number in
`results.json["summary"]` is labeled `measured` (from real subprocess runs)
or `simulated` (computed by the policy simulator over those measurements).
"""
from __future__ import annotations

import argparse
import heapq
import json
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def checkout(repo: Path, sha: str) -> None:
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-f", sha], check=True)


def recent_first_parent_commits(repo: Path, count: int) -> list[str]:
    out = git(repo, "log", "--first-parent", "-n", str(count), "--pretty=%H", "main")
    return list(reversed(out.splitlines()))


def run_suite(repo: Path, python: str) -> tuple[bool, float]:
    start = time.perf_counter()
    result = subprocess.run([python, "-m", "pytest", "-q"], cwd=repo, capture_output=True, text=True)
    duration = time.perf_counter() - start
    return result.returncode == 0, duration


def measure_commit_durations(repo: Path, python: str, commits: list[str]) -> dict[str, dict]:
    durations = {}
    for sha in commits:
        checkout(repo, sha)
        ok, dur = run_suite(repo, python)
        durations[sha] = {"duration_s": dur, "suite_passed": ok}
    return durations


def measure_flake_rate(repo: Path, python: str, sha: str, repeats: int) -> tuple[float, list[bool]]:
    checkout(repo, sha)
    outcomes = []
    for _ in range(repeats):
        ok, _dur = run_suite(repo, python)
        outcomes.append(ok)
    fails = sum(1 for o in outcomes if not o)
    return fails / len(outcomes), outcomes


# --- Simulation layer: pure functions over measured durations, no subprocesses below here. ---

def build_prs(commits: list[str], durations: dict[str, dict], broken_rate: float, seed: int) -> list[dict]:
    rng = random.Random(seed)
    return [
        {"index": i, "sha": sha, "duration_s": durations[sha]["duration_s"], "broken": rng.random() < broken_rate}
        for i, sha in enumerate(commits)
    ]


def arrival_times(n: int, base_interval_s: float, multiplier: float) -> list[float]:
    interval = base_interval_s / multiplier
    return [i * interval for i in range(n)]


def simulate_serial(prs: list[dict], arrivals: list[float]) -> dict:
    clock = 0.0
    ci_seconds = 0.0
    merged, rejected = [], []
    for pr, arrival in zip(prs, arrivals):
        start = max(clock, arrival)
        finish = start + pr["duration_s"]
        ci_seconds += pr["duration_s"]
        clock = finish
        if pr["broken"]:
            rejected.append({"index": pr["index"], "finish_s": finish})
        else:
            merged.append({"index": pr["index"], "finish_s": finish, "wait_s": finish - arrival})
    return {"strategy": "serial", "makespan_s": clock, "ci_seconds": ci_seconds, "jobs": len(prs),
            "merged": merged, "rejected": rejected}


def bisect_broken(prs_slice: list[dict]) -> tuple[set[int], float, int]:
    """Binary search a contiguous PR range for broken members. Each probe's cost is the
    real measured duration of running the suite at that sub-range's own terminal real
    commit (bisection here never invents a commit outside real history)."""
    if len(prs_slice) == 1:
        broken = {prs_slice[0]["index"]} if prs_slice[0]["broken"] else set()
        return broken, 0.0, 0
    mid = len(prs_slice) // 2
    left, right = prs_slice[:mid], prs_slice[mid:]
    broken: set[int] = set()
    extra_s = left[-1]["duration_s"]
    extra_jobs = 1
    if any(p["broken"] for p in left):
        b, s, j = bisect_broken(left)
        broken |= b
        extra_s += s
        extra_jobs += j
    extra_s += right[-1]["duration_s"]
    extra_jobs += 1
    if any(p["broken"] for p in right):
        b, s, j = bisect_broken(right)
        broken |= b
        extra_s += s
        extra_jobs += j
    return broken, extra_s, extra_jobs


def simulate_batch(prs: list[dict], arrivals: list[float], batch_size: int) -> dict:
    clock = 0.0
    ci_seconds = 0.0
    jobs = 0
    merged, rejected = [], []
    for start_i in range(0, len(prs), batch_size):
        batch = prs[start_i:start_i + batch_size]
        batch_arrival = max(arrivals[start_i:start_i + batch_size])
        start = max(clock, batch_arrival)
        batch_duration = batch[-1]["duration_s"]
        finish = start + batch_duration
        ci_seconds += batch_duration
        jobs += 1
        if not any(p["broken"] for p in batch):
            for p in batch:
                merged.append({"index": p["index"], "finish_s": finish, "wait_s": finish - arrivals[p["index"]]})
            clock = finish
        else:
            broken_idx, extra_s, extra_jobs = bisect_broken(batch)
            finish2 = finish + extra_s
            ci_seconds += extra_s
            jobs += extra_jobs
            for p in batch:
                target = rejected if p["index"] in broken_idx else merged
                entry = {"index": p["index"], "finish_s": finish2}
                if target is merged:
                    entry["wait_s"] = finish2 - arrivals[p["index"]]
                target.append(entry)
            clock = finish2
    return {"strategy": f"batch-{batch_size}", "makespan_s": clock, "ci_seconds": ci_seconds, "jobs": jobs,
            "merged": merged, "rejected": rejected}


def simulate_speculative(prs: list[dict], arrivals: list[float], workers: int) -> dict:
    """W-worker speculative queue: a free worker starts the next undispatched or
    invalidated PR as soon as it has arrived, without waiting for earlier PRs to be
    confirmed. A PR's speculative pass is only final once every earlier PR is
    confirmed non-broken; a rejection invalidates every later PR that already passed
    or is in flight, forcing a real rerun (same measured duration charged again)."""
    n = len(prs)
    worker_free_at = [0.0] * workers
    # index -> "pending" | "in_flight" | "passed" | "rejected"
    state = ["pending"] * n
    confirmed_up_to = -1  # highest index i such that prs[0..i] are all confirmed merged
    dispatch_cursor = 0
    ci_seconds = 0.0
    jobs = 0
    attempts: dict[int, int] = {i: 0 for i in range(n)}
    # events: (time, kind, payload); kind in {"start_check", "finish"}
    events: list[tuple[float, int, str, int]] = []
    seq = 0

    def try_dispatch(now: float) -> None:
        nonlocal dispatch_cursor, seq
        for w in range(workers):
            if worker_free_at[w] > now:
                continue
            # Prefer the smallest-index PR that is pending or needs a rerun and has arrived.
            candidate = None
            for i in range(n):
                if state[i] in ("pending",) and arrivals[i] <= now:
                    candidate = i
                    break
            if candidate is None:
                continue
            state[candidate] = "in_flight"
            duration = prs[candidate]["duration_s"]
            finish_t = now + duration
            worker_free_at[w] = finish_t
            attempts[candidate] += 1
            seq += 1
            heapq.heappush(events, (finish_t, seq, "finish", candidate))

    def advance_confirmed(now: float, merged: list[dict], rejected: list[dict]) -> None:
        # The frontier only advances in real commit order: a PR isn't truly merged
        # until every earlier PR has resolved (merged or rejected), even if its own
        # CI check finished earlier. A rejected PR resolves the frontier without
        # itself merging; it was already recorded in `rejected` when it failed.
        nonlocal confirmed_up_to
        while confirmed_up_to + 1 < n and state[confirmed_up_to + 1] in ("passed", "rejected"):
            confirmed_up_to += 1
            i = confirmed_up_to
            if state[i] == "passed":
                merged.append({"index": i, "finish_s": now, "wait_s": now - arrivals[i], "attempts": attempts[i]})

    merged: list[dict] = []
    rejected: list[dict] = []

    now = 0.0
    try_dispatch(now)
    while events or any(state[i] == "pending" for i in range(n)):
        if not events:
            # nothing in flight but unarrived work remains; jump to next arrival
            future = min(a for i, a in enumerate(arrivals) if state[i] == "pending")
            now = future
            try_dispatch(now)
            continue
        now, _seq, kind, idx = heapq.heappop(events)
        ci_seconds += prs[idx]["duration_s"]
        jobs += 1
        if prs[idx]["broken"]:
            state[idx] = "rejected"
            rejected.append({"index": idx, "finish_s": now, "attempts": attempts[idx]})
            # Invalidate every later PR that already passed speculatively or is in flight:
            # its tested base assumed idx would be part of history.
            for j in range(idx + 1, n):
                if state[j] in ("passed",):
                    state[j] = "pending"  # will be re-attempted against the corrected base
                elif state[j] == "in_flight":
                    # mark so its eventual finish is discarded and it is requeued
                    state[j] = "tainted_in_flight"
        else:
            if state[idx] == "in_flight":
                state[idx] = "passed"
            elif state[idx] == "tainted_in_flight":
                state[idx] = "pending"  # discard this result; it assumed a base that no longer holds
        advance_confirmed(now, merged, rejected)
        try_dispatch(now)
        if not events:
            remaining_pending = [i for i in range(n) if state[i] == "pending"]
            if remaining_pending:
                future = min(arrivals[i] for i in remaining_pending)
                now = max(now, future)
                try_dispatch(now)

    makespan = max([m["finish_s"] for m in merged] + [r["finish_s"] for r in rejected] + [0.0])
    return {"strategy": f"speculative-{workers}", "makespan_s": makespan, "ci_seconds": ci_seconds, "jobs": jobs,
            "merged": merged, "rejected": rejected}


def summarize(result: dict, n: int) -> dict:
    waits = [m["wait_s"] for m in result["merged"]]
    return {
        "strategy": result["strategy"],
        "makespan_s": round(result["makespan_s"], 3),
        "ci_seconds": round(result["ci_seconds"], 3),
        "jobs_run": result["jobs"],
        "merged_count": len(result["merged"]),
        "rejected_count": len(result["rejected"]),
        "throughput_prs_per_ci_minute": round(len(result["merged"]) / (result["ci_seconds"] / 60), 4) if result["ci_seconds"] else None,
        "mean_wait_s": round(statistics.mean(waits), 3) if waits else None,
        "p95_wait_s": round(sorted(waits)[max(0, int(0.95 * len(waits)) - 1)], 3) if waits else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True, help="Path to a prepared itsdangerous checkout on main.")
    parser.add_argument("--python", default=sys.executable, help="Interpreter with itsdangerous installed editable, pytest and freezegun.")
    parser.add_argument("--commits", type=int, default=64)
    parser.add_argument("--flake-repeats", type=int, default=30)
    parser.add_argument("--multipliers", type=float, nargs="+", default=[1.0, 2.0, 5.0, 10.0])
    parser.add_argument("--broken-rates", type=float, nargs="+", default=[0.0, 0.05, 0.15, 0.30])
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=[4, 8])
    parser.add_argument("--speculative-workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    args = parser.parse_args()

    repo = Path(args.repo_dir).resolve()
    starting_branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    try:
        commits = recent_first_parent_commits(repo, args.commits)
        durations = measure_commit_durations(repo, args.python, commits)
        real_failures = sum(1 for d in durations.values() if not d["suite_passed"])

        flake_rate, flake_outcomes = measure_flake_rate(repo, args.python, commits[-1], args.flake_repeats)
    finally:
        checkout(repo, starting_branch if starting_branch != "HEAD" else "main")

    mean_duration = statistics.mean(d["duration_s"] for d in durations.values())
    base_interval_s = mean_duration  # 1x = one PR arrives roughly every mean-suite-duration

    broken_rates = sorted(set(args.broken_rates) | {round(flake_rate, 4)})
    runs = []
    for multiplier in args.multipliers:
        arrivals = arrival_times(len(commits), base_interval_s, multiplier)
        for broken_rate in broken_rates:
            prs = build_prs(commits, durations, broken_rate, args.seed)
            results = [simulate_serial(prs, arrivals)]
            for bs in args.batch_sizes:
                results.append(simulate_batch(prs, arrivals, bs))
            results.append(simulate_speculative(prs, arrivals, args.speculative_workers))
            runs.append({
                "multiplier": multiplier,
                "broken_rate": broken_rate,
                "broken_rate_source": "measured_flake" if abs(broken_rate - flake_rate) < 1e-9 else "swept_synthetic",
                "strategies": [summarize(r, len(commits)) for r in results],
            })

    summary = {
        "repo": "https://github.com/pallets/itsdangerous",
        "commits_measured": len(commits),
        "measured": {
            "mean_suite_duration_s": round(mean_duration, 4),
            "min_suite_duration_s": round(min(d["duration_s"] for d in durations.values()), 4),
            "max_suite_duration_s": round(max(d["duration_s"] for d in durations.values()), 4),
            "real_suite_failures_across_history": real_failures,
            "flake_repeats_at_head": args.flake_repeats,
            "flake_rate_at_head": round(flake_rate, 4),
        },
        "simulated": {
            "multipliers": args.multipliers,
            "broken_rates_swept": broken_rates,
            "batch_sizes": args.batch_sizes,
            "speculative_workers": args.speculative_workers,
        },
    }

    report = {"summary": summary, "durations": durations, "flake_outcomes": flake_outcomes, "runs": runs}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

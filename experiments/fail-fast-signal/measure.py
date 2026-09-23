"""How much of a real red round's wall-clock is spent after the failure is
already known — waiting for other, unrelated checks on the same push to
finish — versus the time to the first failure signal itself.

Reuses `ci-convergence`'s round-discovery method: real workflow runs on a
same-repo PR branch, grouped by `head_sha` into chronological rounds. Real CI
structure fans a push out two different ways — vscode triggers several
separate workflow files per push (Telemetry, CodeQL, ...), while deno runs
one workflow with a 100+ job matrix — so this measures at job granularity,
fetching every job across every run in the round. Nothing is executed:
`completed_at` on the real job objects already says when each one finished.
For every red round this compares `signal_s` (elapsed time from the round's
start to the FIRST failing job reporting its conclusion) against `settle_s`
(elapsed time to the LAST job in the round reporting, success or failure).
The gap between them is real wall-clock a developer who waits for "all
checks done" before looking spends watching jobs that can no longer change
the outcome.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BOT_BRANCH_PREFIXES = ("dependabot/", "renovate/", "actions/", "release/", "backport/")


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def gh_json(path: str) -> dict:
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout)


def gh_paginate(path: str, key: str, max_pages: int) -> list[dict]:
    items: list[dict] = []
    sep = "&" if "?" in path else "?"
    for page in range(1, max_pages + 1):
        data = gh_json(f"{path}{sep}page={page}")
        chunk = data[key]
        if not chunk:
            break
        items.extend(chunk)
        if len(chunk) < 100:
            break
    return items


def candidate_prs(repo: str, limit: int, max_list_pages: int) -> list[dict]:
    found = []
    for page in range(1, max_list_pages + 1):
        prs = gh_json(f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc"
                       f"&per_page=100&page={page}")
        if not prs:
            break
        for pr in prs:
            if not pr.get("merged_at"):
                continue
            head, base = pr["head"], pr["base"]
            if not head.get("repo") or head["repo"]["full_name"] != base["repo"]["full_name"]:
                continue
            if head["ref"].startswith(BOT_BRANCH_PREFIXES):
                continue
            found.append(pr)
            if len(found) >= limit:
                return found
        if len(prs) < 100:
            break
    return found


def branch_runs(repo: str, ref: str, max_pages: int) -> list[dict]:
    total = gh_json(f"repos/{repo}/actions/runs?branch={ref}&per_page=1").get("total_count", 0)
    if total == 0 or total > 200:
        return []
    return gh_paginate(f"repos/{repo}/actions/runs?branch={ref}&per_page=100", "workflow_runs",
                        max_pages)


def group_rounds(runs: list[dict], opened_at: datetime, merged_at: datetime) -> list[dict]:
    window_start = opened_at - timedelta(minutes=30)
    by_sha: dict[str, list[dict]] = {}
    for r in runs:
        if r["status"] != "completed":
            continue
        started = parse_ts(r["created_at"])
        if started < window_start or started > merged_at:
            continue
        by_sha.setdefault(r["head_sha"], []).append(r)
    rounds = []
    for sha, group in by_sha.items():
        conclusions = {r["conclusion"] for r in group}
        if conclusions <= {"cancelled", "skipped", "stale"} or "action_required" in conclusions:
            continue
        red = any(r["conclusion"] in ("failure", "timed_out") for r in group)
        rounds.append({
            "start": min(parse_ts(r["created_at"]) for r in group),
            "conclusion": "red" if red else "green",
            "runs": group,
        })
    rounds.sort(key=lambda r: r["start"])
    return rounds


def round_jobs(repo: str, round_: dict) -> list[dict]:
    """All jobs across every workflow run in the round. Real CI structure
    varies: some repos fan a push out into several separate workflow files
    (vscode: Telemetry, CodeQL, ...), others run one workflow with a large
    internal job matrix (deno's `ci`, 130+ jobs in one run). Job-level
    granularity is the one that generalizes across both."""
    jobs = []
    for r in round_["runs"]:
        jobs.extend(gh_paginate(f"repos/{repo}/actions/runs/{r['id']}/jobs?per_page=100",
                                 "jobs", max_pages=5))
    return [j for j in jobs if j.get("completed_at")]


def signal_and_settle(repo: str, round_: dict) -> tuple[float, float, int, int] | None:
    jobs = round_jobs(repo, round_)
    if len(jobs) < 2:
        return None
    failed = [j for j in jobs if j["conclusion"] in ("failure", "timed_out")]
    if not failed:
        return None
    start = round_["start"]
    signal_s = min((parse_ts(j["completed_at"]) - start).total_seconds() for j in failed)
    settle_s = max((parse_ts(j["completed_at"]) - start).total_seconds() for j in jobs)
    return signal_s, settle_s, len(jobs), len(failed)


def measure_pr(repo: str, pr: dict, max_run_pages: int) -> list[dict]:
    ref = pr["head"]["ref"]
    runs = branch_runs(repo, ref, max_run_pages)
    if not runs:
        return []
    rounds = group_rounds(runs, parse_ts(pr["created_at"]), parse_ts(pr["merged_at"]))
    green_idx = next((i for i, r in enumerate(rounds) if r["conclusion"] == "green"), None)
    scored = rounds[: green_idx + 1] if green_idx is not None else rounds
    out = []
    for r in scored:
        if r["conclusion"] != "red":
            continue
        result = signal_and_settle(repo, r)
        if result is None:
            continue
        signal_s, settle_s, n_jobs, n_failed = result
        out.append({
            "repo": repo, "pr": pr["number"],
            "jobs_in_round": n_jobs, "failed_jobs_in_round": n_failed,
            "signal_s": round(signal_s, 1), "settle_s": round(settle_s, 1),
            "tail_s": round(settle_s - signal_s, 1),
        })
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repos", nargs="+", default=[
        "microsoft/vscode", "denoland/deno", "python/mypy",
        "sveltejs/svelte", "pola-rs/polars",
    ])
    parser.add_argument("--sample-prs", type=int, default=40)
    parser.add_argument("--max-list-pages", type=int, default=8)
    parser.add_argument("--max-run-pages", type=int, default=3)
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    args = parser.parse_args()

    rounds_out: list[dict] = []
    for repo in args.repos:
        cands = candidate_prs(repo, args.sample_prs, args.max_list_pages)
        for pr in cands:
            try:
                found = measure_pr(repo, pr, args.max_run_pages)
            except RuntimeError as e:
                print(f"  skip {repo}#{pr['number']}: {e}", file=sys.stderr)
                continue
            for rec in found:
                rounds_out.append(rec)
                print(f"  {repo}#{pr['number']}: signal {rec['signal_s']:.0f}s, "
                      f"settle {rec['settle_s']:.0f}s, tail {rec['tail_s']:.0f}s "
                      f"({rec['failed_jobs_in_round']}/{rec['jobs_in_round']} jobs failed)",
                      file=sys.stderr)

    tails = [r["tail_s"] for r in rounds_out]
    signals = [r["signal_s"] for r in rounds_out]
    settles = [r["settle_s"] for r in rounds_out]

    def pct(x: list[float]) -> dict:
        if not x:
            return {"mean": None, "median": None, "p90": None}
        s = sorted(x)
        return {
            "mean": round(statistics.mean(x), 1),
            "median": round(statistics.median(x), 1),
            "p90": round(s[min(len(s) - 1, int(len(s) * 0.9))], 1),
        }

    zero_tail = sum(1 for t in tails if t <= 1.0)
    summary = {
        "measured": True,
        "repos": args.repos,
        "multi_run_red_rounds_found": len(rounds_out),
        "signal_s": pct(signals),
        "settle_s": pct(settles),
        "tail_s": pct(tails),
        "rounds_with_no_tail_fraction": round(zero_tail / len(tails), 4) if tails else None,
        "mean_tail_as_fraction_of_settle": round(
            statistics.mean(t / s for t, s in zip(tails, settles) if s > 0), 4
        ) if any(s > 0 for s in settles) else None,
    }

    report = {"summary": summary, "rounds": rounds_out}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

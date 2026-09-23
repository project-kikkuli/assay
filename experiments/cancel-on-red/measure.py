"""What first-failure-first cancel-on-red actually buys, in real CI compute
minutes, on real red rounds.

[`fail-fast-signal`](../fail-fast-signal/) already showed the *wall-clock*
side of this: a real gap between a round's first failure and its last job
finishing. That gap is watched time, not necessarily wasted runner time —
GitHub Actions jobs already run concurrently, so cancelling a still-running
sibling doesn't make the failure land any sooner. What cancellation actually
buys is real **compute minutes**: the runner-seconds a still-in-flight job
would have burned after the round is already known to be red. This measures
that, using the same real job start/end timestamps: for every red round,
compares real total job-seconds consumed against what a policy that cancels
every job still running at the first failure signal would have consumed
(a job that starts after the signal never runs at all; one straddling it is
truncated to the signal instant; one that already finished is unaffected).

```sh
python3 experiments/cancel-on-red/measure.py \
  --repos microsoft/vscode denoland/deno python/mypy sveltejs/svelte pola-rs/polars \
  --sample-prs 20
```
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
    jobs = []
    for r in round_["runs"]:
        jobs.extend(gh_paginate(f"repos/{repo}/actions/runs/{r['id']}/jobs?per_page=100",
                                 "jobs", max_pages=5))
    return [j for j in jobs if j.get("started_at") and j.get("completed_at")]


def compute_savings(jobs: list[dict]) -> dict | None:
    failed = [j for j in jobs if j["conclusion"] in ("failure", "timed_out")]
    if not failed or len(jobs) < 2:
        return None
    signal = min(parse_ts(j["completed_at"]) for j in failed)
    total_s = 0.0
    kept_s = 0.0
    for j in jobs:
        start, end = parse_ts(j["started_at"]), parse_ts(j["completed_at"])
        dur = (end - start).total_seconds()
        total_s += dur
        if start >= signal:
            kept_s += 0.0  # would never have been scheduled under cancel-on-red
        elif end <= signal:
            kept_s += dur  # already finished before the signal; unaffected
        else:
            kept_s += (signal - start).total_seconds()  # truncated mid-flight
    return {
        "jobs": len(jobs),
        "real_total_job_s": round(total_s, 1),
        "cancel_on_red_job_s": round(kept_s, 1),
        "saved_job_s": round(total_s - kept_s, 1),
    }


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
        jobs = round_jobs(repo, r)
        result = compute_savings(jobs)
        if result is None:
            continue
        out.append({"repo": repo, "pr": pr["number"], **result})
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repos", nargs="+", default=[
        "microsoft/vscode", "denoland/deno", "python/mypy",
        "sveltejs/svelte", "pola-rs/polars",
    ])
    parser.add_argument("--sample-prs", type=int, default=20)
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
                print(f"  {repo}#{pr['number']}: {rec['real_total_job_s']:.0f}s real, "
                      f"{rec['cancel_on_red_job_s']:.0f}s under cancel-on-red, "
                      f"saves {rec['saved_job_s']:.0f}s ({rec['jobs']} jobs)", file=sys.stderr)

    saved = [r["saved_job_s"] for r in rounds_out]
    real = [r["real_total_job_s"] for r in rounds_out]

    def pct(x: list[float]) -> dict:
        if not x:
            return {"mean": None, "median": None, "p90": None}
        s = sorted(x)
        return {
            "mean": round(statistics.mean(x), 1),
            "median": round(statistics.median(x), 1),
            "p90": round(s[min(len(s) - 1, int(len(s) * 0.9))], 1),
        }

    total_real = sum(real)
    total_saved = sum(saved)
    summary = {
        "measured": True,
        "repos": args.repos,
        "red_rounds_with_multiple_jobs": len(rounds_out),
        "saved_job_s_per_round": pct(saved),
        "real_total_job_s_per_round": pct(real),
        "fleet_wide_compute_saved_fraction": round(total_saved / total_real, 4)
        if total_real else None,
        "note": "compute-minutes saved by a fail-fast concurrency-cancel policy, "
                "not developer wall-clock (that's fail-fast-signal's tail_s: jobs "
                "already run concurrently on GitHub Actions, so cancelling a "
                "sibling doesn't make the failure land sooner, only cheaper).",
    }

    report = {"summary": summary, "rounds": rounds_out}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

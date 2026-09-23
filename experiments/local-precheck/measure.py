"""What a cheap, deterministic pre-push local check (lint/format/type-check)
would have saved, measured against real red CI rounds.

Reuses `ci-convergence`'s round-discovery method (group real workflow runs on
a same-repo PR branch by `head_sha` into chronological rounds) on a fresh,
independent PR sample, then narrows to rounds whose failure classifies as
`lint_or_format` or `type_check` — the two categories a local linter/type
checker reproduces deterministically and near-instantly, no test fixtures or
service dependencies required. For each such round it measures two real,
independently-timestamped quantities: the failing job's own execution
duration (`completed_at - started_at`, a same-command proxy for the local
run: no queueing, no other matrix legs, no artifact upload), and the full
cost that round imposed on the convergence loop (its own CI wall time plus
the real gap until the next push). The difference is what a pre-push hook
would have removed from that PR's timeline, using real job timings, not an
assumed check duration.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

BOT_BRANCH_PREFIXES = ("dependabot/", "renovate/", "actions/", "release/", "backport/")
CAUSE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("infra_or_setup", ("timeout", "timed out", "network", "docker", "runner", "set up job",
                         "checkout", "cache", "download", "econnreset", "econnrefused",
                         "enotfound", "503", "502", "artifact", "disk", "no space")),
    ("lint_or_format", ("lint", "eslint", "prettier", "format", "style", "ruff", "flake8",
                         "clippy", "rustfmt", "rubocop", "stylelint")),
    ("type_check", ("type check", "typecheck", "mypy", "tsc", " types", "pyright")),
    ("build_or_compile", ("build", "compile", "bundle", "webpack", "rollup", "cargo build")),
    ("test", ("test", "spec", "jest", "pytest", "vitest", "mocha", "unit", "integration",
              "e2e")),
]
LOCAL_CATCHABLE = {"lint_or_format", "type_check"}
MAX_PR_AGE_DAYS = 3  # exclude long-lived/stacked PRs: their between-round gaps are
                      # dominated by review/merge-queue latency, not fix time
GAP_CAP_S = 4 * 3600  # cap a single between-round gap at 4h: a longer gap plausibly
                       # reflects the developer being away, not blocked on this round


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
            "end": max(parse_ts(r["updated_at"]) for r in group),
            "conclusion": "red" if red else "green",
            "runs": group,
        })
    rounds.sort(key=lambda r: r["start"])
    return rounds


def failed_jobs(repo: str, run_id: int) -> list[dict]:
    jobs = gh_paginate(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", "jobs",
                        max_pages=5)
    return [j for j in jobs if j["conclusion"] in ("failure", "timed_out")]


def classify_and_time(repo: str, round_: dict) -> tuple[str, float | None]:
    """Returns (category, max real duration in seconds among failed jobs)."""
    failed_runs = [r for r in round_["runs"] if r["conclusion"] in ("failure", "timed_out")]
    text_parts, durations = [], []
    for r in failed_runs:
        text_parts.append(r["name"].lower())
        try:
            jobs = failed_jobs(repo, r["id"])
        except RuntimeError:
            continue
        for j in jobs:
            text_parts.append(j["name"].lower())
            for step in j.get("steps") or []:
                if step.get("conclusion") in ("failure", "timed_out"):
                    text_parts.append(step["name"].lower())
            if j.get("started_at") and j.get("completed_at"):
                durations.append((parse_ts(j["completed_at"]) - parse_ts(j["started_at"]))
                                  .total_seconds())
    blob = " ".join(text_parts)
    category = "other_or_unclassified"
    for cat, keywords in CAUSE_KEYWORDS:
        if any(k in blob for k in keywords):
            category = cat
            break
    return category, (max(durations) if durations else None)


def measure_pr(repo: str, pr: dict, max_run_pages: int) -> list[dict]:
    """Returns one record per locally-catchable red round found in this PR."""
    days_open = (parse_ts(pr["merged_at"]) - parse_ts(pr["created_at"])).total_seconds() / 86400
    if days_open > MAX_PR_AGE_DAYS:
        return []
    ref = pr["head"]["ref"]
    runs = branch_runs(repo, ref, max_run_pages)
    if not runs:
        return []
    rounds = group_rounds(runs, parse_ts(pr["created_at"]), parse_ts(pr["merged_at"]))
    green_idx = next((i for i, r in enumerate(rounds) if r["conclusion"] == "green"), None)
    if green_idx is None:
        return []
    scored = rounds[: green_idx + 1]
    out = []
    for i, r in enumerate(scored):
        if r["conclusion"] != "red":
            continue
        category, job_duration_s = classify_and_time(repo, r)
        if category not in LOCAL_CATCHABLE or job_duration_s is None:
            continue
        next_start = scored[i + 1]["start"]  # always exists: a red round is never `scored[-1]`
        gap_s = min((next_start - r["end"]).total_seconds(), GAP_CAP_S)
        round_cost_s = (r["end"] - r["start"]).total_seconds() + gap_s
        out.append({
            "repo": repo, "pr": pr["number"], "category": category,
            "round_cost_s": round(round_cost_s, 1),
            "round_ci_duration_s": round((r["end"] - r["start"]).total_seconds(), 1),
            "local_check_duration_s": round(job_duration_s, 1),
            "savings_s": round(round_cost_s - job_duration_s, 1),
        })
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repos", nargs="+", default=[
        "denoland/deno", "sveltejs/svelte", "python/mypy",
    ])
    parser.add_argument("--sample-prs", type=int, default=25)
    parser.add_argument("--max-list-pages", type=int, default=8)
    parser.add_argument("--max-run-pages", type=int, default=3)
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    args = parser.parse_args()

    prs_examined = 0
    caught_rounds: list[dict] = []
    for repo in args.repos:
        cands = candidate_prs(repo, args.sample_prs, args.max_list_pages)
        for pr in cands:
            prs_examined += 1
            try:
                found = measure_pr(repo, pr, args.max_run_pages)
            except RuntimeError as e:
                print(f"  skip {repo}#{pr['number']}: {e}", file=sys.stderr)
                continue
            for rec in found:
                caught_rounds.append(rec)
                print(f"  {repo}#{pr['number']}: {rec['category']} red round, "
                      f"cost {rec['round_cost_s']:.0f}s, local {rec['local_check_duration_s']:.0f}s, "
                      f"saves {rec['savings_s']:.0f}s", file=sys.stderr)

    by_category = Counter(r["category"] for r in caught_rounds)
    savings = [r["savings_s"] for r in caught_rounds]
    summary = {
        "measured": True,
        "repos": args.repos,
        "prs_examined": prs_examined,
        "locally_catchable_red_rounds_found": len(caught_rounds),
        "red_rounds_by_category": dict(by_category),
        "rounds_per_100_prs": round(len(caught_rounds) / prs_examined * 100, 2) if prs_examined else None,
        "savings_s_per_caught_round": {
            "mean": round(statistics.mean(savings), 1) if savings else None,
            "median": round(statistics.median(savings), 1) if savings else None,
            "min": round(min(savings), 1) if savings else None,
            "max": round(max(savings), 1) if savings else None,
        },
        "estimated_hours_saved_per_100_prs": round(
            (len(caught_rounds) / prs_examined * 100) * statistics.mean(savings) / 3600, 2
        ) if prs_examined and savings else None,
        "local_check_duration_s": {
            "mean": round(statistics.mean(r["local_check_duration_s"] for r in caught_rounds), 1)
            if caught_rounds else None,
        },
    }

    report = {"summary": summary, "caught_rounds": caught_rounds}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

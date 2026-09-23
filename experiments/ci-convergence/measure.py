"""CI convergence ground truth from real public GitHub Actions history.

For a sample of real merged pull requests on same-repo branches (so the
branch name alone recovers full push history, including force-pushes, via
`GET /repos/{repo}/actions/runs?branch=...`), groups workflow runs by
`head_sha` into "rounds" (one round per distinct push that triggered CI),
orders them chronologically, and reports: how many rounds until the first
all-green round, wall-clock from the first round's start to that round's
end, how much of that wall-clock was CI actually executing versus the gap
between one round finishing and the next starting (the developer noticing,
fixing, and re-pushing), and — for every red round — a job/step-name-based
classification of why it was red. Every API call goes through the real
`gh api` CLI against real GitHub history; no run outcome, timestamp, or job
name is synthesized.
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
    # Order matters: first match wins when a round's failed job/step text
    # matches more than one category.
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
        chunk = data[key] if key else data
        if not chunk:
            break
        items.extend(chunk)
        if len(chunk) < 100:
            break
    return items


def candidate_prs(repo: str, limit: int, max_list_pages: int) -> list[dict]:
    """Recent merged PRs whose head branch lived in the base repo itself
    (so its full push history is recoverable by branch name) and whose
    branch name doesn't look like a recurring bot/automation branch."""
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
            ref = head["ref"]
            if ref.startswith(BOT_BRANCH_PREFIXES):
                continue
            found.append(pr)
            if len(found) >= limit:
                return found
        if len(prs) < 100:
            break
    return found


def branch_run_count(repo: str, ref: str) -> int:
    data = gh_json(f"repos/{repo}/actions/runs?branch={ref}&per_page=1")
    return data.get("total_count", 0)


def branch_runs(repo: str, ref: str, max_pages: int) -> list[dict]:
    return gh_paginate(f"repos/{repo}/actions/runs?branch={ref}&per_page=100",
                        "workflow_runs", max_pages)


def group_rounds(runs: list[dict], opened_at: datetime, merged_at: datetime) -> list[dict]:
    """Groups runs into rounds, restricted to the PR's own open window. A
    same-repo branch can carry CI history from long before the PR that
    finally opened against it (private iteration, or an old branch reused);
    a 30-minute lookback keeps a push made just ahead of `Open PR` without
    pulling in unrelated earlier history."""
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
        if conclusions <= {"cancelled", "skipped", "stale"}:
            continue  # superseded before any verdict landed; not a scored round
        if "action_required" in conclusions:
            return []  # gated/approval-blocked branch; not a normal dev fix loop
        red = any(r["conclusion"] in ("failure", "timed_out") for r in group)
        rounds.append({
            "head_sha": sha,
            "start": min(parse_ts(r["created_at"]) for r in group),
            "queue_ready": min(parse_ts(r.get("run_started_at") or r["created_at"]) for r in group),
            "end": max(parse_ts(r["updated_at"]) for r in group),
            "conclusion": "red" if red else "green",
            "runs": group,
        })
    rounds.sort(key=lambda r: r["start"])
    return rounds


def classify_round(repo: str, round_: dict) -> tuple[str, list[str]]:
    failed_runs = [r for r in round_["runs"] if r["conclusion"] in ("failure", "timed_out")]
    text_parts = []
    for r in failed_runs:
        text_parts.append(r["name"].lower())
        try:
            jobs = gh_paginate(f"repos/{repo}/actions/runs/{r['id']}/jobs?per_page=100",
                                "jobs", max_pages=5)
        except RuntimeError:
            continue
        for j in jobs:
            if j["conclusion"] not in ("failure", "timed_out"):
                continue
            text_parts.append(j["name"].lower())
            for step in j.get("steps") or []:
                if step.get("conclusion") in ("failure", "timed_out"):
                    text_parts.append(step["name"].lower())
    blob = " ".join(text_parts)
    for category, keywords in CAUSE_KEYWORDS:
        if any(k in blob for k in keywords):
            return category, text_parts
    return "other_or_unclassified", text_parts


def measure_pr(repo: str, pr: dict, max_run_pages: int, classify: bool) -> dict | None:
    ref = pr["head"]["ref"]
    total = branch_run_count(repo, ref)
    if total == 0 or total > 200:
        return None  # no CI, or a reused/long-lived branch name (not this PR's own loop)
    runs = branch_runs(repo, ref, max_run_pages)
    rounds = group_rounds(runs, parse_ts(pr["created_at"]), parse_ts(pr["merged_at"]))
    if not rounds:
        return None
    green_idx = next((i for i, r in enumerate(rounds) if r["conclusion"] == "green"), None)
    scored = rounds[: green_idx + 1] if green_idx is not None else rounds
    working_s = sum((r["end"] - r["start"]).total_seconds() for r in scored)
    queue_s = sum((r["queue_ready"] - r["start"]).total_seconds() for r in scored)
    waiting_s = sum(
        max(0.0, (scored[i + 1]["start"] - scored[i]["end"]).total_seconds())
        for i in range(len(scored) - 1)
    )
    wall_s = (scored[-1]["end"] - scored[0]["start"]).total_seconds() if scored else 0.0
    causes = []
    if classify:
        for r in scored:
            if r["conclusion"] == "red":
                category, evidence = classify_round(repo, r)
                causes.append({"category": category, "evidence": evidence[:6]})
    days_open = (parse_ts(pr["merged_at"]) - parse_ts(pr["created_at"])).total_seconds() / 86400
    return {
        "repo": repo,
        "pr": pr["number"],
        "branch": ref,
        "days_pr_open": round(days_open, 2),
        "rounds_total": len(rounds),
        "rounds_to_green": (green_idx + 1) if green_idx is not None else None,
        "reached_green": green_idx is not None,
        "wall_clock_s": round(wall_s, 1),
        "ci_working_s": round(working_s, 1),
        "runner_queue_s": round(queue_s, 1),
        "between_round_waiting_s": round(waiting_s, 1),
        "red_causes": causes,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repos", nargs="+", default=[
        "microsoft/vscode", "denoland/deno", "python/mypy",
        "sveltejs/svelte", "pola-rs/polars",
    ])
    parser.add_argument("--sample-prs", type=int, default=20)
    parser.add_argument("--max-list-pages", type=int, default=6)
    parser.add_argument("--max-run-pages", type=int, default=3)
    parser.add_argument("--no-classify", action="store_true")
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    args = parser.parse_args()

    per_pr: list[dict] = []
    for repo in args.repos:
        cands = candidate_prs(repo, args.sample_prs * 3, args.max_list_pages)
        kept = 0
        for pr in cands:
            if kept >= args.sample_prs:
                break
            try:
                m = measure_pr(repo, pr, args.max_run_pages, classify=not args.no_classify)
            except RuntimeError as e:
                print(f"  skip {repo}#{pr['number']}: {e}", file=sys.stderr)
                continue
            if m is None:
                continue
            per_pr.append(m)
            kept += 1
            print(f"  {repo}#{pr['number']}: {m['rounds_total']} rounds, "
                  f"green at {m['rounds_to_green']}, wall {m['wall_clock_s']:.0f}s",
                  file=sys.stderr)

    converged = [p for p in per_pr if p["reached_green"]]
    multi_round = [p for p in converged if p["rounds_total"] > 1]
    fast_multi_round = [p for p in multi_round if p["days_pr_open"] <= 3]

    cause_counts: Counter[str] = Counter()
    for p in per_pr:
        for c in p["red_causes"]:
            cause_counts[c["category"]] += 1
    total_causes = sum(cause_counts.values())

    def pct(x: list[float]) -> dict:
        if not x:
            return {"mean": None, "median": None, "p90": None}
        s = sorted(x)
        return {
            "mean": round(statistics.mean(x), 1),
            "median": round(statistics.median(x), 1),
            "p90": round(s[min(len(s) - 1, int(len(s) * 0.9))], 1),
        }

    summary = {
        "measured": True,
        "repos": args.repos,
        "prs_sampled": len(per_pr),
        "prs_reached_green": len(converged),
        "prs_never_observed_green": len(per_pr) - len(converged),
        "rounds_to_green_histogram": dict(Counter(p["rounds_to_green"] for p in converged)),
        "rounds_to_green": pct([p["rounds_to_green"] for p in converged]),
        "single_round_fraction": round(
            sum(1 for p in converged if p["rounds_to_green"] == 1) / len(converged), 4
        ) if converged else None,
        "wall_clock_s_all_converged": pct([p["wall_clock_s"] for p in converged]),
        "wall_clock_s_multi_round_only": pct([p["wall_clock_s"] for p in multi_round]),
        "ci_working_s_multi_round_only": pct([p["ci_working_s"] for p in multi_round]),
        "between_round_waiting_s_multi_round_only": pct(
            [p["between_round_waiting_s"] for p in multi_round]
        ),
        "waiting_fraction_of_wall_clock_multi_round_only": round(
            statistics.mean(
                p["between_round_waiting_s"] / p["wall_clock_s"]
                for p in multi_round if p["wall_clock_s"] > 0
            ), 4
        ) if any(p["wall_clock_s"] > 0 for p in multi_round) else None,
        "runner_queue_fraction_of_ci_working_multi_round_only": round(
            statistics.mean(
                p["runner_queue_s"] / p["ci_working_s"]
                for p in multi_round if p["ci_working_s"] > 0
            ), 4
        ) if any(p["ci_working_s"] > 0 for p in multi_round) else None,
        "fast_iteration_multi_round_only": {
            "note": "same multi-round PRs, restricted to days_pr_open <= 3 "
                    "(excludes long-lived/stacked PRs where between-round time "
                    "is dominated by review/merge-queue latency, not fix time)",
            "n": len(fast_multi_round),
            "wall_clock_s": pct([p["wall_clock_s"] for p in fast_multi_round]),
            "ci_working_s": pct([p["ci_working_s"] for p in fast_multi_round]),
            "between_round_waiting_s": pct(
                [p["between_round_waiting_s"] for p in fast_multi_round]
            ),
            "waiting_fraction_of_wall_clock": round(
                statistics.mean(
                    p["between_round_waiting_s"] / p["wall_clock_s"]
                    for p in fast_multi_round if p["wall_clock_s"] > 0
                ), 4
            ) if any(p["wall_clock_s"] > 0 for p in fast_multi_round) else None,
        },
        "red_round_causes": {
            "total_classified": total_causes,
            "counts": dict(cause_counts),
            "fractions": {k: round(v / total_causes, 4) for k, v in cause_counts.items()}
            if total_causes else {},
        },
    }

    report = {"summary": summary, "prs": per_pr}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

"""Re-run the merge-queue policy comparison at a MEASURED real broken rate,
not a synthetic sweep.

`measure.py` had to sweep a synthetic broken-PR rate because `itsdangerous`'s
own real commit history never contains a broken commit on `main`. This script
gets a real rate a different way: it fetches pytest's actual GitHub Actions
history for its `test` workflow (the public REST API, no token required) —
every real `push` run on `main`, with its real conclusion — and computes the
real historical failure rate directly from that. It then re-runs the exact
same `simulate_serial` / `simulate_batch` / `simulate_speculative` functions
from `measure.py`, unmodified, against `itsdangerous`'s already-measured real
per-commit durations (read from `results.json`, not re-executed) at that one
real rate instead of a swept parameter.

The duration source (itsdangerous) and the broken-rate source (pytest) are
two different real repositories — this script does not claim they are the
same population, only that both numbers are independently measured, real,
and labeled as to their real origin.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from measure import (  # noqa: E402
    arrival_times, build_prs, simulate_batch, simulate_serial,
    simulate_speculative, summarize,
)

API_ROOT = "https://api.github.com"


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "assay-agent-throughput"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def find_workflow_id(owner: str, repo: str, workflow_name: str) -> int:
    data = fetch_json(f"{API_ROOT}/repos/{owner}/{repo}/actions/workflows")
    for w in data["workflows"]:
        if w["name"] == workflow_name:
            return w["id"]
    raise SystemExit(f"workflow {workflow_name!r} not found in {owner}/{repo}")


def fetch_push_runs(owner: str, repo: str, workflow_id: int, branch: str, max_pages: int) -> list[dict]:
    runs = []
    for page in range(1, max_pages + 1):
        url = f"{API_ROOT}/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs?per_page=100&event=push&page={page}"
        try:
            data = fetch_json(url)
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"GitHub API request failed ({exc.code}): {url}\n{exc.read()[:500]!r}") from exc
        batch = data.get("workflow_runs", [])
        if not batch:
            break
        runs.extend(batch)
        if len(batch) < 100:
            break
        time.sleep(0.2)  # be polite to the unauthenticated rate limit
    return [r for r in runs if r.get("head_branch") == branch]


def real_failure_rate(runs: list[dict]) -> dict:
    conclusions: dict[str, int] = {}
    for r in runs:
        conclusions[r["conclusion"]] = conclusions.get(r["conclusion"], 0) + 1
    cancelled = conclusions.get("cancelled", 0)
    failures = conclusions.get("failure", 0)
    non_cancelled = len(runs) - cancelled
    return {
        "runs_fetched": len(runs),
        "conclusions": conclusions,
        "failure_rate_all_runs": round(failures / len(runs), 4) if runs else None,
        "failure_rate_excluding_cancelled": round(failures / non_cancelled, 4) if non_cancelled else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--durations-source", default=str(Path(__file__).with_name("results.json")),
                         help="Existing merge-queue-throughput results.json holding real per-commit durations.")
    parser.add_argument("--rate-owner", default="pytest-dev")
    parser.add_argument("--rate-repo", default="pytest")
    parser.add_argument("--rate-workflow-name", default="test")
    parser.add_argument("--rate-branch", default="main")
    parser.add_argument("--max-pages", type=int, default=7)
    parser.add_argument("--multipliers", type=float, nargs="+", default=[1.0, 2.0, 5.0, 10.0])
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=[4, 8])
    parser.add_argument("--speculative-workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--offline-rate", type=float, default=None,
                         help="Skip the live API call and use this rate instead (for reruns without network access); still labeled as the source that produced it in the README, not as freshly fetched.")
    parser.add_argument("--out", default=str(Path(__file__).with_name("results-real-rate.json")))
    args = parser.parse_args()

    source = json.loads(Path(args.durations_source).read_text())
    durations = source["durations"]
    commits = list(durations.keys())

    if args.offline_rate is not None:
        rate_info = {"source": "offline_rate_argument", "failure_rate_excluding_cancelled": args.offline_rate}
        broken_rate = args.offline_rate
    else:
        workflow_id = find_workflow_id(args.rate_owner, args.rate_repo, args.rate_workflow_name)
        runs = fetch_push_runs(args.rate_owner, args.rate_repo, workflow_id, args.rate_branch, args.max_pages)
        rate_info = real_failure_rate(runs)
        rate_info["source"] = f"https://github.com/{args.rate_owner}/{args.rate_repo} actions workflow {args.rate_workflow_name!r}, push events on {args.rate_branch!r}"
        broken_rate = rate_info["failure_rate_excluding_cancelled"]

    mean_duration = statistics.mean(d["duration_s"] for d in durations.values())
    base_interval_s = mean_duration

    runs_out = []
    for multiplier in args.multipliers:
        arrivals = arrival_times(len(commits), base_interval_s, multiplier)
        prs = build_prs(commits, durations, broken_rate, args.seed)
        results = [simulate_serial(prs, arrivals)]
        for bs in args.batch_sizes:
            results.append(simulate_batch(prs, arrivals, bs))
        results.append(simulate_speculative(prs, arrivals, args.speculative_workers))
        runs_out.append({
            "multiplier": multiplier,
            "broken_rate": broken_rate,
            "strategies": [summarize(r, len(commits)) for r in results],
        })

    summary = {
        "durations_repo": "https://github.com/pallets/itsdangerous",
        "durations_commits": len(commits),
        "broken_rate_measured": rate_info,
        "broken_rate_used": broken_rate,
        "multipliers": args.multipliers,
        "batch_sizes": args.batch_sizes,
        "speculative_workers": args.speculative_workers,
    }
    report = {"summary": summary, "runs": runs_out}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

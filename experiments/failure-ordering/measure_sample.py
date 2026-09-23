"""Statistical time-to-first-failure ordering measurement across a real,
multi-repo sample of regression/fix pairs (see `mine_cases.py` and
`cases/*.json`; n=1 build was `measure.py` / `scenarios.json`, kept as-is).

For each of N real cases (pytest + click, `cases/*.json`), this runs a real
`-x` pytest subprocess per strategy against the repo's real, full test
directory, using `order_plugin.py`'s real collection-reordering hook -- the
same mechanism `measure.py` already validated on the n=1 case. Test COUNT
(`tests_run_before_stop`) is the metric this script optimizes for measuring
correctly; wall time is recorded too but is not the primary comparison,
per instruction, since the mining/measurement machine has been contended
throughout this lane's runs.

Strategies:
- `default`        -- pytest's own collection order.
- `duration_asc`   -- ascending duration from ONE real reference run per
  repo (the repo's current default-branch tip, unmodified) -- a real prior
  timing profile, not measured fresh per case (that would need N full-suite
  runs per repo; durations for tests outside the case's own overlay file(s)
  don't depend on which bug is being tested).
- `proximity`      -- the case's own overlaid file(s) first (score 0),
  everything else default order.
- `recent_failure` -- the chronologically nearest EARLIER case in the same
  repo's real failing nodeids run first (approximating "yesterday's CI
  cache"); the earliest case per repo has no prior and falls back to
  default order (recorded, not skipped).

`../pytest-order-plugin/assay_order.py` implements the actual combined
(recent-failure, proximity, duration) policy as a real, standalone plugin;
`replay_plugin.py` in this directory runs it for real, in chronological
order, sharing one pytest cache across the sample the way a real CI history
would.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_PYTHONPATH = str(HERE)


def git(repo_dir: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True, text=True, check=True
    ).stdout.strip()


def checkout_case(repo_dir: str, case: dict) -> None:
    subprocess.run(["git", "checkout", "--quiet", "--force", "--detach", case["base_ref"]], cwd=repo_dir, check=True)
    subprocess.run(["git", "clean", "--quiet", "-fd", "--", *case["overlay_paths"]], cwd=repo_dir, check=True)
    for path in case["overlay_paths"]:
        content = subprocess.run(
            ["git", "show", f"{case['overlay_ref']}:{path}"], cwd=repo_dir, capture_output=True, text=True, check=True
        ).stdout
        (Path(repo_dir) / path).write_text(content)


def run_full(repo_dir: str, python: str, test_root: str, report_path: Path, timeout: int) -> dict:
    if report_path.exists():
        report_path.unlink()
    env = dict(os.environ, PYTHONPATH=PLUGIN_PYTHONPATH, ASSAY_REPORT_PATH=str(report_path))
    env.pop("ASSAY_ORDER_SCORES", None)
    proc = subprocess.run(
        [python, "-m", "pytest", test_root, "-q", "-p", "order_plugin", "-o", "filterwarnings="],
        cwd=repo_dir, capture_output=True, text=True, env=env, timeout=timeout,
    )
    results = {}
    if report_path.exists():
        for line in report_path.read_text().splitlines():
            rec = json.loads(line)
            results[rec["nodeid"]] = {"outcome": rec["outcome"], "duration": rec["duration"]}
    return {"returncode": proc.returncode, "results": results}


def run_ordered_stop(repo_dir: str, python: str, test_root: str, scores: dict | None, report_path: Path, timeout: int) -> dict:
    if report_path.exists():
        report_path.unlink()
    env = dict(os.environ, PYTHONPATH=PLUGIN_PYTHONPATH, ASSAY_REPORT_PATH=str(report_path))
    scores_path = report_path.with_suffix(".scores.json")
    if scores:
        scores_path.write_text(json.dumps(scores))
        env["ASSAY_ORDER_SCORES"] = str(scores_path)
    else:
        env.pop("ASSAY_ORDER_SCORES", None)
    start = time.monotonic()
    proc = subprocess.run(
        [python, "-m", "pytest", test_root, "-q", "-x", "-p", "order_plugin", "-o", "filterwarnings="],
        cwd=repo_dir, capture_output=True, text=True, env=env, timeout=timeout,
    )
    elapsed = time.monotonic() - start
    tests_run, first_failure = 0, None
    if report_path.exists():
        for line in report_path.read_text().splitlines():
            rec = json.loads(line)
            tests_run += 1
            if rec["outcome"] == "failed" and first_failure is None:
                first_failure = rec["nodeid"]
    return {"wall_s": round(elapsed, 3), "returncode": proc.returncode, "tests_run_before_stop": tests_run, "first_failure": first_failure}


def build_duration_scores(reference_results: dict) -> dict:
    return {nodeid: rec["duration"] for nodeid, rec in reference_results.items()}


def build_priority_scores(nodeids: list[str]) -> dict:
    return {nodeid: i for i, nodeid in enumerate(nodeids)}


def measure_case(repo_dir: str, python: str, test_root: str, case: dict, reference: dict, prior_case: dict | None, work: Path, case_tag: str, timeout: int) -> dict:
    checkout_case(repo_dir, case)

    # `default` also gives us the real, current collected nodeid list.
    default_run = run_ordered_stop(repo_dir, python, test_root, None, work / f"{case_tag}_default.jsonl", timeout)

    default_report = work / f"{case_tag}_default.jsonl"
    collected_this_run = [json.loads(l)["nodeid"] for l in default_report.read_text().splitlines()] if default_report.exists() else []

    if default_run["tests_run_before_stop"] == 0 and default_run["first_failure"] is None:
        # A whole-suite collection error unrelated to this case's own
        # overlay (a version-drift incompatibility elsewhere in an old
        # commit's tree, distinct from the real regression under test) --
        # excluded rather than silently reported as a 0-test win.
        return {
            "case": {"base_ref": case["base_ref"], "overlay_paths": case["overlay_paths"], "fix_subject": case["fix_subject"], "failing_count": len(case["failing_nodeids"])},
            "excluded_reason": "whole_suite_collection_error",
        }

    # The reference run's durations exclude this case's overlay file(s),
    # since their content (and so their real durations) just changed to the
    # overlay's; those tests are simply absent from `duration_scores` and
    # keep default position among themselves (order_plugin's documented
    # behavior for unscored items), which is exactly their real relative
    # order within that one file -- consistent with `duration_asc` in the
    # n=1 build, just without a second, expensive full run per case to
    # re-measure their durations fresh.
    overlay_set = set(case["overlay_paths"])
    duration_scores = {k: v for k, v in build_duration_scores(reference["results"]).items() if reference["file_by_nodeid"].get(k) not in overlay_set}
    duration_run = run_ordered_stop(repo_dir, python, test_root, duration_scores, work / f"{case_tag}_duration.jsonl", timeout)

    proximity_scores = {nodeid: 0 for nodeid in collected_this_run if nodeid.split("::", 1)[0] in overlay_set}
    proximity_run = run_ordered_stop(repo_dir, python, test_root, proximity_scores, work / f"{case_tag}_proximity.jsonl", timeout)

    if prior_case is not None:
        recent_scores = build_priority_scores(prior_case["failing_nodeids"])
    else:
        recent_scores = None
    recent_run = run_ordered_stop(repo_dir, python, test_root, recent_scores, work / f"{case_tag}_recent.jsonl", timeout)

    return {
        "case": {"base_ref": case["base_ref"], "overlay_paths": case["overlay_paths"], "fix_subject": case["fix_subject"], "failing_count": len(case["failing_nodeids"])},
        "has_prior": prior_case is not None,
        "strategies": {
            "default": default_run,
            "duration_asc": duration_run,
            "proximity": proximity_run,
            "recent_failure": recent_run,
        },
    }


def run_reference(repo_dir: str, python: str, test_root: str, work: Path, timeout: int) -> dict:
    subprocess.run(["git", "checkout", "--quiet", "--force", "main"], cwd=repo_dir, check=True)
    ref = run_full(repo_dir, python, test_root, work / "reference.jsonl", timeout)
    file_by_nodeid = {nodeid: nodeid.split("::", 1)[0] for nodeid in ref["results"]}
    return {"results": ref["results"], "file_by_nodeid": file_by_nodeid, "returncode": ref["returncode"], "test_count": len(ref["results"])}


def summarize(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    sorted_v = sorted(values)
    return {
        "n": len(values),
        "median": statistics.median(sorted_v),
        "min": sorted_v[0],
        "max": sorted_v[-1],
        "mean": round(statistics.mean(sorted_v), 2),
        "stdev": round(statistics.stdev(sorted_v), 2) if len(sorted_v) > 1 else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--test-root", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--repo-tag", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text())
    cases.sort(key=lambda c: c["base_commit_date"])

    work = HERE / "_work_sample" / args.repo_tag
    work.mkdir(parents=True, exist_ok=True)

    print(f"[{args.repo_tag}] reference run", file=sys.stderr)
    reference = run_reference(args.repo_dir, args.python, args.test_root, work, args.timeout)
    print(f"[{args.repo_tag}] reference: {reference['test_count']} tests, rc={reference['returncode']}", file=sys.stderr)

    per_case = []
    for i, case in enumerate(cases):
        prior = cases[i - 1] if i > 0 else None
        print(f"[{args.repo_tag}] case {i + 1}/{len(cases)}: {case['fix_subject'][:60]}", file=sys.stderr)
        result = measure_case(args.repo_dir, args.python, args.test_root, case, reference, prior, work, f"case{i}", args.timeout)
        per_case.append(result)
        if "excluded_reason" in result:
            print(f"    EXCLUDED: {result['excluded_reason']}", file=sys.stderr)
            continue
        s = result["strategies"]
        print(f"    default={s['default']['tests_run_before_stop']} duration_asc={s['duration_asc']['tests_run_before_stop']} proximity={s['proximity']['tests_run_before_stop']} recent_failure={s['recent_failure']['tests_run_before_stop']}", file=sys.stderr)

    valid = [r for r in per_case if "excluded_reason" not in r]
    summary = {}
    for name in ("default", "duration_asc", "proximity", "recent_failure"):
        counts = [r["strategies"][name]["tests_run_before_stop"] for r in valid]
        beats_default = sum(
            1 for r in valid
            if r["strategies"][name]["tests_run_before_stop"] < r["strategies"]["default"]["tests_run_before_stop"]
        )
        summary[name] = {
            "tests_run_before_stop": summarize(counts),
            "cases_strictly_beating_default": beats_default,
            "cases_total": len(valid),
        }

    report = {
        "measured": True,
        "repo_tag": args.repo_tag,
        "reference_test_count": reference["test_count"],
        "excluded_count": len(per_case) - len(valid),
        "valid_case_count": len(valid),
        "summary": summary,
        "cases": per_case,
    }
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()

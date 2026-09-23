"""Replay the real sample through the real `assay_order.py` plugin, in
chronological order, sharing one pytest cache across the whole replay --
the same way a real repo's CI history accumulates a `lastfailed` cache and
this plugin's own duration cache run over run. This is the mechanism
measurement `measure_sample.py`'s per-factor scores stand in for: here
nothing is scored by this experiment's code, `assay_order.py` computes its
own ordering live from the plugin's real `git diff` and the real pytest
cache on disk.

For each case (`checkout_case`, same real pre-fix-commit + real fix-test
overlay as `measure_sample.py`), the overlay is left as a real uncommitted
change relative to the checkout, so the plugin's own `git diff --name-only
HEAD` proximity factor sees it without any special-casing. `order_plugin.py`
is loaded alongside `assay_order.py` purely for its passive per-test report
(no scores set, so it never reorders); `assay_order.py` does the real
reordering and the real `--assay-fail-fast` stop.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ORDER_PLUGIN_PYTHONPATH = str(HERE)
REAL_PLUGIN_PYTHONPATH = str(HERE.parent / "pytest-order-plugin")


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


def run_real_plugin(repo_dir: str, python: str, test_root: str, cache_dir: Path, report_path: Path, timeout: int) -> dict:
    if report_path.exists():
        report_path.unlink()
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join([ORDER_PLUGIN_PYTHONPATH, REAL_PLUGIN_PYTHONPATH]),
        ASSAY_REPORT_PATH=str(report_path),
    )
    env.pop("ASSAY_ORDER_SCORES", None)
    start = time.monotonic()
    proc = subprocess.run(
        [
            python, "-m", "pytest", test_root, "-q",
            "-p", "order_plugin", "-p", "assay_order",
            "--assay-fail-fast", "--assay-diff-ref=HEAD",
            "-o", f"cache_dir={cache_dir}",
            "-o", "filterwarnings=",
        ],
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--test-root", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--repo-tag", required=True)
    parser.add_argument("--sample-results", required=True, help="This repo's measure_sample.py output, for the default-order comparison.")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text())
    cases.sort(key=lambda c: c["base_commit_date"])
    sample = json.loads(Path(args.sample_results).read_text())
    default_by_ref = {c["case"]["base_ref"]: c["strategies"]["default"]["tests_run_before_stop"] for c in sample["cases"] if "excluded_reason" not in c}

    work = HERE / "_work_replay" / args.repo_tag
    if work.exists():
        import shutil
        shutil.rmtree(work)
    work.mkdir(parents=True)
    cache_dir = work / "shared_pytest_cache"

    results = []
    for i, case in enumerate(cases):
        checkout_case(args.repo_dir, case)
        print(f"[{args.repo_tag}] replay {i + 1}/{len(cases)}: {case['fix_subject'][:60]}", file=sys.stderr)
        run = run_real_plugin(args.repo_dir, args.python, args.test_root, cache_dir, work / f"case{i}.jsonl", args.timeout)
        default_count = default_by_ref.get(case["base_ref"])
        results.append({
            "base_ref": case["base_ref"], "fix_subject": case["fix_subject"],
            "plugin_order": run, "default_tests_run_before_stop": default_count,
        })
        beat = "beats" if default_count is not None and run["tests_run_before_stop"] < default_count else "does not beat"
        print(f"    plugin_order={run['tests_run_before_stop']} vs default={default_count} ({beat} default)", file=sys.stderr)

    valid = [r for r in results if r["default_tests_run_before_stop"] is not None]
    beats = sum(1 for r in valid if r["plugin_order"]["tests_run_before_stop"] < r["default_tests_run_before_stop"])
    report = {
        "measured": True, "repo_tag": args.repo_tag, "cases_replayed": len(results),
        "cases_comparable_to_default": len(valid), "cases_plugin_beats_default": beats,
        "results": results,
    }
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

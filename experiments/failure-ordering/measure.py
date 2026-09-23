"""Time-to-first-failure under five real test orderings, on pytest's own
`testing/` suite (4,600+ tests) run against pytest's own real git history.

An agent iterating on a failing change re-runs the suite after every edit and
reads the first FAIL it sees; every test the runner executes before that
point is wasted wall-clock the agent spent waiting, not signal. This measures
how much of that wait five real orderings avoid, using real subprocess pytest
runs (collection, fixtures, and `-x` stop-on-first-failure are all real;
nothing here is replayed from a log).

`scenarios.json` names four real historical pytest commits by SHA: one
"target" (the failure the agent is trying to surface) and three "priors"
(what earlier CI runs would have shown). Each scenario checks out a real
pre-fix commit (`base_ref`) and overlays the corresponding real fix commit's
own regression test file(s) (`overlay_ref`) on top of it, unmodified --
"a developer just wrote the test that exposes a real, still-open bug".
Nothing here is a seeded or authored fault: the bug, the fix, and the test
that exposes it are all real pytest commits; only the *pairing* (this test
against that earlier tree) is constructed. Everything that isn't the
overlaid test file(s) is exactly what shipped at `base_ref`.

Every measured run uses `-p order_plugin` from this directory: unmodified
pytest, one collection-reordering hook (see `order_plugin.py`).

Strategies:
- `default`   — pytest's own collection order (file path, then definition
  order); every other strategy is scored relative to this.
- `duration_asc` — ascending real duration, from a real full baseline run at
  the target scenario (a team's own prior-CI timing data, not an estimate).
- `recently_failed` — real failures from the nearest prior scenario run
  first, in their original order; the rest keep default order. This is what
  `pytest --ff`/`--lf` would compute from a real cache.
- `historical_rate` — ranked by how many of the 3 prior scenarios a test
  failed on (ties broken toward the more recent prior); a test that failed
  on 0 priors is unscored and keeps default position.
- `proximity_<file>` — one strategy per distinct real-failure file among the
  target's own failures: that file's tests first, then the rest of its
  directory, then default order for everything else. Models an agent that
  just edited the module under test and reruns starting from what it
  touched.
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


def git(repo_dir: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True, text=True, check=True
    ).stdout.strip()


def checkout_scenario(repo_dir: str, scenario: dict) -> str:
    """Checks out `base_ref` clean, then overwrites `overlay_paths` with
    their real content at `overlay_ref` -- a real pre-fix tree plus a real
    fix commit's own regression test, nothing else changed. Returns the
    resolved base SHA."""
    base_sha = git(repo_dir, "rev-parse", scenario["base_ref"])
    subprocess.run(["git", "checkout", "--quiet", "--force", "--detach", base_sha], cwd=repo_dir, check=True)
    subprocess.run(["git", "clean", "--quiet", "-fd", "--", *scenario["overlay_paths"]], cwd=repo_dir, check=True)
    for path in scenario["overlay_paths"]:
        content = subprocess.run(
            ["git", "show", f"{scenario['overlay_ref']}:{path}"], cwd=repo_dir, capture_output=True, text=True, check=True
        ).stdout
        (Path(repo_dir) / path).write_text(content)
    return base_sha


def run_full(repo_dir: str, python: str, report_path: Path, timeout: int, testpath: str) -> dict:
    """A real, unordered, non-stopping full run of `testpath`. Returns
    {nodeid: {"outcome": ..., "duration": ...}} read back from the plugin's
    real per-test reports, plus wall time and the process return code."""
    if report_path.exists():
        report_path.unlink()
    env = dict(os.environ, PYTHONPATH=str(HERE), ASSAY_REPORT_PATH=str(report_path))
    env.pop("ASSAY_ORDER_SCORES", None)
    start = time.monotonic()
    proc = subprocess.run(
        [python, "-m", "pytest", testpath, "-q", "-p", "order_plugin"],
        cwd=repo_dir, capture_output=True, text=True, env=env, timeout=timeout,
    )
    elapsed = time.monotonic() - start
    results = {}
    if report_path.exists():
        for line in report_path.read_text().splitlines():
            rec = json.loads(line)
            results[rec["nodeid"]] = {"outcome": rec["outcome"], "duration": rec["duration"]}
    return {"wall_s": round(elapsed, 3), "returncode": proc.returncode, "results": results}


def run_ordered_stop_on_failure(repo_dir: str, python: str, scores: dict | None, report_path: Path, timeout: int, testpath: str) -> dict:
    """A real `-x` run: stop at the first failing test under the given
    per-nodeid score ordering (None = pytest's own default order). Returns
    real wall time to that stop and the nodeid that actually triggered it."""
    if report_path.exists():
        report_path.unlink()
    env = dict(os.environ, PYTHONPATH=str(HERE), ASSAY_REPORT_PATH=str(report_path))
    scores_path = report_path.with_suffix(".scores.json")
    if scores:
        scores_path.write_text(json.dumps(scores))
        env["ASSAY_ORDER_SCORES"] = str(scores_path)
    else:
        env.pop("ASSAY_ORDER_SCORES", None)
    start = time.monotonic()
    proc = subprocess.run(
        [python, "-m", "pytest", testpath, "-q", "-x", "-p", "order_plugin"],
        cwd=repo_dir, capture_output=True, text=True, env=env, timeout=timeout,
    )
    elapsed = time.monotonic() - start
    first_failure = None
    tests_run = 0
    if report_path.exists():
        for line in report_path.read_text().splitlines():
            rec = json.loads(line)
            tests_run += 1
            if rec["outcome"] == "failed" and first_failure is None:
                first_failure = rec["nodeid"]
    return {
        "wall_s": round(elapsed, 3),
        "returncode": proc.returncode,
        "tests_run_before_stop": tests_run,
        "first_failure": first_failure,
    }


def build_duration_scores(baseline_results: dict) -> dict:
    return {nodeid: rec["duration"] for nodeid, rec in baseline_results.items()}


def build_recently_failed_scores(prior_results: dict) -> dict:
    failed = [nodeid for nodeid, rec in prior_results.items() if rec["outcome"] == "failed"]
    return {nodeid: i for i, nodeid in enumerate(failed)}


def build_historical_rate_scores(prior_results_list: list[dict]) -> dict:
    """`prior_results_list` is ordered nearest-to-target first (index 0), so
    a smaller `most_recent_index` means the test failed in a prior closer to
    the target in history."""
    counts: dict[str, int] = {}
    most_recent_index: dict[str, int] = {}
    for idx, results in enumerate(prior_results_list):
        for nodeid, rec in results.items():
            if rec["outcome"] == "failed":
                counts[nodeid] = counts.get(nodeid, 0) + 1
                most_recent_index[nodeid] = min(most_recent_index.get(nodeid, idx), idx)
    # A test that failed more often runs first; among ties, the one whose
    # most recent failure is closest to the target runs first.
    ranked = sorted(counts, key=lambda n: (-counts[n], most_recent_index[n]))
    return {nodeid: i for i, nodeid in enumerate(ranked)}


def build_proximity_scores(all_nodeids: list[str], anchor_file: str) -> dict:
    anchor_dir = anchor_file.rsplit("/", 1)[0]
    scores = {}
    for nodeid in all_nodeids:
        file_part = nodeid.split("::", 1)[0]
        if file_part == anchor_file:
            scores[nodeid] = 0
        elif file_part.rsplit("/", 1)[0] == anchor_dir:
            scores[nodeid] = 1
    return scores


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True, help="A local pytest checkout (writable; will be checked out and overlaid repeatedly).")
    parser.add_argument("--python", default=sys.executable, help="Interpreter with the repo installed editable (`pip install -e .[dev]`).")
    parser.add_argument("--scenarios", default=str(HERE / "scenarios.json"))
    parser.add_argument("--testpath", default="testing", help="Restrict the pool for a smoke test, e.g. testing/python.")
    parser.add_argument("--baseline-timeout", type=int, default=900)
    parser.add_argument("--stop-timeout", type=int, default=900)
    parser.add_argument("--out", default=str(HERE / "results.json"))
    args = parser.parse_args()

    scenarios = json.loads(Path(args.scenarios).read_text())
    work = HERE / "_work"
    work.mkdir(exist_ok=True)

    print("target scenario", file=sys.stderr)
    target_base_sha = checkout_scenario(args.repo_dir, scenarios["target"])
    baseline = run_full(args.repo_dir, args.python, work / "baseline_target.jsonl", args.baseline_timeout, args.testpath)
    target_failures = sorted(n for n, r in baseline["results"].items() if r["outcome"] == "failed")
    print(f"target real failures: {len(target_failures)}", file=sys.stderr)
    if not target_failures:
        sys.exit("target scenario produced zero real failures; pick a different fix commit in scenarios.json")

    prior_runs = []
    prior_base_shas = []
    for i, prior in enumerate(scenarios["priors"]):
        print(f"prior[{i}]", file=sys.stderr)
        prior_base_shas.append(checkout_scenario(args.repo_dir, prior))
        prior_runs.append(run_full(args.repo_dir, args.python, work / f"baseline_prior{i}.jsonl", args.baseline_timeout, args.testpath))
        print(f"  real failures: {sum(1 for r in prior_runs[-1]['results'].values() if r['outcome'] == 'failed')}", file=sys.stderr)

    checkout_scenario(args.repo_dir, scenarios["target"])

    strategies: dict[str, dict | None] = {"default": None}
    strategies["duration_asc"] = build_duration_scores(baseline["results"])
    strategies["recently_failed"] = build_recently_failed_scores(prior_runs[0]["results"])
    strategies["historical_rate"] = build_historical_rate_scores([r["results"] for r in prior_runs])

    all_nodeids = list(baseline["results"].keys())
    anchor_files = sorted({n.split("::", 1)[0] for n in target_failures})
    for anchor in anchor_files:
        strategies[f"proximity_{anchor.replace('/', '_')}"] = build_proximity_scores(all_nodeids, anchor)

    strategy_results = {}
    for name, scores in strategies.items():
        print(f"strategy: {name}", file=sys.stderr)
        strategy_results[name] = run_ordered_stop_on_failure(
            args.repo_dir, args.python, scores, work / f"stop_{name}.jsonl", args.stop_timeout, args.testpath
        )

    report = {
        "measured": True,
        "target_base_sha": target_base_sha,
        "prior_base_shas": prior_base_shas,
        "baseline_full_run": {"wall_s": baseline["wall_s"], "returncode": baseline["returncode"], "test_count": len(baseline["results"])},
        "target_real_failure_count": len(target_failures),
        "target_real_failures": target_failures,
        "prior_full_runs": [
            {"sha": sha, "wall_s": run["wall_s"], "failure_count": sum(1 for r in run["results"].values() if r["outcome"] == "failed")}
            for sha, run in zip(prior_base_shas, prior_runs)
        ],
        "strategies": strategy_results,
    }
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps({k: v for k, v in report.items() if k != "target_real_failures"}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

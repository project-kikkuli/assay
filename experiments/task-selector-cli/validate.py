"""Validate the task selector against real regressions: for each real
(buggy base, real fix) pair, would the selector — evaluated on the real
fix's own source-only diff, at the real pre-fix manifest — have selected
the exact real test file that exposes the bug? Scores BOTH manifests: the
naive `test_*.py`-only one `diff-affected-fraction` already published, and
`task_select.py`'s real-`python_files`-aware correction, so the fix this
validation motivated is itself validated, not just asserted.

The four scenarios are the same real pytest commits `failure-ordering`
already established (its `scenarios.json`, read here, never edited): one
real regression (#14934, `approx()` on mismatched nested containers) plus
three real unrelated bug fixes, each a real (base_ref, overlay_ref,
overlay_paths) triple where `overlay_ref` is the real commit that fixed a
real bug and `overlay_paths` is the real test file(s) that expose it.

For each scenario this reads the real diff between `base_ref` and
`overlay_ref` restricted to real `src/_pytest/` files (the hypothetical
"developer fixed the bug" PR, before anyone thought to add or update a
test), builds each real explicit-input manifest at `base_ref` (before that
diff lands), and checks whether `overlay_paths` is among the tasks each
selector picks. A real regression whose real test a selector would have
skipped is a real miss, reported as such — not hidden in an aggregate.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import task_select  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "diff-affected-fraction"))
import measure as naive_manifest  # noqa: E402

FAILURE_ORDERING_SCENARIOS = Path(__file__).resolve().parents[1] / "failure-ordering" / "scenarios.json"


def load_scenarios() -> list[dict]:
    data = json.loads(FAILURE_ORDERING_SCENARIOS.read_text())
    return [data["target"]] + data["priors"]


def collect_only_count(repo: Path, python: str, targets: list[str]) -> int | None:
    import re
    import subprocess
    result = subprocess.run(
        [python, "-m", "pytest", "--collect-only", "-q", *targets],
        cwd=repo, capture_output=True, text=True,
    )
    match = re.search(r"^(\d+) tests? collected", result.stdout, re.MULTILINE)
    return int(match.group(1)) if match else None


def run_selected_real(repo: Path, python: str, targets: list[str]) -> float:
    """Real wall-clock time to actually execute (not just collect) the real
    selected subset."""
    import subprocess
    import time
    start = time.perf_counter()
    subprocess.run([python, "-m", "pytest", "-q", *targets], cwd=repo, capture_output=True, text=True)
    return time.perf_counter() - start


def score(build_manifest_fn, repo: Path, real_base: str, changed_src_files: set[str], overlay_paths: list[str]) -> dict:
    manifest = build_manifest_fn(repo, real_base) or {}
    selected = sorted(t for t, inputs in manifest.items() if inputs & changed_src_files)
    hit = any(p in selected for p in overlay_paths)
    return {"task_count_total": len(manifest), "selected_tasks": selected, "selected_count": len(selected), "hit": hit}


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--python", default=None, help="Interpreter for real --collect-only counts (default: skip real counts).")
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    args = parser.parse_args()

    repo = Path(args.repo_dir).resolve()
    scenarios = load_scenarios()
    git = task_select.git
    starting_branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")

    results = []
    try:
        for sc in scenarios:
            base_ref, overlay_ref, overlay_paths = sc["base_ref"], sc["overlay_ref"], sc["overlay_paths"]
            real_base = git(repo, "rev-parse", base_ref)

            changed_src = git(repo, "diff", "--name-only", real_base, overlay_ref, "--", "src/_pytest")
            changed_src_files = set(f for f in changed_src.splitlines() if f)
            if not changed_src_files:
                results.append({"description": sc["description"], "skipped": "no src/_pytest/ diff between base_ref and overlay_ref"})
                continue

            naive = score(naive_manifest.build_manifest, repo, real_base, changed_src_files, overlay_paths)
            corrected = score(task_select.build_manifest, repo, real_base, changed_src_files, overlay_paths)

            entry = {
                "description": sc["description"],
                "base_ref": base_ref,
                "real_base_sha": real_base,
                "overlay_ref": overlay_ref,
                "real_regression_test_files": overlay_paths,
                "source_only_diff": sorted(changed_src_files),
                "naive_test_star_manifest": naive,
                "corrected_python_files_manifest": corrected,
            }
            if args.python and corrected["hit"]:
                import subprocess
                subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-f", real_base], check=True)
                full_count = collect_only_count(repo, args.python, ["testing"])
                selected_count = collect_only_count(repo, args.python, corrected["selected_tasks"])
                if full_count is not None and selected_count is not None:
                    entry["real_full_suite_test_count"] = full_count
                    entry["real_selected_test_count"] = selected_count
                entry["real_selected_execution_s"] = round(run_selected_real(repo, args.python, corrected["selected_tasks"]), 4)
            results.append(entry)
    finally:
        git(repo, "checkout", "-q", "-f", starting_branch if starting_branch != "HEAD" else "main")

    scored = [r for r in results if "naive_test_star_manifest" in r]
    naive_misses = [r for r in scored if not r["naive_test_star_manifest"]["hit"]]
    corrected_misses = [r for r in scored if not r["corrected_python_files_manifest"]["hit"]]
    summary = {
        "measured": True,
        "repo": "https://github.com/pytest-dev/pytest",
        "scenarios_source": "experiments/failure-ordering/scenarios.json",
        "scenarios_scored": len(scored),
        "naive_manifest": {
            "misses": len(naive_misses),
            "miss_rate": round(len(naive_misses) / len(scored), 4) if scored else None,
            "missed_descriptions": [m["description"] for m in naive_misses],
        },
        "corrected_manifest": {
            "misses": len(corrected_misses),
            "miss_rate": round(len(corrected_misses) / len(scored), 4) if scored else None,
            "missed_descriptions": [m["description"] for m in corrected_misses],
        },
    }
    timed = [r for r in scored if "real_full_suite_test_count" in r]
    if timed:
        full = timed[0]["real_full_suite_test_count"]
        mean_selected = sum(r["real_selected_test_count"] for r in timed) / len(timed)
        summary["real_full_suite_test_count"] = full
        summary["mean_real_selected_test_count_on_hits"] = round(mean_selected, 2)
        summary["mean_test_count_reduction_fraction_on_hits"] = round(1 - mean_selected / full, 4) if full else None
    executed = [r for r in scored if "real_selected_execution_s" in r]
    if executed:
        summary["mean_real_selected_execution_s"] = round(sum(r["real_selected_execution_s"] for r in executed) / len(executed), 4)

    report = {"summary": summary, "scenarios": results}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

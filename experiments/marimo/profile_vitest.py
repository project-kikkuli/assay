#!/usr/bin/env python3
"""Profile direct, cache-disabled Vitest runs and a controlled source mutant."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


FOCUS_PATHS = [
    "frontend/src/core/cells/cells.ts",
    "frontend/src/core/cells/runs.ts",
    "frontend/src/core/state/jotai.ts",
    "frontend/src/core/runtime/runtime.ts",
]


def load_fresh_positive_report(
    report: Path, started_wall_ns: int
) -> tuple[dict[str, Any] | None, str]:
    try:
        if not report.exists() or report.stat().st_mtime_ns < started_wall_ns:
            return None, "unknown_missing_or_stale_report"
        data = json.loads(report.read_text(encoding="utf-8"))
        if int(data["numTotalTests"]) <= 0:
            return None, "unknown_empty_report"
        return data, "fresh_positive_report"
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None, "unknown_malformed_report"


def safe_env(work_dir: Path, timing_file: Path) -> dict[str, str]:
    cache = Path(tempfile.mkdtemp(prefix="marimo-vitest-profile-"))
    temp = cache / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL")
        if os.environ.get(key)
    }
    env.update(
        {
            "CI": "true",
            "MARIMO_SKIP_UPDATE_CHECK": "1",
            "NODE_OPTIONS": "",
            "npm_config_cache": str(cache / "npm-cache"),
            "npm_config_update_notifier": "false",
            "npm_config_fund": "false",
            "npm_config_audit": "false",
            "TMPDIR": str(temp),
            "VITEST_TIMING_FILE": str(timing_file),
        }
    )
    return env


def run_vitest(repo: Path, work_dir: Path, name: str, args: list[str]) -> dict[str, Any]:
    report = work_dir / f".{name}.json"
    timing_report = work_dir / f".{name}.timing.json"
    report.unlink(missing_ok=True)
    timing_report.unlink(missing_ok=True)
    reporter = Path(__file__).resolve().with_name("vitest_timing_reporter.mjs")
    command = [
        "npx",
        "--yes",
        "--package=pnpm@10.34.4",
        "pnpm",
        "--filter",
        "@marimo-team/frontend",
        "exec",
        "vitest",
        *args,
        "--reporter=json",
        "--outputFile",
        str(report),
        "--reporter",
        str(reporter),
    ]
    started = time.perf_counter()
    started_wall_ns = time.time_ns()
    process = subprocess.Popen(
        command,
        cwd=repo,
        env=safe_env(work_dir, timing_report),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        output, _ = process.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            output, _ = process.communicate()

    duration_s = round(time.perf_counter() - started, 3)
    result: dict[str, Any] = {
        "name": name,
        "command": [
            token.replace(str(repo), "<repo>").replace(str(work_dir), "<experiment>")
            .replace(str(reporter.parent), "<experiment>")
            for token in command
        ],
        "duration_s": duration_s,
        "status": "timeout" if timed_out else ("passed" if process.returncode == 0 else "failed"),
        "cache_mode": "Vitest direct --no-cache; Turbo not invoked",
        "counts": {},
        "files": [],
        "timing": {},
    }
    try:
        data, validity = load_fresh_positive_report(report, started_wall_ns)
        if data is None:
            result["status"] = "timeout" if timed_out else ("failed" if process.returncode else "unknown")
            result["validity"] = validity
            return result
        result["counts"] = {
            "test_suites": data["numTotalTestSuites"],
            "passed_suites": data["numPassedTestSuites"],
            "failed_suites": data["numFailedTestSuites"],
            "tests": data["numTotalTests"],
            "passed": data["numPassedTests"],
            "failed": data["numFailedTests"],
            "pending": data["numPendingTests"],
        }
        for file_result in data["testResults"]:
            assertions = file_result.get("assertionResults", [])
            result["files"].append(
                {
                    "path": Path(file_result["name"]).relative_to(repo).as_posix(),
                    "duration_ms": round(file_result["endTime"] - file_result["startTime"], 3),
                    "tests": len(assertions),
                    "failed": sum(item["status"] == "failed" for item in assertions),
                }
            )
        if timing_report.exists() and timing_report.stat().st_mtime_ns >= started_wall_ns:
            timing = json.loads(timing_report.read_text(encoding="utf-8"))
            totals = {
                key: round(sum(module.get(key, 0) for module in timing["modules"]), 3)
                for key in (
                    "environment_setup_ms",
                    "prepare_ms",
                    "transform_prepare_ms",
                    "collect_ms",
                    "setup_ms",
                    "tests_and_hooks_ms",
                )
            }
            result["timing"] = {"aggregate_module_ms": totals}
            for module in timing["modules"]:
                path = Path(module["path"]).relative_to(repo).as_posix()
                next(file for file in result["files"] if file["path"] == path).update(
                    {key: value for key, value in module.items() if key != "path"}
                )
        result["validity"] = validity
        if not timed_out and process.returncode == 0:
            result["status"] = "passed"
    except (OSError, KeyError, TypeError, ValueError):
        result["status"] = "timeout" if timed_out else ("failed" if process.returncode else "unknown")
        result["validity"] = "unknown_malformed_report"
    finally:
        report.unlink(missing_ok=True)
        timing_report.unlink(missing_ok=True)
    return result


def seeded_runs(repo: Path, work_dir: Path) -> dict[str, Any]:
    source = repo / "frontend/src/core/cells/runs.ts"
    original = source.read_text(encoding="utf-8")
    old = "if (!runId) {\n      return state;\n    }"
    mutant = "if (runId) {\n      return state;\n    }"
    if old not in original:
        raise RuntimeError("expected runs.ts guard was not found")
    source.write_text(original.replace(old, mutant, 1), encoding="utf-8")
    try:
        return {
            "mutant": "runs.ts run-id guard inverted",
            "related": run_vitest(
                repo,
                work_dir,
                "defect_related_runs",
                ["related", "src/core/cells/runs.ts", "--no-cache"],
            ),
            "all": run_vitest(repo, work_dir, "defect_all", ["run", "--no-cache"]),
        }
    finally:
        source.write_text(original, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path(os.environ.get("MARIMO", "marimo")),
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=Path("experiments/marimo/vitest-profile.json"),
    )
    args = parser.parse_args()
    repo = args.repo_dir.resolve()
    results_path = args.results.resolve()
    work_dir = results_path.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    all_run = run_vitest(repo, work_dir, "all_direct", ["run", "--no-cache"])
    related_run = run_vitest(
        repo,
        work_dir,
        "related_core",
        ["related", *[path.removeprefix("frontend/") for path in FOCUS_PATHS], "--no-cache"],
    )
    profile = {
        "project": "marimo",
        "revision": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            env=safe_env(work_dir, work_dir / ".git-timing.json"),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
        "focus_paths": FOCUS_PATHS,
        "all_direct": all_run,
        "related_core": related_run,
        "seeded_defect": seeded_runs(repo, work_dir),
        "notes": [
            "All runs invoke Vitest directly with --no-cache; Turbo is not used for these timings.",
            "The seeded source defect is restored in a finally block; tests are never modified.",
            "Only structured counts and relative test paths are retained; raw output is discarded.",
        ],
    }
    results_path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    checks = [all_run, related_run, profile["seeded_defect"]["related"], profile["seeded_defect"]["all"]]
    return 0 if all(item["validity"] == "fresh_positive_report" for item in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())

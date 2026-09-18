#!/usr/bin/env python3
"""Run a pinned marimo setup and genuine Python/frontend checks.

The runner records only structured summaries. Test output stays in memory and
is not copied into the Assay repository.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


PYTEST_COUNT = re.compile(r"(?P<count>\d+) (?P<kind>passed|failed|errors?|skipped|xfailed|xpassed)")


def normalize_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def classify_status(returncode: int | None, timed_out: bool, validity: str) -> str:
    if timed_out:
        return "timeout"
    if returncode not in (0, None):
        return "failed"
    if validity.startswith("unknown") or validity == "exit_code_only":
        return "unknown"
    return "passed"


def pytest_summary_validity(counts: dict[str, int]) -> str:
    return "pytest_summary" if sum(counts.values()) > 0 else "unknown_empty_summary"


def all_measured_passed(items: list[dict[str, Any]]) -> bool:
    return bool(items) and all(item["status"] == "passed" for item in items)


def run_command(
    name: str,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    repo: Path,
    experiment_dir: Path,
    report_path: Path | None = None,
) -> dict[str, Any]:
    if report_path:
        report_path.unlink(missing_ok=True)
    started = time.perf_counter()
    started_wall_ns = time.time_ns()
    timed_out = False
    completed: subprocess.Popen[str] | None = None
    try:
        completed = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        output, _ = completed.communicate(timeout=timeout)
        returncode = completed.returncode
    except subprocess.TimeoutExpired as exc:
        assert completed is not None
        try:
            try:
                os.killpg(completed.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            output, _ = completed.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(completed.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            output, _ = completed.communicate()
        output = normalize_output(output or exc.stdout)
        returncode = completed.returncode
        timed_out = True
    output = normalize_output(output)
    duration = time.perf_counter() - started

    counts: dict[str, int] = {}
    validity = "exit_code_only"
    if name == "python_tests":
        for match in PYTEST_COUNT.finditer(output):
            kind = match.group("kind").rstrip("s")
            counts["errors" if kind == "error" else kind] = int(match.group("count"))
        validity = pytest_summary_validity(counts)
    elif name.startswith("frontend_tests"):
        fresh_report = (
            report_path is not None
            and report_path.exists()
            and report_path.stat().st_mtime_ns >= started_wall_ns
        )
        if fresh_report:
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                counts = {
                    "test_suites": report["numTotalTestSuites"],
                    "passed_suites": report["numPassedTestSuites"],
                    "tests": report["numTotalTests"],
                    "passed": report["numPassedTests"],
                    "failed": report["numFailedTests"],
                    "pending": report["numPendingTests"],
                }
                validity = (
                    "fresh_positive_report"
                    if counts["tests"] > 0
                    else "unknown_empty_report"
                )
            except (OSError, KeyError, TypeError, ValueError):
                validity = "unknown_malformed_report"
        else:
            validity = "unknown_missing_or_stale_report"
        if report_path:
            report_path.unlink(missing_ok=True)

    cache_events = {
        "hits": output.count("cache hit"),
        "misses": output.count("cache miss"),
        "bypasses": output.count("cache bypass"),
    }
    if name.startswith("frontend_turbo"):
        validity = "turbo_cache_observed"

    return {
        "name": name,
        "command": [
            token.replace(str(repo), "<repo>").replace(str(experiment_dir), "<experiment>")
            for token in command
        ],
        "status": classify_status(returncode, timed_out, validity),
        "returncode": returncode,
        "duration_s": round(duration, 3),
        "counts": counts,
        "validity": validity,
        "turbo_cache": cache_events if name.startswith("frontend_turbo") else None,
    }


def git_revision(repo: Path, env: dict[str, str]) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path(os.environ.get("MARIMO", "marimo")),
        help="Pinned marimo checkout; defaults to $MARIMO or ./marimo",
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=Path("experiments/marimo/run-results.json"),
        help="Sanitized run summary path",
    )
    parser.add_argument("--mode", choices=("setup", "tests", "all"), default="all")
    parser.add_argument("--python-group", choices=("test", "test-optional"), default="test")
    parser.add_argument("--timeout-s", type=float, default=900.0)
    args = parser.parse_args()

    repo = args.repo_dir.resolve()
    results_path = args.results.resolve()
    experiment_dir = results_path.parent
    cache_parent = Path(os.environ.get("MARIMO_BENCHMARK_CACHE", tempfile.gettempdir()))
    cache_parent.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(tempfile.mkdtemp(prefix="marimo-benchmark-", dir=cache_parent))
    pnpm_store = cache_dir / "pnpm-store"
    uv_cache = cache_dir / "uv-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    pnpm_store.mkdir(parents=True, exist_ok=True)
    uv_cache.mkdir(parents=True, exist_ok=True)

    temp_dir = cache_dir / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL")
        if os.environ.get(key)
    }
    env.update(
        {
            "CI": "true",
            "MARIMO_SKIP_UPDATE_CHECK": "1",
            "UV_EXCLUDE_NEWER": "2026-09-18",
            "UV_CACHE_DIR": str(uv_cache),
            "PNPM_HOME": str(cache_dir / "pnpm-home"),
            "npm_config_cache": str(cache_dir / "npm-cache"),
            "npm_config_update_notifier": "false",
            "npm_config_fund": "false",
            "npm_config_audit": "false",
            "TMPDIR": str(temp_dir),
            "PYTHONHASHSEED": "0",
        }
    )
    vitest_report = experiment_dir / ".vitest-report.json"

    evidence: dict[str, Any] = {
        "project": "marimo",
        "revision": git_revision(repo, env),
        "runner_python": sys.version.split()[0],
        "benchmark_python": "3.12 via uv",
        "python_group": args.python_group,
        "setup": [],
        "tests": [],
        "notes": [
            "Python dependencies were resolved into a generated local uv.lock because upstream does not commit one at this revision.",
            "UV_EXCLUDE_NEWER is pinned to 2026-09-18 for repeatable public-index resolution.",
            "Frontend dependencies use the checked-in pnpm-lock.yaml with frozen resolution.",
            "The benchmark commands use Python 3.12 through uv; the runner interpreter version is reported separately.",
            "Counts are parsed from runner summaries; no raw worker transcript is persisted.",
            "Subprocess environment is an explicit allowlist with dedicated cache and temp directories; ambient credentials are not forwarded.",
            "Vitest runs remove and validate a fresh JSON report; Turbo probes record cache hit/miss/bypass text separately.",
        ],
    }

    if args.mode in ("setup", "all"):
        evidence["setup"] = [
            run_command(
                "python_lock",
                ["uv", "lock", "--python", "3.12"],
                repo,
                env,
                args.timeout_s,
                repo,
                experiment_dir,
            ),
            run_command(
                "python_install",
                [
                    "uv",
                    "sync",
                    "--locked",
                    "--python",
                    "3.12",
                    "--no-default-groups",
                    "--group",
                    args.python_group,
                ],
                repo,
                env,
                args.timeout_s,
                repo,
                experiment_dir,
            ),
            run_command(
                "frontend_install",
                [
                    "npx",
                    "--yes",
                    "--package=pnpm@10.34.4",
                    "pnpm",
                    "install",
                    "--frozen-lockfile",
                    "--store-dir",
                    str(pnpm_store),
                ],
                repo,
                env,
                args.timeout_s,
                repo,
                experiment_dir,
            ),
        ]

    if args.mode in ("tests", "all"):
        evidence["tests"] = [
            run_command(
                "python_tests",
                [
                    "uv",
                    "run",
                    "--locked",
                    "--python",
                    "3.12",
                    "--no-default-groups",
                    "--group",
                    args.python_group,
                    "pytest",
                ],
                repo,
                env,
                args.timeout_s,
                repo,
                experiment_dir,
            ),
            run_command(
                "frontend_tests_direct",
                [
                    "npx",
                    "--yes",
                    "--package=pnpm@10.34.4",
                    "pnpm",
                    "--filter",
                    "@marimo-team/frontend",
                    "exec",
                    "vitest",
                    "run",
                    "--no-cache",
                    "--reporter=json",
                    "--outputFile",
                    str(vitest_report),
                ],
                repo,
                env,
                args.timeout_s,
                repo,
                experiment_dir,
                vitest_report,
            ),
            run_command(
                "frontend_turbo_typecheck",
                [
                    "npx",
                    "--yes",
                    "--package=pnpm@10.34.4",
                    "pnpm",
                    "turbo",
                    "--filter",
                    "@marimo-team/frontend",
                    "typecheck",
                ],
                repo,
                env,
                args.timeout_s,
                repo,
                experiment_dir,
            ),
        ]

    results_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    measured = evidence["setup"] + evidence["tests"]
    return 0 if all_measured_passed(measured) else 1


if __name__ == "__main__":
    raise SystemExit(main())

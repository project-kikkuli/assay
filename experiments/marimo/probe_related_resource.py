#!/usr/bin/env python3
"""Compare Vitest related selection from a source module and generated JSON."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path


def run(repo: Path, temp: Path, name: str, target: str) -> dict[str, object]:
    report = temp / f"{name}.json"
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL")
        if os.environ.get(key)
    }
    env.update(
        {
            "CI": "true",
            "TMPDIR": str(temp / "tmp"),
            "npm_config_cache": str(temp / "npm-cache"),
            "npm_config_update_notifier": "false",
            "npm_config_fund": "false",
            "npm_config_audit": "false",
        }
    )
    (temp / "tmp").mkdir(exist_ok=True)
    command = [
        "npx",
        "--yes",
        "--package=pnpm@10.34.4",
        "pnpm",
        "--filter",
        "@marimo-team/llm-info",
        "exec",
        "vitest",
        "related",
        target,
        "--no-cache",
        "--reporter=json",
        "--outputFile",
        str(report),
    ]
    started = time.perf_counter()
    process = subprocess.Popen(
        command,
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        output, _ = process.communicate(timeout=10)
    result: dict[str, object] = {
        "target": target,
        "command": [token.replace(str(temp), "<temporary>") for token in command],
        "duration_s": round(time.perf_counter() - started, 3),
        "status": "passed" if process.returncode == 0 else "failed",
        "process_exit": process.returncode,
        "validity": "missing_or_stale_report",
        "admission": "unresolved_no_tests_selected",
        "counts": {},
        "selected_files": [],
    }
    if report.exists():
        data = json.loads(report.read_text(encoding="utf-8"))
        result["counts"] = {
            "test_suites": data["numTotalTestSuites"],
            "tests": data["numTotalTests"],
            "passed": data["numPassedTests"],
            "failed": data["numFailedTests"],
        }
        result["selected_files"] = [
            Path(item["name"]).relative_to(repo).as_posix() for item in data["testResults"]
        ]
        if data["numTotalTests"] > 0:
            result["validity"] = "fresh_positive_report"
            result["admission"] = "admitted_positive_count"
        else:
            result["validity"] = "fresh_empty_report"
    report.unlink(missing_ok=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", type=Path, default=Path(os.environ.get("MARIMO", "marimo")))
    parser.add_argument(
        "--results", type=Path, default=Path("experiments/marimo/vitest-resource-related.json")
    )
    args = parser.parse_args()
    repo = args.repo_dir.resolve()
    results = args.results.resolve()
    results.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="marimo-related-resource-") as temp_name:
        temp = Path(temp_name)
        runs = {
            "source_ts": run(repo, temp, "source_ts", "src/index.ts"),
            "generated_json": run(
                repo, temp, "generated_json", "data/generated/models.json"
            ),
        }
    results.write_text(
        json.dumps(
            {
                "project": "marimo",
                "revision": subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repo,
                    env={key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if os.environ.get(key)},
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                "runs": runs,
                "notes": [
                    "This is a selector observation for one generated JSON boundary, not a graph-safety claim.",
                    "Both runs are direct Vitest --no-cache runs with isolation left at its default.",
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0 if all(run["validity"] == "fresh_positive_report" for run in runs.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

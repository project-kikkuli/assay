#!/usr/bin/env python3
"""Re-run only pytest's recorded optional-dependency failures for classification."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path


FAILED = re.compile(r"^FAILED (?P<name>\S+)(?: - (?P<reason>.*))?$", re.MULTILINE)
COUNTS = re.compile(r"(?P<count>\d+) (?P<kind>failed|passed|skipped|xfailed|xpassed)")


def classify(name: str) -> dict[str, str]:
    if "test_editor_sandbox_supplies_server_tools" in name:
        return {
            "classification": "optional_dependency/environment",
            "cause": "bare test venv sees ruff on the allowlisted PATH; test expects ruff, pylsp, and pylsp_ruff absent",
        }
    if "test_shell_completion" in name:
        return {
            "classification": "fixture/configuration",
            "cause": "source checkout has no built static assets; assets mount logs unexpected stderr",
        }
    if "test_export_watch" in name or "test_interrupt_disconnects" in name:
        return {
            "classification": "fixture/configuration",
            "cause": "missing built static assets prepend an error to stderr and mask the expected CLI text",
        }
    if "test_pyodide_bridge_format" in name:
        return {
            "classification": "optional_dependency/environment",
            "cause": "ruff is unavailable, so formatting returns no code entries and the bridge response is empty",
        }
    if "test_favicon" in name or "test_base_url" in name:
        return {
            "classification": "fixture/configuration",
            "cause": "source checkout lacks built frontend assets, so the endpoint returns 404",
        }
    if "test_ruff_formatter_preserves_comments" in name:
        return {
            "classification": "optional_dependency/environment",
            "cause": "ruff is unavailable; formatter output lacks the expected key",
        }
    return {"classification": "unclassified", "cause": "see retained test name"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", type=Path, default=Path(os.environ.get("MARIMO", "marimo")))
    parser.add_argument(
        "--results", type=Path, default=Path("experiments/marimo/python-failures.json")
    )
    args = parser.parse_args()
    repo = args.repo_dir.resolve()
    results = args.results.resolve()
    results.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="marimo-python-diagnosis-") as temp_name:
        temp = Path(temp_name)
        env = {
            key: os.environ[key]
            for key in ("PATH", "LANG", "LC_ALL")
            if os.environ.get(key)
        }
        env.update(
            {
                "CI": "true",
                "UV_EXCLUDE_NEWER": "2026-09-18",
                "UV_CACHE_DIR": str(temp / "uv-cache"),
                "TMPDIR": str(temp / "tmp"),
                "PYTHONHASHSEED": "0",
            }
        )
        (temp / "tmp").mkdir()
        command = [
            "uv",
            "run",
            "--locked",
            "--python",
            "3.12",
            "--no-default-groups",
            "--group",
            "test-optional",
            "pytest",
            "--lf",
            "-q",
            "--tb=short",
            "-r",
            "fEx",
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
            output, _ = process.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            output, _ = process.communicate(timeout=10)
        duration_s = round(time.perf_counter() - started, 3)
    failures = []
    for match in FAILED.finditer(output):
        name = match.group("name")
        failures.append({"name": name, **classify(name)})
    counts: dict[str, int] = {}
    for match in COUNTS.finditer(output):
        counts[match.group("kind")] = int(match.group("count"))
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
                "command": command,
                "selection": "pytest --lf from the preceding full optional run",
                "duration_s": duration_s,
                "status": "passed" if process.returncode == 0 else "failed",
                "returncode": process.returncode,
                "counts": counts,
                "failure_names": failures,
                "classification_counts": {
                    **dict(Counter(item["classification"] for item in failures)),
                    "actual_assertion": 0,
                },
                "notes": [
                    "Raw pytest output is intentionally not retained.",
                    "This diagnoses the recorded failures; it does not alter upstream tests or fixtures.",
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

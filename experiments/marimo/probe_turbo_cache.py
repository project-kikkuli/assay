#!/usr/bin/env python3
"""Run the upstream typecheck twice and retain only Turbo cache evidence."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path


def run(repo: Path, cache: Path, name: str) -> dict[str, object]:
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL")
        if os.environ.get(key)
    }
    npm_cache = cache / "npm-cache"
    npm_cache.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "CI": "true",
            "MARIMO_SKIP_UPDATE_CHECK": "1",
            "PNPM_HOME": str(cache / "pnpm-home"),
            "TURBO_CACHE_DIR": str(cache / "turbo-cache"),
            "npm_config_cache": str(npm_cache),
            "npm_config_update_notifier": "false",
            "npm_config_fund": "false",
            "npm_config_audit": "false",
            "TMPDIR": str(cache / "tmp"),
        }
    )
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    command = [
        "npx",
        "--yes",
        "--package=pnpm@10.34.4",
        "pnpm",
        "turbo",
        "--filter",
        "@marimo-team/frontend",
        "typecheck",
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
    timed_out = False
    try:
        output, _ = process.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        output, _ = process.communicate(timeout=10)
    lowered = output.lower()
    events = {
        "hits": lowered.count("cache hit"),
        "misses": lowered.count("cache miss"),
        "bypasses": lowered.count("cache bypass"),
    }
    return {
        "name": name,
        "status": "timeout" if timed_out else ("passed" if process.returncode == 0 else "failed"),
        "duration_s": round(time.perf_counter() - started, 3),
        "turbo_cache": events,
        "validity": "cache_marker_observed" if sum(events.values()) else "no_cache_marker",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", type=Path, default=Path(os.environ.get("MARIMO", "marimo")))
    parser.add_argument(
        "--results", type=Path, default=Path("experiments/marimo/turbo-cache.json")
    )
    args = parser.parse_args()
    repo = args.repo_dir.resolve()
    results = args.results.resolve()
    results.parent.mkdir(parents=True, exist_ok=True)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env={key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if os.environ.get(key)},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="marimo-turbo-probe-") as cache_name:
        cache = Path(cache_name)
        runs = [run(repo, cache, "first"), run(repo, cache, "second")]
    results.write_text(
        json.dumps(
            {
                "project": "marimo",
                "revision": revision,
                "runs": runs,
                "notes": [
                    "Runs use the upstream Turbo typecheck task twice with the same dedicated external cache.",
                    "Only cache markers, status, and duration are retained; raw output is discarded.",
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0 if all(run["status"] == "passed" for run in runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())

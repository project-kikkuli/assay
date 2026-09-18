#!/usr/bin/env python3
"""Compare direct Vitest wall time with default and bounded worker pools."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from profile_vitest import run_vitest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", type=Path, default=Path(os.environ.get("MARIMO", "marimo")))
    parser.add_argument("--results", type=Path, default=Path("experiments/marimo/vitest-workers.json"))
    args = parser.parse_args()
    repo = args.repo_dir.resolve()
    results = args.results.resolve()
    work_dir = results.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    safe_env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL")
        if os.environ.get(key)
    }
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env=safe_env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    runs = {
        "default": run_vitest(repo, work_dir, "workers_default", ["run", "--no-cache"]),
        "max_workers_4": run_vitest(
            repo, work_dir, "workers_4", ["run", "--no-cache", "--maxWorkers", "4"]
        ),
        "max_workers_8": run_vitest(
            repo, work_dir, "workers_8", ["run", "--no-cache", "--maxWorkers", "8"]
        ),
    }
    data = {
        "project": "marimo",
        "revision": revision,
        "runs": runs,
        "notes": [
            "All runs use direct Vitest with --no-cache and keep isolation enabled.",
            "Default leaves maxWorkers unset; bounded runs set maxWorkers to 4 and 8.",
            "Only structured results are retained; raw runner output is discarded.",
        ],
    }
    results.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return 0 if all(run["validity"] == "fresh_positive_report" for run in runs.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

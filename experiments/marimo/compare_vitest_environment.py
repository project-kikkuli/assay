#!/usr/bin/env python3
"""Compare the configured jsdom mechanism with an explicit node environment."""

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
    parser.add_argument(
        "--results", type=Path, default=Path("experiments/marimo/vitest-environment.json")
    )
    args = parser.parse_args()
    repo = args.repo_dir.resolve()
    results = args.results.resolve()
    results.parent.mkdir(parents=True, exist_ok=True)
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
    test_files = [
        "src/core/cells/__tests__/runs.test.ts",
        "src/core/state/__tests__/jotai.test.ts",
    ]
    runs = {
        "configured_jsdom": run_vitest(
            repo, results.parent, "environment_jsdom", ["run", "--no-cache", *test_files]
        ),
        "explicit_node": run_vitest(
            repo,
            results.parent,
            "environment_node",
            ["run", "--no-cache", "--environment", "node", *test_files],
        ),
    }
    results.write_text(
        json.dumps(
            {
                "project": "marimo",
                "revision": revision,
                "test_files": test_files,
                "runs": runs,
                "notes": [
                    "This is a mechanism comparison, not a replacement for the configured suite.",
                    "Isolation remains enabled; only the environment is changed in the node case.",
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

#!/usr/bin/env python3
"""Profile the focused Vitest related gate before and after one seeded fault."""

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
        "--results", type=Path, default=Path("experiments/marimo/vitest-affected.json")
    )
    args = parser.parse_args()
    repo = args.repo_dir.resolve()
    results = args.results.resolve()
    results.parent.mkdir(parents=True, exist_ok=True)
    source = repo / "frontend/src/core/cells/runs.ts"
    original = source.read_text(encoding="utf-8")
    old = "if (!runId) {\n      return state;\n    }"
    mutant = "if (runId) {\n      return state;\n    }"
    if old not in original:
        raise RuntimeError("expected runs.ts guard was not found")
    args_for_gate = ["related", "src/core/cells/runs.ts", "--no-cache"]
    baseline = run_vitest(repo, results.parent, "affected_baseline", args_for_gate)
    source.write_text(original.replace(old, mutant, 1), encoding="utf-8")
    try:
        defect = run_vitest(repo, results.parent, "affected_seeded_defect", args_for_gate)
    finally:
        source.write_text(original, encoding="utf-8")
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
                "gate": "vitest related src/core/cells/runs.ts --no-cache",
                "baseline": baseline,
                "seeded_defect": defect,
                "notes": [
                    "The source guard is restored in a finally block; upstream tests are not changed.",
                    "Both reports were required to be fresh and positive-count; raw reports are discarded.",
                    "Vitest exposes prepareDuration rather than a separate transform counter; transform_prepare_ms is that diagnostic, not an inferred wall-time split.",
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0 if all(run["validity"] == "fresh_positive_report" for run in (baseline, defect)) else 1


if __name__ == "__main__":
    raise SystemExit(main())

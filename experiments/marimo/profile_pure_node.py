#!/usr/bin/env python3
"""Run the unchanged pure-state tests in an isolated minimal Vitest project."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from profile_vitest import load_fresh_positive_report


TEST_FILES = [
    "src/core/cells/__tests__/runs.test.ts",
    "src/core/state/__tests__/jotai.test.ts",
]


def run(repo: Path, temp: Path, config: Path, seed: int) -> dict[str, object]:
    report = temp / f"seed-{seed}.json"
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL")
        if os.environ.get(key)
    }
    env.update(
        {
            "CI": "true",
            "NODE_OPTIONS": "",
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
        "@marimo-team/frontend",
        "exec",
        "vitest",
        "run",
        "--no-cache",
        "--isolate",
        "--config",
        str(config),
        *TEST_FILES,
        "--reporter=json",
        "--outputFile",
        str(report),
        "--sequence.shuffle=true",
        f"--sequence.seed={seed}",
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
        output, _ = process.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        output, _ = process.communicate(timeout=10)
    result: dict[str, object] = {
        "seed": seed,
        "command": [
            token.replace(str(repo), "<repo>").replace(str(temp), "<temporary>")
            for token in command
        ],
        "duration_s": round(time.perf_counter() - started, 3),
        "status": "timeout" if timed_out else ("passed" if process.returncode == 0 else "failed"),
        "validity": "missing_or_stale_report",
        "counts": {},
    }
    data, validity = load_fresh_positive_report(report, 0)
    if data is not None:
        result["counts"] = {
            "test_suites": data["numTotalTestSuites"],
            "tests": data["numTotalTests"],
            "passed": data["numPassedTests"],
            "failed": data["numFailedTests"],
            "pending": data["numPendingTests"],
        }
        result["validity"] = validity
    else:
        result["status"] = "timeout" if timed_out else ("failed" if process.returncode else "unknown")
        result["validity"] = validity
    report.unlink(missing_ok=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", type=Path, default=Path(os.environ.get("MARIMO", "marimo")))
    parser.add_argument(
        "--results", type=Path, default=Path("experiments/marimo/vitest-pure-node.json")
    )
    args = parser.parse_args()
    repo = args.repo_dir.resolve()
    results = args.results.resolve()
    results.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="marimo-pure-node-") as temp_name:
        temp = Path(temp_name)
        frontend = repo / "frontend"
        vitest_config = frontend / "node_modules/vitest/dist/config.js"
        minimal_setup = temp / "minimal-setup.mjs"
        config = temp / "vitest.node.config.mjs"
        minimal_setup.write_text(
            'import { beforeEach, vi } from "vitest";\n'
            "beforeEach(() => vi.clearAllMocks());\n",
            encoding="utf-8",
        )
        config.write_text(
            "import { defineConfig } from "
            + json.dumps(vitest_config.as_uri())
            + ";\n"
            + "export default defineConfig({\n"
            + "  test: { environment: \"node\", isolate: true, setupFiles: ["
            + json.dumps(minimal_setup.as_uri())
            + "] },\n"
            + "  resolve: { alias: { \"@\": "
            + json.dumps((frontend / "src").as_posix())
            + " } },\n"
            + "});\n",
            encoding="utf-8",
        )
        runs = [run(repo, temp, config, seed) for seed in (1, 2)]
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
                "tests": TEST_FILES,
                "setup": {
                    "environment": "node",
                    "isolation": True,
                    "setup": "beforeEach(vi.clearAllMocks()) only",
                    "omitted": [
                        "blob-polyfill",
                        "@testing-library/react cleanup",
                        "ResizeObserver/IntersectionObserver shims",
                    ],
                    "reason": "These tests exercise reducer/Jotai state only; import tracing found no DOM use in their module boundary.",
                },
                "runs": runs,
                "notes": [
                    "Separate temporary Vitest config; original tests and upstream config are unchanged.",
                    "This is a 19-test mechanism check, not a whole-suite speed estimate.",
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0 if all(run["validity"] == "fresh_positive_report" for run in runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())

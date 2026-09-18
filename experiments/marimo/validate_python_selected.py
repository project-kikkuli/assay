#!/usr/bin/env python3
"""Validate the 15 recorded Python failures with upstream-like preparation."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path


SELECTED = [
    "tests/_cli/test_cli.py::test_editor_sandbox_supplies_server_tools[uv-file]",
    "tests/_cli/test_cli.py::test_editor_sandbox_supplies_server_tools[uv-folder]",
    "tests/_cli/test_cli.py::test_editor_sandbox_supplies_server_tools[uv-stdin]",
    "tests/_cli/test_cli.py::test_editor_sandbox_supplies_server_tools[uv-new]",
    "tests/_cli/test_cli.py::test_shell_completion[bash-0-True-False]",
    "tests/_cli/test_cli.py::test_shell_completion[bash.exe-0-True-False]",
    "tests/_cli/test_cli.py::test_shell_completion[/usr/bin/zsh-0-True-False]",
    "tests/_cli/test_cli_export.py::TestExportHTML::test_export_watch_no_out_dir",
    "tests/_cli/test_cli_export.py::TestExportScript::test_export_watch_script_no_out_dir",
    "tests/_cli/test_cli_export.py::TestExportMarkdown::test_export_watch_markdown_no_out_dir",
    "tests/_cli/test_pair_integration.py::test_interrupt_disconnects_and_kernel_recovers",
    "tests/_pyodide/test_pyodide_session.py::test_pyodide_bridge_format",
    "tests/_server/api/endpoints/test_assets.py::test_favicon",
    "tests/_server/api/test_middleware.py::test_base_url",
    "tests/_utils/test_formatter.py::TestRuffFormatter::test_ruff_formatter_preserves_comments",
]
PYTEST_COUNTS = re.compile(r"(?P<count>\d+) (?P<kind>passed|failed|skipped|xfailed|xpassed)")


def base_env(temp: Path, *, isolated_uv: Path | None = None) -> dict[str, str]:
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL")
        if os.environ.get(key)
    }
    if isolated_uv is not None:
        env["PATH"] = os.pathsep.join([str(isolated_uv), os.defpath])
    env.update(
        {
            "CI": "true",
            "UV_EXCLUDE_NEWER": "2026-09-18",
            "UV_CACHE_DIR": str(temp / "uv-cache"),
            "TMPDIR": str(temp / "tmp"),
            "PYTHONHASHSEED": "0",
            "npm_config_update_notifier": "false",
            "npm_config_fund": "false",
            "npm_config_audit": "false",
        }
    )
    (temp / "tmp").mkdir(exist_ok=True)
    return env


def run_command(
    command: list[str],
    repo: Path,
    env: dict[str, str],
    timeout: float = 300,
) -> tuple[dict[str, object], str]:
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
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        output, _ = process.communicate(timeout=10)
    return (
        {
            "command": command,
            "status": "timeout" if timed_out else ("passed" if process.returncode == 0 else "failed"),
            "returncode": process.returncode,
            "duration_s": round(time.perf_counter() - started, 3),
        },
        output,
    )


def parse_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in PYTEST_COUNTS.finditer(output):
        counts[match.group("kind")] = int(match.group("count"))
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", type=Path, default=Path(os.environ.get("MARIMO", "marimo")))
    parser.add_argument(
        "--results", type=Path, default=Path("experiments/marimo/python-selected-validation.json")
    )
    args = parser.parse_args()
    repo = args.repo_dir.resolve()
    results = args.results.resolve()
    results.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="marimo-python-selected-") as temp_name:
        temp = Path(temp_name)
        uv_path = Path(shutil.which("uv") or "uv").resolve()
        uv_bin = temp / "uv-bin"
        uv_bin.mkdir()
        (uv_bin / "uv").symlink_to(uv_path)
        setup_env = base_env(temp)
        sync_command = [
            "uv",
            "sync",
            "--locked",
            "--python",
            "3.12",
            "--no-default-groups",
            "--group",
            "test-optional",
            "--group",
            "dev",
        ]
        sync, _ = run_command(sync_command, repo, setup_env)
        ruff_probe_command = [
            "uv",
            "run",
            "--locked",
            "--python",
            "3.12",
            "--no-default-groups",
            "--group",
            "test-optional",
            "--group",
            "dev",
            "python",
            "-c",
            "import importlib.metadata, importlib.util, shutil; print(importlib.metadata.version('ruff')); print(importlib.util.find_spec('ruff') is not None); print(shutil.which('ruff') is not None)",
        ]
        probe, probe_output = run_command(ruff_probe_command, repo, setup_env)
        probe_lines = [line.strip() for line in probe_output.splitlines() if line.strip()]
        ruff_probe = {
            **probe,
            "module_version": next((line for line in probe_lines if re.fullmatch(r"\d+\.\d+\.\d+", line)), None),
            "module_present": "True" in probe_lines,
            "binary_present": probe_lines[-1] == "True" if probe_lines else False,
        }

        build_env = base_env(temp)
        build_command = [
            "npx",
            "--yes",
            "--package=pnpm@10.34.4",
            "pnpm",
            "turbo",
            "build",
            "--filter",
            "@marimo-team/frontend",
            "--force",
        ]
        build, _ = run_command(build_command, repo, build_env, timeout=600)

        static_dir = repo / "marimo/_static"
        dist_dir = repo / "frontend/dist"
        static_backup = temp / "static-backup"
        dist_backup = temp / "dist-backup"
        if static_dir.exists():
            shutil.copytree(static_dir, static_backup)
        if dist_dir.exists():
            shutil.copytree(dist_dir, dist_backup)
        if build["status"] == "passed" and dist_dir.is_dir():
            if static_dir.exists():
                shutil.rmtree(static_dir)
            shutil.copytree(dist_dir, static_dir)
        asset_count = sum(1 for path in static_dir.rglob("*") if path.is_file()) if static_dir.exists() else 0
        asset_bytes = sum(path.stat().st_size for path in static_dir.rglob("*") if path.is_file()) if static_dir.exists() else 0

        test_env = base_env(temp, isolated_uv=uv_bin)
        pytest_command = [
            "uv",
            "run",
            "--locked",
            "--python",
            "3.12",
            "--no-default-groups",
            "--group",
            "test-optional",
            "--group",
            "dev",
            "pytest",
            "-q",
            "--tb=short",
            *SELECTED,
        ]
        selected, _ = run_command(pytest_command, repo, test_env, timeout=300)
        selected["counts"] = parse_counts(_)

        if static_dir.exists():
            shutil.rmtree(static_dir)
        if static_backup.exists():
            shutil.copytree(static_backup, static_dir)
        if dist_dir.exists():
            shutil.rmtree(dist_dir)
        if dist_backup.exists():
            shutil.copytree(dist_backup, dist_dir)

    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env={key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if os.environ.get(key)},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    results.write_text(
        json.dumps(
            {
                "project": "marimo",
                "revision": revision,
                "selected_tests": SELECTED,
                "upstream_preparation": {
                    "asset_build_command": build_command,
                    "asset_build": build,
                    "asset_source": "frontend/dist copied to marimo/_static temporarily; generated paths restored afterward",
                    "asset_files": asset_count,
                    "asset_bytes": asset_bytes,
                    "python_sync_command": sync_command,
                    "python_sync": sync,
                    "ruff_probe": ruff_probe,
                    "test_path_note": "uv-bin is an isolated symlink directory so bare sandbox venvs do not inherit the host Ruff binary",
                },
                "selected_run": selected,
                "notes": [
                    "The upstream tests, fixtures, and source checkout are unchanged after the run.",
                    "This is a selected validation, not a rerun of the 12,500-test suite.",
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0 if selected["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

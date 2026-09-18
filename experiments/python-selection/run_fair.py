#!/usr/bin/env python3
"""Run the FAIR subject-venv pytest-testmon experiment with reversible setup."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from run_selection import (
    MUTANT_NEW,
    MUTANT_OLD,
    PLUGIN,
    RESOURCE_SCOPE,
    pytest_run,
    run_command,
    safe_env,
    sanitize_output,
)


REVISION = "e8009f0f50220af0825fc920ba48af90fdddf617"


def freeze(repo: Path, python: Path, temp: Path) -> dict[str, Any]:
    result, output = run_command(
        ["uv", "pip", "freeze", "--python", str(python)],
        repo,
        safe_env(temp, path_prefix=repo / ".venv/bin"),
        180,
        temp,
    )
    result["packages"] = [sanitize_output(line.replace(str(repo), "<repo>")) for line in output.splitlines() if line]
    result["package_count"] = len(result["packages"])
    return result


def versions(repo: Path, python: Path, temp: Path) -> dict[str, str]:
    code = (
        "import importlib.metadata as m; "
        "names=('pytest','coverage','pytest-testmon','testmon','pytest-asyncio','inline-snapshot'); "
        "[(print(n+'=='+m.version(n))) if _present(m,n) else print(n+'==ABSENT') for n in names]"
    )
    # Keep the probe stdlib-only and avoid ambient imports/credentials.
    code = """import importlib.metadata as m
names = ('pytest', 'coverage', 'pytest-testmon', 'testmon', 'pytest-asyncio', 'inline-snapshot')
for name in names:
    try:
        print(name + '==' + m.version(name))
    except m.PackageNotFoundError:
        print(name + '==ABSENT')
"""
    result, output = run_command(
        [str(python), "-c", code],
        repo,
        safe_env(temp, path_prefix=repo / ".venv/bin"),
        60,
        temp,
    )
    result["versions"] = dict(line.split("==", 1) for line in output.splitlines() if "==" in line)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", type=Path, required=True)
    parser.add_argument("--results", type=Path, default=Path("experiments/python-selection/fair-results.json"))
    parser.add_argument("--baseline-timeout-s", type=float, default=1500.0)
    args = parser.parse_args()
    repo = args.repo_dir.resolve()
    results_path = args.results.resolve()
    started = time.perf_counter()
    data: dict[str, Any] = {
        "project": "marimo",
        "revision": REVISION,
        "plugin": PLUGIN,
        "status": "unknown_not_started",
        "validity": "subject_venv_plugin_install",
        "assumptions": [
            "Only pytest-testmon==2.2.0 is installed into the existing subject .venv, with --no-deps; no lock or tracked source is changed.",
            "The clean test environment sets UV to the discovered uv executable, puts subject .venv/bin first on PATH, uses a fresh HOME/cache, and stages 480 real frontend assets.",
            "Testmon records executed Python dependencies through Coverage.py; static resources and unseen branches are not soundness claims.",
        ],
    }
    base_python = repo / ".venv/bin/python"
    plugin_was_present = False
    plugin_installed = False
    with tempfile.TemporaryDirectory(prefix="marimo-python-fair-") as temp_name:
        temp = Path(temp_name)
        try:
            if not base_python.exists():
                data["status"] = "unknown_missing_prepared_environment"
                return 1
            data["before_versions"] = versions(repo, base_python, temp)
            plugin_was_present = data["before_versions"]["versions"].get("pytest-testmon") not in (None, "ABSENT")
            data["freeze_before"] = freeze(repo, base_python, temp)
            if plugin_was_present:
                data["status"] = "unknown_plugin_preexisting"
                data["blocker"] = "pytest-testmon was already present; refusing to alter an existing plugin state"
                return 1

            install, install_output = run_command(
                ["uv", "pip", "install", "--python", str(base_python), "--no-deps", PLUGIN],
                repo,
                safe_env(temp, path_prefix=repo / ".venv/bin"),
                180,
                temp,
            )
            install["output_excerpt"] = sanitize_output(install_output)
            data["plugin_install"] = install
            plugin_installed = install["status"] == "passed"
            data["after_install_versions"] = versions(repo, base_python, temp)
            data["freeze_after_install"] = freeze(repo, base_python, temp)
            if not plugin_installed or data["after_install_versions"]["versions"].get("pytest-testmon") != "2.2.0":
                data["status"] = "unknown_plugin_install"
                return 1

            base_site = Path(
                subprocess.run(
                    [str(base_python), "-c", "import site; print(site.getsitepackages()[0])"],
                    cwd=repo,
                    env=safe_env(temp, path_prefix=repo / ".venv/bin"),
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            )
            probe, probe_output = run_command(
                [str(base_python), "-m", "pytest", "--trace-config", "--collect-only", "-q", "tests/_utils/test_formatter.py"],
                repo,
                safe_env(temp, pythonpath=[repo, Path(__file__).resolve().parent, base_site], path_prefix=repo / ".venv/bin"),
                180,
                temp,
            )
            probe["testmon_trace_lines"] = [sanitize_output(line) for line in probe_output.splitlines() if "testmon" in line.lower()]
            data["plugin_probe"] = probe

            static_dir = repo / "marimo/_static"
            dist_dir = repo / "frontend/dist"
            static_backup = temp / "static-backup"
            if not dist_dir.is_dir():
                data["status"] = "unknown_missing_assets"
                return 1
            shutil.copytree(static_dir, static_backup)
            shutil.rmtree(static_dir)
            shutil.copytree(dist_dir, static_dir)
            data["readiness"] = {
                "frontend_dist_files": sum(1 for path in dist_dir.rglob("*") if path.is_file()),
                "frontend_dist_bytes": sum(path.stat().st_size for path in dist_dir.rglob("*") if path.is_file()),
                "staged_static_files": sum(1 for path in static_dir.rglob("*") if path.is_file()),
                "staged_static_bytes": sum(path.stat().st_size for path in static_dir.rglob("*") if path.is_file()),
            }
            try:
                baseline_args = ["--testmon", "-q", "--tb=short", "--durations=10", "tests/"]
                data["baseline"] = pytest_run(repo, base_python, temp, base_site, baseline_args, args.baseline_timeout_s)
                if data["baseline"]["status"] != "unknown_timeout":
                    data["warm_unchanged"] = pytest_run(repo, base_python, temp, base_site, ["--testmon", "-q", "tests/"], 600)

                    source = repo / "marimo/_utils/formatter.py"
                    original = source.read_text(encoding="utf-8")
                    if MUTANT_OLD not in original:
                        data["formatter_mutation"] = {"status": "unknown_mutant_not_found"}
                    else:
                        source.write_text(original.replace(MUTANT_OLD, MUTANT_NEW, 1), encoding="utf-8")
                        try:
                            data["formatter_mutation_testmon"] = pytest_run(repo, base_python, temp, base_site, ["--testmon", "-q", "tests/_utils/test_formatter.py"], 300)
                            data["formatter_mutation_direct"] = pytest_run(repo, base_python, temp, base_site, ["--no-testmon", "-q", "tests/_utils/test_formatter.py"], 180)
                        finally:
                            source.write_text(original, encoding="utf-8")

                    favicon = static_dir / "favicon.ico"
                    favicon_backup = temp / "favicon.ico"
                    if favicon.exists():
                        shutil.copy2(favicon, favicon_backup)
                        favicon.unlink()
                        try:
                            data["resource_mutation_testmon"] = pytest_run(repo, base_python, temp, base_site, ["--testmon", "-q", RESOURCE_SCOPE], 300)
                            data["resource_mutation_direct"] = pytest_run(repo, base_python, temp, base_site, ["--no-testmon", "-q", RESOURCE_SCOPE], 180)
                        finally:
                            shutil.copy2(favicon_backup, favicon)
                    else:
                        data["resource_mutation"] = {"status": "unknown_asset_not_found"}
                    data["status"] = "observed"
                else:
                    data["status"] = "unknown_baseline_timeout"
            finally:
                shutil.rmtree(static_dir, ignore_errors=True)
                shutil.copytree(static_backup, static_dir)
        except Exception as exc:  # preserve an explicit unknown rather than green
            data["status"] = "unknown_exception"
            data["exception"] = sanitize_output(repr(exc))
        finally:
            if plugin_installed and not plugin_was_present:
                data["plugin_restore"], restore_output = run_command(
                    ["uv", "pip", "uninstall", "--python", str(base_python), "pytest-testmon"],
                    repo,
                    safe_env(temp, path_prefix=repo / ".venv/bin"),
                    180,
                    temp,
                )
                data["plugin_restore"]["output_excerpt"] = sanitize_output(restore_output)
            data["after_restore_versions"] = versions(repo, base_python, temp)
            data["freeze_after_restore"] = freeze(repo, base_python, temp)
            data["restore_matches_before"] = data.get("freeze_after_restore", {}).get("packages") == data.get("freeze_before", {}).get("packages")
            status_result = subprocess.run(["git", "status", "--short"], cwd=repo, env=safe_env(temp, path_prefix=repo / ".venv/bin"), capture_output=True, text=True, check=False)
            data["checkout_status"] = status_result.stdout.strip()
            data["elapsed_s"] = round(time.perf_counter() - started, 3)
            results_path.parent.mkdir(parents=True, exist_ok=True)
            results_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return 0 if data.get("status") == "observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

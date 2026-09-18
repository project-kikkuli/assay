#!/usr/bin/env python3
"""Run a bounded, isolated pytest-testmon selection experiment."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


REVISION = "e8009f0f50220af0825fc920ba48af90fdddf617"
PLUGIN = "pytest-testmon==2.2.0"
PYTEST_SUMMARY = re.compile(r"(?P<count>\d+) (?P<kind>passed|failed|skipped|xfailed|xpassed|deselected)")
TARGET_NODE = "tests/_utils/test_formatter.py::TestRuffFormatter::test_ruff_formatter_preserves_comments"
RESOURCE_SCOPE = "tests/_server/api/endpoints/test_assets.py"
RESOURCE_NODE = "tests/_server/api/endpoints/test_assets.py::test_favicon"
MUTANT_OLD = "formatted_codes[key] = formatted.strip()"
MUTANT_NEW = "formatted_codes[key] = \"\""


def safe_env(
    temp: Path,
    *,
    pythonpath: list[Path] | None = None,
    path_prefix: Path | None = None,
) -> dict[str, str]:
    inherited_path = os.environ.get("PATH", "")
    if path_prefix:
        inherited_path = str(path_prefix) + os.pathsep + inherited_path
    uv_executable = shutil.which("uv", path=inherited_path)
    env = {
        "PATH": inherited_path,
        "HOME": str(temp / "home"),
        "XDG_CACHE_HOME": str(temp / "cache"),
        **{
            key: os.environ[key]
            for key in ("LANG", "LC_ALL")
            if os.environ.get(key)
        },
    }
    env.update(
        {
            "CI": "true",
            "UV_EXCLUDE_NEWER": "2026-09-18",
            "UV_CACHE_DIR": str(temp / "uv-cache"),
            "TMPDIR": str(temp / "tmp"),
            "PYTHONHASHSEED": "0",
            "PYSEL_TIMING_FILE": str(temp / "timing.json"),
            "TESTMON_DATAFILE": str(temp / "testmon.data"),
        }
    )
    if uv_executable:
        env["UV"] = uv_executable
    (temp / "home").mkdir(parents=True, exist_ok=True)
    (temp / "cache").mkdir(parents=True, exist_ok=True)
    if pythonpath:
        env["PYTHONPATH"] = os.pathsep.join(str(path) for path in pythonpath)
    (temp / "tmp").mkdir(parents=True, exist_ok=True)
    return env


def normalize_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def sanitize_output(value: str) -> str:
    value = re.sub(r"/(?:Users|private/tmp|tmp|var/folders)/[^\s:)]+", "<host-path>", value)
    return value[-5000:]


def run_command(
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
    sanitize_root: Path,
) -> tuple[dict[str, Any], str]:
    started = time.perf_counter()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        output, _ = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            output, _ = process.communicate()
        output = output or exc.stdout or ""
    output = normalize_output(output)
    returncode = process.returncode
    if timed_out:
        status = "unknown_timeout"
    elif returncode != 0:
        status = "failed"
    else:
        status = "passed"
    return (
        {
            "command": [
                token.replace(str(sanitize_root), "<external>").replace(str(cwd), "<repo>")
                for token in command
            ],
            "status": status,
            "returncode": returncode,
            "duration_s": round(time.perf_counter() - started, 3),
        },
        output,
    )


def summary_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in PYTEST_SUMMARY.finditer(output):
        counts[match.group("kind")] = int(match.group("count"))
    return counts


def read_timing(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"validity": "unknown_missing_timing"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or type(data.get("selected_tests")) is not int
                or data["selected_tests"] < 0
                or type(data.get("exit_status")) is not int
                or not isinstance(data.get("phases_s"), dict)
                or not data["phases_s"]
                or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                       for value in data["phases_s"].values())):
            return {"validity": "unknown_malformed_timing"}
        return {"validity": "fresh_timing", **data}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {"validity": "unknown_malformed_timing"}
    finally:
        path.unlink(missing_ok=True)


def size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def classify_selection(
    result: dict[str, Any], timing: dict[str, Any], counts: dict[str, int]
) -> dict[str, Any]:
    result["timing"] = timing
    if timing.get("validity") != "fresh_timing":
        result["status"] = timing["validity"]
        result["selection_admission"] = "unknown_timing"
    elif timing.get("selected_tests") == 0:
        result["status"] = "unknown_zero_tests"
        result["selection_admission"] = "unknown_zero_tests"
    elif not counts:
        result["status"] = "unknown_empty_counts"
        result["selection_admission"] = "unknown_empty_counts"
    elif (sum(counts.get(kind, 0) for kind in ("passed", "failed", "skipped", "xfailed", "xpassed"))
          != timing["selected_tests"]
          or (result["status"] == "passed" and (timing["exit_status"] != 0 or counts.get("failed", 0)))):
        result["status"] = "unknown_contradictory_counts"
        result["selection_admission"] = "unknown_contradictory_counts"
    else:
        result["selection_admission"] = "positive_test_count"
    return result


def pytest_run(
    repo: Path,
    python: Path,
    temp: Path,
    base_site: Path,
    args: list[str],
    timeout_s: float,
    pythonpath_extra: list[Path] | None = None,
) -> dict[str, Any]:
    pythonpath = [repo, Path(__file__).resolve().parent]
    if pythonpath_extra:
        pythonpath.extend(pythonpath_extra)
    pythonpath.append(base_site)
    env = safe_env(
        temp,
        pythonpath=pythonpath,
        path_prefix=repo / ".venv/bin",
    )
    command = [str(python), "-m", "pytest", *args, "-p", "python_selection_timing"]
    (temp / "timing.json").unlink(missing_ok=True)
    result, output = run_command(command, repo, env, timeout_s, temp)
    result["counts"] = summary_counts(output)
    timing = read_timing(temp / "timing.json")
    if result["status"] != "passed":
        result["failure_excerpt"] = sanitize_output(output)
    return classify_selection(result, timing, result["counts"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", type=Path, default=Path(os.environ.get("MARIMO", "marimo")))
    parser.add_argument("--results", type=Path, default=Path("experiments/python-selection/results.json"))
    parser.add_argument("--baseline-timeout-s", type=float, default=1800.0)
    args = parser.parse_args()
    repo = args.repo_dir.resolve()
    results_path = args.results.resolve()
    results_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    data: dict[str, Any] = {
        "project": "marimo",
        "revision": REVISION,
        "plugin": PLUGIN,
        "status": "unknown_not_started",
        "assumptions": [
            "Testmon uses Coverage.py execution dependencies and its first --testmon run must be whole-suite.",
            "Static files/resources and external services are not tracked; a resource deletion is tested as a counterexample.",
            "A zero-test unchanged selection is conditional reuse evidence, not acceptance or soundness evidence.",
        ],
    }
    with tempfile.TemporaryDirectory(prefix="marimo-python-selection-") as temp_name:
        temp = Path(temp_name)
        base_python = repo / ".venv/bin/python"
        if not base_python.exists():
            data["status"] = "unknown_missing_prepared_environment"
            data["blocker"] = "expected prepared .venv was not present"
            results_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            return 1
        base_site = Path(
            subprocess.run(
                [str(base_python), "-c", "import site; print(site.getsitepackages()[0])"],
                cwd=repo,
                env=safe_env(temp),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        overlay = temp / "testmon-venv"
        bootstrap_env = safe_env(temp)
        venv_result, _ = run_command(
            ["uv", "venv", "--python", "3.12", str(overlay)], repo, bootstrap_env, 180, temp
        )
        install_result, _ = run_command(
            ["uv", "pip", "install", "--python", str(overlay / "bin/python"), PLUGIN],
            repo,
            bootstrap_env,
            180,
            temp,
        )
        overlay_python = overlay / "bin/python"
        overlay_site = Path(
            subprocess.run(
                [str(overlay_python), "-c", "import site; print(site.getsitepackages()[0])"],
                cwd=repo,
                env=bootstrap_env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        test_python = base_python
        test_pythonpath_extra = [overlay_site]
        test_env = safe_env(temp, pythonpath=[repo, overlay_site, base_site], path_prefix=repo / ".venv/bin")
        plugin_probe, probe_output = run_command(
            [str(overlay_python), "-c", "import importlib.metadata; print(importlib.metadata.version('pytest-testmon'))"],
            repo,
            test_env,
            60,
            temp,
        )
        plugin_probe["version_observed"] = normalize_output(probe_output).strip().splitlines()[-1:] or [None]
        data["bootstrap"] = {"venv": venv_result, "plugin_install": install_result, "plugin_probe": plugin_probe}
        if any(item["status"] != "passed" for item in (venv_result, install_result, plugin_probe)):
            data["status"] = "unknown_bootstrap"
            data["blocker"] = "isolated pytest-testmon overlay was not ready"
            results_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            return 1

        static_dir = repo / "marimo/_static"
        dist_dir = repo / "frontend/dist"
        static_backup = temp / "static-backup"
        if static_dir.exists():
            shutil.copytree(static_dir, static_backup)
        if not dist_dir.is_dir():
            data["status"] = "unknown_missing_assets"
            data["blocker"] = "real frontend/dist was not available"
            results_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            return 1
        shutil.rmtree(static_dir, ignore_errors=True)
        shutil.copytree(dist_dir, static_dir)
        data["readiness"] = {
            "frontend_dist_files": sum(1 for path in dist_dir.rglob("*") if path.is_file()),
            "frontend_dist_bytes": size_bytes(dist_dir),
            "staged_static_files": sum(1 for path in static_dir.rglob("*") if path.is_file()),
            "staged_static_bytes": size_bytes(static_dir),
            "extra_overlay_bytes": size_bytes(temp),
            "extra_budget_bytes": 1_000_000_000,
        }
        if data["readiness"]["extra_overlay_bytes"] > 900_000_000:
            data["status"] = "unknown_resource_budget"
            data["blocker"] = "isolated overlay exceeded the 1GB extra-resource budget"
        else:
            baseline_args = ["--testmon", "-q", "--tb=short", "--durations=10", "tests/"]
            data["baseline"] = pytest_run(repo, test_python, temp, base_site, baseline_args, args.baseline_timeout_s, test_pythonpath_extra)
            if data["baseline"]["status"] == "unknown_timeout":
                data["status"] = "unknown_baseline_timeout"
            else:
                warm_args = ["--testmon", "-q", "--tb=short", "tests/"]
                data["warm_unchanged"] = pytest_run(repo, test_python, temp, base_site, warm_args, 600, test_pythonpath_extra)
                data["target_baseline"] = pytest_run(repo, test_python, temp, base_site, ["--no-testmon", "-q", TARGET_NODE], 180, test_pythonpath_extra)

                source = repo / "marimo/_utils/formatter.py"
                original = source.read_text(encoding="utf-8")
                if MUTANT_OLD not in original:
                    data["mutant"] = {"status": "unknown_mutant_not_found"}
                else:
                    source.write_text(original.replace(MUTANT_OLD, MUTANT_NEW, 1), encoding="utf-8")
                    try:
                        data["mutant_testmon"] = pytest_run(repo, test_python, temp, base_site, ["--testmon", "-q", TARGET_NODE], 300, test_pythonpath_extra)
                        data["mutant_direct"] = pytest_run(repo, test_python, temp, base_site, ["--no-testmon", "-q", TARGET_NODE], 180, test_pythonpath_extra)
                    finally:
                        source.write_text(original, encoding="utf-8")

                favicon = static_dir / "favicon.ico"
                favicon_backup = temp / "favicon.ico"
                if favicon.exists():
                    shutil.copy2(favicon, favicon_backup)
                    favicon.unlink()
                    try:
                        data["resource_mutation_testmon"] = pytest_run(repo, test_python, temp, base_site, ["--testmon", "-q", RESOURCE_SCOPE], 300, test_pythonpath_extra)
                        data["resource_mutation_direct"] = pytest_run(repo, test_python, temp, base_site, ["--no-testmon", "-q", RESOURCE_SCOPE], 180, test_pythonpath_extra)
                    finally:
                        shutil.copy2(favicon_backup, favicon)
                else:
                    data["resource_mutation"] = {"status": "unknown_asset_not_found"}
                data["status"] = "observed"
        if static_dir.exists():
            shutil.rmtree(static_dir)
        if static_backup.exists():
            shutil.copytree(static_backup, static_dir)
    data["elapsed_s"] = round(time.perf_counter() - started, 3)
    results_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return 0 if data["status"] == "observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

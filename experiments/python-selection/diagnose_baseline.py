#!/usr/bin/env python3
"""Compare representative baseline failures across subject and Testmon environments."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from run_selection import PLUGIN, pytest_run, read_timing, run_command, safe_env, summary_counts


CASES = {
    "cli_path": "tests/_cli/test_cli.py::test_cli_help_exit_code",
    "package_manager": "tests/_code_mode/test_code_mode_context.py::TestPackages::test_add_single",
    "static_asset": "tests/_server/api/endpoints/test_assets.py::test_favicon",
    "export": "tests/_export/test_exporter.py::TestPDFExport::test_webpdf_inlines_code_wrapping_css",
    "runtime": "tests/_runtime/test_wasm_threading.py::test_top_level_marimo_import_bootstraps_wasm_threading_first",
}


def sanitize(value: str) -> str:
    value = re.sub(r"/(?:Users|private/tmp|tmp|var/folders)/[^\s:)]+", "<host-path>", value)
    return value[-5000:]


def subject_run(repo: Path, python: Path, temp: Path, node: str) -> dict:
    env = safe_env(
        temp,
        pythonpath=[repo, Path(__file__).resolve().parent],
        path_prefix=repo / ".venv/bin",
    )
    result, output = run_command(
        [str(python), "-m", "pytest", "-q", "--tb=short", node, "-p", "python_selection_timing"],
        repo,
        env,
        180,
        temp,
    )
    result["counts"] = summary_counts(output)
    result["timing"] = read_timing(temp / "timing.json")
    if result["status"] != "passed":
        result["failure_excerpt"] = sanitize(output)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", type=Path, required=True)
    parser.add_argument("--results", type=Path, default=Path("experiments/python-selection/diagnosis.json"))
    args = parser.parse_args()
    repo = args.repo_dir.resolve()
    output_path = args.results.resolve()
    data = {
        "project": "marimo",
        "revision": "e8009f0f50220af0825fc920ba48af90fdddf617",
        "plugin": PLUGIN,
        "cases": CASES,
        "modes": {},
        "assumptions": [
            "Subject mode uses the prepared .venv and prepends its bin directory to the explicit PATH allowlist.",
            "Overlay modes use a fresh venv containing only pytest-testmon==2.2.0 and the prepared subject site-packages.",
            "All modes stage the real frontend/dist (480 files) before comparison, matching the full bootstrap; the separate resource deletion is not this diagnosis.",
        ],
    }
    with tempfile.TemporaryDirectory(prefix="marimo-python-diagnosis-") as temp_name:
        temp = Path(temp_name)
        base_python = repo / ".venv/bin/python"
        if not base_python.exists():
            data["status"] = "unknown_missing_prepared_environment"
        else:
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
            overlay = temp / "testmon-venv"
            bootstrap_env = safe_env(temp, path_prefix=repo / ".venv/bin")
            venv, _ = run_command(["uv", "venv", "--python", "3.12", str(overlay)], repo, bootstrap_env, 180, temp)
            overlay_python = overlay / "bin/python"
            install, _ = run_command(["uv", "pip", "install", "--python", str(overlay_python), PLUGIN], repo, bootstrap_env, 180, temp)
            data["bootstrap"] = {"venv": venv, "plugin_install": install}
            if venv["status"] != "passed" or install["status"] != "passed":
                data["status"] = "unknown_bootstrap"
            else:
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
                probe_env = safe_env(
                    temp,
                    pythonpath=[repo, Path(__file__).resolve().parent, overlay_site, base_site],
                    path_prefix=repo / ".venv/bin",
                )
                plugin_probe, probe_output = run_command(
                    [str(base_python), "-m", "pytest", "--trace-config", "--collect-only", "-q", CASES["cli_path"]],
                    repo,
                    probe_env,
                    180,
                    temp,
                )
                data["plugin_probe"] = {
                    **plugin_probe,
                    "plugin_id": "testmon.pytest_testmon",
                    "testmon_trace_lines": [
                        sanitize(line) for line in probe_output.splitlines() if "testmon" in line.lower()
                    ],
                }
                static_dir = repo / "marimo/_static"
                dist_dir = repo / "frontend/dist"
                static_backup = temp / "static-backup"
                if not dist_dir.is_dir():
                    data["status"] = "unknown_missing_assets"
                else:
                    shutil.copytree(static_dir, static_backup)
                    shutil.rmtree(static_dir)
                    shutil.copytree(dist_dir, static_dir)
                    data["readiness"] = {
                        "staged_static_files": sum(1 for path in static_dir.rglob("*") if path.is_file()),
                        "staged_static_bytes": sum(path.stat().st_size for path in static_dir.rglob("*") if path.is_file()),
                    }
                    try:
                        for mode, python, option, extra in (
                            ("subject_no_testmon", base_python, None, []),
                            ("subject_with_testmon", base_python, "--testmon", [overlay_site]),
                            ("separate_overlay_no_testmon", overlay_python, "--no-testmon", []),
                            ("separate_overlay_with_testmon", overlay_python, "--testmon", []),
                        ):
                            data["modes"][mode] = {}
                            for name, node in CASES.items():
                                (temp / "testmon.data").unlink(missing_ok=True)
                                if option is None:
                                    result = subject_run(repo, python, temp, node)
                                else:
                                    result = pytest_run(repo, python, temp, base_site, [option, "-q", "--tb=short", node], 180, extra)
                                data["modes"][mode][name] = result
                        data["status"] = "observed"
                    finally:
                        shutil.rmtree(static_dir, ignore_errors=True)
                        shutil.copytree(static_backup, static_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return 0 if data.get("status") == "observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

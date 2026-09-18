#!/usr/bin/env python3
"""Run bounded Testmon defect and static-resource follow-ups after the baseline."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from run_selection import (
    MUTANT_NEW,
    MUTANT_OLD,
    PLUGIN,
    RESOURCE_NODE,
    RESOURCE_SCOPE,
    REVISION,
    TARGET_NODE,
    pytest_run,
    run_command,
    safe_env,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", type=Path, required=True)
    parser.add_argument("--results", type=Path, default=Path("experiments/python-selection/followup.json"))
    args = parser.parse_args()
    repo = args.repo_dir.resolve()
    result_path = args.results.resolve()
    started = time.perf_counter()
    data = {
        "project": "marimo",
        "revision": REVISION,
        "plugin": PLUGIN,
        "status": "unknown_not_started",
        "target": TARGET_NODE,
        "resource_scope": RESOURCE_SCOPE,
        "resource_test": RESOURCE_NODE,
        "assumptions": [
            "The formatter test exercises the real output path, so the output mutation should be caught by the original test.",
            "The resource run uses a file scope, not an explicit test node; zero selection after deleting a static file is unresolved evidence, not a pass.",
        ],
    }
    with tempfile.TemporaryDirectory(prefix="marimo-python-followup-") as temp_name:
        temp = Path(temp_name)
        base_python = repo / ".venv/bin/python"
        if not base_python.exists():
            data["blocker"] = "expected prepared .venv was not present"
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
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
        env = safe_env(temp, path_prefix=repo / ".venv/bin")
        data["bootstrap"] = {}
        data["bootstrap"]["venv"], _ = run_command(
            ["uv", "venv", "--python", "3.12", str(overlay)], repo, env, 180, temp
        )
        overlay_python = overlay / "bin/python"
        data["bootstrap"]["plugin_install"], _ = run_command(
            ["uv", "pip", "install", "--python", str(overlay_python), PLUGIN], repo, env, 180, temp
        )
        overlay_site = Path(
            subprocess.run(
                [str(overlay_python), "-c", "import site; print(site.getsitepackages()[0])"],
                cwd=repo,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        test_python = base_python
        test_pythonpath_extra = [overlay_site]
        if any(item["status"] != "passed" for item in data["bootstrap"].values()):
            data["status"] = "unknown_bootstrap"
        else:
            # Start with a clean Testmon DB and establish a positive real target baseline.
            data["target_baseline"] = pytest_run(repo, test_python, temp, base_site, ["--testmon", "-q", TARGET_NODE], 180, test_pythonpath_extra)
            source = repo / "marimo/_utils/formatter.py"
            original = source.read_text(encoding="utf-8")
            if MUTANT_OLD not in original:
                data["formatter_mutation"] = {"status": "unknown_mutant_not_found"}
            else:
                source.write_text(original.replace(MUTANT_OLD, MUTANT_NEW, 1), encoding="utf-8")
                try:
                    data["mutant_testmon"] = pytest_run(repo, test_python, temp, base_site, ["--testmon", "-q", TARGET_NODE], 180, test_pythonpath_extra)
                    data["mutant_direct"] = pytest_run(repo, test_python, temp, base_site, ["--no-testmon", "-q", TARGET_NODE], 180, test_pythonpath_extra)
                finally:
                    source.write_text(original, encoding="utf-8")

            # Use a fresh DB and the real built assets so this asks whether an
            # unchanged static resource is selected automatically.
            static_dir = repo / "marimo/_static"
            dist_dir = repo / "frontend/dist"
            static_backup = temp / "static-backup"
            if not dist_dir.is_dir():
                data["resource_mutation"] = {"status": "unknown_missing_assets"}
            else:
                shutil.copytree(static_dir, static_backup)
                shutil.rmtree(static_dir)
                shutil.copytree(dist_dir, static_dir)
                data["readiness"] = {
                    "staged_static_files": sum(1 for path in static_dir.rglob("*") if path.is_file()),
                    "staged_static_bytes": sum(path.stat().st_size for path in static_dir.rglob("*") if path.is_file()),
                }
                try:
                    Path(temp / "testmon.data").unlink(missing_ok=True)
                    data["resource_baseline"] = pytest_run(repo, test_python, temp, base_site, ["--testmon", "-q", RESOURCE_SCOPE], 180, test_pythonpath_extra)
                    favicon = static_dir / "favicon.ico"
                    backup = temp / "favicon.ico"
                    if favicon.exists():
                        shutil.copy2(favicon, backup)
                        favicon.unlink()
                        try:
                            data["resource_mutation_testmon"] = pytest_run(repo, test_python, temp, base_site, ["--testmon", "-q", RESOURCE_SCOPE], 180, test_pythonpath_extra)
                            data["resource_mutation_direct"] = pytest_run(repo, test_python, temp, base_site, ["--no-testmon", "-q", RESOURCE_SCOPE], 180, test_pythonpath_extra)
                        finally:
                            shutil.copy2(backup, favicon)
                    else:
                        data["resource_mutation"] = {"status": "unknown_asset_not_found"}
                finally:
                    shutil.rmtree(static_dir, ignore_errors=True)
                    shutil.copytree(static_backup, static_dir)
            data["status"] = "observed"
    data["elapsed_s"] = round(time.perf_counter() - started, 3)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return 0 if data["status"] == "observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

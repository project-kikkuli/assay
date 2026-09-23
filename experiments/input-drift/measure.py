"""Declared-input manifest drift: does a task's `inputs` list in assay.json
actually cover every file its command reads?

For each task, this traces real file opens during the task's real command
(via `trace_task.py`, a subprocess with a PEP 578 `sys.addaudithook`) and
compares the traced reads against the task's declared inputs expanded with
the project's own `assay.runner.inventory`. A traced read outside the
declared set is exactly the failure mode explicit-input selection depends on
not having: a real dependency the manifest doesn't know about.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ASSAY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ASSAY_ROOT))
from assay import runner  # noqa: E402

TRACER = Path(__file__).with_name("trace_task.py")
SOURCE_SUFFIXES = {".py"}


def clear_pycache(root: Path) -> None:
    """A pyc already on disk short-circuits the interpreter's import machinery
    before it ever opens the .py, which would hide a real read from the audit
    hook. This driver process itself imports `assay.runner`, so without this
    the first traced task would silently inherit the driver's own cache."""
    for cache_dir in root.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)


def trace_task(root: Path, command: list[str]) -> tuple[set[str], int]:
    assert command[0] == "{python}" and command[1] == "-m", f"unsupported command shape: {command}"
    clear_pycache(root)
    module, module_args = command[2], command[3:]
    out_path = root / ".assay-trace-out.json"
    out_path.unlink(missing_ok=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "ASSAY_TRACE_ROOT": str(root), "ASSAY_TRACE_OUT": str(out_path)}
    result = subprocess.run([sys.executable, str(TRACER), module, *module_args], cwd=root, env=env, capture_output=True, text=True)
    traced = set(json.loads(out_path.read_text())) if out_path.exists() else set()
    out_path.unlink(missing_ok=True)
    return traced, result.returncode


def source_only(paths: set[str]) -> set[str]:
    """Traced reads restricted to project source files: drop bytecode cache,
    the manifest itself, and the tracer's own output plumbing."""
    return {
        p for p in paths
        if Path(p).suffix in SOURCE_SUFFIXES
        and "__pycache__" not in Path(p).parts
        and p != "assay.json"
    }


def measure_task(root: Path, task: dict) -> dict:
    declared = set(runner.inventory(root, task.get("inputs")))
    traced_raw, returncode = trace_task(root, task["command"])
    traced = source_only(traced_raw)
    missed = sorted(traced - declared)
    over_declared = sorted(declared - traced)
    covered = declared & traced
    return {
        "task": task["id"],
        "returncode": returncode,
        "declared_count": len(declared),
        "traced_source_reads": len(traced),
        "covered": len(covered),
        "missed_undeclared_reads": missed,
        "over_declared_unused_inputs": over_declared,
        "precision": round(len(covered) / len(declared), 4) if declared else None,
        "recall": round(len(covered) / len(traced), 4) if traced else None,
    }


def measure_manifest(manifest_path: Path) -> dict:
    manifest = runner.load_manifest(manifest_path)
    root = manifest_path.parent
    return {"manifest": str(manifest_path.relative_to(ASSAY_ROOT)) if manifest_path.is_relative_to(ASSAY_ROOT) else str(manifest_path),
            "tasks": [measure_task(root, task) for task in manifest["tasks"]]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    args = parser.parse_args()

    manifests = [
        ASSAY_ROOT / "assay.json",
        Path(__file__).with_name("fixtures") / "undeclared" / "assay.json",
        Path(__file__).with_name("fixtures") / "declared" / "assay.json",
    ]
    report = {"measured": True, "manifests": [measure_manifest(m) for m in manifests]}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

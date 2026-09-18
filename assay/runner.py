"""Small command runner with explicit, inspectable evidence reuse.

This is not a sandbox or a proof system. In particular, declared inputs are a
human-maintained contract and the local cache is not an authenticated store.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any

IGNORED = {".git", ".assay", "out", ".venv", "node_modules", "__pycache__"}
STATUSES = {"verified", "rejected", "unresolved"}
SCHEMA = "assay.report/v1"


class ManifestError(ValueError):
    """An obligation cannot be determined unambiguously."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _relative(value: str) -> None:
    if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise ManifestError(f"Expected a non-empty project-relative path: {value!r}")


def load_manifest(path: Path) -> dict:
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ManifestError(f"Cannot read manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ManifestError("Expected manifest version 1")
    if set(manifest) - {"version", "name", "tasks"}:
        raise ManifestError("Unknown manifest keys")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ManifestError("Manifest must have non-empty tasks")
    ids = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ManifestError("Task must be an object")
        if set(task) - {"id", "command", "inputs", "needs", "timeout_s", "env", "description", "source"}:
            raise ManifestError("Unknown task keys")
        name = task.get("id")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", name) or name in ids:
            raise ManifestError(f"Invalid or duplicate task id: {name!r}")
        ids.add(name)
        command = task.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
            raise ManifestError(f"{name}: command must be a non-empty argv list")
        if "inputs" in task:
            if not isinstance(task["inputs"], list) or not task["inputs"]:
                raise ManifestError(f"{name}: empty inputs are not evidence")
            for pattern in task["inputs"]:
                _relative(pattern)
        needs = task.get("needs", [])
        if not isinstance(needs, list) or any(not isinstance(x, str) for x in needs) or len(needs) != len(set(needs)):
            raise ManifestError(f"{name}: needs must contain unique task ids")
        timeout = task.get("timeout_s", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
            raise ManifestError(f"{name}: timeout must be finite and positive")
        env = task.get("env", {})
        if not isinstance(env, dict) or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", k) or not isinstance(v, str) or "\x00" in v for k, v in env.items()):
            raise ManifestError(f"{name}: env must map variable names to string values")
        sources = task.get("source", [])
        if not isinstance(sources, list):
            raise ManifestError(f"{name}: source must be a list")
        for source in sources:
            _relative(source)
    by_id = {task["id"]: task for task in tasks}
    visiting, visited = set(), set()

    def visit(name: str) -> None:
        if name not in by_id:
            raise ManifestError(f"Unknown dependency: {name}")
        if name in visiting:
            raise ManifestError(f"Dependency cycle involving {name}")
        if name in visited:
            return
        visiting.add(name)
        for child in by_id[name].get("needs", []):
            visit(child)
        visiting.remove(name)
        visited.add(name)

    for name in ids:
        visit(name)
    return manifest


def inventory(root: Path, patterns: list[str] | None = None) -> dict[str, str]:
    """Hash names as well as content: added/deleted glob matches invalidate."""
    files: dict[str, str] = {}
    for pattern in patterns or ["**/*"]:
        _relative(pattern)
        matched = False
        for path in sorted(root.glob(pattern)):
            relative = path.relative_to(root)
            if any(part in IGNORED for part in relative.parts) or path.suffix == ".pyc":
                continue
            # Parent symlinks matter too: glob can traverse a symlinked directory.
            if any(p.is_symlink() for p in [path, *path.parents] if p != root and root in p.parents):
                raise ManifestError(f"Symlink input is not supported: {relative}")
            if not path.is_file():
                continue
            if not path.resolve().is_relative_to(root):
                raise ManifestError(f"Input escaped project: {relative}")
            matched = True
            files[relative.as_posix()] = file_digest(path)
        if patterns is not None and not matched:
            raise ManifestError(f"Input pattern matches no files: {pattern}")
    if not files:
        raise ManifestError("No source inputs found")
    return dict(sorted(files.items()))


def runtime_identity() -> dict:
    return {
        "python": sys.version,
        "implementation": platform.python_implementation(),
        "executable_sha256": file_digest(Path(sys.executable).resolve()),
        "system": platform.system(), "release": platform.release(),
        "machine": platform.machine(), "sqlite": sqlite3.sqlite_version,
        "runner_sha256": file_digest(Path(__file__)),
    }


def _read_cache(cache: Path, key: str) -> dict | None:
    try:
        envelope = json.loads((cache / f"{key}.json").read_text())
        value = envelope["evidence"]
        if envelope["sha256"] != digest(value) or value["key"] != key or value["status"] != "verified" or value["returncode"] != 0:
            return None
        if not all(k in value for k in ("stdout", "stderr", "duration_ms", "input_files", "command")):
            return None
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_cache(cache: Path, evidence: dict) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    blob = canonical({"evidence": evidence, "sha256": digest(evidence)})
    with tempfile.NamedTemporaryFile(dir=cache, delete=False) as output:
        output.write(blob)
        temporary = Path(output.name)
    temporary.replace(cache / f"{evidence['key']}.json")


def _tail(stream, limit: int = 65536) -> str:
    size = stream.tell()
    stream.seek(max(0, size - limit))
    text = stream.read().decode("utf-8", "replace")
    return ("[output truncated; last 64 KiB]\n" if size > limit else "") + text


def _execute(root: Path, command: list[str], env: dict, timeout: float) -> dict:
    """Spool output to disk, and stop the owned process group on timeout."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, start_new_session=True)
        except (OSError, ValueError) as exc:
            return {"status": "unresolved", "reason": f"Could not launch command: {exc}", "returncode": None, "stdout": "", "stderr": ""}
        try:
            code = process.wait(timeout=timeout)
            result = {"status": "verified" if code == 0 else "rejected", "reason": "Command completed successfully" if code == 0 else f"Command exited {code}", "returncode": code}
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.wait()
            result = {"status": "unresolved", "reason": f"Execution exceeded {timeout:g}s; no verdict", "returncode": None}
        result.update(stdout=_tail(stdout), stderr=_tail(stderr))
        return result


def run(manifest_path: str | Path, *, root: str | Path | None = None,
        cache_dir: str | Path | None = None, use_cache: bool = True, jobs: int = 4) -> dict:
    """Run all obligations, including every dependency, and return a report."""
    started = time.perf_counter()
    manifest_path = Path(manifest_path).resolve()
    root = Path(root).resolve() if root else manifest_path.parent
    report = {"schema": SCHEMA, "name": manifest_path.stem, "status": "unresolved",
              "candidate": "", "tasks": [], "warnings": [
                  "Local advisory evidence, not authenticated CI attestations.",
                  "Declared inputs and a narrowed environment do not constitute hermetic execution.",
                  "Generated directories are excluded; do not place source inputs there."]}
    try:
        if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
            raise ManifestError("jobs must be a positive integer")
        manifest = load_manifest(manifest_path)
        report["name"] = manifest.get("name", manifest_path.stem)
        before = inventory(root)
        report["candidate"] = digest(before)
        policy_hash = digest(manifest)
        runtime = runtime_identity()
        cache = Path(cache_dir).resolve() if cache_dir else root / ".assay/cache"
        tasks = {t["id"]: t for t in manifest["tasks"]}
        completed: dict[str, dict] = {}

        def task_run(task: dict, dependencies: dict[str, dict]) -> dict:
            tick = time.perf_counter()
            base = {"id": task["id"], "description": task.get("description", ""),
                    "source": task.get("source", []), "cached": False,
                    "needs": task.get("needs", []), "start_ms": (tick - started) * 1000,
                    "input_files": [], "command": task["command"], "key": "",
                    "returncode": None, "stdout": "", "stderr": ""}
            try:
                if any(d["status"] != "verified" for d in dependencies.values()):
                    return {**base, "status": "unresolved", "reason": "Required dependency has no successful evidence", "duration_ms": 0.0}
                inputs = inventory(root, task.get("inputs"))
                base["input_files"] = list(inputs)
                command = [sys.executable if part == "{python}" else part for part in task["command"]]
                environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "TZ": "UTC", "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1", **task.get("env", {})}
                executable = shutil.which(command[0], path=environment["PATH"])
                executable_hash = file_digest(Path(executable).resolve()) if executable else None
                key = digest({"schema": SCHEMA, "policy": policy_hash, "task": task,
                              "inputs": inputs, "runtime": runtime, "environment": environment,
                              "executable_sha256": executable_hash,
                              "dependencies": {k: v["key"] for k, v in dependencies.items()}})
                base["key"] = key
                cached = _read_cache(cache, key) if use_cache else None
                if cached is not None:
                    outcome = {**base, "status": "verified", "cached": True,
                               "reason": "Reused successful evidence: all declared inputs, policy, runtime, and dependencies match",
                               "returncode": 0, "stdout": cached["stdout"], "stderr": cached["stderr"],
                               "original_duration_ms": cached["duration_ms"]}
                else:
                    outcome = {**base, **_execute(root, command, environment, task.get("timeout_s", 30))}
                    outcome["reason"] += "; no reusable evidence" if use_cache else "; cache bypassed"
                if inventory(root, task.get("inputs")) != inputs:
                    outcome.update(status="unresolved", cached=False, reason="Source inputs changed during execution; evidence discarded")
                outcome["duration_ms"] = (time.perf_counter() - tick) * 1000
                if use_cache and not outcome["cached"] and outcome["status"] == "verified":
                    try:
                        _write_cache(cache, outcome)
                    except OSError:
                        outcome["reason"] += "; cache write failed (execution result retained)"
                return outcome
            except (ManifestError, OSError, ValueError) as exc:
                return {**base, "status": "unresolved", "reason": str(exc), "duration_ms": (time.perf_counter() - tick) * 1000}

        pending = list(tasks)
        active = {}
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            while pending or active:
                for name in pending[:]:
                    dependencies = tasks[name].get("needs", [])
                    if all(dep in completed for dep in dependencies):
                        future = pool.submit(task_run, tasks[name], {dep: completed[dep] for dep in dependencies})
                        active[future] = name
                        pending.remove(name)
                if not active:
                    raise ManifestError("No progress possible in obligation graph")
                finished, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in finished:
                    name = active.pop(future)
                    completed[name] = future.result()
        report["tasks"] = [completed[name] for name in tasks]
        statuses = {t["status"] for t in report["tasks"]}
        report["status"] = "rejected" if "rejected" in statuses else "unresolved" if "unresolved" in statuses else "verified"
        if inventory(root) != before or digest(load_manifest(manifest_path)) != policy_hash:
            report["status"] = "unresolved"
            report["warnings"].append("Project or policy changed during the run; candidate-level evidence is unresolved.")
    except (ManifestError, OSError, ValueError) as exc:
        report["warnings"].append(str(exc))
    report["duration_ms"] = (time.perf_counter() - started) * 1000
    return report


def audit(manifest_path: str | Path, *, repeats: int = 3, **kwargs) -> dict:
    """Never retry-to-green: retain all outcomes and flag observed variation."""
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 2:
        raise ValueError("An audit needs at least two executions")
    kwargs.pop("use_cache", None)
    reports = [run(manifest_path, use_cache=False, **kwargs) for _ in range(repeats)]
    result = dict(reports[-1])
    result["duration_ms"] = sum(r["duration_ms"] for r in reports)
    result["repetitions"] = reports
    observations: dict[str, list[str]] = {}
    for report in reports:
        for task in report["tasks"]:
            observations.setdefault(task["id"], []).append(task["status"])
    result["unstable_tasks"] = {name: statuses for name, statuses in observations.items() if len(set(statuses)) > 1}
    states = {r["status"] for r in reports}
    result["status"] = "rejected" if "rejected" in states else "unresolved" if "unresolved" in states else "verified"
    if result["unstable_tasks"]:
        result["warnings"] = [*result["warnings"], "Observed verdict variation; a later pass does not erase earlier failures."]
    result["warnings"] = [*result["warnings"], "Repeat stability is sampled evidence, not proof of determinism."]
    return result

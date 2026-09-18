#!/usr/bin/env python3
"""Replay an explicit-input fast path on Marimo's actual 41-test asset suite."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from closure import action_key, digest, inventory, reusable
from run_selection import RESOURCE_SCOPE, REVISION, pytest_run, safe_env, sanitize_output

HERE = Path(__file__).resolve().parent
INPUTS = ["marimo", "tests", "pyproject.toml", "uv.lock", "conftest.py"]
EXPECTED = 41


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=HERE / "closure-results.json")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"status": "unknown", "revision": REVISION, "scope": RESOURCE_SCOPE,
              "expected_tests": EXPECTED, "cases": [], "inputs": INPUTS,
              "limits": ["One 41-test obligation, not admission for the entire application",
                         "Trusted sequential host fixture; no protected issuer or immutable sandbox",
                         "Dependency versions identify the reused venv, not all dependency bytes",
                         "Explicit roots, not inferred dependency closure; cache directories excluded",
                         "Preparation and dependencies outside measured decision time"]}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    try:
        original = args.repo_dir.resolve()
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=original, text=True).strip()
        if revision != REVISION:
            raise ValueError("requires the pinned public Marimo revision")
        python = original / ".venv/bin/python"
        with tempfile.TemporaryDirectory(prefix="assay-input-closure-") as scratch:
            temp = Path(scratch)
            repo = temp / "subject"
            repo.mkdir()
            started = time.perf_counter()
            archive = subprocess.check_output(["git", "archive", REVISION], cwd=original)
            with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
                stream.extractall(repo, filter="data")
            # Only the scratch checkout changes. No mutation/restoration of the caller's source.
            shutil.copytree(original / "frontend/dist", repo / "marimo/_static", dirs_exist_ok=True)
            (repo / ".venv").symlink_to(original / ".venv", target_is_directory=True)
            python = repo / ".venv/bin/python"
            env = safe_env(temp, pythonpath=[repo], path_prefix=repo / ".venv/bin")
            probe = subprocess.check_output([str(python), "-c",
                "import marimo,site,sys,json,importlib.metadata as m; print(json.dumps(dict("
                "source=marimo.__file__,site=site.getsitepackages()[0],python=sys.version,"
                "packages=sorted((d.metadata['Name'],d.version) for d in m.distributions()))))"],
                cwd=repo, env=env, text=True)
            runtime = json.loads(probe)
            if not Path(runtime.pop("source")).resolve().is_relative_to(repo.resolve()):
                raise ValueError("interpreter imported the original checkout, not the captured candidate")
            site = Path(runtime.pop("site"))
            runtime["executable_sha256"] = hashlib.sha256(python.read_bytes()).hexdigest()
            report["preparation_seconds"] = round(time.perf_counter() - started, 6)
            report["runtime"] = runtime
            sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
                       [Path(__file__), HERE / "closure.py", HERE / "run_selection.py",
                        HERE / "python_selection_timing.py", HERE / "test_closure.py"]}
            report["source_sha256"] = sources
            command = ["python", "-m", "pytest", "-q", RESOURCE_SCOPE]
            verifier = digest(sources)
            receipt = None

            def check(name: str, execute: bool = True, context: dict | None = None,
                      invocation: list[str] | None = None, oracle: str | None = None) -> dict:
                nonlocal receipt
                started = time.perf_counter()
                before = inventory(repo, INPUTS)
                key = action_key(before, invocation or command, context or runtime, oracle or verifier)
                record = {"name": name, "key": key, "inventory_entries": len(before),
                          "hash_seconds": round(time.perf_counter() - started, 6)}
                if reusable(receipt, key, EXPECTED):
                    record.update(decision="reuse", passed=receipt["passed"])
                elif not execute:
                    record.update(decision="rerun_required")
                else:
                    result = pytest_run(repo, python, temp, site, ["-q", RESOURCE_SCOPE], 120)
                    after = inventory(repo, INPUTS)
                    counts = result["counts"]
                    valid = (result["status"] == "passed" and counts.get("passed") == EXPECTED
                             and not any(counts.get(k, 0) for k in ("failed", "skipped", "xfailed", "xpassed"))
                             and before == after)
                    record.update(decision="executed_pass" if valid else "not_green",
                                  result=result, inputs_stable=before == after)
                    if valid:
                        receipt = dict(key=key, status="passed", passed=EXPECTED, failed=0, skipped=0)
                record["decision_seconds"] = round(time.perf_counter() - started, 6)
                report["cases"].append(record)
                return record

            baseline = check("baseline")
            if baseline["decision"] != "executed_pass":
                raise ValueError("no positive baseline; reuse is forbidden")
            for index in range(5):
                check(f"unchanged-{index + 1}")
            favicon = repo / "marimo/_static/favicon.ico"
            original_icon = favicon.read_bytes()
            favicon.unlink()
            check("deleted-favicon")
            favicon.write_bytes(original_icon)
            check("restored-favicon")
            extra = repo / "marimo/_static/assay-added-resource.txt"
            extra.write_text("synthetic resource addition\n")
            check("added-resource")
            extra.unlink()
            # A real source change also invalidates; do not claim semantic equivalence from comments.
            endpoint = repo / "marimo/_server/api/endpoints/assets.py"
            endpoint.write_text(endpoint.read_text() + "\n# Synthetic invalidation probe.\n")
            check("changed-python-source")
            correct_source = endpoint.read_text()
            if correct_source.count('cache_control = "private, no-cache"') != 1:
                raise ValueError("expected cache-policy mutation point is absent")
            endpoint.write_text(correct_source.replace('cache_control = "private, no-cache"',
                                                       'cache_control = "public, max-age=86400"', 1))
            check("unsafe-public-html-cache")
            endpoint.write_text(correct_source)
            check("changed-runtime", execute=False, context=runtime | {"probe": "different-runtime"})
            check("changed-command", execute=False, invocation=command + ["--new-mode"])
            check("changed-verifier", execute=False, oracle="different-verifier")
            observed = {case["name"]: case["decision"] for case in report["cases"]}
            expected = {"baseline": "executed_pass", "deleted-favicon": "not_green",
                        "restored-favicon": "reuse", "added-resource": "executed_pass",
                        "changed-python-source": "executed_pass", "unsafe-public-html-cache": "not_green",
                        "changed-runtime": "rerun_required",
                        "changed-command": "rerun_required", "changed-verifier": "rerun_required",
                        **{f"unchanged-{i + 1}": "reuse" for i in range(5)}}
            deleted = next(case for case in report["cases"] if case["name"] == "deleted-favicon")
            reproduced_fault = deleted.get("result", {}).get("counts", {}).get("failed") == 1
            unsafe = next(case for case in report["cases"] if case["name"] == "unsafe-public-html-cache")
            detected_policy_fault = unsafe.get("result", {}).get("counts", {}).get("failed", 0) > 0
            report["status"] = "passed" if observed == expected and reproduced_fault and detected_policy_fault else "not_green"
    except Exception as exc:
        report["error"] = type(exc).__name__
        report["error_detail"] = sanitize_output(str(exc))
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "cases": [
        {k: case[k] for k in ("name", "decision", "decision_seconds")} for case in report["cases"]]}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

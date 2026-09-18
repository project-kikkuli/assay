#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]==3.3.4"]
# ///
"""Measure complete warm verification, including real API, browser, and wire checks."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from run import HERE, REVISION, Lab, artifact_identity, clean_env, run, sanitize, tree_identity


def policy_identity():
    roots = [HERE, HERE.parent / "boundaries"]
    return {str(path.relative_to(HERE.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
            for root in roots for path in sorted(root.iterdir())
            if path.suffix in {".py", ".ts", ".mts", ".cjs", ".json"}}


def decide(records, boundary, boundary_exit, expected_source):
    """Positive evidence is mandatory; absence, setup failure, or drift is not green."""
    by_name = {record["check"]: record for record in records}
    required = {"upstream": 62, "oracle": 7, "lifecycle": 4, "browser": 62}
    required_names = (*required, "build", "typecheck", "mypy", "ty", "ruff", "python_format")
    if any(sum(record["check"] == name for record in records) != 1 for name in required_names):
        return "unresolved"
    for name, minimum in required.items():
        tests = by_name[name].get("tests", {})
        observed = tests.get("actual_passed", 0) if name == "browser" else tests.get("passed", 0)
        problems = any(tests.get(key, 0) for key in ("failed", "errors", "unexpected", "flaky", "skipped"))
        if problems:
            return "unresolved" if by_name[name]["status"] == "passed" else "rejected"
        if observed < minimum:
            return "rejected" if tests.get("failed") or tests.get("unexpected") else "unresolved"
    if any(record["status"] != "passed" for record in records):
        return "unresolved" if any(record["status"] == "unresolved" for record in records) else "rejected"
    required_claims = {
        "invalid_title_is_client_error", "invalid_title_does_not_modify_storage",
        "invalid_offset_is_client_error", "all_owned_items_reachable",
        "stored_title_does_not_execute",
    }
    claims = boundary.get("claims", {})
    if boundary.get("source_sha256") != expected_source or boundary.get("revision") != REVISION:
        return "unresolved"
    if not required_claims.issubset(claims) or not boundary.get("source_unchanged") or not boundary.get("oracle_unchanged"):
        return "unresolved"
    if boundary_exit not in {0, 1}:
        return "unresolved"
    return "supported" if boundary_exit == 0 and all(claims[name] is True for name in required_claims) else "rejected"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--serial", action="store_true", help="run all checks sequentially for comparison")
    parser.add_argument("--observe-http", action="store_true", help="record local Node fixture transport metadata without retrying")
    parser.add_argument("--output", type=Path, default=Path("out/gate.json"))
    args = parser.parse_args()
    if args.repeat < 1 or args.workers < 1:
        parser.error("repeat and workers must be positive")
    subject = args.subject.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=subject, text=True).strip()
    if revision != REVISION:
        parser.error("prepare the pinned upstream revision first")
    report = {
        "revision": revision, "source_sha256": tree_identity(subject), "policy_sha256": policy_identity(),
        "logical_cpus": os.cpu_count(), "http_observation_enabled": args.observe_http,
        "runs": [], "setup_included": False, "schedule": "serial" if args.serial else "isolated-parallel",
        "scope": "Complete repaired-template suite, seven frozen API checks, four lifecycle checks, five boundary claims; no test selection or result cache",
        "limits": ["Warm installed dependencies and running disposable PostgreSQL/Mailpit", "Hosted dispatch excluded",
                   "Host execution, not an adversarial sandbox", "Finite observations, not universal correctness",
                   "Candidate-owned upstream tests; additional oracles are separate but not OS-protected"],
    }
    dsn = os.environ.get("ASSAY_PG_DSN", "postgresql://postgres:assay-local-only@127.0.0.1:55439/postgres")
    try:
        for _ in range(args.repeat):
            started = time.perf_counter()
            lab = Lab(subject, dsn, shutil.which("node") or "node", subject / ".venv/bin/python", [])
            sample = {"records": lab.records, "verdict": "unresolved"}
            report["runs"].append(sample)
            try:
                lab.frontend(subject)
                built = lab.frontend(subject, build=True)
                if built["status"] != "passed":
                    raise RuntimeError("build failed; refusing to test stale assets")
                artifact = subject / "backend/app/frontend"
                sample["built_artifact_sha256"] = artifact_identity(artifact)
                def api_checks():
                    lab.python_static()
                    lab.backend(subject)
                    lab.backend(subject, "oracle")
                    lab.backend(subject, "lifecycle", oracle=HERE / "lifecycle_tests.py")
                with tempfile.TemporaryDirectory(prefix="assay-gate-") as scratch:
                    evidence = Path(scratch) / "boundary.json"
                    def boundary_checks():
                        return run([sys.executable, HERE.parent / "boundaries/run.py", "--subject", subject,
                                    "--enforce", "--output", evidence, "--built-artifact", sample["built_artifact_sha256"]],
                                   HERE.parent.parent, clean_env() | {"ASSAY_PG_DSN": dsn}, timeout=210)
                    if args.serial:
                        api_checks()
                        lab.browser(args.workers, args.observe_http)
                        result = boundary_checks()
                    else:
                        # Separate processes and UUID databases; the built assets are read-only inputs.
                        with ThreadPoolExecutor(max_workers=3) as pool:
                            api = pool.submit(api_checks)
                            browser = pool.submit(lab.browser, args.workers, args.observe_http)
                            boundaries = pool.submit(boundary_checks)
                            api.result()
                            browser.result()
                            result = boundaries.result()
                    boundary = json.loads(evidence.read_text()) if evidence.exists() else {}
                    sample["boundary"] = boundary
                    sample["boundary_process_seconds"] = result["seconds"]
                    sample["boundary_exit"] = result["exit_code"]
                    sample["verdict"] = decide(lab.records, boundary, result["exit_code"], report["source_sha256"])
                    sample["artifact_unchanged"] = sample["built_artifact_sha256"] == artifact_identity(artifact)
                    if not sample["artifact_unchanged"]:
                        sample["verdict"] = "unresolved"
            finally:
                sample["seconds"] = time.perf_counter() - started
            print(json.dumps({"gate": sample["verdict"], "seconds": sample["seconds"]}), flush=True)
    except Exception as exc:
        report["harness_error"] = sanitize(f"{type(exc).__name__}: {exc}", subject)
    finally:
        report["source_unchanged"] = report["source_sha256"] == tree_identity(subject)
        report["policy_unchanged"] = report["policy_sha256"] == policy_identity()
        supported = (len(report["runs"]) == args.repeat and not report.get("harness_error")
                     and report["source_unchanged"] and report["policy_unchanged"]
                     and all(sample["verdict"] == "supported" for sample in report["runs"]))
        unknown = (report.get("harness_error") or not report["source_unchanged"] or not report["policy_unchanged"]
                   or any(sample["verdict"] == "unresolved" for sample in report["runs"]))
        report["verdict"] = "supported" if supported else "unresolved" if unknown else "rejected"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if supported else 1


if __name__ == "__main__":
    raise SystemExit(main())

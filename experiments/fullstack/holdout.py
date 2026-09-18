# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]==3.3.4"]
# ///
"""Challenge frozen oracles with faults proposed without access to those oracles."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time

from run import HERE, REVISION, Lab, sanitize, tree_identity


def classify(record):
    tests = record.get("tests", {})
    if record["status"] == "passed" and tests.get("passed", 0) > 0:
        return "survived"
    if tests.get("failed", 0) > 0 and tests.get("errors", 0) == 0:
        return "caught"
    return "unresolved"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("out/holdout.json"))
    args = parser.parse_args()
    subject = args.subject.resolve()
    if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=subject, text=True).strip() != REVISION:
        parser.error("expected pinned original public subject")
    if subprocess.check_output(["git", "diff", "HEAD", "--name-only"], cwd=subject, text=True).strip():
        parser.error("holdout compares against the unmodified pinned subject")
    policy_files = [HERE / name for name in ("run.py", "oracle_tests.py", "holdout.py", "holdout-faults.json")]
    def identities():
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in policy_files}
    report = {
        "revision": REVISION, "source_sha256": tree_identity(subject), "policy_sha256": identities(),
        "provenance": "Muse worker saw only public backend source: no tests, oracle, original faults, or parent conversation",
        "limits": ["Hand-proposed faults, not a random sample or unseen production defect rate",
                   "H09 independently duplicates the original count-filter challenge; not a novel holdout",
                   "Host execution, not adversarial confinement", "Original oracle remains unchanged"],
        "records": [], "matrix": [], "status": "unresolved",
    }
    lab = Lab(subject, os.environ.get("ASSAY_PG_DSN", "postgresql://postgres:assay-local-only@127.0.0.1:55439/postgres"),
              shutil.which("node") or "node", subject / ".venv/bin/python", report["records"])
    started = time.perf_counter()
    try:
        baseline = [lab.backend(subject), lab.backend(subject, "oracle")]
        if any(classify(record) != "survived" for record in baseline):
            raise RuntimeError("positive baseline required before interpreting faults")
        with tempfile.TemporaryDirectory(prefix="assay-holdout-") as temporary:
            root = Path(temporary).resolve()
            archive = root / "source.tar"
            subprocess.run(["git", "archive", "--output", str(archive), REVISION], cwd=subject, check=True)
            candidate = root / "candidate"
            candidate.mkdir()
            with tarfile.open(archive) as bundle:
                bundle.extractall(candidate, filter="data")
            for fault in json.loads((HERE / "holdout-faults.json").read_text()):
                target = (candidate / fault["path"]).resolve()
                if not target.is_relative_to(candidate):
                    raise ValueError("fault escaped candidate directory")
                original = target.read_text()
                if original.count(fault["before"]) != 1:
                    raise ValueError(f"{fault['id']}: ambiguous preimage")
                mutated = original.replace(fault["before"], fault["after"], 1)
                ast.parse(mutated)
                target.write_text(mutated)
                try:
                    outcomes = {kind: classify(lab.backend(candidate, kind, fault["id"]))
                                for kind in ("upstream", "oracle")}
                    report["matrix"].append({"fault": fault["id"], "category": fault["category"],
                                             "claim": fault["claim"], **outcomes})
                finally:
                    target.write_text(original)
        report["status"] = "completed"
    except Exception as error:
        report["error"] = sanitize(f"{type(error).__name__}: {error}", subject)
    finally:
        report["seconds"] = time.perf_counter() - started
        report["policy_unchanged"] = report["policy_sha256"] == identities()
        report["source_unchanged"] = report["source_sha256"] == tree_identity(subject)
        if not report["policy_unchanged"] or not report["source_unchanged"]:
            report["status"] = "unresolved"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "seconds": report["seconds"], "matrix": report["matrix"]}), flush=True)
    return 0 if report["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

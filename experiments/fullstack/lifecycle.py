# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]==3.3.4"]
# ///
"""Replay two blind survivors against additional, explicitly post-challenge checks."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from holdout import classify
from run import HERE, REVISION, Lab, sanitize


def replay(subject, report):
    lab = Lab(subject, os.environ.get("ASSAY_PG_DSN", "postgresql://postgres:assay-local-only@127.0.0.1:55439/postgres"),
              shutil.which("node") or "node", subject / ".venv/bin/python", report["records"])
    with tempfile.TemporaryDirectory(prefix="assay-lifecycle-") as temporary:
        candidate = Path(temporary).resolve() / "candidate"
        candidate.mkdir()
        archive = Path(temporary) / "source.tar"
        subprocess.run(["git", "archive", "--output", str(archive), REVISION], cwd=subject, check=True)
        with tarfile.open(archive) as bundle:
            bundle.extractall(candidate, filter="data")
        baseline = lab.backend(candidate, "lifecycle", oracle=HERE / "lifecycle_tests.py")
        if classify(baseline) != "survived" or baseline.get("tests", {}).get("passed") != 4:
            raise RuntimeError("four positive lifecycle checks required")
        for fault in json.loads((HERE / "holdout-faults.json").read_text()):
            if fault["id"] not in {"H10", "H12"}:
                continue
            target = (candidate / fault["path"]).resolve()
            if not target.is_relative_to(candidate):
                raise ValueError("fault escaped candidate")
            before = target.read_text()
            if before.count(fault["before"]) != 1:
                raise ValueError("ambiguous preimage")
            target.write_text(before.replace(fault["before"], fault["after"], 1))
            try:
                result = lab.backend(candidate, "lifecycle", fault["id"], oracle=HERE / "lifecycle_tests.py")
                report["matrix"].append({"fault": fault["id"], "outcome": classify(result)})
            finally:
                target.write_text(before)
    report["oracle_unchanged"] = report["oracle_sha256"] == hashlib.sha256((HERE / "lifecycle_tests.py").read_bytes()).hexdigest()
    report["expected_outcomes_observed"] = (
        report["oracle_unchanged"] and len(report["matrix"]) == 2
        and {row["fault"] for row in report["matrix"]} == {"H10", "H12"}
        and all(row["outcome"] == "caught" for row in report["matrix"])
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("out/lifecycle.json"))
    args = parser.parse_args()
    subject = args.subject.resolve()
    report = {"revision": REVISION, "scope": "Post-challenge requirements; these are no longer blind faults",
              "records": [], "matrix": [], "expected_outcomes_observed": False, "status": "unresolved"}
    # Replace old evidence before setup; even interruption cannot leave a stale green report.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    try:
        report["oracle_sha256"] = hashlib.sha256((HERE / "lifecycle_tests.py").read_bytes()).hexdigest()
        replay(subject, report)
        report["status"] = "supported" if report["expected_outcomes_observed"] else "rejected"
    except Exception as error:
        report["error"] = sanitize(f"{type(error).__name__}: {error}", subject)
        report["expected_outcomes_observed"] = False
    finally:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["matrix"]))
    return 0 if report["expected_outcomes_observed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Establish a local operator-trusted baseline; never run this in candidate CI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time

from run import HERE, ROOT, source_identity, trusted_inputs, write


def composition_valid(report):
    cases = [r for r in report.get("records", []) if r.get("mode") in {"baseline", "cell"}]
    if report.get("status") != "passed" or report.get("source_unchanged") is not True:
        return False
    if len(cases) != 2 or {r["mode"] for r in cases} != {"baseline", "cell"}:
        return False
    for case in cases:
        browser = case.get("browser", {})
        inventory = browser.get("inventory", {})
        if (case.get("status") != "passed" or case.get("cleanup") != "removed"
                or browser.get("status") != "passed" or inventory.get("item_tests") != 9
                or inventory.get("auth_setup_tests") != 1 or inventory.get("total") != 10
                or len(browser.get("cases", [])) != 10
                or any(c.get("status") != "passed" for c in browser.get("cases", []))
                or not browser.get("cross_tenant", {}).get("passed")
                or not browser.get("admin_positive_control", {}).get("passed")):
            return False
        if case["mode"] == "cell":
            shutdown = case.get("shutdown", {})
            actor = shutdown.get("actor", {})
            if (shutdown.get("status") != "passed" or not actor.get("calls")
                    or actor.get("cleanup", {}).get("status") != "removed"
                    or actor.get("container_controls", {}).get("passed") is not True
                    or actor.get("source_identity_stable") is not True):
                return False
    return True


def main():
    started = time.perf_counter()
    subject = ROOT / "out/lab/fullstack"
    python = subject / ".venv/bin/python"
    output = ROOT / "out/cell"
    report = {"status": "unknown", "steps": [], "baseline_written": False}
    write(output / "qualification.json", report)
    # A failed refresh must not leave a previously accepted local baseline active.
    write(output / "baseline.json", {})
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", "/tmp"), "PYTHONDONTWRITEBYTECODE": "1",
           "ASSAY_CELL_ADMIN_DSN": "host=127.0.0.1 port=55439 dbname=postgres user=postgres password=assay-local-only"}
    try:
        identity = source_identity(subject)
        before = trusted_inputs(identity)
        commands = [
            ("boundary_tests", [str(python), "-m", "unittest", "discover", "-s", "experiments/cell", "-v"]),
            ("core_challenges", [str(python), str(HERE / "run.py"), "--repeat", "3", "--challenges",
                                 "--output", str(output / "measured.json")]),
            ("composition", [str(python), str(HERE / "integration.py"), "--output", str(output / "composition.json")]),
        ]
        for name, argv in commands:
            step_started = time.perf_counter()
            result = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
            step = {"name": name, "exit_code": result.returncode,
                    "seconds": time.perf_counter() - step_started}
            report["steps"].append(step)
            print(json.dumps(step), flush=True)
            if result.returncode:
                raise RuntimeError(name + " failed; see its fresh report")
            if name == "boundary_tests":
                count = re.search(r"Ran (\d+) tests", result.stderr)
                if not count or int(count[1]) < 19 or "skipped=" in result.stderr:
                    raise RuntimeError("missing or skipped boundary tests")
                step["tests_passed"] = int(count[1])
            write(output / "qualification.json", report)
        measured = json.loads((output / "measured.json").read_text())
        composition = json.loads((output / "composition.json").read_text())
        if (measured.get("status") != "completed" or measured.get("challenge_expectations_met") is not True
                or measured.get("positive_gate_passed") is not True or not composition_valid(composition)):
            raise RuntimeError("qualification evidence incomplete")
        if before != trusted_inputs(source_identity(subject)) or before != measured.get("trusted_inputs"):
            raise RuntimeError("inputs changed while qualification was running")
        write(output / "baseline.json", before)
        report.update(status="qualified_locally", baseline_written=True)
    except BaseException as error:
        report.update(status="unknown", failure=type(error).__name__)
    finally:
        report["seconds"] = time.perf_counter() - started
        write(output / "qualification.json", report)
    return 0 if report["baseline_written"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

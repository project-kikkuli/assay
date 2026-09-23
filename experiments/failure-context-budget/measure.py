"""How much of a failing CI log does an agent actually need to localize the
failure -- the first N lines, the last N lines, a compact traceback mode, or
structured JUnit XML -- measured against real failures in pytest's own
`testing/` suite (same real historical bug and injection method as
`../failure-ordering/`: a real pre-fix commit plus the real fix commit's own
regression test overlaid on top, unmodified).

Two real, unordered, non-stopping full runs at the same scenario:
  1. `--tb=long -q --junit-xml=...` -- pytest's default human report (full
     tracebacks in a FAILURES section, then a compact "short test summary
     info" section) plus structured JUnit XML in the same run.
  2. `--tb=line -q` -- pytest's most compact human traceback mode, one line
     per failure.

Ground truth per failing test comes from JUnit XML's own `<failure type=...>`
attribute (the exception class) and the testcase's `classname`/`name` (the
nodeid) -- structured data pytest emits itself, not a regex over prose. For
each representation, this finds the fewest lines from the relevant end of
the log a reader would need before the exception class name and the test's
own file appear together, and reports the byte size of each representation
so the comparison isn't lines-shaped only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent


def git(repo_dir: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True, text=True, check=True
    ).stdout.strip()


def checkout_scenario(repo_dir: str, scenario: dict) -> str:
    base_sha = git(repo_dir, "rev-parse", scenario["base_ref"])
    subprocess.run(["git", "checkout", "--quiet", "--force", "--detach", base_sha], cwd=repo_dir, check=True)
    subprocess.run(["git", "clean", "--quiet", "-fd", "--", *scenario["overlay_paths"]], cwd=repo_dir, check=True)
    for path in scenario["overlay_paths"]:
        content = subprocess.run(
            ["git", "show", f"{scenario['overlay_ref']}:{path}"], cwd=repo_dir, capture_output=True, text=True, check=True
        ).stdout
        (Path(repo_dir) / path).write_text(content)
    return base_sha


def run_pytest(repo_dir: str, python: str, args: list[str], timeout: int, testpath: str) -> tuple[str, int]:
    proc = subprocess.run(
        [python, "-m", "pytest", testpath, "-q", *args],
        cwd=repo_dir, capture_output=True, text=True, timeout=timeout,
    )
    return proc.stdout, proc.returncode


def reconstruct_nodeid(classname: str, name: str, overlay_paths: list[str]) -> str | None:
    """pytest's junitxml has no reliable `file`/`line` attribute in this
    pytest version; `classname` is the module's dotted path optionally
    followed by a dotted class chain (e.g. "testing.python.approx.TestApprox"
    for file "testing/python/approx.py", class "TestApprox"). Matching each
    known overlay file's own dotted form against the classname prefix
    recovers the exact real nodeid pytest itself would print, rather than
    guessing where the module path ends and the class chain begins."""
    for path in overlay_paths:
        dotted = path[:-3].replace("/", ".") if path.endswith(".py") else path.replace("/", ".")
        if classname == dotted:
            return f"{path}::{name}"
        if classname.startswith(dotted + "."):
            class_chain = classname[len(dotted) + 1:]
            return f"{path}::{class_chain}::{name}"
    return None


def parse_junit_ground_truth(xml_path: Path, overlay_paths: list[str]) -> dict[str, dict]:
    """{nodeid: {"exception_type": ..., "message": ..., "junit_failure_chars": ...}}
    for every <testcase> with a <failure> child, keyed by the real pytest
    nodeid (see `reconstruct_nodeid`)."""
    root = ET.parse(xml_path).getroot()
    out = {}
    for tc in root.iter("testcase"):
        failure = tc.find("failure")
        if failure is None:
            continue
        nodeid = reconstruct_nodeid(tc.get("classname", ""), tc.get("name", ""), overlay_paths)
        if nodeid is None:
            continue
        message = failure.get("message", "")
        # pytest's junitxml only sets `type=` for a handful of exception
        # classes; `message` (its own short summary line, e.g.
        # "Failed: DID NOT RAISE TypeError") is populated unconditionally.
        exception_type = failure.get("type") or message.split(":", 1)[0]
        out[nodeid] = {
            "exception_type": exception_type,
            "message": message,
            "junit_failure_chars": len(ET.tostring(failure, encoding="unicode")),
        }
    return out


def lines_needed_from_top(log_lines: list[str], needle: str) -> int | None:
    for i, line in enumerate(log_lines, start=1):
        if needle in line:
            return i
    return None


def lines_needed_from_bottom(log_lines: list[str], needle: str) -> int | None:
    n = len(log_lines)
    for i in range(n - 1, -1, -1):
        if needle in log_lines[i]:
            return n - i
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--scenario", default=str(HERE.parent / "failure-ordering" / "scenarios.json"),
                         help="Reuses the failure-ordering experiment's scenarios.json target by default.")
    parser.add_argument("--scenario-key", default="target")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--testpath", default="testing")
    parser.add_argument("--out", default=str(HERE / "results.json"))
    args = parser.parse_args()

    scenario = json.loads(Path(args.scenario).read_text())[args.scenario_key]

    work = HERE / "_work"
    work.mkdir(exist_ok=True)
    junit_path = work / "run.xml"

    print("checkout + long-traceback run", file=sys.stderr)
    base_sha = checkout_scenario(args.repo_dir, scenario)
    long_log, long_rc = run_pytest(args.repo_dir, args.python, ["--tb=long", f"--junit-xml={junit_path}"], args.timeout, args.testpath)
    (work / "long_log.txt").write_text(long_log)

    print("line-traceback run", file=sys.stderr)
    checkout_scenario(args.repo_dir, scenario)
    line_log, line_rc = run_pytest(args.repo_dir, args.python, ["--tb=line"], args.timeout, args.testpath)
    (work / "line_log.txt").write_text(line_log)

    ground_truth = parse_junit_ground_truth(junit_path, scenario["overlay_paths"])
    failing = {k: v for k, v in ground_truth.items() if v["exception_type"]}
    print(f"real failures with structured ground truth: {len(failing)}", file=sys.stderr)

    long_lines = long_log.splitlines()
    line_lines = line_log.splitlines()

    # Two separate localization questions, since a representation can answer
    # one without the other (pytest's own --tb=line body never names the
    # test; only its bottom summary section does):
    #   "which_test" -- the exact real nodeid appears in the log.
    #   "why"        -- the exception type string appears in the log.
    per_failure = {}
    for nodeid, gt in failing.items():
        per_failure[nodeid] = {
            "exception_type": gt["exception_type"],
            "junit_failure_chars": gt["junit_failure_chars"],
            "long_log_which_test_from_top": lines_needed_from_top(long_lines, nodeid),
            "long_log_which_test_from_bottom": lines_needed_from_bottom(long_lines, nodeid),
            "long_log_why_from_top": lines_needed_from_top(long_lines, gt["exception_type"]),
            "line_log_which_test_from_top": lines_needed_from_top(line_lines, nodeid),
            "line_log_which_test_from_bottom": lines_needed_from_bottom(line_lines, nodeid),
            "line_log_why_from_top": lines_needed_from_top(line_lines, gt["exception_type"]),
        }

    def summarize(field: str) -> dict:
        values = [v[field] for v in per_failure.values() if v[field] is not None]
        found = len(values)
        return {
            "found_for": found,
            "of": len(per_failure),
            "min": min(values) if values else None,
            "median": sorted(values)[len(values) // 2] if values else None,
            "max": max(values) if values else None,
        }

    report = {
        "measured": True,
        "base_sha": base_sha,
        "long_log_total_lines": len(long_lines),
        "long_log_total_chars": len(long_log),
        "line_log_total_lines": len(line_lines),
        "line_log_total_chars": len(line_log),
        "long_returncode": long_rc,
        "line_returncode": line_rc,
        "junit_total_chars": junit_path.stat().st_size,
        "junit_total_failure_chars": sum(v["junit_failure_chars"] for v in failing.values()),
        "failure_count": len(failing),
        "summary": {
            "long_log_which_test_from_top": summarize("long_log_which_test_from_top"),
            "long_log_which_test_from_bottom": summarize("long_log_which_test_from_bottom"),
            "long_log_why_from_top": summarize("long_log_why_from_top"),
            "line_log_which_test_from_top": summarize("line_log_which_test_from_top"),
            "line_log_which_test_from_bottom": summarize("line_log_which_test_from_bottom"),
            "line_log_why_from_top": summarize("line_log_why_from_top"),
        },
        "per_failure": per_failure,
    }
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps({k: v for k, v in report.items() if k != "per_failure"}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

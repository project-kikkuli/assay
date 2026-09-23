"""Lines-to-localize across the same real, multi-repo sample
`../failure-ordering/mine_cases.py` built (pytest + click regression/fix
pairs), measuring `localize.py` against raw pytest logs and JUnit XML from
real, scoped (overlay-file-only) runs -- cheap even under a contended
machine, since each run is one or two real test files, not a whole suite.

For every case, this captures one real `--tb=long -q --junit-xml=...` run of
just the overlaid file(s) and measures, for every real failing test (ground
truth from the real JUnit `<failure>` elements, matched to a real nodeid via
`../failure-ordering/`'s scenario-overlay convention): how many lines from
the top of the raw log are needed before the nodeid appears, vs. from the
bottom, vs. `localize.py --log`'s own tail-summary output (which should
match the from-bottom number, since that's exactly what it extracts) and
`localize.py --junit`'s output (which should need 0 extra lines: the
failing test is either printed or it isn't).
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent


def git(repo_dir: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True, text=True, check=True
    ).stdout.strip()


def checkout_case(repo_dir: str, case: dict) -> None:
    subprocess.run(["git", "checkout", "--quiet", "--force", "--detach", case["base_ref"]], cwd=repo_dir, check=True)
    subprocess.run(["git", "clean", "--quiet", "-fd", "--", *case["overlay_paths"]], cwd=repo_dir, check=True)
    for path in case["overlay_paths"]:
        content = subprocess.run(
            ["git", "show", f"{case['overlay_ref']}:{path}"], cwd=repo_dir, capture_output=True, text=True, check=True
        ).stdout
        (Path(repo_dir) / path).write_text(content)


def reconstruct_nodeid(classname: str, name: str, overlay_paths: list[str]) -> str | None:
    for path in overlay_paths:
        dotted = path[:-3].replace("/", ".") if path.endswith(".py") else path.replace("/", ".")
        if classname == dotted:
            return f"{path}::{name}"
        if classname.startswith(dotted + "."):
            return f"{path}::{classname[len(dotted) + 1:]}::{name}"
    return None


def parse_junit_ground_truth(xml_path: Path, overlay_paths: list[str]) -> dict[str, dict]:
    root = ET.parse(xml_path).getroot()
    out = {}
    for tc in root.iter("testcase"):
        problem = tc.find("failure")
        if problem is None:
            problem = tc.find("error")
        if problem is None:
            continue
        nodeid = reconstruct_nodeid(tc.get("classname", ""), tc.get("name", ""), overlay_paths)
        if nodeid is None:
            continue
        message = problem.get("message", "")
        exception_type = problem.get("type") or message.split(":", 1)[0]
        out[nodeid] = {"exception_type": exception_type, "message": message}
    return out


def lines_needed(log_lines: list[str], needle: str, from_bottom: bool) -> int | None:
    rng = range(len(log_lines) - 1, -1, -1) if from_bottom else range(len(log_lines))
    for i in rng:
        if needle in log_lines[i]:
            return (len(log_lines) - i) if from_bottom else (i + 1)
    return None


def measure_case(repo_dir: str, python: str, case: dict, work: Path, tag: str, timeout: int) -> dict:
    checkout_case(repo_dir, case)
    junit_path = work / f"{tag}.xml"
    log_path = work / f"{tag}.log"
    proc = subprocess.run(
        [python, "-m", "pytest", *case["overlay_paths"], "-q", "--tb=long", f"--junit-xml={junit_path}", "-o", "filterwarnings="],
        cwd=repo_dir, capture_output=True, text=True, timeout=timeout,
    )
    log_path.write_text(proc.stdout)

    if not junit_path.exists():
        return {"case": case["base_ref"], "excluded_reason": "no_junit_produced"}
    ground_truth = parse_junit_ground_truth(junit_path, case["overlay_paths"])
    if not ground_truth:
        return {"case": case["base_ref"], "excluded_reason": "zero_real_failures_this_run"}

    log_lines = proc.stdout.splitlines()

    localizer = subprocess.run(
        [python, str(HERE / "localize.py"), "--log", str(log_path), "--junit", str(junit_path)],
        capture_output=True, text=True,
    )
    localizer_lines = localizer.stdout.splitlines()
    localizer_matches = {
        nodeid: any(nodeid in line for line in localizer_lines)
        for nodeid in ground_truth
    }

    per_failure = {}
    for nodeid in ground_truth:
        per_failure[nodeid] = {
            "from_top": lines_needed(log_lines, nodeid, from_bottom=False),
            "from_bottom": lines_needed(log_lines, nodeid, from_bottom=True),
            "localizer_found_it": localizer_matches[nodeid],
        }

    return {
        "case": case["base_ref"], "overlay_paths": case["overlay_paths"],
        "log_total_lines": len(log_lines),
        "localizer_output_lines": len(localizer_lines),
        "failure_count": len(ground_truth),
        "per_failure": per_failure,
    }


def summarize(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    s = sorted(values)
    return {"n": len(s), "median": statistics.median(s), "min": s[0], "max": s[-1], "mean": round(statistics.mean(s), 2)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--repo-tag", required=True)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text())
    work = HERE / "_work_sample" / args.repo_tag
    work.mkdir(parents=True, exist_ok=True)

    per_case = []
    for i, case in enumerate(cases):
        print(f"[{args.repo_tag}] case {i + 1}/{len(cases)}: {case['fix_subject'][:60]}", file=sys.stderr)
        result = measure_case(args.repo_dir, args.python, case, work, f"case{i}", args.timeout)
        per_case.append(result)
        if "excluded_reason" in result:
            print(f"    EXCLUDED: {result['excluded_reason']}", file=sys.stderr)

    all_from_top, all_from_bottom, all_localizer_found = [], [], []
    for r in per_case:
        if "excluded_reason" in r:
            continue
        for f in r["per_failure"].values():
            if f["from_top"] is not None:
                all_from_top.append(f["from_top"])
            if f["from_bottom"] is not None:
                all_from_bottom.append(f["from_bottom"])
            all_localizer_found.append(f["localizer_found_it"])

    summary = {
        "from_top": summarize(all_from_top),
        "from_bottom": summarize(all_from_bottom),
        "localizer_found_every_failure": all(all_localizer_found) if all_localizer_found else None,
        "localizer_recall": round(sum(all_localizer_found) / len(all_localizer_found), 4) if all_localizer_found else None,
        "total_failures_measured": len(all_localizer_found),
    }

    report = {"measured": True, "repo_tag": args.repo_tag, "summary": summary, "cases": per_case}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()

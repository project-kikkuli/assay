"""Mine a real sample of regression/fix pairs from a repo's own git history,
using the same real-bug-injection method `measure_sample.py` and the earlier
n=1 build used: checkout a real commit's real parent (`base_ref`), overlay
the corresponding real fix commit's own test file(s) on top unmodified, and
verify the real, unmodified source at `base_ref` actually fails against it.

A candidate fix commit is one whose message contains "fix" and which
touches exactly one source file under `src_root` plus one or two test files
under `test_root`, with at most 4 files changed total -- small, single-cause
commits, the same shape verified by hand for the first four cases. Each
candidate is verified with a SCOPED run (only the touched test file(s), not
the whole suite), so mining is cheap even under a contended machine: no full
suite executes until `measure_sample.py` needs its one-time per-repo
reference run.

Nothing here is a seeded or authored fault. The bug, the fix, and the test
that exposes it are all real, unmodified commits from the named public
repo; only the pairing (this test file against that earlier tree) is
constructed, exactly as in the original n=1 build.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

MAX_FAILURES = 60  # drop candidates whose overlay breaks far more than one bug's worth of tests


def git(repo_dir: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True, text=True, check=True
    ).stdout.strip()


def commit_date(repo_dir: str, sha: str) -> str:
    return git(repo_dir, "show", "-s", "--format=%cI", sha)


def candidate_commits(repo_dir: str, src_root: str, test_root: str, limit: int) -> list[str]:
    log = git(repo_dir, "log", "--pretty=format:%H %s", f"-n{limit}", "HEAD", "--", src_root, test_root)
    out = []
    for line in log.splitlines():
        sha, _, msg = line.partition(" ")
        if "fix" in msg.lower():
            out.append(sha)
    return out


def classify_files(repo_dir: str, sha: str, src_root: str, test_root: str) -> tuple[list[str], list[str], int] | None:
    files = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", sha], cwd=repo_dir, capture_output=True, text=True
    ).stdout.split()
    files = [f for f in files if f]
    src_files = [f for f in files if f.startswith(src_root) and f.endswith(".py")]
    test_files = [f for f in files if f.startswith(test_root) and f.endswith(".py")]
    if len(src_files) == 1 and 1 <= len(test_files) <= 2 and len(files) <= 4:
        return src_files, test_files, len(files)
    return None


def verify_candidate(repo_dir: str, python: str, sha: str, test_files: list[str], timeout: int) -> dict | None:
    """Checks out `sha`'s real parent, overlays `test_files` from `sha`
    itself, and runs only those files. Returns the verified case dict, or
    None if the overlay produces zero real failures or looks like an
    unrelated mass breakage."""
    parent = git(repo_dir, "rev-parse", f"{sha}~1")
    subprocess.run(["git", "checkout", "--quiet", "--force", "--detach", parent], cwd=repo_dir, check=True)
    subprocess.run(["git", "clean", "--quiet", "-fd", "--", *test_files], cwd=repo_dir, check=True)
    for path in test_files:
        content = subprocess.run(
            ["git", "show", f"{sha}:{path}"], cwd=repo_dir, capture_output=True, text=True
        )
        if content.returncode != 0:
            return None
        (Path(repo_dir) / path).write_text(content.stdout)

    try:
        proc = subprocess.run(
            [python, "-m", "pytest", *test_files, "-q", "-o", "filterwarnings="],
            cwd=repo_dir, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    finally:
        # Best-effort: a fix that ADDED a new test file has nothing to
        # restore it to at this parent; the next candidate's own
        # checkout+clean is what actually guarantees a clean tree.
        subprocess.run(["git", "checkout", "--quiet", "--", *test_files], cwd=repo_dir, capture_output=True)

    if proc.returncode == 0:
        return None
    failing = [
        line.removeprefix("FAILED ").split(" - ", 1)[0].strip()
        for line in proc.stdout.splitlines()
        if line.startswith("FAILED ")
    ]
    if not failing or len(failing) > MAX_FAILURES:
        return None
    return {
        "base_ref": parent,
        "overlay_ref": sha,
        "overlay_paths": test_files,
        "failing_nodeids": sorted(failing),
        "base_commit_date": commit_date(repo_dir, parent),
        "fix_commit_date": commit_date(repo_dir, sha),
        "fix_subject": git(repo_dir, "show", "-s", "--format=%s", sha),
    }


def mine(repo_dir: str, python: str, src_root: str, test_root: str, target_count: int, scan_limit: int, timeout: int) -> list[dict]:
    start_ref = git(repo_dir, "rev-parse", "HEAD")
    verified: list[dict] = []
    tried = 0
    for sha in candidate_commits(repo_dir, src_root, test_root, scan_limit):
        if len(verified) >= target_count:
            break
        classified = classify_files(repo_dir, sha, src_root, test_root)
        if classified is None:
            continue
        _, test_files, _ = classified
        tried += 1
        case = verify_candidate(repo_dir, python, sha, test_files, timeout)
        if case is not None:
            verified.append(case)
            print(f"  verified [{len(verified)}/{target_count}] {sha[:10]} \"{case['fix_subject'][:60]}\" -> {len(case['failing_nodeids'])} failing", file=sys.stderr)
    subprocess.run(["git", "checkout", "--quiet", "--force", "--detach", start_ref], cwd=repo_dir, check=True)
    print(f"tried {tried} classified candidates, verified {len(verified)}", file=sys.stderr)
    return verified


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--src-root", required=True)
    parser.add_argument("--test-root", required=True)
    parser.add_argument("--target-count", type=int, default=10)
    parser.add_argument("--scan-limit", type=int, default=1500)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    cases = mine(args.repo_dir, args.python, args.src_root, args.test_root, args.target_count, args.scan_limit, args.timeout)
    Path(args.out).write_text(json.dumps(cases, indent=2, sort_keys=True))
    print(f"wrote {len(cases)} cases to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()

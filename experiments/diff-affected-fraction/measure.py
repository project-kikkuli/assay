"""How the affected-task fraction scales with real diff size, under an
explicit-input manifest, on a real codebase's real history.

`python-selection` shows a coverage-based selector failing on a large real
suite. This asks a narrower, decision-relevant question about the fast-lane
threshold itself: for real historical single-commit diffs of a public
codebase (pytest), bucketed by how many files they actually touched, what
fraction of a real explicit-input task manifest does each diff affect — and
at what diff size does that fraction stop looking like "a handful of
checks" and start looking like "most of the suite"?

One task per real test file under `testing/`. A task's declared inputs are
the test file itself plus the `_pytest` submodules it statically imports
(parsed with `ast`, not executed — an explicit static manifest, not runtime
coverage). Both the task manifest and the diff's changed files are read at
the real commit via `git cat-file --batch`, one subprocess per sampled
commit instead of one per file, so the manifest is the one that would
actually have gated that diff at the time it landed, not a manifest built
once from a later state of the tree.
"""
from __future__ import annotations

import argparse
import ast
import json
import random
import statistics
import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def first_parent_history(repo: Path) -> list[str]:
    return git(repo, "rev-list", "--first-parent", "--reverse", "main").splitlines()


def changed_files(repo: Path, sha: str) -> list[str]:
    out = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha)
    return [f for f in out.splitlines() if f]


def test_files_at(repo: Path, sha: str) -> list[str]:
    out = git(repo, "ls-tree", "-r", "--name-only", sha, "--", "testing")
    return [f for f in out.splitlines() if Path(f).name.startswith("test_") and f.endswith(".py")]


def batch_read(repo: Path, sha: str, paths: list[str]) -> dict[str, str | None]:
    """One `git cat-file --batch` process reads every path's blob at `sha`."""
    if not paths:
        return {}
    refs = [f"{sha}:{p}" for p in paths]
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input=("\n".join(refs) + "\n").encode(),
        capture_output=True,
    )
    out = proc.stdout
    result: dict[str, str | None] = {}
    pos = 0
    for ref, path in zip(refs, paths):
        nl = out.index(b"\n", pos)
        header = out[pos:nl].decode()
        pos = nl + 1
        if header.endswith("missing"):
            result[path] = None
            continue
        _sha, _type, size_s = header.rsplit(" ", 2)
        size = int(size_s)
        content = out[pos:pos + size]
        pos += size + 1  # skip the trailing newline cat-file appends after each object
        try:
            result[path] = content.decode("utf-8", errors="replace")
        except Exception:
            result[path] = None
    return result


PYTEST_MODULE_PREFIX = "_pytest"


def statically_imported_modules(source: str) -> set[str]:
    """Real `_pytest` submodules a test file statically imports, parsed with `ast`."""
    modules: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return modules
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PYTEST_MODULE_PREFIX or alias.name.startswith(PYTEST_MODULE_PREFIX + "."):
                    modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == PYTEST_MODULE_PREFIX or node.module.startswith(PYTEST_MODULE_PREFIX + ".")):
                modules.add(node.module)
    return modules


def module_to_candidate_paths(module: str) -> list[str]:
    """`_pytest.assertion.rewrite` -> src/_pytest/assertion/rewrite.py or
    src/_pytest/assertion (package) or src/_pytest/assertion.py — return every
    plausible real path; the manifest keeps whichever ones actually exist."""
    tail = module[len(PYTEST_MODULE_PREFIX) + 1:] if module != PYTEST_MODULE_PREFIX else ""
    parts = tail.split(".") if tail else []
    base = "src/_pytest" + ("/" + "/".join(parts) if parts else "")
    return [base, base + ".py", base + "/__init__.py"]


def build_manifest(repo: Path, sha: str) -> dict[str, set[str]] | None:
    tests = test_files_at(repo, sha)
    if not tests:
        return None
    sources = batch_read(repo, sha, tests)
    manifest: dict[str, set[str]] = {}
    for test_file, content in sources.items():
        if content is None:
            continue
        inputs = {test_file}
        for module in statically_imported_modules(content):
            for candidate in module_to_candidate_paths(module):
                inputs.add(candidate)
        manifest[test_file] = inputs
    return manifest


def affected_fraction(manifest: dict[str, set[str]], changed: set[str]) -> float:
    if not manifest:
        return 0.0
    affected = sum(1 for inputs in manifest.values() if inputs & changed)
    return affected / len(manifest)


def bucket_for(size: int) -> str:
    if size <= 1:
        return "1"
    if size <= 3:
        return "2-3"
    if size <= 6:
        return "4-6"
    if size <= 15:
        return "7-15"
    if size <= 30:
        return "16-30"
    return "31+"


BUCKET_ORDER = ["1", "2-3", "4-6", "7-15", "16-30", "31+"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True, help="Path to a prepared pytest checkout (main branch, first-parent history available).")
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    args = parser.parse_args()

    repo = Path(args.repo_dir).resolve()
    commits = first_parent_history(repo)
    rng = random.Random(args.seed)
    candidates = list(range(1, len(commits)))
    rng.shuffle(candidates)

    samples = []
    for i in candidates:
        if len(samples) >= args.samples:
            break
        sha = commits[i]
        parent = commits[i - 1]
        changed = changed_files(repo, sha)
        if not changed:
            continue
        manifest = build_manifest(repo, parent)  # the manifest as it existed BEFORE this diff landed
        if not manifest:
            continue
        changed_set = set(changed)
        fraction = affected_fraction(manifest, changed_set)
        code_relevant = any(f.startswith("testing/") or f.startswith("src/_pytest/") for f in changed)
        samples.append({
            "sha": sha,
            "files_changed": len(changed),
            "bucket": bucket_for(len(changed)),
            "task_count": len(manifest),
            "affected_task_count": round(fraction * len(manifest)),
            "affected_fraction": round(fraction, 4),
            "code_relevant": code_relevant,
        })

    def bucket_summary(rows_by_bucket: dict[str, list[dict]], denom: int) -> dict:
        out = {}
        for bucket in BUCKET_ORDER:
            rows = rows_by_bucket[bucket]
            if not rows:
                out[bucket] = {"count": 0}
                continue
            fractions = [r["affected_fraction"] for r in rows]
            out[bucket] = {
                "count": len(rows),
                "share_of_samples": round(len(rows) / denom, 4) if denom else None,
                "mean_affected_fraction": round(statistics.mean(fractions), 4),
                "median_affected_fraction": round(statistics.median(fractions), 4),
                "p95_affected_fraction": round(sorted(fractions)[max(0, int(0.95 * len(fractions)) - 1)], 4),
                "zero_affected_count": sum(1 for f in fractions if f == 0.0),
            }
        return out

    by_bucket_all: dict[str, list[dict]] = {b: [] for b in BUCKET_ORDER}
    for s in samples:
        by_bucket_all[s["bucket"]].append(s)

    code_relevant_samples = [s for s in samples if s["code_relevant"]]
    by_bucket_code_relevant: dict[str, list[dict]] = {b: [] for b in BUCKET_ORDER}
    for s in code_relevant_samples:
        by_bucket_code_relevant[s["bucket"]].append(s)

    summary = {
        "measured": True, "repo": "https://github.com/pytest-dev/pytest", "samples_requested": args.samples,
        "samples_with_data": len(samples),
        "code_relevant_samples": len(code_relevant_samples),
        "code_relevant_fraction_of_all_samples": round(len(code_relevant_samples) / len(samples), 4) if samples else None,
        "buckets_all_samples": bucket_summary(by_bucket_all, len(samples)),
        "buckets_code_relevant_only": bucket_summary(by_bucket_code_relevant, len(code_relevant_samples)),
    }

    report = {"summary": summary, "samples": samples}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

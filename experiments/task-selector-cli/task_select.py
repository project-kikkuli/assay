#!/usr/bin/env python3
"""A runnable task selector: given a real base commit and a real diff, print
which real pytest test files a real explicit-input manifest says are
affected. This reuses `diff-affected-fraction`'s import graph and blob
reading unmodified, but corrects one real gap that CLI validation
(`validate.py`) found in that experiment's `test_*.py` naming heuristic: it
reads the real `python_files` patterns from the target repo's own real
`pyproject.toml` (`tomllib`, stdlib) and matches against those, because
pytest's own real config is
`python_files = ["test_*.py", "*_test.py", "testing/python/*.py"]` — a real
project can and does declare test files that don't start with `test_`.
`diff-affected-fraction`'s own committed results are left as they are: this
is a documented refinement made and validated here, not a silent rewrite of
an already-published measurement.

Usage:
    python3 task_select.py --repo-dir /path/to/pytest --base <sha> [--against <sha>]

`--against` defaults to `<base>^` (the commit's own real parent), so the
default invocation answers "which tests does this one real commit's own
diff affect?" — the same question a merge-queue or PR check asks.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "diff-affected-fraction"))
from measure import (  # noqa: E402
    batch_read, git, module_to_candidate_paths, statically_imported_modules,
)

DEFAULT_PATTERNS = ["test_*.py", "*_test.py"]


def python_files_patterns(repo: Path, sha: str) -> list[str]:
    """The real `python_files` patterns this repo's own real `pyproject.toml`
    declares at this commit, falling back to pytest's own documented default
    when the repo has none."""
    result = git_show(repo, sha, "pyproject.toml")
    if result is None:
        return DEFAULT_PATTERNS
    try:
        data = tomllib.loads(result)
    except Exception:
        return DEFAULT_PATTERNS
    pytest_table = data.get("tool", {}).get("pytest", {})
    # Real historical config shape varies: older pytest reads `[tool.pytest]`
    # directly, newer pytest reads `[tool.pytest.ini_options]` — check both
    # real locations rather than assuming the current one always applied.
    patterns = pytest_table.get("ini_options", {}).get("python_files") or pytest_table.get("python_files")
    if not patterns:
        return DEFAULT_PATTERNS
    return patterns if isinstance(patterns, list) else [patterns]


def git_show(repo: Path, sha: str, path: str) -> str | None:
    import subprocess
    result = subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{path}"], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def test_files_at(repo: Path, sha: str) -> list[str]:
    """Every real `.py` file under `testing/` whose real path or basename
    matches one of the repo's own real `python_files` patterns — a
    path-scoped pattern (contains `/`) matches the full real path; a
    name-only pattern matches the real basename."""
    out = git(repo, "ls-tree", "-r", "--name-only", sha, "--", "testing")
    all_py = [f for f in out.splitlines() if f.endswith(".py")]
    patterns = python_files_patterns(repo, sha)
    matched = []
    for f in all_py:
        name = Path(f).name
        for pat in patterns:
            if ("/" in pat and fnmatch.fnmatch(f, pat)) or ("/" not in pat and fnmatch.fnmatch(name, pat)):
                matched.append(f)
                break
    return matched


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


def diff_changed_files(repo: Path, base: str, against: str) -> list[str]:
    out = git(repo, "diff", "--name-only", against, base)
    return [f for f in out.splitlines() if f]


def select(repo: Path, base: str, against: str | None) -> dict:
    against_ref = against or f"{base}^"
    changed = set(diff_changed_files(repo, base, against_ref))
    manifest = build_manifest(repo, against_ref) or {}
    selected = sorted(task for task, inputs in manifest.items() if inputs & changed)
    return {
        "base": base,
        "against": against_ref,
        "changed_files": sorted(changed),
        "task_count_total": len(manifest),
        "selected_tasks": selected,
        "selected_count": len(selected),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--against", default=None)
    args = parser.parse_args()

    repo = Path(args.repo_dir).resolve()
    result = select(repo, args.base, args.against)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

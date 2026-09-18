"""Fail-closed scope check, not a signature service or remote attestation.

The baseline must come from a protected base revision, never from a candidate.
Every byte outside the explicitly nominated actor is protected. There is no
dependency-graph inference, ignored extension, or fallback-to-empty selection.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re


ACTOR = "candidate.py"
REQUIRED = frozenset({ACTOR, "kernel.py", "actor_runtime.py", "worker.py", "behavior.py",
                      "admission.py", "fixture.py", "run.py", "subject.identity"})


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inventory(root: Path) -> dict[str, str]:
    """Hash a dedicated staged input tree, including new and hidden files.

    Callers must stage only verification inputs, not a live worktree containing
    generated reports. Symlinks are forbidden: their targets can escape the tree.
    """
    if not root.is_dir() or root.is_symlink():
        raise ValueError("input root must be a real directory")
    records = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlink input is not admitted")
        if path.is_file():
            records[path.relative_to(root).as_posix()] = digest(path.read_bytes())
        elif not path.is_dir():
            raise ValueError("special input is not admitted")
    if not records:
        raise ValueError("empty input tree")
    return records


def decide(base: dict[str, str], candidate: dict[str, str]) -> dict:
    """Decide eligibility only. Eligible is NOT verified or mergeable."""
    if (not isinstance(base, dict) or not isinstance(candidate, dict)
            or not REQUIRED <= base.keys() or not REQUIRED <= candidate.keys()):
        return {"lane": "broader_required", "reason": "missing baseline or actor"}
    for records in (base, candidate):
        if any(not isinstance(k, str) or not isinstance(v, str)
               or re.fullmatch(r"[0-9a-f]{64}", v) is None for k, v in records.items()):
            return {"lane": "broader_required", "reason": "malformed inventory"}
    changed = sorted(key for key in base.keys() | candidate.keys()
                     if base.get(key) != candidate.get(key))
    protected = [key for key in changed if key != ACTOR]
    if protected:
        return {"lane": "broader_required", "reason": "trusted inputs changed",
                "changed": changed, "protected_changes": protected}
    return {"lane": "actor_checks_required", "changed": changed,
            "reason": "only the bounded actor may differ"}

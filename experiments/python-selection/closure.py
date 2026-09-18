"""Conservative declared-input invalidation, not a sandbox or CI replacement."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat


IGNORED = {"__pycache__", ".pytest_cache", ".hypothesis", ".ruff_cache"}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def inventory(root: Path, inputs: list[str]) -> dict:
    """Include membership/absence; only allow file links resolved inside the root."""
    entries = {}

    def visit(path: Path) -> None:
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = path.resolve(strict=True)
            if not target.is_relative_to(root.resolve()) or not target.is_file():
                raise ValueError(f"unsafe symlink input: {name}")
            entries[name] = {"kind": "file_link", "target": target.relative_to(root.resolve()).as_posix(),
                             "mode": stat.S_IMODE(target.stat().st_mode),
                             "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
            return
        if not path.exists():
            entries[name] = {"kind": "missing"}
            return
        mode = path.stat().st_mode
        if stat.S_ISREG(mode):
            entries[name] = {"kind": "file", "mode": stat.S_IMODE(mode),
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        elif stat.S_ISDIR(mode):
            entries[name] = {"kind": "directory"}
            for child in sorted(path.iterdir()):
                if child.name not in IGNORED:
                    visit(child)
        else:
            raise ValueError(f"special input: {name}")

    for name in inputs:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("inputs must be nonempty relative paths")
        for ancestor in relative.parents:
            if (root / ancestor).is_symlink():
                raise ValueError("symlink input ancestor")
        visit(root / relative)
    return entries


def action_key(files: dict, command: list[str], context: dict, verifier: str) -> str:
    return digest({"files": files, "command": command, "context": context, "verifier": verifier})


def reusable(receipt: dict | None, key: str, expected_tests: int) -> bool:
    if not isinstance(receipt, dict) or expected_tests <= 0:
        return False
    count = receipt.get("passed")
    return (receipt.get("key") == key and receipt.get("status") == "passed"
            and type(count) is int and count == expected_tests
            and receipt.get("failed") == 0 and receipt.get("skipped") == 0)

#!/usr/bin/env python3
"""Fetch pinned public Cedar sources and prepare a Docker build context."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


SPEC_URL = "https://github.com/cedar-policy/cedar-spec.git"
CEDAR_URL = "https://github.com/cedar-policy/cedar.git"
SPEC_COMMIT = "acb0db7daa838249d894d2af64c850e1c9bf0d7d"
CEDAR_COMMIT = "9502ae02564a23028c732f8c1f2311635394c34f"
SCRATCH = Path(tempfile.gettempdir())
REPO_ROOT = Path(__file__).resolve().parent
LOCK_FILES = {
    "cedar-lean-ffi/Cargo.lock": REPO_ROOT / "locks" / "cedar-lean-ffi.Cargo.lock",
    "cedar-lean-cli/Cargo.lock": REPO_ROOT / "locks" / "cedar-lean-cli.Cargo.lock",
}


def run(command: list[str], *, cwd: Path | None = None) -> str:
    env = {
        key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ
    }
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0"
    )
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        timeout=1800 if command[:2] == ["docker", "build"] else 300,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def clone_at(url: str, commit: str, destination: Path) -> None:
    if destination.exists():
        raise RuntimeError(f"refusing to overwrite existing checkout: {destination}")
    run(["git", "clone", "--filter=blob:none", "--no-checkout", url, str(destination)])
    run(["git", "checkout", "--detach", commit], cwd=destination)
    actual = run(["git", "rev-parse", "HEAD"], cwd=destination)
    if actual != commit:
        raise RuntimeError(f"public checkout mismatch: {actual} != {commit}")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch", type=Path, default=SCRATCH)
    parser.add_argument("--context-name", default="cedar-build-context")
    parser.add_argument("--build", action="store_true", help="also run docker build")
    parser.add_argument("--tag", default="kikkuli-cedar-lean-cli:acb0db7-slim")
    args = parser.parse_args()
    if Path(args.context_name).name != args.context_name or args.context_name in {
        ".",
        "..",
    }:
        parser.error("context-name must be a single directory name")

    scratch = args.scratch.resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    owned = Path(tempfile.mkdtemp(prefix="cedar-build-", dir=scratch))
    sources = owned / "cedar-public-sources"
    spec = sources / "cedar-spec"
    cedar = spec / "cedar"
    sources.mkdir(exist_ok=True)
    clone_at(SPEC_URL, SPEC_COMMIT, spec)
    clone_at(CEDAR_URL, CEDAR_COMMIT, cedar)

    for manifest in (spec / "cedar-lean" / "lake-manifest.json", cedar / "Cargo.toml"):
        if not manifest.is_file():
            raise RuntimeError(f"missing pinned dependency manifest: {manifest.name}")

    for relative, locked_file in LOCK_FILES.items():
        if not locked_file.is_file():
            raise RuntimeError(f"missing committed lockfile: {locked_file}")
        destination = spec / relative
        shutil.copyfile(locked_file, destination)
        if digest(destination) != digest(locked_file):
            raise RuntimeError(f"lockfile copy mismatch: {relative}")

    context = owned / args.context_name
    context.mkdir()
    shutil.copytree(
        spec,
        context / "cedar-spec",
        ignore=shutil.ignore_patterns(".git", ".lake", "target"),
    )
    provenance = {
        "cedar_spec_commit": SPEC_COMMIT,
        "cedar_dependency_commit": CEDAR_COMMIT,
        "lean_toolchain": (spec / "lean-toolchain").read_text().strip(),
        "lake_manifest_sha256": digest(spec / "cedar-lean" / "lake-manifest.json"),
        "cargo_lock_sha256": {
            relative: digest(path) for relative, path in LOCK_FILES.items()
        },
    }
    (context / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")

    if args.build:
        run(
            [
                "docker",
                "build",
                "--pull",
                "-f",
                str(Path(__file__).with_name("Dockerfile")),
                "-t",
                args.tag,
                str(context),
            ],
            cwd=Path(__file__).parent,
        )
    print(context)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

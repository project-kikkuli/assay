#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]==3.3.4"]
# ///
"""Run the real public UI against the unchanged app or the cell Item gateway."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import uuid

from fixture import REVISION, app_env, public_fixture

HERE = Path(__file__).resolve().parent
PATCH = HERE.parent / "fullstack" / "repaired.patch"
CANDIDATE = HERE / "candidates" / "healthy.py"


def digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root)
        if "node_modules" in relative.parts or ".venv" in relative.parts:
            continue
        if relative.parts[:3] == ("backend", "app", "frontend"):
            continue
        digest.update(relative.as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def digest_files(paths: list[Path]) -> dict[str, str]:
    return {
        path.relative_to(HERE).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def public_manifest(subject: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-tree", "-r", REVISION],
        cwd=subject,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    paths = []
    for line in result.stdout.splitlines():
        metadata, name = line.split("\t", 1)
        if metadata.split()[0] != "160000":
            paths.append(name)
    return paths


def digest_manifest(root: Path, manifest: list[str]) -> str:
    digest = hashlib.sha256()
    for name in manifest:
        path = root / name
        if path.is_symlink():
            content = os.readlink(path).encode()
        elif path.is_file():
            content = path.read_bytes()
        else:
            raise RuntimeError(f"pinned public source file missing: {name}")
        digest.update(name.encode() + b"\0")
        digest.update(content)
    return digest.hexdigest()


def sanitize_text(value: str) -> str:
    value = value.replace("assay-local-only", "[redacted]")
    value = re.sub(r"/(?:Users|private/tmp|tmp|var/folders)/[^\s,\"']+", "<redacted-path>", value)
    return value


def sanitize(value):
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}
    return value


def write_report(path: Path, report: dict) -> None:
    path.write_text(json.dumps(sanitize(report), indent=2) + "\n")


def command(command: list[str | Path], cwd: Path, env: dict[str, str], timeout: int = 120) -> dict:
    started = time.perf_counter()
    process = subprocess.Popen(
        [str(value) for value in command],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
        status = "passed" if process.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()
        status = "unresolved"
    return {
        "status": status,
        "exit_code": process.returncode,
        "seconds": round(time.perf_counter() - started, 3),
        "output_tail": sanitize_text(output[-2000:]),
    }


def source_variant(subject: Path, root: Path) -> Path:
    archive = root / "source.tar"
    subprocess.run(
        ["git", "archive", "--output", str(archive), REVISION],
        cwd=subject,
        check=True,
        timeout=30,
    )
    candidate = root / "subject"
    candidate.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(candidate, filter="data")
    subprocess.run(["git", "apply", str(PATCH)], cwd=candidate, check=True, timeout=30)
    (candidate / ".venv").symlink_to(subject / ".venv", target_is_directory=True)
    (candidate / "node_modules").symlink_to(subject / "node_modules", target_is_directory=True)
    return candidate


def build(subject: Path, node: str) -> dict:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C",
        "VITE_API_URL": "",
    }
    return command(
        [node, subject / "node_modules/vite/bin/vite.js", "build"],
        subject / "frontend",
        env,
        timeout=180,
    )


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_ready(base_url: str, process: subprocess.Popen[str], timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/api/v1/utils/health-check/", timeout=0.5) as response:
                if response.status == 200:
                    return
        except OSError:
            if process.poll() is not None:
                raise RuntimeError("server exited before readiness")
            time.sleep(0.05)
    raise RuntimeError("server readiness timeout")


@contextmanager
def server(subject: Path, dsn: str, mode: str, scratch: Path, port: int):
    env = app_env(subject, dsn)
    env.update(
        {
            "PYTHONPATH": f"{scratch}:{subject / 'backend'}:{HERE}",
            "FRONTEND_HOST": f"http://127.0.0.1:{port}",
            "CELL_ACTOR_CANDIDATE": str(CANDIDATE),
            "CELL_ACTOR_SCRATCH": str(scratch),
            "CELL_SHUTDOWN_EVIDENCE": str(scratch / "shutdown.json"),
        }
    )
    bootstrap = scratch / "bootstrap.py"
    bootstrap.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(HERE)!r})\n"
        "from app.main import app\n"
        "from gateway import install\n"
        "install(app)\n"
    )
    log = (scratch / "server.log").open("w+")
    target = [env.get("PYTHON", str(subject / ".venv/bin/python")), "-m", "uvicorn"]
    target.append("bootstrap:app" if mode == "cell" else "app.main:app")
    target.extend(["--host", "127.0.0.1", "--port", str(port)])
    process = subprocess.Popen(
        target,
        cwd=subject / "backend",
        env=env,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    try:
        base_url = f"http://127.0.0.1:{port}"
        wait_ready(base_url, process)
        yield base_url, env, process
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        log.close()


def run_case(subject: Path, admin_dsn: str, mode: str, node: str, variant_identity: dict) -> dict:
    started = time.perf_counter()
    evidence: dict = {"mode": mode, "status": "unknown"}
    with tempfile.TemporaryDirectory(prefix="cell-run-") as raw_scratch:
        scratch = Path(raw_scratch)
        with public_fixture(subject, admin_dsn, evidence) as dsn:
            env = app_env(subject, dsn)
            seed = command([subject / ".venv/bin/python", "app/initial_data.py"], subject / "backend", env)
            evidence["seed"] = seed
            if seed["status"] != "passed":
                evidence["status"] = "failed"
                evidence["total_seconds"] = round(time.perf_counter() - started, 3)
                return evidence
            with server(variant_identity["path"], dsn, mode, scratch, free_port()) as (base_url, _, _):
                browser_output = scratch / "browser.json"
                browser = command(
                    [node, HERE / "browser.mjs", "--source", variant_identity["path"], "--base-url", base_url, "--output", browser_output],
                    HERE,
                    {
                        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                        "LANG": "C",
                        "HOME": os.environ.get("HOME", "/tmp"),
                        "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
                        "FIRST_SUPERUSER": "admin@example.com",
                        "FIRST_SUPERUSER_PASSWORD": "AssayFixturePassword123",
                    },
                    timeout=180,
                )
                evidence["browser_command"] = browser
                if browser_output.exists():
                    evidence["browser"] = json.loads(browser_output.read_text())
            shutdown = scratch / "shutdown.json"
            if shutdown.exists():
                evidence["shutdown"] = json.loads(shutdown.read_text())
            evidence["status"] = (
                "passed"
                if browser["status"] == "passed"
                and evidence.get("browser", {}).get("status") == "passed"
                and (
                    mode == "baseline"
                    or (
                        evidence.get("shutdown", {}).get("status") == "passed"
                        and evidence.get("shutdown", {}).get("actor", {}).get("cleanup", {}).get("status") == "removed"
                        and evidence.get("shutdown", {}).get("actor", {}).get("calls")
                    )
                )
                else "failed"
            )
            evidence["total_seconds"] = round(time.perf_counter() - started, 3)
        return evidence


def run_experiment(subject: Path, admin_dsn: str, output: Path, repeats: int = 1) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    report = {"status": "unknown", "phase": "initializing", "records": [], "source_unchanged": None}
    write_report(output, report)
    source_before: dict[str, str] = {}
    try:
        subject = subject.resolve()
        if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=subject, text=True).strip() != REVISION:
            raise RuntimeError("subject revision is not the pinned public revision")
        node = shutil.which("node") or "node"
        with tempfile.TemporaryDirectory(prefix="cell-source-") as raw_root:
            root = Path(raw_root)
            variant = source_variant(subject, root)
            manifest = public_manifest(subject)
            source_before = {
                "public_variant": digest_manifest(variant, manifest),
                "cell_core": digest_files([
                    path
                    for path in HERE.rglob("*")
                    if path.is_file()
                    and path.suffix in {".py", ".mjs"}
                    and "__pycache__" not in path.parts
                ]),
            }
            report["variant_identity"] = {
                "public_revision": REVISION,
                "repaired_patch_sha256": hashlib.sha256(PATCH.read_bytes()).hexdigest(),
                "source_sha256_before_build": source_before["public_variant"],
            }
            for repeat in range(repeats):
                built = build(variant, node)
                report["records"].append({"repeat": repeat, "check": "build", **built, "artifact_sha256": digest_tree(variant / "backend/app/frontend")})
                if built["status"] != "passed":
                    raise RuntimeError("production frontend build failed")
                for mode in ("baseline", "cell"):
                    case = run_case(subject, admin_dsn, mode, node, {"path": variant})
                    report["records"].append({"repeat": repeat, **case})
                    if case["status"] != "passed":
                        raise RuntimeError(f"{mode} run did not pass")
            source_after = {
                "public_variant": digest_manifest(variant, manifest),
                "cell_core": digest_files([
                    path
                    for path in HERE.rglob("*")
                    if path.is_file()
                    and path.suffix in {".py", ".mjs"}
                    and "__pycache__" not in path.parts
                ]),
            }
            report["source_identity"] = {"before": source_before, "after": source_after}
            report["source_unchanged"] = source_before == source_after
            report["variant_identity"]["source_sha256_after_run"] = source_after["public_variant"]
            report["status"] = "passed" if report["source_unchanged"] else "failed"
            report["phase"] = "complete"
            report["variant_sha256"] = digest_tree(variant)
    except Exception as error:
        report.update(status="failed", phase="failed", error=f"{type(error).__name__}: {error}")
    finally:
        report["total_seconds"] = round(time.perf_counter() - started, 3)
        write_report(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", type=Path, default=Path("out/lab/fullstack"))
    parser.add_argument("--dsn", default=os.environ.get("ASSAY_PG_DSN", "postgresql://postgres:assay-local-only@127.0.0.1:55439/postgres"))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("out/cell/integration.json"))
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    report = run_experiment(args.subject, args.dsn, args.output, args.repeats)
    print(json.dumps({"status": report["status"], "records": len(report["records"])}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

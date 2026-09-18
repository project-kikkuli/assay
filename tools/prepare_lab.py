#!/usr/bin/env python3
"""Prepare the public fixture. Setup cost is deliberately outside the warm gate."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "out/lab/state.json"
REVISION = "cb740b656d7a0a6c5e12c7bf8e50343ec94ee9c7"
UPSTREAM = "https://github.com/fastapi/full-stack-fastapi-template.git"
PG_IMAGE = (
    "postgres@sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280"
)
LABEL = "org.project-kikkuli.assay-lab"
MAIL_VERSION = "1.29.2"
MAIL_SHA256 = {
    ("Darwin", "arm64"): (
        "darwin-arm64",
        "4cc025b6e4757020d030ab892e7b42dec3e6a10ba49bf8ed93677890d13e86e5",
    ),
    ("Darwin", "x86_64"): (
        "darwin-amd64",
        "0f7f94aeca68cf9df1a7724ee8f1e8d58b4bedfd5fc5d595b76e48092ffa5efb",
    ),
    ("Linux", "aarch64"): (
        "linux-arm64",
        "c7eb4824afd45cd111f76d39c85e9e55de25885260a5ddd19b0af9c13cfc6b49",
    ),
    ("Linux", "x86_64"): (
        "linux-amd64",
        "22d9a97cdeb32174deb2162a53754f70b582c61c2ec97da534c96d1b6da8f3c6",
    ),
}


def environment():
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "NPM_CONFIG_USERCONFIG": os.devnull,
        "NPM_CONFIG_GLOBALCONFIG": str(STATE.parent / "empty-global.npmrc"),
        "PIP_CONFIG_FILE": os.devnull,
        "UV_NO_CONFIG": "1",
    }


def run(argv, cwd=ROOT, timeout=600, capture=False):
    started = time.perf_counter()
    print("setup:", " ".join(map(str, argv)), flush=True)
    process = subprocess.Popen(
        list(map(str, argv)),
        cwd=cwd,
        env=environment(),
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise RuntimeError(f"setup timed out: {argv[0]}") from None
    if process.returncode:
        raise RuntimeError(
            f"setup failed ({process.returncode}): {argv[0]}\n{stderr or ''}"
        )
    return stdout or "", time.perf_counter() - started


def write_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.replace(STATE)


def require_free(port):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            raise RuntimeError(
                f"port {port} is occupied; refusing to replace an existing service"
            ) from None


def mail_ready():
    with socket.create_connection(("127.0.0.1", 51027), timeout=1) as smtp:
        if not smtp.recv(512).startswith(b"220"):
            return False
    connection = http.client.HTTPConnection("127.0.0.1", 58027, timeout=1)
    try:
        connection.request("GET", "/api/v1/messages")
        response = connection.getresponse()
        response.read()
        return response.status == 200
    finally:
        connection.close()


def wait_ready(probe, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if probe():
                return
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.1)  # Service readiness, not a test correctness condition.
    raise RuntimeError("fixture service did not become ready")


def prepare_mailpit(directory):
    architecture, expected = MAIL_SHA256[(platform.system(), platform.machine())]
    binary = directory / "mailpit"
    receipt = directory / "mailpit.sha256"
    if (
        binary.is_file()
        and receipt.is_file()
        and receipt.read_text().strip()
        == hashlib.sha256(binary.read_bytes()).hexdigest()
    ):
        return binary
    url = f"https://github.com/axllent/mailpit/releases/download/v{MAIL_VERSION}/mailpit-{architecture}.tar.gz"
    with tempfile.TemporaryFile() as archive:
        with urllib.request.urlopen(url, timeout=30) as response:
            shutil.copyfileobj(response, archive)
        archive.seek(0)
        if hashlib.file_digest(archive, "sha256").hexdigest() != expected:
            raise RuntimeError("Mailpit release checksum mismatch")
        archive.seek(0)
        with tarfile.open(fileobj=archive, mode="r:gz") as bundle:
            member = bundle.getmember("mailpit")
            if not member.isfile():
                raise RuntimeError("Mailpit archive member is not a regular file")
            with bundle.extractfile(member) as source, binary.open("wb") as target:
                shutil.copyfileobj(source, target)
    binary.chmod(0o755)
    receipt.write_text(hashlib.sha256(binary.read_bytes()).hexdigest() + "\n")
    return binary


def prepare_services(state, external):
    if external:
        with socket.create_connection(("127.0.0.1", 55439), timeout=2):
            pass
        wait_ready(mail_ready)
        state["services"] = {"external": True}
        write_state(state)
        return
    for port in (55439, 58027, 51027):
        require_free(port)
    directory = STATE.parent
    binary = prepare_mailpit(directory)
    identity = hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
    name = f"assay-lab-pg-{identity}"
    cid, _ = run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--label",
            f"{LABEL}={identity}",
            "-p",
            "127.0.0.1:55439:5432",
            "-e",
            "POSTGRES_PASSWORD=assay-local-only",
            "--tmpfs",
            "/var/lib/postgresql:rw",
            PG_IMAGE,
        ],
        capture=True,
    )
    state["services"] = {
        "external": False,
        "postgres_id": cid.strip(),
        "identity": identity,
    }
    write_state(state)  # Persist ownership before any later step can fail.
    wait_ready(
        lambda: (
            subprocess.run(
                ["docker", "exec", cid.strip(), "pg_isready", "-U", "postgres"],
                env=environment(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).returncode
            == 0
        )
    )
    argv = [
        str(binary),
        "--smtp",
        "127.0.0.1:51027",
        "--listen",
        "127.0.0.1:58027",
        "--database",
        str(directory / "mailpit.db"),
        "--disable-version-check",
    ]
    with (directory / "mailpit.log").open("a") as log:
        mail = subprocess.Popen(
            argv,
            cwd=directory,
            env=environment(),
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    state["services"].update(mailpit_pid=mail.pid, mailpit_command=" ".join(argv))
    write_state(state)
    wait_ready(mail_ready)


def inspect_postgres(subject):
    code = (
        "import json, psycopg; "
        "c=psycopg.connect('host=127.0.0.1 port=55439 user=postgres "
        "password=assay-local-only dbname=postgres connect_timeout=3'); "
        "print(json.dumps({'server_version':c.info.server_version})); c.close()"
    )
    output, _ = run(
        [subject / ".venv/bin/python", "-c", code], capture=True, timeout=10
    )
    observed = json.loads(output)
    if observed.get("server_version") != 180006:
        raise RuntimeError("fixture must be PostgreSQL 18.6; observed " + str(observed))
    return observed


def stop_services():
    state = json.loads(STATE.read_text())
    services = state.get("services", {})
    if services.get("external"):
        print("Services were supplied externally; nothing stopped.")
        return
    pid = services.get("mailpit_pid")
    if pid:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            text=True,
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            if result.stdout.strip() != services.get("mailpit_command"):
                raise RuntimeError(
                    "Mailpit PID no longer matches the owned process; refusing to signal it"
                )
            os.kill(pid, signal.SIGTERM)
    cid = services.get("postgres_id")
    if cid:
        description, _ = run(["docker", "inspect", cid], capture=True, timeout=10)
        resource = json.loads(description)[0]
        if resource["Id"] != cid or resource["Config"].get("Labels", {}).get(
            LABEL
        ) != services.get("identity"):
            raise RuntimeError("PostgreSQL ownership mismatch; refusing to remove it")
        run(["docker", "rm", "-f", cid], timeout=10)
    state["services"] = {"stopped": True}
    write_state(state)
    print(
        "Stopped owned Mailpit and removed the owned disposable PostgreSQL container; fixture DB data is not retained."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subject",
        type=Path,
        help="use an already prepared repaired checkout; no installation or patching",
    )
    parser.add_argument(
        "--services-external",
        action="store_true",
        help="use disposable local PG/Mailpit already running; never stop them",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="stop only services recorded as created by this tool",
    )
    args = parser.parse_args()
    if args.stop:
        stop_services()
        return
    for command in ("git", "uv", "node", "npm"):
        if not shutil.which(command):
            parser.error(f"install {command} first")
    node, _ = run(["node", "--version"], capture=True)
    if node.strip() != "v22.12.0":
        parser.error("use Node 22.12.0 (the measured toolchain)")
    if STATE.exists():
        old = json.loads(STATE.read_text()).get("services", {})
        if old and not old.get("external") and not old.get("stopped"):
            parser.error(
                "owned services already recorded; use --stop before preparing again"
            )
    patch = ROOT / "experiments/fullstack/repaired.patch"
    state = {
        "setup_seconds": {},
        "revision": REVISION,
        "ready": False,
        "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
    }
    STATE.parent.mkdir(parents=True, exist_ok=True)
    subject = args.subject.resolve() if args.subject else STATE.parent / "fullstack"
    if not args.subject:
        if subject.exists():
            parser.error(
                "out/lab/fullstack already exists; use --subject after preparing it, or choose a fresh checkout"
            )
        subject.mkdir()
        state["subject"] = str(subject)
        state["created_subject"] = True
        write_state(state)
        run(["git", "init", "-q", subject])
        _, state["setup_seconds"]["fetch"] = run(
            ["git", "fetch", "--depth=1", UPSTREAM, REVISION], cwd=subject
        )
        run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=subject)
        run(
            ["git", "apply", ROOT / "experiments/fullstack/repaired.patch"], cwd=subject
        )
        _, state["setup_seconds"]["python_dependencies"] = run(
            ["uv", "sync", "--python", "3.14.6", "--frozen", "--all-packages"],
            cwd=subject,
        )
        _, state["setup_seconds"]["javascript_dependencies"] = run(
            [
                "npm",
                "exec",
                "--yes",
                "--package=bun@1.3.12",
                "--",
                "bun",
                "install",
                "--frozen-lockfile",
            ],
            cwd=subject,
        )
        _, state["setup_seconds"]["chromium"] = run(
            ["node", "node_modules/@playwright/test/cli.js", "install", "chromium"],
            cwd=subject,
        )
    revision, _ = run(["git", "rev-parse", "HEAD"], cwd=subject, capture=True)
    if revision.strip() != REVISION:
        parser.error("subject is not the pinned public revision")
    run(
        [
            "git",
            "apply",
            "--reverse",
            "--check",
            ROOT / "experiments/fullstack/repaired.patch",
        ],
        cwd=subject,
    )
    if (
        not (subject / ".venv/bin/python").is_file()
        or not (subject / "node_modules").is_dir()
    ):
        parser.error("subject dependencies are missing")
    state["subject"] = str(subject)
    write_state(state)
    prepare_services(state, args.services_external)
    state["postgres"] = inspect_postgres(subject)
    for experiment in ("outbox", "compatibility"):
        _, state["setup_seconds"][experiment] = run(
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=ROOT / "experiments" / experiment,
        )
    if state["patch_sha256"] != hashlib.sha256(patch.read_bytes()).hexdigest():
        raise RuntimeError("repair patch changed during setup")
    state["ready"] = True
    write_state(state)
    print(
        "Ready: ./lab replay gate. Setup timings and local resource ownership: out/lab/state.json"
    )


if __name__ == "__main__":
    main()

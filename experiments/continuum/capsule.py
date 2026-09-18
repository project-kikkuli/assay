"""Disposable execution worlds. These control application inputs, not the Docker host."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid


def run_process(args, **kwargs):
    """A timed-out Docker CLI must not leave its application running anonymously."""
    args = [str(value) for value in args]
    owned = None
    if args[:2] == ["docker", "run"] and "--name" not in args:
        owned = f"assay-continuum-action-{uuid.uuid4().hex[:12]}"
        args[2:2] = ["--name", owned]
    try:
        return subprocess.run(args, **kwargs)
    except BaseException:
        if owned:
            subprocess.run(["docker", "rm", "-f", owned], capture_output=True, timeout=15)
        raise


def command(args, *, cwd=None, timeout=90):
    result = run_process(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr[-2500:]} {result.stdout[-1000:]}")
    return result.stdout.strip()


def request(url, path, body=None, *, token=None, timeout=10):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            payload = json.load(error)
        except ValueError:
            payload = {"error": "non-JSON error response"}
        return error.code, payload


class World:
    def __init__(self, root, environment, *, database=None, provider=True):
        self.root, self.environment = Path(root).resolve(), environment
        self.db_dir = Path(database).resolve() if database else self.root / "state"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.prefix = f"assay-continuum-{uuid.uuid4().hex[:12]}"
        self.network = self.prefix
        self.containers = []
        self.provider_enabled = provider
        self.started = time.perf_counter()
        self.urls = {}
        self.runtime = {}

    def docker(self, *args, timeout=90):
        return command(["docker", *args], timeout=timeout)

    def launch(self, name, image, args, *, mounts=(), port=None, working=None, network=None):
        identity = f"{self.prefix}-{name}"
        cli = ["run", "-d", "--name", identity, "--network", network or self.network,
               "--user", f"{os.getuid()}:{os.getgid()}",
               "--read-only", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=256m", "--cpus=1",
               "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m", "-e", "PYTHONDONTWRITEBYTECODE=1"]
        if not network:
            cli += ["--network-alias", name]
        if port:
            cli += ["-p", f"127.0.0.1::{port}"]
        if working:
            cli += ["-w", working]
        for source, target, mode in mounts:
            cli += ["--mount", f"type=bind,source={source},target={target}" + (",readonly" if mode == "ro" else "")]
        cli += [image, *args]
        self.containers.append(identity)
        self.docker(*cli)
        if network:
            self.docker("network", "connect", self.network, identity)
        facts = json.loads(self.docker("inspect", identity))[0]
        self.runtime[name] = {"image_id": facts["Image"], "image_reference": image,
                              "readonly_root": facts["HostConfig"]["ReadonlyRootfs"],
                              "memory_limit": facts["HostConfig"]["Memory"],
                              "network": "fixed-target-ingress" if network else "internal-owned", "cap_drop": facts["HostConfig"]["CapDrop"]}
        if port:
            host_port = facts["NetworkSettings"]["Ports"][f"{port}/tcp"][0]["HostPort"]
            self.urls[name] = f"http://127.0.0.1:{host_port}"
        return identity

    def ready(self, name, path="/health"):
        deadline = time.monotonic() + 15
        last = ""
        while time.monotonic() < deadline:
            try:
                status, _ = request(self.urls[name], path, timeout=1)
                if status == 200:
                    return
                last = str(status)
            except (OSError, ValueError) as error:
                last = type(error).__name__
            # A bounded readiness poll, not a test's interleaving or assertion.
            threading.Event().wait(0.025)
        logs = self.docker("logs", f"{self.prefix}-{name}")
        raise RuntimeError(f"{name} failed readiness ({last}): {logs[-2000:]}")

    def __enter__(self):
        try:
            self.docker("network", "create", "--internal", self.network)
            self.launch("api", self.environment["python"],
                        ["python", "/app/api.py", "--db", "/state/app.db", "--port", "8000", "--test-control",
                         "--static", "/client"],
                        mounts=[(self.root / "app", "/app", "ro"), (self.root / "client", "/client", "ro"),
                                (self.db_dir, "/state", "rw")])
            if self.provider_enabled:
                self.launch("provider", self.environment["python"],
                            ["python", "/verify/verify.py", "--serve-provider", "--port", "8001"],
                            mounts=[(self.root / "verifier", "/verify", "ro")])
            self.launch("edge", self.environment["python"], ["python", "/edge/edge.py"],
                        mounts=[(self.root / "verifier", "/edge", "ro")], port=8080, network="bridge")
            self.urls["api"] = self.urls["edge"]
            self.urls["provider"] = self.urls["edge"] + "/provider"
            self.ready("api")
            if self.provider_enabled:
                self.ready("provider", "/records")
            return self
        except BaseException:
            self.close()
            raise

    def worker_command(self):
        identity = f"{self.prefix}-worker-{uuid.uuid4().hex[:6]}"
        self.containers.append(identity)
        return ["docker", "run", "--rm", "--name", identity, "--network", self.network,
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128",
                "--memory=256m", "--cpus=1", "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m",
                "--mount", f"type=bind,source={self.root / 'client'},target=/client,readonly", "-w", "/client",
                self.environment["node"], "node", "dist/worker.js", "--api", "http://api:8000",
                "--provider", "http://provider:8001", "--token", "synthetic-worker", "--once"]

    def worker(self):
        return command(self.worker_command(), timeout=20)

    @property
    def db(self):
        return self.db_dir / "app.db"

    def close(self):
        failures = []
        for identity in reversed(self.containers):
            try:
                result = subprocess.run(["docker", "rm", "-f", identity], capture_output=True, text=True, timeout=15)
            except subprocess.TimeoutExpired:
                failures.append(identity)
                continue
            if result.returncode and "No such container" not in result.stderr:
                failures.append(identity)
        result = subprocess.run(["docker", "network", "rm", self.network], capture_output=True, text=True, timeout=15)
        if result.returncode and "not found" not in result.stderr:
            failures.append(self.network)
        if failures:
            raise RuntimeError(f"owned resources could not be removed: {failures}")
        self.containers.clear()

    def __exit__(self, *_):
        self.close()

"""Persistent transport for an untrusted proposal candidate.

This module never imports the candidate and performs no authorization or
business-policy validation. The caller owns semantic validation and effects.
The Docker daemon and host kernel remain trusted; this is not a VM or a
complete side-channel defense.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import selectors
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path


IMAGE_TAG = "python:3.12-slim-bookworm"
IMAGE_DIGESTS = {
    "aarch64": "sha256:d04f49f5882f49a3b91f874e75e19f0c265f7222da8659741a9d7eab148f22a9",
    "x86_64": "sha256:9c47360a2a0355e2da18516d0b1c2126ec22c195d2185e97347c9d98398c5bef",
}
MAX_OUTPUT_BYTES = 32 * 1024
MAX_INPUT_BYTES = 32 * 1024
DEFAULT_TIMEOUT = 2.0
PROTOCOL = "cell.actor.v1"


class ActorError(RuntimeError):
    pass


class ActorUnknown(ActorError):
    pass


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decode(line):
    return json.loads(line, object_pairs_hook=_strict_object, parse_constant=_bad_constant)


def _bad_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_env():
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent", "DOCKER_CONFIG": "/nonexistent"}


class Actor:
    """Persistent Docker-backed candidate transport."""

    def __init__(self, candidate_path, *, timeout=DEFAULT_TIMEOUT, scratch=None):
        self.candidate_path = Path(candidate_path)
        self.worker_path = Path(__file__).with_name("worker.py")
        self.timeout = timeout
        self.scratch = Path(scratch or tempfile.gettempdir())
        self.process = None
        self.cidfile = None
        self.container_id = None
        self._temp = None
        self._closed = False
        self._lock = threading.RLock()
        self.observations = {
            "candidate_sha256_before": _hash(self.candidate_path),
            "worker_sha256_before": _hash(self.worker_path),
            "calls": [],
        }

    def __enter__(self):
        if self.process is not None:
            raise ActorError("actor already entered")
        try:
            self.scratch.mkdir(parents=True, exist_ok=True)
            machine = platform.machine().lower()
            arch = {"arm64": "aarch64", "aarch64": "aarch64", "amd64": "x86_64", "x86_64": "x86_64"}.get(machine)
            if arch is None:
                raise ActorError(f"unsupported host architecture: {machine}")
            self.image_digest = IMAGE_DIGESTS[arch]
            self.image_reference = f"{IMAGE_TAG}@{self.image_digest}"
            started = time.perf_counter()
            self._temp = tempfile.TemporaryDirectory(prefix="cell-actor-", dir=self.scratch)
            root = Path(self._temp.name)
            candidate = root / "candidate.py"
            worker = root / "worker.py"
            candidate.write_bytes(self.candidate_path.read_bytes())
            worker.write_bytes(self.worker_path.read_bytes())
            candidate.chmod(0o444)
            worker.chmod(0o444)
            self.cidfile = root / "container.id"
            inspect = subprocess.run(["docker", "image", "inspect", "--format", "{{json .}}", self.image_reference], capture_output=True, text=True, check=True, env=_safe_env(), timeout=10)
            image = json.loads(inspect.stdout)
            self.observations.update({"image_reference": self.image_reference, "image_digest": self.image_digest, "image_id": image["Id"], "startup_ms": None})
            command = ["docker", "run", "--interactive", "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "65532:65532", "--memory", "128m", "--pids-limit", "64", "--cpus", "1.0", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m", "--env", "PATH=/usr/local/bin:/usr/bin:/bin", "--env", "PYTHONUNBUFFERED=1", "--cidfile", str(self.cidfile), "--mount", f"type=bind,src={candidate},dst=/candidate/program.py,readonly", "--mount", f"type=bind,src={worker},dst=/worker/worker.py,readonly", self.image_reference, "python", "/worker/worker.py", "/candidate/program.py"]
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_safe_env())
            self.container_id = self.cidfile.read_text().strip() if self.cidfile.exists() else None
            ready = self._read_line(time.monotonic() + self.timeout)
            if ready.get("ready") is not True or ready.get("protocol") != PROTOCOL:
                raise ActorUnknown("worker startup was not trusted")
            if not self.container_id and self.cidfile.exists():
                self.container_id = self.cidfile.read_text().strip()
            self.observations["startup_ms"] = round((time.perf_counter() - started) * 1000, 1)
            self.observations["container_controls"] = self._inspect_controls(candidate, worker)
            if not self.observations["container_controls"]["passed"]:
                raise ActorUnknown("observed container controls failed")
            return self
        except Exception:
            if not self._closed:
                self._abort("startup failed")
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def __call__(self, command, view):
        with self._lock:
            if self.process is None or self._closed:
                raise ActorError("actor is not running")
            request_id = uuid.uuid4().hex
            envelope = {"request_id": request_id, "command": command, "view": view}
            encoded = (json.dumps(envelope, separators=(",", ":"), allow_nan=False) + "\n").encode()
            if len(encoded) > MAX_INPUT_BYTES:
                raise ActorError("request exceeds input cap")
            started = time.perf_counter()
            try:
                self.process.stdin.write(encoded)
                self.process.stdin.flush()
                response = self._read_line(time.monotonic() + self.timeout)
            except Exception as exc:
                self._abort("request became unknown")
                raise ActorUnknown(str(exc)) from exc
            if response.get("request_id") != request_id or set(response) != {"request_id", "proposal"}:
                self._abort("response request binding failed")
                raise ActorUnknown("stale, malformed, or error response")
            proposal = response["proposal"]
            if not isinstance(proposal, dict):
                self._abort("proposal is not an object")
                raise ActorUnknown("proposal is not an object")
            self.observations["calls"].append({"request_id": request_id, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)})
            return proposal

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self.process is not None:
                try:
                    self.process.kill()
                    self.process.wait(timeout=2)
                except Exception:
                    pass
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    try:
                        stream.close()
                    except Exception:
                        pass
            cleanup = "unknown_no_cid"
            if not self.container_id and self.cidfile is not None and self.cidfile.exists():
                self.container_id = self.cidfile.read_text().strip()
            valid_cid = isinstance(self.container_id, str) and len(self.container_id) == 64 and all(char in "0123456789abcdef" for char in self.container_id)
            if valid_cid:
                try:
                    removed = subprocess.run(["docker", "rm", "--force", self.container_id], capture_output=True, text=True, check=False, env=_safe_env(), timeout=10)
                    cleanup = "removed" if removed.returncode == 0 else "unknown_cleanup"
                except Exception:
                    cleanup = "unknown_cleanup"
            self.observations["cleanup"] = {"container_id": self.container_id, "status": cleanup}
            try:
                candidate_after = _hash(self.candidate_path)
                worker_after = _hash(self.worker_path)
            except OSError:
                candidate_after = None
                worker_after = None
            self.observations.update(
                {
                    "candidate_sha256_after": candidate_after,
                    "worker_sha256_after": worker_after,
                    "source_identity_stable": (
                        candidate_after == self.observations["candidate_sha256_before"]
                        and worker_after == self.observations["worker_sha256_before"]
                    ),
                }
            )
            if self._temp is not None:
                self._temp.cleanup()

    def _abort(self, reason):
        self.observations["unknown"] = reason
        self.close()

    def _read_line(self, deadline):
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
        stdout = bytearray()
        stderr = bytearray()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ActorUnknown("deadline exceeded")
                for key, _ in selector.select(min(remaining, 0.05)):
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        if self.process.poll() is not None or not selector.get_map():
                            raise ActorUnknown("worker process ended")
                        continue
                    if key.data == "stderr":
                        stderr.extend(chunk)
                        raise ActorUnknown("worker wrote to stderr")
                    else:
                        stdout.extend(chunk)
                    if len(stdout) + len(stderr) > MAX_OUTPUT_BYTES:
                        raise ActorUnknown("output cap exceeded")
                    if key.data == "stdout" and b"\n" in stdout:
                        line, extra = bytes(stdout).split(b"\n", 1)
                        if extra:
                            raise ActorUnknown("extra protocol output")
                        value = _decode(line.decode("utf-8"))
                        if not isinstance(value, dict):
                            raise ActorUnknown("response is not an object")
                        return value
        finally:
            selector.close()

    def _inspect_controls(self, candidate, worker):
        if not self.container_id:
            return {"passed": False, "failures": ["missing_cid"]}
        result = subprocess.run(["docker", "inspect", "--format", "{{json .}}", self.container_id], capture_output=True, text=True, check=False, env=_safe_env(), timeout=10)
        if result.returncode != 0:
            return {"passed": False, "failures": ["inspect_failed"]}
        record = json.loads(result.stdout)
        host = record.get("HostConfig", {})
        config = record.get("Config", {})
        mounts = [m for m in record.get("Mounts", []) if m.get("Type") in {"bind", "volume"}]
        expected = {("/candidate/program.py", str(candidate)), ("/worker/worker.py", str(worker))}
        actual = {(m.get("Destination"), m.get("Source")) for m in mounts}
        failures = []
        checks = {"network": host.get("NetworkMode") == "none", "readonly_rootfs": host.get("ReadonlyRootfs") is True, "user": config.get("User") == "65532:65532", "pids": host.get("PidsLimit") == 64, "memory": host.get("Memory") == 128 * 1024 * 1024, "nano_cpus": host.get("NanoCpus") == 1_000_000_000, "cap_drop": host.get("CapDrop") == ["ALL"], "no_new_privileges": host.get("SecurityOpt") == ["no-new-privileges"], "tmpfs": host.get("Tmpfs") == {"/tmp": "rw,noexec,nosuid,nodev,size=16m"}, "mounts": actual == expected and all(m.get("RW") is False for m in mounts)}
        failures.extend(key for key, passed in checks.items() if not passed)
        source_tokens = {str(candidate): "candidate", str(worker): "worker"}
        reported_mounts = [
            {
                "destination": destination,
                "source": source_tokens.get(source, "unexpected"),
                "readonly": next(
                    mount.get("RW") is False
                    for mount in mounts
                    if mount.get("Destination") == destination and mount.get("Source") == source
                ),
            }
            for destination, source in sorted(actual)
        ]
        return {"passed": not failures, "failures": failures, "observed": {"host_config": {"network": host.get("NetworkMode"), "memory": host.get("Memory"), "pids": host.get("PidsLimit"), "nano_cpus": host.get("NanoCpus"), "cap_drop": host.get("CapDrop"), "security_opt": host.get("SecurityOpt")}, "user": config.get("User"), "mounts": reported_mounts, "checks": checks}}

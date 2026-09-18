"""Content-addressed actions and operator-issued evidence, independent of app language."""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree(root, *, exclude=()):
    """Exact regular-file inventory. No inferred dependencies or followed symlinks."""
    root = Path(root)
    found = {".": {"kind": "directory", "mode": root.stat().st_mode & 0o777}}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in exclude for part in rel.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"symlink input refused: {rel}")
        if path.is_file():
            found[rel.as_posix()] = {"sha256": file_hash(path), "mode": path.stat().st_mode & 0o777}
        elif path.is_dir():
            found[rel.as_posix()] = {"kind": "directory", "mode": path.stat().st_mode & 0o777}
    return found


class InvalidEvidence(ValueError):
    pass


class Authority:
    """Local operator fixture, not remote attestation or protection from host administrators."""

    def __init__(self, root, *, readonly=False):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.private = self.root / "issuer.pem"
        self.public = self.root / "issuer.pub"
        self.openssl = ("/opt/homebrew/bin/openssl" if Path("/opt/homebrew/bin/openssl").exists()
                        else shutil.which("openssl"))
        if not self.openssl:
            raise RuntimeError("OpenSSL with Ed25519 support required")
        if not self.public.exists():
            if readonly:
                raise InvalidEvidence("trusted public key missing")
            subprocess.run([self.openssl, "genpkey", "-algorithm", "ED25519", "-out", str(self.private)],
                           check=True, capture_output=True)
            self.private.chmod(0o600)
            subprocess.run([self.openssl, "pkey", "-in", str(self.private), "-pubout", "-out", str(self.public)],
                           check=True, capture_output=True)
        self.issuer = file_hash(self.public)

    def sign(self, payload):
        with tempfile.TemporaryDirectory(prefix="continuum-sign-") as temporary:
            message = Path(temporary) / "message"
            message.write_bytes(canonical(payload))
            signature = subprocess.run([self.openssl, "pkeyutl", "-sign", "-rawin", "-inkey",
                                        str(self.private), "-in", str(message)], check=True,
                                       capture_output=True).stdout
        return {"payload": payload, "issuer": self.issuer,
                "signature": base64.b64encode(signature).decode()}

    def verify(self, envelope):
        try:
            if envelope["issuer"] != self.issuer:
                raise InvalidEvidence("untrusted issuer")
            with tempfile.TemporaryDirectory(prefix="continuum-check-") as temporary:
                message, signature = Path(temporary) / "message", Path(temporary) / "signature"
                message.write_bytes(canonical(envelope["payload"]))
                signature.write_bytes(base64.b64decode(envelope["signature"], validate=True))
                outcome = subprocess.run([self.openssl, "pkeyutl", "-verify", "-pubin", "-rawin",
                                          "-inkey", str(self.public), "-in", str(message),
                                          "-sigfile", str(signature)], capture_output=True)
                if outcome.returncode:
                    raise InvalidEvidence("invalid signature")
            return envelope["payload"]
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidEvidence(str(error)) from error


class Store:
    def __init__(self, root, authority):
        self.root, self.authority = Path(root), authority
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, key):
        path = self.root / f"{key}.json"
        if not path.exists():
            return None
        receipt = json.loads(path.read_text())
        payload = self.authority.verify(receipt)
        if payload["action_key"] != key or digest(payload["spec"]) != key:
            raise InvalidEvidence("cache identity mismatch")
        if digest(payload["result"]) != payload["result_digest"]:
            raise InvalidEvidence("result content mismatch")
        if payload["result"].get("kind") == "inconclusive":
            return None
        return receipt

    def put(self, spec, result, seconds):
        payload = {"format": "continuum.evidence.v1", "action_key": digest(spec), "spec": spec,
                   "result": result, "result_digest": digest(result), "execution_seconds": seconds}
        receipt = self.authority.sign(payload)
        destination = self.root / f"{payload['action_key']}.json"
        with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as temporary:
            temporary.write(canonical(receipt))
            staged = Path(temporary.name)
        staged.replace(destination)
        return receipt


def run_graph(actions, store, *, jobs=1, reuse=True):
    """Each action declares deps, exact inputs, recipe identity, environment and a trusted run()."""
    started = time.perf_counter()
    completed, pending, running = {}, dict(actions), {}

    def execute(name, action):
        observed_start = time.perf_counter()
        def observed(result):
            return {**result, "start_seconds": observed_start - started,
                    "observed_seconds": time.perf_counter() - observed_start,
                    "dependencies": list(action.get("deps", []))}
        spec = {"name": name, "inputs": action["inputs"], "recipe": action["recipe"],
                "environment": action.get("environment", {}),
                "dependencies": {dep: {field: completed[dep]["receipt"]["payload"][field]
                                       for field in ("action_key", "result_digest")}
                                 for dep in action.get("deps", [])}}
        key = digest(spec)
        previous = store.get(key) if reuse else None
        if previous is not None:
            if action.get("restore") and previous["payload"]["result"]["passed"]:
                action["restore"](previous["payload"]["result"])
            return observed({"cache": "hit", "receipt": previous})
        begin = time.perf_counter()
        try:
            result = action["run"]({dep: completed[dep]["receipt"]["payload"]["result"]
                                    for dep in action.get("deps", [])})
            if not isinstance(result, dict) or type(result.get("passed")) is not bool:
                raise ValueError("action did not produce an explicit verdict")
        except Exception as error:
            result = {"passed": False, "kind": "inconclusive", "error": f"{type(error).__name__}: {error}"}
        return observed({"cache": "miss", "receipt": store.put(spec, result, time.perf_counter() - begin)})

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        while pending or running:
            for name, action in list(pending.items()):
                dependencies = action.get("deps", [])
                if all(dep in completed for dep in dependencies):
                    if any(not completed[dep]["receipt"]["payload"]["result"]["passed"] for dep in dependencies):
                        action = {**action, "run": lambda _: {"passed": False, "kind": "blocked_by_dependency"}}
                    running[pool.submit(execute, name, action)] = name
                    del pending[name]
            if not running:
                raise ValueError("cyclic graph or missing dependency")
            done, _ = concurrent.futures.wait(running, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                name = running.pop(future)
                completed[name] = future.result()
    return {"passed": all(item["receipt"]["payload"]["result"]["passed"] for item in completed.values()),
            "seconds": time.perf_counter() - started, "jobs": jobs,
            "cache_hits": sum(item["cache"] == "hit" for item in completed.values()),
            "actions": completed}


def admit(receipts, required, authority, *, artifact, environment, policy, artifact_action="package"):
    """The caller derives required action keys from the target tree, not from candidate evidence."""
    reasons = []
    for name, key in required.items():
        try:
            payload = authority.verify(receipts[name])
            if payload["action_key"] != key or digest(payload["spec"]) != key:
                reasons.append(f"{name}: stale or different inputs")
            if digest(payload["result"]) != payload["result_digest"]:
                reasons.append(f"{name}: corrupt result")
            if payload["result"]["passed"] is not True:
                reasons.append(f"{name}: not verified")
        except (KeyError, TypeError, InvalidEvidence) as error:
            reasons.append(f"{name}: {error}")
    if not required:
        reasons.append("empty verification policy")
    if not isinstance(artifact_action, str) or not artifact_action:
        reasons.append("deployment requires a package evidence binding")
    else:
        try:
            package = authority.verify(receipts[artifact_action])
            if artifact_action not in required or package["result"]["artifact"] != artifact:
                reasons.append("deployed bytes differ from verified package")
        except (KeyError, TypeError, InvalidEvidence) as error:
            reasons.append(f"package: {error}")
    return {"decision": "reject" if reasons else "admit", "reasons": reasons,
            "artifact": artifact, "environment": environment, "policy": policy,
            "required": required}


def expected_keys(actions, receipts):
    """Reconstruct the target plan independently of the submitted action-key claims."""
    required, pending = {}, dict(actions)
    while pending:
        ready = [name for name, action in pending.items() if all(dep in required for dep in action.get("deps", []))]
        if not ready:
            raise ValueError("invalid target plan")
        for name in ready:
            action = pending.pop(name)
            spec = {"name": name, "inputs": action["inputs"], "recipe": action["recipe"],
                    "environment": action.get("environment", {}),
                    "dependencies": {dep: {"action_key": required[dep],
                                           "result_digest": receipts.get(dep, {}).get("payload", {}).get("result_digest")}
                                     for dep in action.get("deps", [])}}
            required[name] = digest(spec)
    return required

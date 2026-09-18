#!/usr/bin/env python3
"""Run an untrusted JSON candidate behind a real Docker execution boundary."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import selectors
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SCRATCH_ROOT = Path(
    os.environ.get("KIKKULI_CONFINEMENT_SCRATCH", tempfile.gettempdir())
)
IMAGE_TAG = "python:3.12-slim-bookworm"
IMAGE_DIGESTS = {
    "aarch64": "sha256:d04f49f5882f49a3b91f874e75e19f0c265f7222da8659741a9d7eab148f22a9",
    "x86_64": "sha256:9c47360a2a0355e2da18516d0b1c2126ec22c195d2185e97347c9d98398c5bef",
}
MAX_OUTPUT_BYTES = 32 * 1024
TIMEOUT_SECONDS = 2.0
EXPECTED_REQUEST = {
    "protocol": "confinement.v1",
    "operation": "add",
    "a": 2,
    "b": 3,
    "canary_path": "/oracle-canary.txt",
    "network_target": "network-canary:8765",
}
EXPECTED_RESULT = 5
NETWORK_PORT = 8765
EXPECTED_ATTACKS = {
    "false_attestation",
    "silent",
    "wrong_result",
    "read_canary",
    "edit_oracle",
    "network",
    "output_flood",
    "hang",
}
EXPECTED_CLASSIFICATIONS = {
    "false_attestation": "rejected_wrong_business_response",
    "silent": "rejected_invalid_json_or_empty",
    "wrong_result": "rejected_wrong_business_response",
    "read_canary": "rejected_wrong_business_response",
    "edit_oracle": "rejected_wrong_business_response",
    "network": "rejected_wrong_business_response",
    "output_flood": "rejected_output_limit",
    "hang": "candidate_unknown_timeout",
}
VERIFIER_FILES = ("README.md", "run.py", "test_run.py")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def hash_files(paths: list[Path]) -> dict[str, str]:
    return {str(path.relative_to(ROOT)): sha256_file(path) for path in paths}


def write_evidence(payload: dict[str, Any], path: Path | None = None) -> None:
    (path or (ROOT / "evidence.json")).write_text(json.dumps(payload, indent=2) + "\n")


def fresh_non_green(reason: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "evidence_status": "incomplete",
        "admission": "not_green",
        "checks": {"passed": False, "failures": [reason]},
        "details": details or {},
    }


def safe_env() -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/nonexistent",
        "DOCKER_CONFIG": "/nonexistent",
    }


def image_ref() -> tuple[str, str, str, str]:
    machine = platform.machine().lower()
    architecture = {
        "arm64": "aarch64",
        "aarch64": "aarch64",
        "amd64": "x86_64",
        "x86_64": "x86_64",
    }.get(machine)
    if architecture is None:
        raise RuntimeError(f"unsupported host architecture: {machine}")
    try:
        digest = IMAGE_DIGESTS[architecture]
    except KeyError as error:
        raise RuntimeError(f"unsupported host architecture: {machine}") from error
    docker_platform = "linux/arm64" if architecture == "aarch64" else "linux/amd64"
    return f"{IMAGE_TAG}@{digest}", digest, docker_platform, machine


def docker_pull(reference: str, docker_platform: str) -> dict[str, Any]:
    started = time.perf_counter()
    completed = subprocess.run(
        ["docker", "pull", "--platform", docker_platform, reference],
        capture_output=True,
        text=True,
        check=False,
        env=safe_env(),
        timeout=120,
    )
    return {
        "status": "ok" if completed.returncode == 0 else "error",
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "exit_code": completed.returncode,
        "stdout": completed.stdout[-512:],
        "stderr": completed.stderr[-512:],
    }


def image_identity(reference: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{json .}}", reference],
        capture_output=True,
        text=True,
        check=True,
        env=safe_env(),
    )
    record = json.loads(completed.stdout)
    return {
        "image_id": record["Id"],
        "repo_digests": record.get("RepoDigests", []),
        "size_bytes": record["Size"],
    }


def create_network() -> tuple[str, str, float]:
    name = f"confinement-net-{uuid.uuid4().hex[:16]}"
    started = time.perf_counter()
    created = subprocess.run(
        [
            "docker",
            "network",
            "create",
            "--driver",
            "bridge",
            "--label",
            "kikkuli.confinement=owned",
            name,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=safe_env(),
        timeout=20,
    )
    network_id = created.stdout.strip()
    if created.returncode != 0 or len(network_id) != 64:
        raise RuntimeError(f"owned network creation failed: {created.stderr[-256:]}")
    return name, network_id, round((time.perf_counter() - started) * 1000, 1)


def start_network_server(reference: str, network_name: str, cidfile: Path) -> dict[str, Any]:
    server_code = (
        "import socket; "
        f"s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
        f"s.bind(('0.0.0.0', {NETWORK_PORT})); s.listen(1); "
        "c,_=s.accept(); c.sendall(b'owned-network-canary'); c.close(); s.close()"
    )
    started = time.perf_counter()
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--network",
            network_name,
            "--network-alias",
            "network-canary",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65532:65532",
            "--memory",
            "64m",
            "--pids-limit",
            "32",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=8m",
            "--cidfile",
            str(cidfile),
            reference,
            "python",
            "-c",
            server_code,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=safe_env(),
        timeout=20,
    )
    server_id = container_id_from(cidfile)
    if completed.returncode != 0 or server_id is None:
        if server_id is not None:
            cleanup_container(server_id)
        raise RuntimeError(f"owned network server failed: {completed.stderr[-256:]}")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        inspected = inspect_container(server_id)
        if inspected.get("status") == "ok" and inspected["record"]["State"].get("Running"):
            return {
                "container_id": server_id,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        time.sleep(0.05)
    cleanup_container(server_id)
    raise RuntimeError("owned network server did not become ready")


def network_server_connected(server_id: str) -> bool:
    inspected = inspect_container(server_id)
    if inspected.get("status") != "ok":
        return False
    state = inspected["record"].get("State", {})
    return state.get("Running") is False and state.get("ExitCode") == 0


def cleanup_network(network_id: str | None) -> str:
    if network_id is None or len(network_id) != 64 or any(
        char not in "0123456789abcdef" for char in network_id
    ):
        return "unknown_invalid_network_id"
    try:
        removed = subprocess.run(
            ["docker", "network", "rm", network_id],
            capture_output=True,
            text=True,
            check=False,
            env=safe_env(),
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "unknown_network_cleanup_timeout"
    if removed.returncode == 0:
        return "removed"
    inspected = subprocess.run(
        ["docker", "network", "inspect", network_id],
        capture_output=True,
        text=True,
        check=False,
        env=safe_env(),
        timeout=10,
    )
    if inspected.returncode != 0 and any(
        marker in inspected.stderr.lower()
        for marker in ("no such object", "not found")
    ):
        return "already_absent"
    return "unknown_network_cleanup_failure"


def normalize_bytes(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def container_id_from(cidfile: Path) -> str | None:
    if not cidfile.is_file():
        return None
    container_id = cidfile.read_text().strip()
    if len(container_id) != 64 or any(char not in "0123456789abcdef" for char in container_id):
        return None
    return container_id


def inspect_container(container_id: str) -> dict[str, Any]:
    try:
        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{json .}}", container_id],
            capture_output=True,
            text=True,
            check=False,
            env=safe_env(),
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return {"status": "unknown_inspect_timeout"}
    if inspected.returncode != 0:
        return {"status": "unknown_inspect_failure"}
    try:
        return {"status": "ok", "record": json.loads(inspected.stdout)}
    except json.JSONDecodeError:
        return {"status": "unknown_inspect_json"}


def check_container_controls(
    record: dict[str, Any],
    candidate_dir: Path,
    canary_exposed: bool,
    expected_network: str = "none",
) -> dict[str, Any]:
    config = record.get("Config", {})
    host_config = record.get("HostConfig", {})
    failures: list[str] = []
    if host_config.get("NetworkMode") != expected_network:
        failures.append("network_not_none")
    if host_config.get("ReadonlyRootfs") is not True:
        failures.append("root_filesystem_not_read_only")
    if config.get("User") != "65532:65532":
        failures.append("wrong_user")
    if host_config.get("PidsLimit") != 64:
        failures.append("wrong_pids_limit")
    if host_config.get("Memory") != 128 * 1024 * 1024:
        failures.append("wrong_memory_limit")
    if host_config.get("CapDrop") != ["ALL"]:
        failures.append("capabilities_not_dropped")
    if host_config.get("SecurityOpt") != ["no-new-privileges"]:
        failures.append("no_new_privileges_missing")
    if host_config.get("NanoCpus") != 1_000_000_000:
        failures.append("wrong_cpu_limit")
    if host_config.get("Tmpfs") != {"/tmp": "rw,noexec,nosuid,nodev,size=16m"}:
        failures.append("wrong_tmpfs")

    mounts = record.get("Mounts", [])
    host_mounts = [mount for mount in mounts if mount.get("Type") in {"bind", "volume"}]
    expected_destinations = {"/candidate"}
    if canary_exposed:
        expected_destinations.add("/oracle-canary.txt")
    actual_destinations = {mount.get("Destination") for mount in host_mounts}
    if actual_destinations != expected_destinations:
        failures.append("unexpected_host_mounts")
    candidate_mounts = [mount for mount in host_mounts if mount.get("Destination") == "/candidate"]
    if len(candidate_mounts) != 1:
        failures.append("candidate_mount_missing_or_duplicated")
    else:
        candidate_mount = candidate_mounts[0]
        if candidate_mount.get("Source") != str(candidate_dir):
            failures.append("candidate_mount_source_changed")
        if candidate_mount.get("RW") is not False:
            failures.append("candidate_mount_not_read_only")
    if canary_exposed:
        canary_mounts = [
            mount for mount in host_mounts if mount.get("Destination") == "/oracle-canary.txt"
        ]
        if len(canary_mounts) != 1 or canary_mounts[0].get("RW") is not False:
            failures.append("canary_probe_mount_not_read_only")

    observed = {
        "network_mode": host_config.get("NetworkMode"),
        "readonly_rootfs": host_config.get("ReadonlyRootfs"),
        "user": config.get("User"),
        "pids_limit": host_config.get("PidsLimit"),
        "memory": host_config.get("Memory"),
        "cap_drop": host_config.get("CapDrop"),
        "security_opt": host_config.get("SecurityOpt"),
        "nano_cpus": host_config.get("NanoCpus"),
        "tmpfs": host_config.get("Tmpfs"),
        "host_mount_destinations": sorted(actual_destinations),
        "host_mount_read_only": all(mount.get("RW") is False for mount in host_mounts),
    }
    return {"passed": not failures, "failures": failures, "observed": observed}


def cleanup_container(container_id: str | None) -> str:
    if container_id is None:
        return "unknown_no_valid_cid"
    try:
        removed = subprocess.run(
            ["docker", "rm", "--force", container_id],
            capture_output=True,
            text=True,
            check=False,
            env=safe_env(),
            timeout=10,
        )
        if removed.returncode == 0:
            return "removed"
        inspected = subprocess.run(
            ["docker", "inspect", container_id],
            capture_output=True,
            text=True,
            check=False,
            env=safe_env(),
            timeout=10,
        )
        if inspected.returncode != 0 and any(
            marker in inspected.stderr.lower()
            for marker in ("no such object", "no such container", "not found")
        ):
            return "already_absent"
        return "unknown_cleanup_failure"
    except subprocess.TimeoutExpired:
        return "unknown_cleanup_timeout"


def collect_process(process: subprocess.Popen[bytes], deadline: float) -> dict[str, Any]:
    selector = selectors.DefaultSelector()
    streams: dict[int, str] = {}
    for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        assert stream is not None
        selector.register(stream, selectors.EVENT_READ)
        streams[stream.fileno()] = name
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    timed_out = False
    output_limited = False
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        for key, _ in selector.select(min(remaining, 0.05)):
            chunk = os.read(key.fd, 4096)
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            allowed = MAX_OUTPUT_BYTES - total
            if len(chunk) > allowed:
                chunk = chunk[: max(allowed, 0)]
                output_limited = True
            buffers[streams[key.fd]].extend(chunk)
            total += len(chunk)
            if output_limited:
                break
        if timed_out or output_limited:
            break
    if timed_out or output_limited:
        process.kill()
    elif process.poll() is None:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    selector.close()
    return {
        "stdout": bytes(buffers["stdout"]),
        "stderr": bytes(buffers["stderr"]),
        "returncode": process.returncode,
        "timed_out": timed_out,
        "output_limited": output_limited,
        "output_bytes": total,
    }


def candidate_command(
    candidate_dir: Path,
    cidfile: Path,
    reference: str,
    host_canary: Path | None = None,
    network_name: str = "none",
) -> list[str]:
    command = [
        "docker",
        "run",
        "--interactive",
        "--network",
        network_name,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "65532:65532",
        "--memory",
        "128m",
        "--pids-limit",
        "64",
        "--cpus",
        "1.0",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=16m",
        "--env",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "--env",
        "PYTHONUNBUFFERED=1",
        "--cidfile",
        str(cidfile),
        "--mount",
        f"type=bind,src={candidate_dir},dst=/candidate,readonly",
    ]
    if host_canary is not None:
        command.extend(
            [
                "--mount",
                f"type=bind,src={host_canary},dst=/oracle-canary.txt,readonly",
            ]
        )
    command.extend([reference, "python", "/candidate/program.py"])
    return command


def strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_response(stdout: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        response = json.loads(stdout, object_pairs_hook=strict_json_object)
    except (json.JSONDecodeError, ValueError):
        return None, "rejected_invalid_json_or_empty"
    if not isinstance(response, dict):
        return None, "rejected_response_not_object"
    return response, None


def exact_business_response(response: dict[str, Any] | None) -> bool:
    return (
        response is not None
        and set(response) == {"result"}
        and type(response["result"]) is int
        and response["result"] == EXPECTED_RESULT
    )


def normal_control_passed(outcome: dict[str, Any]) -> bool:
    return (
        outcome.get("container_controls_passed") is True
        and outcome.get("host_cache_unchanged") is True
        and outcome.get("canary_visible") is False
        and outcome.get("container_cleanup") in {"removed", "already_absent"}
    )


def check_admission(
    outcomes: list[dict[str, Any]],
    resource_cleanup: dict[str, str] | None = None,
    identity_stable: bool = True,
) -> dict[str, Any]:
    by_name = {outcome.get("name"): outcome for outcome in outcomes}
    failures: list[str] = []
    if len(by_name) != len(outcomes):
        failures.append("duplicate_outcome_names")
    if not outcomes:
        failures.append("empty_outcomes")
    if "healthy" not in by_name:
        failures.append("missing_healthy")
    else:
        if not by_name["healthy"].get("accepted"):
            failures.append("healthy_not_accepted")
        if not normal_control_passed(by_name["healthy"]):
            failures.append("control:healthy")
    missing_attacks = sorted(EXPECTED_ATTACKS - set(by_name))
    if missing_attacks:
        failures.append("missing_attacks:" + ",".join(missing_attacks))
    for name in EXPECTED_ATTACKS & set(by_name):
        outcome = by_name[name]
        if not normal_control_passed(outcome):
            failures.append(f"control:{name}")
        if outcome.get("reason") != EXPECTED_CLASSIFICATIONS[name]:
            failures.append(f"classification:{name}")
        if outcome.get("accepted"):
            failures.append(f"attack_accepted:{name}")
    exposed = by_name.get("exposed_canary_control")
    if exposed is None:
        failures.append("missing_exposed_canary_control")
    elif not (
        exposed.get("canary_visible") is True
        and exposed.get("accepted") is False
        and exposed.get("container_controls_passed") is True
        and exposed.get("host_cache_unchanged") is True
        and exposed.get("boundary_control_passed") is False
        and exposed.get("container_cleanup") in {"removed", "already_absent"}
    ):
        failures.append("exposed_canary_probe_failed")
    network_probe = by_name.get("exposed_network_control")
    if network_probe is None:
        failures.append("missing_exposed_network_control")
    elif not (
        network_probe.get("network_server_connected") is True
        and network_probe.get("accepted") is False
        and network_probe.get("container_controls_passed") is True
        and network_probe.get("host_cache_unchanged") is True
        and network_probe.get("boundary_control_passed") is False
        and network_probe.get("container_cleanup") in {"removed", "already_absent"}
    ):
        failures.append("exposed_network_probe_failed")
    safety_outcomes = [
        by_name[name] for name in ({"healthy"} | EXPECTED_ATTACKS) if name in by_name
    ]
    safety_failures = [
        outcome["name"]
        for outcome in safety_outcomes
        if not normal_control_passed(outcome)
    ]
    exposed_probe_passed = exposed is not None and (
        exposed.get("canary_visible") is True
        and exposed.get("accepted") is False
        and exposed.get("container_controls_passed") is True
        and exposed.get("host_cache_unchanged") is True
        and exposed.get("boundary_control_passed") is False
        and exposed.get("container_cleanup") in {"removed", "already_absent"}
    )
    network_probe_passed = network_probe is not None and (
        network_probe.get("network_server_connected") is True
        and network_probe.get("accepted") is False
        and network_probe.get("container_controls_passed") is True
        and network_probe.get("host_cache_unchanged") is True
        and network_probe.get("boundary_control_passed") is False
        and network_probe.get("container_cleanup") in {"removed", "already_absent"}
    )
    if resource_cleanup is not None:
        for resource, status in resource_cleanup.items():
            if status not in {"removed", "already_absent"}:
                failures.append(f"cleanup:{resource}:{status}")
    if not identity_stable:
        failures.append("source_identity_changed")
    return {
        "passed": not failures,
        "failures": failures,
        "safety_invariants_passed": not safety_failures,
        "safety_invariant_failures": safety_failures,
        "exposed_probe_passed": exposed_probe_passed,
        "network_probe_passed": network_probe_passed,
    }


def run_candidate(
    name: str,
    source: Path,
    reference: str,
    host_canary: Path,
    host_cache: Path,
    canary_exposed: bool = False,
    network_name: str = "none",
    network_exposed: bool = False,
    network_server_id: str | None = None,
) -> dict[str, Any]:
    candidate_bytes = source.read_bytes()
    cache_before = sha256_file(host_cache)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="confinement-", dir=SCRATCH_ROOT) as owned_tmp:
        owned = Path(owned_tmp)
        candidate_dir = owned / "candidate"
        candidate_dir.mkdir(mode=0o755)
        program = candidate_dir / "program.py"
        program.write_bytes(candidate_bytes)
        program.chmod(0o444)
        cidfile = owned / "container.id"
        container_id: str | None = None
        controls: dict[str, Any] = {
            "passed": False,
            "failures": ["candidate_not_inspected"],
            "observed": {},
        }
        cleanup = "unknown_candidate_not_cleaned"
        try:
            process = subprocess.Popen(
                candidate_command(
                    candidate_dir,
                    cidfile,
                    reference,
                    host_canary if canary_exposed else None,
                    network_name,
                ),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=safe_env(),
            )
            assert process.stdin is not None
            try:
                process.stdin.write((json.dumps(EXPECTED_REQUEST) + "\n").encode())
                process.stdin.close()
            except BrokenPipeError:
                process.stdin.close()
            collected = collect_process(process, time.monotonic() + TIMEOUT_SECONDS)
        finally:
            container_id = container_id_from(cidfile)
            try:
                inspection = (
                    inspect_container(container_id)
                    if container_id
                    else {"status": "unknown_no_cid"}
                )
                if inspection.get("status") == "ok":
                    controls = check_container_controls(
                        inspection["record"], candidate_dir, canary_exposed, network_name
                    )
                else:
                    controls = {
                        "passed": False,
                        "failures": [inspection["status"]],
                        "observed": {},
                    }
            except Exception as error:
                controls = {
                    "passed": False,
                    "failures": [f"inspect_exception:{type(error).__name__}"],
                    "observed": {},
                }
            try:
                cleanup = cleanup_container(container_id)
            except Exception as error:
                cleanup = f"unknown_cleanup_exception:{type(error).__name__}"
    stdout = normalize_bytes(collected["stdout"])
    stderr = normalize_bytes(collected["stderr"])
    reason = "rejected"
    if collected["timed_out"]:
        reason = "candidate_unknown_timeout"
    elif collected["output_limited"]:
        reason = "rejected_output_limit"
    elif collected["returncode"] != 0:
        reason = "rejected_nonzero_exit"
    else:
        response, parse_reason = parse_response(stdout)
        if parse_reason:
            reason = parse_reason
        elif not exact_business_response(response) or stderr:
            reason = "rejected_wrong_business_response"
        else:
            reason = "protocol_business_match"
    canary_visible = host_canary.read_text() in stdout or host_canary.read_text() in stderr
    cache_unchanged = cache_before == sha256_file(host_cache)
    server_connected = (
        network_server_connected(network_server_id)
        if network_exposed and network_server_id is not None
        else None
    )
    protocol_accepted = reason == "protocol_business_match"
    safety_canary_ok = not canary_visible
    canary_probe_readable = canary_exposed and canary_visible
    safety_network_ok = network_name == "none"
    boundary_control_passed = (
        controls["passed"]
        and cache_unchanged
        and safety_canary_ok
        and safety_network_ok
        and cleanup in {"removed", "already_absent"}
    )
    accepted = protocol_accepted and boundary_control_passed and not canary_exposed
    if protocol_accepted and not boundary_control_passed:
        reason = "rejected_boundary_control"
    return {
        "name": name,
        "candidate_sha256": sha256_bytes(candidate_bytes),
        "accepted": accepted,
        "protocol_accepted": protocol_accepted,
        "reason": reason,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "returncode": collected["returncode"],
        "output_bytes": collected["output_bytes"],
        "stdout_preview": stdout[:256],
        "stderr_preview": stderr[:256],
        "canary_visible": canary_visible,
        "host_cache_unchanged": cache_unchanged,
        "container_cleanup": cleanup,
        "canary_exposed_for_positive_control": canary_exposed,
        "canary_probe_readable": canary_probe_readable,
        "network_exposed_for_positive_control": network_exposed,
        "network_server_connected": server_connected,
        "container_controls_passed": controls["passed"],
        "container_control_failures": controls["failures"],
        "container_observed": controls["observed"],
        "boundary_control_passed": boundary_control_passed,
    }


def main() -> int:
    write_evidence(fresh_non_green("run_not_completed", {"phase": "start"}))
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    network_name: str | None = None
    network_id: str | None = None
    server_id: str | None = None
    server_cleanup = "unknown_server_not_started"
    network_cleanup = "unknown_network_not_cleaned"
    network_elapsed: float | None = None
    server: dict[str, Any] | None = None
    try:
        reference, digest, docker_platform, host_machine = image_ref()
        pull = docker_pull(reference, docker_platform)
        if pull["status"] != "ok":
            evidence = fresh_non_green("image_pull_failed", {"image_pull": pull})
            write_evidence(evidence)
            print(json.dumps(evidence, indent=2))
            return 1
        identity = image_identity(reference)
        candidates = sorted((ROOT / "candidates").glob("*.py"))
        if not candidates:
            raise RuntimeError("no candidate programs")
        verifier_paths = [ROOT / name for name in VERIFIER_FILES]
        candidate_hashes_before = hash_files(candidates)
        verifier_hashes_before = hash_files(verifier_paths)
        network_name, network_id, network_elapsed = create_network()
        network_tmp = SCRATCH_ROOT / f"network-{uuid.uuid4().hex}"
        network_tmp.mkdir()
        outcomes: list[dict[str, Any]] = []
        try:
            server = start_network_server(reference, network_name, network_tmp / "server.id")
            server_id = server["container_id"]
            with tempfile.TemporaryDirectory(prefix="confinement-host-", dir=SCRATCH_ROOT) as host_tmp:
                host_root = Path(host_tmp)
                host_canary = host_root / "host-canary.txt"
                host_canary.write_text(f"synthetic-host-canary-{uuid.uuid4().hex}")
                host_cache = host_root / "oracle-cache.json"
                host_cache.write_text(json.dumps({"trusted": True}) + "\n")
                outcomes.extend(
                    run_candidate(path.stem, path, reference, host_canary, host_cache)
                    for path in candidates
                )
                outcomes.append(
                    run_candidate(
                        "exposed_canary_control",
                        ROOT / "candidates" / "read_canary.py",
                        reference,
                        host_canary,
                        host_cache,
                        canary_exposed=True,
                    )
                )
                outcomes.append(
                    run_candidate(
                        "exposed_network_control",
                        ROOT / "candidates" / "network.py",
                        reference,
                        host_canary,
                        host_cache,
                        network_name=network_name,
                        network_exposed=True,
                        network_server_id=server_id,
                    )
                )
        finally:
            if server_id is not None:
                try:
                    server_cleanup = cleanup_container(server_id)
                except Exception:
                    server_cleanup = "unknown_server_cleanup_exception"
            try:
                network_cleanup = cleanup_network(network_id)
            except Exception:
                network_cleanup = "unknown_network_cleanup_exception"
            try:
                network_tmp.rmdir()
            except (NameError, OSError):
                pass

        candidate_hashes_after = hash_files(candidates)
        verifier_hashes_after = hash_files(verifier_paths)
        identity_stable = (
            candidate_hashes_before == candidate_hashes_after
            and verifier_hashes_before == verifier_hashes_after
        )
        resource_cleanup = {
            "network_server": server_cleanup,
            "network": network_cleanup,
        }
        admission_check = check_admission(
            outcomes, resource_cleanup=resource_cleanup, identity_stable=identity_stable
        )
        evidence = {
            "evidence_status": "complete",
            "image_tag": IMAGE_TAG,
            "image_reference": reference,
            "image_digest": digest,
            "docker_platform": docker_platform,
            "host_machine": host_machine,
            "image_identity": identity,
            "image_pull": pull,
            "verifier_sha256_before": verifier_hashes_before,
            "verifier_sha256_after": verifier_hashes_after,
            "candidate_sha256_before": candidate_hashes_before,
            "candidate_sha256_after": candidate_hashes_after,
            "source_identity_stable": identity_stable,
            "request": EXPECTED_REQUEST,
            "oracle_expected_result": EXPECTED_RESULT,
            "candidate_protocol": "candidate stdout must be exactly {\"result\": 5}; duplicate keys, floats, and extra fields are rejected",
            "limits": {"timeout_seconds": TIMEOUT_SECONDS, "max_output_bytes": MAX_OUTPUT_BYTES, "memory": "128m", "pids": 64},
            "container_controls": {
                "network": "none",
                "root_filesystem": "read-only",
                "capabilities": "drop-all",
                "no_new_privileges": True,
                "uid_gid": "65532:65532",
                "cpu": "1.0",
                "tmpfs": "/tmp:rw,noexec,nosuid,nodev,size=16m",
                "host_mount_policy": "only candidate read-only bind; positive probes add one deliberate read-only bind or owned network",
            },
            "network_canary": {
                "name": network_name,
                "create_elapsed_ms": network_elapsed,
                "server_start_elapsed_ms": server["elapsed_ms"] if server else None,
                "server_cleanup": server_cleanup,
                "network_cleanup": network_cleanup,
                "external_ports": [],
            },
            "outcomes": outcomes,
            "positive_business_behavior": next((item["accepted"] for item in outcomes if item["name"] == "healthy"), False),
            "negative_controls_passed": all(
                not item["accepted"] and normal_control_passed(item)
                for item in outcomes
                if item["name"] in EXPECTED_ATTACKS
            ),
            "candidate_unknown_timeouts": [
                item["name"]
                for item in outcomes
                if item["reason"] == "candidate_unknown_timeout"
            ],
            "cleanup_unknowns": [
                item["name"]
                for item in outcomes
                if item["container_cleanup"].startswith("unknown")
            ] + [
                resource
                for resource, status in resource_cleanup.items()
                if status.startswith("unknown")
            ],
            "network_verification": {
                "mode": "owned_docker_bridge_tcp_canary",
                "empirical_tcp_canary": True,
                "claim": "only the owned bridge canary was reachable by the deliberate positive-control container; normal candidates observed NetworkMode=none",
            },
            "checks": admission_check,
            "limitations": [
                "This is a Docker process boundary, not a VM or complete side-channel defense.",
                "Host Docker daemon and kernel remain trusted; no claim covers kernel escape or timing/cache channels.",
                "The business fixture is intentionally tiny; it demonstrates authority separation, not application coverage.",
                "A timeout, cleanup uncertainty, or local enforcement failure is non-green/unknown.",
            ],
            "admission": "green" if admission_check["passed"] else "not_green",
        }
        write_evidence(evidence)
        print(json.dumps(evidence, indent=2))
        return 0 if evidence["admission"] == "green" else 1
    except Exception as error:
        if server_id is not None and server_cleanup.startswith("unknown"):
            try:
                server_cleanup = cleanup_container(server_id)
            except Exception:
                server_cleanup = "unknown_server_cleanup_exception"
        if network_id is not None and network_cleanup.startswith("unknown"):
            try:
                network_cleanup = cleanup_network(network_id)
            except Exception:
                network_cleanup = "unknown_network_cleanup_exception"
        evidence = fresh_non_green(
            "runner_exception",
            {
                "exception_type": type(error).__name__,
                "server_cleanup": server_cleanup,
                "network_cleanup": network_cleanup,
            },
        )
        write_evidence(evidence)
        print(json.dumps(evidence, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

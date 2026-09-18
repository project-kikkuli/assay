#!/usr/bin/env python3
"""Run the pinned Cedar Lean/CVC5 experiment without compiling a substitute solver."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
IMAGE = os.environ.get("KIKKULI_CEDAR_IMAGE", "kikkuli-cedar-lean-cli:acb0db7-slim")
EXPECTED_IMAGE_ID = (
    "sha256:9f8a1324cbf6d7c80ff5de21942f3e6b19417930f8085ccee8490ff2f56c43bf"
)
SCRATCH_PARENT = Path(os.environ.get("KIKKULI_CEDAR_SCRATCH", tempfile.gettempdir()))
SOURCE_COMMITS = {
    "cedar_spec": "acb0db7daa838249d894d2af64c850e1c9bf0d7d",
    "cedar_dependency": "9502ae02564a23028c732f8c1f2311635394c34f",
    "lean": "4.34.0",
    "rust": "1.91.1",
    "cvc5": "1.2.1",
}
INPUT_FILES = (
    "schema.cedarschema",
    "envelope.cedar",
    "candidate.cedar",
    "broken.cedar",
    "deny-all.cedar",
    "entities.json",
    "request-positive.json",
    "request-cross-tenant.json",
    "request-missing-public.json",
)
VERIFIER_FILES = (
    "README.md",
    "Dockerfile",
    "PROVENANCE.md",
    "prepare.py",
    "run.py",
    "test_run.py",
    "locks/cedar-lean-ffi.Cargo.lock",
    "locks/cedar-lean-cli.Cargo.lock",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def docker_args(
    cli_args: list[str], *, image_ref: str, cidfile: Path, entrypoint: str | None = None
) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--cidfile",
        str(cidfile),
        "-v",
        f"{ROOT}:/work:ro",
        *(["--entrypoint", entrypoint] if entrypoint else []),
        image_ref,
        *cli_args,
    ]


def run_case(
    name: str,
    cli_args: list[str],
    timeout: float,
    *,
    image_ref: str,
    entrypoint: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    SCRATCH_PARENT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="cedar-run-", dir=SCRATCH_PARENT
    ) as owned_tmp:
        cidfile = Path(owned_tmp) / "container.id"
        try:
            completed = subprocess.run(
                docker_args(
                    cli_args,
                    image_ref=image_ref,
                    cidfile=cidfile,
                    entrypoint=entrypoint,
                ),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            status = "ok" if completed.returncode == 0 else "error"
            stdout = normalize_text(completed.stdout)
            stderr = normalize_text(completed.stderr)
            exit_code: int | None = completed.returncode
        except subprocess.TimeoutExpired as error:
            status = "unknown"
            stdout = normalize_text(error.stdout)
            stderr = normalize_text(error.stderr)
            exit_code = None
        finally:
            cleanup_container(cidfile)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return {
        "name": name,
        "status": status,
        "exit_code": exit_code,
        "elapsed_ms": elapsed_ms,
        "stdout": stdout,
        "stderr": stderr,
        "semantic": semantic_result(name, status, stdout),
    }


def normalize_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value.strip()


def cleanup_container(cidfile: Path) -> None:
    if not cidfile.is_file():
        return
    container_id = cidfile.read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise RuntimeError("refusing to clean up an invalid Docker container ID")
    try:
        removed = subprocess.run(
            ["docker", "rm", "--force", container_id],
            capture_output=True,
            check=False,
            timeout=10,
            text=True,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Docker container cleanup timed out") from error
    if removed.returncode == 0:
        return
    try:
        inspected = subprocess.run(
            ["docker", "inspect", container_id],
            capture_output=True,
            check=False,
            timeout=10,
            text=True,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Docker container absence check timed out") from error
    if inspected.returncode == 0:
        raise RuntimeError("Docker container cleanup failed")
    diagnostic = f"{inspected.stdout}\n{inspected.stderr}"
    if not any(
        marker in diagnostic
        for marker in (
            "No such object",
            "No such container",
            "no such object",
            "not found",
            "does not exist",
        )
    ):
        raise RuntimeError("Docker container cleanup status was not verifiable")


def semantic_result(name: str, status: str, stdout: str) -> str:
    if not name.startswith("smt_") or status != "ok":
        return "runtime" if not name.startswith("smt_") else "unknown"
    lines = {line.strip() for line in stdout.splitlines()}
    positive = "pset1 implies pset2" in lines
    negative = "pset1 does not imply pset2" in lines
    if positive == negative:
        return "unknown"
    if positive:
        return "proved"
    return "counterexample"


def output(case: dict[str, Any]) -> str:
    return f"{case['stdout']}\n{case['stderr']}".strip()


def expect(case: dict[str, Any], needle: str, *, absent: str | None = None) -> None:
    text = output(case)
    if case["status"] != "ok" or needle not in text or (absent and absent in text):
        raise RuntimeError(f"{case['name']} did not meet expectation:\n{text}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="Wall timeout for each Docker/solver call; a timeout is recorded as unknown.",
    )
    parser.add_argument("--write-evidence", action="store_true")
    parser.add_argument("--warm-repeat", type=int, default=3)
    args = parser.parse_args()
    if not 0 < args.timeout_seconds <= 300:
        parser.error("timeout-seconds must be greater than zero and at most 300")
    if not 1 <= args.warm_repeat <= 100:
        parser.error("warm-repeat must be between 1 and 100")

    common = ["--run-analysis"]
    cases = [
        (
            "validator_candidate",
            [
                "validate",
                "policy-set",
                "/work/candidate.cedar",
                "/work/schema.cedarschema",
            ],
        ),
        (
            "validator_broken",
            [
                "validate",
                "policy-set",
                "/work/broken.cedar",
                "/work/schema.cedarschema",
            ],
        ),
        (
            "validator_entities",
            ["validate", "entities", "/work/schema.cedarschema", "/work/entities.json"],
        ),
        (
            "smt_candidate_implies_envelope",
            [
                "symcc",
                "check-implies",
                "/work/candidate.cedar",
                "/work/envelope.cedar",
                "/work/schema.cedarschema",
                *common,
            ],
        ),
        (
            "smt_broken_does_not_imply_envelope",
            [
                "symcc",
                "check-implies",
                "/work/broken.cedar",
                "/work/envelope.cedar",
                "/work/schema.cedarschema",
                *common,
            ],
        ),
        (
            "smt_envelope_implies_candidate_availability",
            [
                "symcc",
                "check-implies",
                "/work/envelope.cedar",
                "/work/candidate.cedar",
                "/work/schema.cedarschema",
                *common,
            ],
        ),
        (
            "smt_deny_all_implies_envelope_vacuously",
            [
                "symcc",
                "check-implies",
                "/work/deny-all.cedar",
                "/work/envelope.cedar",
                "/work/schema.cedarschema",
                *common,
            ],
        ),
        (
            "smt_envelope_does_not_imply_deny_all",
            [
                "symcc",
                "check-implies",
                "/work/envelope.cedar",
                "/work/deny-all.cedar",
                "/work/schema.cedarschema",
                *common,
            ],
        ),
        (
            "replay_positive_access",
            [
                "evaluate",
                "authorize",
                "/work/candidate.cedar",
                "/work/entities.json",
                "/work/schema.cedarschema",
                "--request-file",
                "/work/request-positive.json",
            ],
        ),
        (
            "replay_cross_tenant_candidate",
            [
                "evaluate",
                "authorize",
                "/work/candidate.cedar",
                "/work/entities.json",
                "/work/schema.cedarschema",
                "--request-file",
                "/work/request-cross-tenant.json",
            ],
        ),
        (
            "replay_cross_tenant_envelope",
            [
                "evaluate",
                "authorize",
                "/work/envelope.cedar",
                "/work/entities.json",
                "/work/schema.cedarschema",
                "--request-file",
                "/work/request-cross-tenant.json",
            ],
        ),
        (
            "replay_cross_tenant_broken",
            [
                "evaluate",
                "authorize",
                "/work/broken.cedar",
                "/work/entities.json",
                "/work/schema.cedarschema",
                "--request-file",
                "/work/request-cross-tenant.json",
            ],
        ),
        (
            "replay_missing_public",
            [
                "evaluate",
                "authorize",
                "/work/candidate.cedar",
                "/work/entities.json",
                "/work/schema.cedarschema",
                "--request-file",
                "/work/request-missing-public.json",
            ],
        ),
        (
            "replay_positive_deny_all",
            [
                "evaluate",
                "authorize",
                "/work/deny-all.cedar",
                "/work/entities.json",
                "/work/schema.cedarschema",
                "--request-file",
                "/work/request-positive.json",
            ],
        ),
        (
            "smtlib_emission",
            [
                "symcc",
                "check-implies",
                "/work/candidate.cedar",
                "/work/envelope.cedar",
                "/work/schema.cedarschema",
                "--print-smtlib",
            ],
        ),
    ]

    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE],
        text=True,
        timeout=10,
    ).strip()
    if image_id != EXPECTED_IMAGE_ID:
        raise RuntimeError(
            f"refusing unpinned image {image_id}; expected {EXPECTED_IMAGE_ID}"
        )

    fixture_before = {name: sha256(ROOT / name) for name in INPUT_FILES}
    verifier_before = {name: sha256(ROOT / name) for name in VERIFIER_FILES}
    image_ref = image_id
    results: list[dict[str, Any]] = [
        run_case(
            "container_start",
            [],
            args.timeout_seconds,
            image_ref=image_ref,
            entrypoint="/usr/bin/true",
        )
    ]
    for name, command in cases:
        case = run_case(name, command, args.timeout_seconds, image_ref=image_ref)
        results.append(case)

    by_name = {case["name"]: case for case in results}
    expectations = {
        "validator_candidate": "Policyset successfully validated",
        "validator_broken": "Policyset successfully validated",
        "validator_entities": "Entities successfully validated",
        "smt_candidate_implies_envelope": "pset1 implies pset2",
        "smt_broken_does_not_imply_envelope": "pset1 does not imply pset2",
        "smt_envelope_implies_candidate_availability": "pset1 implies pset2",
        "smt_deny_all_implies_envelope_vacuously": "pset1 implies pset2",
        "smt_envelope_does_not_imply_deny_all": "pset1 does not imply pset2",
        "replay_positive_access": "This request was allowed",
        "replay_cross_tenant_candidate": "This request was denies",
        "replay_cross_tenant_envelope": "This request was denies",
        "replay_cross_tenant_broken": "This request was allowed",
        "replay_missing_public": "implicitly denied",
        "replay_positive_deny_all": "This request was denies",
        "smtlib_emission": "(set-logic",
    }
    failures: list[str] = []
    for name, needle in expectations.items():
        try:
            expect(by_name[name], needle)
        except RuntimeError as error:
            failures.append(str(error))

    semantic_expectations = {
        "smt_candidate_implies_envelope": "proved",
        "smt_broken_does_not_imply_envelope": "counterexample",
        "smt_envelope_implies_candidate_availability": "proved",
        "smt_deny_all_implies_envelope_vacuously": "proved",
        "smt_envelope_does_not_imply_deny_all": "counterexample",
    }
    for name, semantic in semantic_expectations.items():
        if by_name[name]["semantic"] != semantic:
            failures.append(
                f"{name} semantic result was {by_name[name]['semantic']}, expected {semantic}"
            )

    warm = [
        run_case(
            f"smt_warm_candidate_implies_{index}",
            [
                "symcc",
                "check-implies",
                "/work/candidate.cedar",
                "/work/envelope.cedar",
                "/work/schema.cedarschema",
                *common,
            ],
            args.timeout_seconds,
            image_ref=image_ref,
        )
        for index in range(1, args.warm_repeat + 1)
    ]
    for case in warm:
        if case["status"] != "ok" or case["semantic"] != "proved":
            failures.append(f"{case['name']} was not an admitted proof sample")
    fixture_after = {name: sha256(ROOT / name) for name in INPUT_FILES}
    verifier_after = {name: sha256(ROOT / name) for name in VERIFIER_FILES}
    if fixture_before != fixture_after:
        failures.append("fixture hash changed during verification")
    if verifier_before != verifier_after:
        failures.append("verifier hash changed during verification")
    evidence = {
        "tool": "cedar-lean-cli",
        "image": IMAGE,
        "image_id": image_id,
        "versions": SOURCE_COMMITS,
        "verifier_sha256": verifier_before,
        "verifier_sha256_after": verifier_after,
        "verifier_unchanged": verifier_before == verifier_after,
        "fixture_sha256": {
            path.name: sha256(path) for path in (ROOT / name for name in INPUT_FILES)
        },
        "timeout_semantics": "A wall-clock timeout is reported as unknown; no timeout was reclassified as proof.",
        "results": results,
        "fixture_sha256_before": fixture_before,
        "fixture_sha256_after": fixture_after,
        "warm_candidate_implies": warm,
        "admission": "accept" if not failures else "reject",
        "assertions_passed": not failures,
        "failures": failures,
    }
    if args.write_evidence:
        (ROOT / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

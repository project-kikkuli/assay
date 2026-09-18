#!/usr/bin/env python3
"""Exercise one bounded business cell, with fresh state and no result cache."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import time

from actor_runtime import Actor, ActorUnknown
from admission import ACTOR, decide, digest
from behavior import BehavioralFailure, complete, exercise
from fixture import observe, public_fixture, seed_users
from kernel import Kernel, KernelError
import psycopg
from state_machine import ModelFailure, exercise_sequence


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CHALLENGES = {
    "attack_delete_wrong_id.py": "boundary_rejected",
    "attack_drop_patch_field.py": "behavior_rejected",
    "attack_env_read.py": "behavior_passed",
    "attack_forge_green.py": "boundary_rejected",
    "attack_hang.py": "execution_unknown",
    "attack_oracle_probe.py": "boundary_rejected",
    "attack_output_flood.py": "execution_unknown",
    "attack_owner_id_injection.py": "boundary_rejected",
    "attack_paging.py": "behavior_rejected",
    "attack_raw_sql_unknown_op.py": "boundary_rejected",
    "attack_stale_invalidation.py": "boundary_rejected",
    "attack_target_only.py": "boundary_rejected",
    "attack_uppercase_title.py": "behavior_rejected",
    "attack_wrong_request_field.py": "boundary_rejected",
}


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def source_identity(subject):
    """Tracked and non-ignored new source, plus built browser artifact.

    This does not attest installed site-packages or ignored build/tool state.
    Those belong to the explicitly trusted prepared environment.
    """
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=subject
    ).split(b"\0")
    source = hashlib.sha256()
    for raw in sorted(set(n for n in names if n)):
        path = subject / raw.decode()
        contents = (b"symlink:" + os.fsencode(os.readlink(path)) if path.is_symlink()
                    else path.read_bytes() if path.is_file() else b"<absent>")
        source.update(raw + b"\0" + contents + b"\0")
    artifact = hashlib.sha256()
    paths = sorted((subject / "backend/app/frontend").rglob("*"))
    files = [p for p in paths if p.is_file()]
    if not files:
        raise ValueError("production frontend is not prepared")
    for path in files:
        artifact.update(path.relative_to(subject).as_posix().encode() + b"\0" + path.read_bytes())
    return {"tracked_source_sha256": source.hexdigest(), "frontend_sha256": artifact.hexdigest()}


def trusted_inputs(subject_identity):
    # Local experiment baseline: this checkout is operator-trusted. Production
    # must obtain these bytes from protected base CI, not the submitted branch.
    result = {p.relative_to(HERE).as_posix(): digest(p.read_bytes()) for p in HERE.glob("*")
              if p.is_file() and p.suffix in {".py", ".mjs"}}
    result["subject.identity"] = digest(json.dumps(subject_identity, sort_keys=True).encode())
    for name in ("test_cases.json",):
        result[name] = digest((HERE / name).read_bytes())
    for path in sorted((HERE / "candidates").glob("attack_*.py")):
        result[path.relative_to(HERE).as_posix()] = digest(path.read_bytes())
    if (ROOT / "cell").is_file():
        result["launcher"] = digest((ROOT / "cell").read_bytes())
    for path in (ROOT / "tools/prepare_lab.py", HERE.parent / "fullstack/repaired.patch"):
        result[path.relative_to(ROOT).as_posix()] = digest(path.read_bytes())
    result[ACTOR] = digest((HERE / "candidates/healthy.py").read_bytes())
    return result


class Witness:
    def __init__(self, actor, dsn):
        self.actor, self.dsn = actor, dsn
        self.trace = []
        self.before = None

    def __call__(self, command, view):
        self.before = observe(self.dsn)
        proposal = self.actor(command, view)
        # Show exact public requests; do not publish arbitrary candidate strings.
        def strings(value):
            if isinstance(value, dict):
                return set().union(*(strings(v) for v in value.values())) if value else set()
            return {value} if isinstance(value, str) else set()
        allowed_values = strings(command)
        allowed_keys = {"op", "target", "fields", "title", "description", "skip", "limit"}
        def safe(value):
            if isinstance(value, dict):
                return {k if k in allowed_keys else "unknown_key_" + digest(k.encode()): safe(v)
                        for k, v in value.items()}
            if isinstance(value, list):
                return [safe(v) for v in value]
            if isinstance(value, str) and value not in allowed_values:
                return {"redacted_sha256": digest(value.encode())}
            return value
        self.trace.append({"command": command, "proposal": safe(proposal)})
        return proposal


def check_candidate(dsn, path, seed=260918):
    started = time.perf_counter()
    alice, bob = seed_users(dsn)
    report = {"candidate": path.name, "candidate_sha256": digest(path.read_bytes()),
              "status": "unknown", "obligations": [], "cleanup": "pending"}
    actor = Actor(path)
    witness = Witness(actor, dsn)
    kernel = Kernel(dsn, witness)
    try:
        with actor:
            kernel.setup_rls([alice, bob])
            report["obligations"] = exercise(kernel, alice, bob, lambda owner: observe(dsn, owner))
            report["status"] = "behavior_passed" if complete(report["obligations"]) else "unknown"
            if report["status"] == "behavior_passed":
                model_alice, model_bob = seed_users(dsn)
                kernel.setup_rls([model_alice, model_bob])
                report["model"] = exercise_sequence(
                    kernel, model_alice, model_bob,
                    lambda: observe(dsn, model_alice) + observe(dsn, model_bob), seed, steps=60)
                if (report["model"].get("passed") is not True or report["model"].get("steps") != 60
                        or len(report["model"].get("records", [])) != 60):
                    report["status"] = "unknown"
    except BehavioralFailure as error:
        report.update(status="behavior_rejected", reason=str(error), obligations=error.records)
    except ModelFailure as error:
        report.update(status="behavior_rejected", reason=str(error),
                      model={"passed": False, "seed": seed, "records": error.records})
    except KernelError as error:
        if isinstance(error.__cause__, ActorUnknown):
            report.update(status="execution_unknown", reason="actor transport could not complete")
        else:
            unchanged = witness.before is not None and witness.before == observe(dsn)
            report.update(status="boundary_rejected" if unchanged else "unknown",
                          reason=error.detail, http_status=error.status,
                          rejected_operation_left_state_unchanged=unchanged)
    except Exception as error:
        report.update(status="unknown", reason=type(error).__name__)
    finally:
        try:
            expected_roles = set(kernel._created_roles)
            report["role_cleanup"] = kernel.cleanup()
            with psycopg.connect(dsn) as db:
                remaining = db.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                                       (sorted(expected_roles),)).fetchall()
            cleanup_ok = (not remaining and set(report["role_cleanup"].get("roles_dropped", [])) == expected_roles)
            report["cleanup"] = "removed" if cleanup_ok else "failed"
            if not cleanup_ok:
                report["status"] = "unknown"
        except Exception as error:
            report.update(status="unknown", cleanup=type(error).__name__)
        actor.close()
        report["runtime"] = actor.observations
        report["trace"] = witness.trace
        report["seconds"] = time.perf_counter() - started
        report["candidate_unchanged"] = report["candidate_sha256"] == digest(path.read_bytes())
        if not report["candidate_unchanged"] or actor.observations.get("cleanup", {}).get("status") != "removed":
            report["status"] = "unknown"
    return report


def challenges_valid(runs):
    cases = [r for r in runs if r["kind"] == "challenge"]
    if len(cases) != len(CHALLENGES) or {r["candidate"] for r in cases} != set(CHALLENGES):
        return False
    failures = {"attack_hang.py": "deadline_exceeded", "attack_output_flood.py": "output_cap_exceeded"}
    return all(r["status"] == CHALLENGES[r["candidate"]]
               and r.get("cleanup") == "removed" and r.get("candidate_unchanged") is True
               and r.get("runtime", {}).get("container_controls", {}).get("passed") is True
               and r.get("runtime", {}).get("cleanup", {}).get("status") == "removed"
               and (r["candidate"] not in failures
                    or r.get("runtime", {}).get("failure_kind") == failures[r["candidate"]])
               for r in cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", type=Path, default=ROOT / "out/lab/fullstack")
    parser.add_argument("--admin-dsn", default="host=127.0.0.1 port=55439 dbname=postgres user=postgres password=assay-local-only")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--challenges", action="store_true")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--baseline", type=Path, help="operator-protected qualification manifest")
    parser.add_argument("--output", type=Path, default=ROOT / "out/cell/result.json")
    args = parser.parse_args()
    if not 1 <= args.repeat <= 30:
        parser.error("repeat must be between 1 and 30")
    if args.candidate and (not args.baseline or args.challenges):
        parser.error("candidate checks require --baseline and cannot include --challenges")
    started = time.perf_counter()
    report = {"status": "unknown", "merge_admission": "not_implemented",
              "runs": [], "fixture": {}, "dependencies_prepared": True,
              "expected_challenge_count": len(CHALLENGES) if args.challenges else 0,
              "environment": {"system": platform.system(), "machine": platform.machine()},
              "scope": "bounded actor changes only; not whole-app CI or remote attestation"}
    write(args.output, report)
    try:
        subject = args.subject.resolve()
        report["subject"] = source_identity(subject)
        baseline = trusted_inputs(report["subject"])
        report["trusted_inputs"] = baseline
        candidate_path = args.candidate.resolve() if args.candidate else HERE / "candidates/healthy.py"
        if args.baseline:
            protected = json.loads(args.baseline.read_text())
            report["scope_decision"] = decide(protected, baseline | {ACTOR: digest(candidate_path.read_bytes())})
            if report["scope_decision"]["lane"] != "actor_checks_required":
                report["status"] = "broader_required"
                return 2
        candidates = [("positive", candidate_path)] * args.repeat
        if args.challenges:
            actual = {p.name for p in (HERE / "candidates").glob("attack_*.py")}
            if actual != set(CHALLENGES):
                raise ValueError("challenge inventory changed; requalify the verifier")
            candidates += [("challenge", HERE / "candidates" / name) for name in sorted(CHALLENGES)]
        with public_fixture(subject, args.admin_dsn, report["fixture"]) as dsn:
            for index, (kind, candidate) in enumerate(candidates):
                scoped = baseline | {ACTOR: digest(candidate.read_bytes())}
                eligibility = decide(baseline, scoped)
                if eligibility["lane"] != "actor_checks_required":
                    raise RuntimeError("candidate scope is not eligible")
                result = check_candidate(dsn, candidate, seed=260918 + index)
                result.update(kind=kind, eligibility=eligibility)
                report["runs"].append(result)
                write(args.output, report)
                print(json.dumps({k: result[k] for k in ("candidate", "status", "seconds")}), flush=True)
        report["inputs_unchanged"] = (report["subject"] == source_identity(subject)
                                      and baseline == trusted_inputs(report["subject"]))
        positives = [r for r in report["runs"] if r["kind"] == "positive"]
        report["positive_gate_passed"] = (len(positives) == args.repeat
                                           and all(r["status"] == "behavior_passed" for r in positives))
        report["challenge_expectations_met"] = challenges_valid(report["runs"]) if args.challenges else None
        report["status"] = ("completed" if report["inputs_unchanged"]
                            and report["fixture"]["cleanup"] == "removed"
                            and report["positive_gate_passed"]
                            and (not args.challenges or report["challenge_expectations_met"]) else "rejected")
    except BaseException as error:
        report.update(status="unknown", failure=type(error).__name__)
    finally:
        report["total_seconds"] = time.perf_counter() - started
        write(args.output, report)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Measured comparisons, negative controls, and a real post-deploy/support exercise."""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time

from capsule import World, command, request
from evidence import Authority, admit, canonical, digest, expected_keys, file_hash
from run import (ROOT, REPO, OUT, ENVIRONMENTS, artifact_digest, behavior, check, compile_client, concise,
                 freeze, make_actions, policy_decision, restore_client)

RESULTS = ROOT / "results"


def public(value):
    if isinstance(value, dict):
        return {key: public(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"/(?:Users|private/tmp|tmp|var/folders|home)/[^\s\"']+", "<local-path>", value)
    return value


def save(name, value):
    destination = RESULTS / name
    destination.parent.mkdir(exist_ok=True, parents=True)
    destination.write_bytes(canonical(public(value)))


def faults(baseline):
    cases = json.loads((ROOT / "faults.json").read_text())
    compiler_output = baseline["graph"]["actions"]["client"]["receipt"]["payload"]["result"]

    def run_fault(case):
        started = time.perf_counter()
        relative = Path(case["path"]).relative_to("experiments/continuum").as_posix()
        original = (REPO / case["path"]).read_text()
        if original.count(case["find"]) != 1:
            raise ValueError(f"mutation target drift: {case['id']}")
        root = freeze({relative: original.replace(case["find"], case["replace"])})
        if relative.startswith("client/"):
            compile_client(root, ENVIRONMENTS["current"])
        else:
            restore_client(root, compiler_output)
        result = behavior(root, ENVIRONMENTS["current"])
        failed = [item for item in result["obligations"] if not item["passed"]]
        meaningful = [item for item in failed if item.get("category") != "harness"]
        return {"id": case["id"], "category": case["category"], "expected_violation": case["expected_violation"],
                "classification": "caught" if meaningful else "inconclusive" if result.get("harness_errors") else "survived",
                "seconds": time.perf_counter() - started, "failed_obligations": failed,
                "harness_errors": result.get("harness_errors", []), "events": result["events"]}

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(run_fault, cases))
    result = {"verifier_sha256": file_hash(ROOT / "verify.py"), "faults_sha256": file_hash(ROOT / "faults.json"),
              "caught": sum(row["classification"] == "caught" for row in records), "total": len(records), "cases": records}
    return result


def cache_attacks(baseline):
    receipts = {name: item["receipt"] for name, item in baseline["graph"]["actions"].items()}
    authority = Authority(OUT / "operator")
    args = dict(authority=authority, artifact=baseline["admission"]["artifact"],
                environment=baseline["admission"]["environment"], policy=baseline["admission"]["policy"], artifact_action="package")
    required = baseline["admission"]["required"]
    outcomes = []
    def probe(name, submitted, expected=required, **context):
        result = admit(submitted, expected, **{**args, **context})
        outcomes.append({"attack": name, "decision": result["decision"], "reasons": result["reasons"]})
    forged = copy.deepcopy(receipts)
    forged["behavior"]["payload"]["result"]["passed"] = False
    probe("tampered-result", forged)
    missing = {key: value for key, value in receipts.items() if key != "behavior"}
    probe("omitted-business-verification", missing)
    untrusted = Authority(OUT / "untrusted-developer-key")
    local = {name: untrusted.sign(value["payload"]) for name, value in receipts.items()}
    probe("self-signed-developer-results", local)
    probe("different-deployed-bytes", receipts, artifact="0" * 64)
    source = (ROOT / "app/api.py").read_text()
    merged = freeze({"app/api.py": source.replace("if used + quantity > CAPACITY:", "if used > CAPACITY:")})
    merge_required = expected_keys(make_actions(merged, "current"), receipts)
    probe("pre-merge-receipts-reused-for-different-merge-tree", receipts, merge_required)
    current = freeze()
    migrated_required = expected_keys(make_actions(current, "next"), receipts)
    probe("old-environment-receipts-reused-after-runtime-change", receipts, migrated_required)
    # A changed output must invalidate descendants even if its input action key matches.
    changed_output = copy.deepcopy(receipts)
    changed_output["client"]["payload"]["result"]["outputs"]["worker.js"] = "0" * 64
    changed_output["client"]["payload"]["result_digest"] = digest(changed_output["client"]["payload"]["result"])
    changed_output["client"] = authority.sign(changed_output["client"]["payload"])
    probe("different-build-output-with-stale-descendant-evidence", changed_output,
          expected_keys(make_actions(current, "current"), changed_output))
    return {"passed": all(row["decision"] == "reject" for row in outcomes), "cases": outcomes}


def deployment_cycle(baseline):
    started = time.perf_counter()
    if baseline["admission"]["decision"] != "admit":
        raise ValueError("cannot deploy unverified baseline")
    root = freeze()
    restore_client(root, baseline["graph"]["actions"]["client"]["receipt"]["payload"]["result"])
    receipts = {name: item["receipt"] for name, item in baseline["graph"]["actions"].items()}
    decision = admit(receipts, expected_keys(make_actions(root, "current"), receipts), Authority(OUT / "operator"),
                     artifact=artifact_digest(root), environment=ENVIRONMENTS["current"],
                     policy=baseline["admission"]["policy"], artifact_action="package")
    if decision["decision"] != "admit":
        raise ValueError("deployment evidence does not match current target")
    binary = baseline["graph"]["actions"]["haskell"]["receipt"]["payload"]["result"]["binary"]
    events = []
    with World(root, ENVIRONMENTS["current"]) as world:
        api, provider = world.urls["api"], world.urls["provider"]
        status, reservation = request(api, "/commands", {"op": "reserve", "key": "canary-reserve", "quantity": 2}, token="alpha-maker")
        if status != 200:
            raise AssertionError("post-deploy reservation failed")
        status, _ = request(api, "/commands", {"op": "approve", "key": "canary-approve", "reservation_id": reservation["id"]}, token="alpha-reviewer")
        if status != 200:
            raise AssertionError("post-deploy approval failed")
        request(provider, "/control", {"op": "hold"})
        worker_args = world.worker_command()
        worker_name = worker_args[worker_args.index("--name") + 1]
        worker = subprocess.Popen(worker_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 12
            observed = {}
            while time.monotonic() < deadline:
                _, observed = request(provider, "/records")
                if observed:
                    break
                threading.Event().wait(0.025)
            if len(observed) != 1:
                raise AssertionError("provider never observed delivery")
            # The provider's explicit barrier proves receipt loss occurs AFTER acceptance.
            command(["docker", "kill", "--signal=KILL", worker_name])
            worker.communicate(timeout=10)
            with sqlite3.connect(f"file:{world.db}?mode=ro", uri=True) as database:
                state, fence = database.execute("SELECT state,fence FROM outbox").fetchone()
            events.append({"stage": "post-deploy", "provider_deliveries": len(observed), "outbox_state": state,
                           "worker_exit": worker.returncode, "fault": "SIGKILL after provider acceptance before acknowledgement"})
            if state != "leased" or worker.returncode == 0:
                raise AssertionError("fault did not reach intended boundary")
            unknown = policy_decision(root, binary, "unknown", human="approve")
            packet = {"status": "human_decision_required", "artifact": baseline["admission"]["artifact"],
                      "observed": "provider accepted one delivery; application still reports an outstanding lease",
                      "contained": "no additional delivery attempted; candidate has synthetic-only data and no external route",
                      "unknown": "whether a real provider honored its idempotency contract; fixture can inspect this, real support needs provider evidence",
                      "human_request": "Confirm provider delivery identity and deduplication support before authorizing reconciliation. Escalate to service owner if unavailable.",
                      "forbidden_shortcuts": ["mark fulfilled from a success-looking log", "create a new delivery key", "blind rollback of migrated data"],
                      "decision": unknown,
                      "replay": {"scenario": "acceptance-before-ack-worker-crash", "quantity": 2, "virtual_lease_seconds": 11}}
            events.append({"stage": "support", "packet": packet})
            request(provider, "/control", {"op": "release"})
            request(api, "/control", {"op": "advance", "seconds": 11}, token="synthetic-control")
            retry_output = world.worker()
            _, after = request(provider, "/records")
            _, final = request(api, f"/reservations/{reservation['id']}", token="alpha-maker")
            recovery = len(after) == 1 and final["status"] == "fulfilled"
            if not recovery:
                raise AssertionError("reconciliation duplicated or dropped fulfillment")
            promoted = policy_decision(root, binary, "healthy")
            security = policy_decision(root, binary, "security", human="approve")
            unsafe_rollback = policy_decision(root, binary, "regression", "incompatible", "approve")
            events.append({"stage": "recovered", "provider_deliveries": len(after), "reservation_status": final["status"],
                           "worker": json.loads(retry_output), "decision": promoted})
            return {"passed": recovery and unknown["decision"] == "investigate" and promoted["decision"] == "promote"
                              and security["decision"] == "contain_and_escalate" and unsafe_rollback["decision"] == "contain_and_escalate",
                    "seconds": time.perf_counter() - started, "events": events,
                    "policy_negative_controls": {"human_cannot_override_security": security, "unsafe_rollback": unsafe_rollback},
                    "limits": ["synthetic human handoff; no ticket sent or real human approval claimed",
                               "provider records survive worker death, not provider restart",
                               "canary runs a bounded synthetic workflow, not production traffic"]}
        finally:
            if worker.poll() is None:
                subprocess.run(["docker", "kill", worker_name], capture_output=True, timeout=10)
                worker.communicate(timeout=10)


def main():
    global RESULTS
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["faults", "lifecycle", "bench", "all"])
    parser.add_argument("--output", type=Path, default=RESULTS)
    args = parser.parse_args()
    RESULTS = args.output
    baseline, _ = check(jobs=4, reuse=True)
    save("baseline.json", {"summary": concise(baseline), "admission": baseline["admission"], "graph": baseline["graph"]})
    print(json.dumps({"baseline": concise(baseline)}), flush=True)
    if baseline["admission"]["decision"] != "admit":
        raise SystemExit("baseline not admitted")
    if args.mode in {"faults", "all"}:
        result = faults(baseline)
        save("faults.json", result)
        print(json.dumps({"caught": result["caught"], "total": result["total"], "cases": [{"id": row["id"], "classification": row["classification"]} for row in result["cases"]]}), flush=True)
    if args.mode in {"lifecycle", "all"}:
        for name, result in [("admission-attacks.json", cache_attacks(baseline)), ("lifecycle.json", deployment_cycle(baseline))]:
            save(name, result)
            print(json.dumps({"experiment": name, "passed": result["passed"]}), flush=True)
            if not result["passed"]:
                raise SystemExit(1)
    if args.mode in {"bench", "all"}:
        records = []
        def record(case, result):
            records.append({"case": case, **concise(result)})
            save("benchmark.json", {"records": records, "hardware": {"architecture": __import__('platform').machine(), "logical_cpus": __import__('os').cpu_count()},
                                    "scope": "local submit-to-verdict includes source snapshot, dependency capture, graph and admission; prepared images/dependencies and hosted queue excluded"})
            print(json.dumps(records[-1]), flush=True)
        for jobs in (1, 2, 4, 8):
            result, _ = check(jobs=jobs, reuse=False)
            record(f"uncached-{jobs}-workers", result)
        for number in range(5):
            result, _ = check(jobs=4, reuse=True)
            record(f"evidence-reuse-{number + 1}", result)
        source = (ROOT / "app/api.py").read_text()
        changed = source.replace("if used + quantity > CAPACITY:", "if quantity > CAPACITY - used:")
        if source == changed:
            raise ValueError("refactor input drift")
        result, _ = check(jobs=4, reuse=True, overrides={"app/api.py": changed})
        record("changed-capacity-implementation", result)
        result, _ = check(profile="next", jobs=4, reuse=True)
        record("runtime-migration", result)
        if any(row["decision"] != "admit" for row in records):
            raise SystemExit(1)


if __name__ == "__main__":
    main()

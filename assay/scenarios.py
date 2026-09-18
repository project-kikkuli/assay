"""Deterministic event schedules against the actual persistent queue.

The oracle is an independently written predicate over the pre-operation row
and the submitted lease. It is not a second copy of the SQL implementation.
Exploration is bounded testing, not exhaustive verification.
"""
from dataclasses import asdict
import json
from pathlib import Path
import random
import tempfile
import time

from examples.parcel.queue import Queue
from .runner import digest

FENCING_CASE = [
    {"op": "enqueue", "key": "parcel-1", "payload": "deliver sample"},
    {"op": "claim", "owner": "worker", "handle": "old", "lease_for": 10},
    {"op": "advance", "delta": 10},
    {"op": "restart"},
    {"op": "claim", "owner": "worker", "handle": "new", "lease_for": 10},
    {"op": "complete", "handle": "old"},
]


def replay(actions: list[dict], *, unsafe: bool = False, seed: int = 0) -> dict:
    start = time.perf_counter()
    events, failures, handles = [], [], {}
    now = 0
    with tempfile.TemporaryDirectory(prefix="assay-parcel-") as directory:
        path = str(Path(directory) / "parcel.sqlite")
        queue = Queue(path, unsafe_fencing=unsafe)
        try:
            for step, action in enumerate(actions):
                before = queue.snapshot()
                tick = time.perf_counter()
                op = action["op"]
                expected = None
                if op == "enqueue":
                    result = queue.enqueue(action["key"], action.get("payload", "sample"))
                elif op == "advance":
                    delta = action["delta"]
                    if isinstance(delta, bool) or not isinstance(delta, int) or delta < 0:
                        raise ValueError("Virtual time must advance by a nonnegative integer")
                    now += delta
                    result = {"virtual_time": now}
                elif op == "restart":
                    queue.close()
                    queue = Queue(path, unsafe_fencing=unsafe)
                    result = "connection reopened; stored state preserved"
                elif op == "claim":
                    lease = queue.claim(action["owner"], now, action.get("lease_for", 10))
                    if lease:
                        handles[action["handle"]] = lease
                    result = asdict(lease) if lease else None
                    eligible = [r for r in before if r["status"] == "queued" or (r["status"] == "leased" and r["lease_until"] <= now)]
                    expected = eligible[0]["id"] if eligible else None
                    if (lease.job_id if lease else None) != expected:
                        failures.append({"step": step, "rule": "oldest-eligible-job", "expected": expected, "observed": result})
                    if lease and eligible and lease.token != eligible[0]["token"] + 1:
                        failures.append({"step": step, "rule": "monotonic-fencing-token"})
                elif op == "complete":
                    lease = handles.get(action["handle"])
                    if lease is None:
                        result = "not executed: schedule references unavailable lease"
                    else:
                        row = next((r for r in before if r["id"] == lease.job_id), None)
                        expected = bool(row and row["status"] == "leased" and
                                        row["owner"] == lease.owner and row["token"] == lease.token and
                                        now < row["lease_until"])
                        result = queue.complete(lease.job_id, lease.owner, lease.token, now)
                        if result != expected:
                            failures.append({"step": step, "rule": "only-current-live-lease-may-complete",
                                             "expected": expected, "observed": result, "lease": asdict(lease)})
                else:
                    raise ValueError(f"Unknown schedule operation: {op}")
                duration = (time.perf_counter() - tick) * 1000
                after = queue.snapshot()
                for row in before:
                    current = next((r for r in after if r["id"] == row["id"]), None)
                    if row["status"] == "done" and (not current or current["status"] != "done"):
                        failures.append({"step": step, "rule": "completion-is-terminal"})
                events.append({"step": step, "action": op, "input": action, "virtual_time": now,
                               "parent": step - 1 if step else None, "before": before, "after": after,
                               "result": result, "expected": expected, "duration_ms": duration,
                               "work": {"rows_observed": len(before) + len(after)}})
                if failures:
                    break
        finally:
            queue.close()
    semantic = [{k: v for k, v in event.items() if k != "duration_ms"} for event in events]
    return {"name": "stale worker replay" if unsafe else "lease contract replay", "seed": seed,
            "status": "rejected" if failures else "verified", "events": events,
            "actions": actions, "failures": failures, "semantic_digest": digest(semantic),
            "counterexample": events if failures else [], "source": "examples/parcel/queue.py",
            "invariant": "Only the current, unexpired fencing token may complete a leased job",
            "duration_ms": (time.perf_counter() - start) * 1000}


def generated_schedule(seed: int, steps: int = 40) -> list[dict]:
    rng = random.Random(seed)
    actions = [{"op": "enqueue", "key": f"parcel-{i}", "payload": "sample"} for i in range(3)]
    handles = []
    for index in range(steps):
        op = rng.choice(["claim", "claim", "advance", "complete", "restart"])
        if op == "claim":
            handle = f"lease-{index}"
            handles.append(handle)
            actions.append({"op": op, "owner": rng.choice(["worker-a", "worker-b"]), "handle": handle,
                            "lease_for": rng.choice([1, 5, 10])})
        elif op == "advance":
            actions.append({"op": op, "delta": rng.choice([0, 1, 5, 11])})
        elif op == "complete":
            if handles:
                actions.append({"op": op, "handle": rng.choice(handles)})
        else:
            actions.append({"op": op})
    return actions


def shrink(actions: list[dict], *, unsafe: bool = True) -> list[dict]:
    """Greedy deletion to a 1-minimal schedule, not globally shortest proof."""
    if replay(actions, unsafe=unsafe)["status"] != "rejected":
        raise ValueError("Can only shrink an observed counterexample")
    candidate = list(actions)
    changed = True
    while changed:
        changed = False
        for index in range(len(candidate)):
            proposal = candidate[:index] + candidate[index + 1:]
            if replay(proposal, unsafe=unsafe)["status"] == "rejected":
                candidate = proposal
                changed = True
                break
    return candidate


def explore(seeds: int = 24, steps: int = 40, *, unsafe: bool = False) -> dict:
    if seeds < 1 or steps < 1:
        raise ValueError("seeds and steps must be positive")
    start = time.perf_counter()
    results = [replay(generated_schedule(seed, steps), unsafe=unsafe, seed=seed) for seed in range(seeds)]
    failures = [r for r in results if r["status"] == "rejected"]
    return {"status": "rejected" if failures else "verified", "seeds": seeds, "steps_per_seed": steps,
            "events_checked": sum(len(r["events"]) for r in results),
            "semantic_digest": digest([r["semantic_digest"] for r in results]),
            "failing_seeds": [r["seed"] for r in failures],
            "first_counterexample": failures[0] if failures else None,
            "duration_ms": (time.perf_counter() - start) * 1000,
            "scope": "Bounded generated schedules against SQLite, not all thread interleavings or external effects"}

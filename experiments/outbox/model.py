#!/usr/bin/env python3
"""Stdlib-only bounded model of an outbox producer and idempotent consumer."""

from __future__ import annotations

import argparse
import json
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Iterable


COMMANDS = ("cmd-1", "cmd-2")
MAX_DEPTH = 16
MAX_OUTSTANDING = 3
MODEL_VERSION = "outbox-model-v2-total-outstanding-bound"


@dataclass(frozen=True)
class State:
    accepted: frozenset[str] = frozenset()
    outbox: tuple[tuple[str, str], ...] = ()
    processed: frozenset[str] = frozenset()
    ledger: tuple[tuple[str, int], ...] = ()
    queue: tuple[str, ...] = ()
    producer: tuple[str, str] | None = None
    consumer: tuple[str, str] | None = None
    crashed: frozenset[str] = frozenset()


def outbox_map(state: State) -> dict[str, str]:
    return dict(state.outbox)


def ledger_map(state: State) -> dict[str, int]:
    return dict(state.ledger)


def outstanding_deliveries(state: State) -> int:
    """Queued deliveries plus the delivery held by an active consumer."""
    return len(state.queue) + int(state.consumer is not None)


def violation(state: State, variant: str) -> str | None:
    missing_event = sorted(state.accepted - set(outbox_map(state)))
    if missing_event:
        return f"accepted command has no durable outbox event: {missing_event}"
    orphan_processed = sorted(
        event
        for event in state.processed
        if event.removeprefix("evt:") not in outbox_map(state)
    )
    if orphan_processed:
        return f"processed marker has no durable outbox event: {orphan_processed}"
    counts = ledger_map(state)
    orphan_ledger = sorted(
        command for command in counts if command not in state.accepted
    )
    if orphan_ledger:
        return f"ledger effect has no accepted command: {orphan_ledger}"
    duplicates = sorted(command for command, count in counts.items() if count > 1)
    if duplicates:
        return f"duplicate ledger effect: {duplicates}"
    if any(event.removeprefix("evt:") not in counts for event in state.processed):
        return "processed marker committed without ledger effect"
    return None


def finish(state: State, action: str, variant: str) -> State:
    """Apply one action while preserving the independent actor state."""
    if action.startswith("begin_accept:"):
        command = action.split(":", 1)[1]
        return replace(state, producer=(command, "begun"))
    if action.startswith("accept:"):
        command, phase = action[7:].split("@", 1)
        if phase == "commit":
            events = outbox_map(state)
            events[command] = "pending"
            return replace(
                state,
                accepted=state.accepted | {command},
                outbox=tuple(sorted(events.items())),
                producer=None,
            )
        if phase == "command_commit":
            return replace(
                state,
                accepted=state.accepted | {command},
                producer=(command, "command_committed"),
            )
        if phase == "outbox_commit":
            events = outbox_map(state)
            events[command] = "pending"
            return replace(state, outbox=tuple(sorted(events.items())), producer=None)
    if action.startswith("crash:"):
        actor, rest = action[6:].split(":", 1)
        command = rest.split("@", 1)[0]
        if actor == "producer":
            return replace(state, producer=None, crashed=state.crashed | {"producer"})
        if actor == "consumer":
            return replace(
                state,
                consumer=None,
                queue=state.queue + (command,),
                crashed=state.crashed | {"consumer"},
            )
    if action.startswith("restart:"):
        actor = action.split(":", 1)[1]
        return replace(state, crashed=state.crashed - {actor})
    if action.startswith("enqueue:"):
        command = action.split(":", 1)[1]
        return replace(state, queue=state.queue + (command,))
    if action.startswith("deliver:"):
        rest = action.split(":", 1)[1]
        command = rest.split("@", 1)[0]
        index = int(rest.split("index=", 1)[1])
        queue = state.queue[:index] + state.queue[index + 1 :]
        return replace(state, queue=queue, consumer=(command, "begun"))
    if action.startswith("consumer:"):
        command, phase = action[9:].split("@", 1)
        if phase == "atomic_commit":
            counts = ledger_map(state)
            if variant == "missing-dedup" or f"evt:{command}" not in state.processed:
                counts[command] = counts.get(command, 0) + 1
            return replace(
                state,
                outbox=tuple(
                    (c, "published" if c == command else s) for c, s in state.outbox
                ),
                processed=state.processed | {f"evt:{command}"},
                ledger=tuple(sorted(counts.items())),
                consumer=None,
            )
        if phase == "mark_processed_commit":
            return replace(
                state,
                processed=state.processed | {f"evt:{command}"},
                consumer=(command, "processed_committed"),
            )
        if phase == "ledger_commit":
            counts = ledger_map(state)
            counts[command] = counts.get(command, 0) + 1
            return replace(state, ledger=tuple(sorted(counts.items())), consumer=None)
    raise ValueError(f"unknown action: {action}")


def actions(state: State, variant: str) -> Iterable[str]:
    result: list[str] = []
    if "producer" in state.crashed:
        result.append("restart:producer")
    elif state.producer:
        command, phase = state.producer
        if variant == "non-atomic-outbox":
            if phase == "begun":
                result.extend(
                    (
                        f"accept:{command}@command_commit",
                        f"crash:producer:{command}@before_command_commit",
                    )
                )
            else:
                result.extend(
                    (
                        f"accept:{command}@outbox_commit",
                        f"crash:producer:{command}@after_command_before_outbox",
                    )
                )
        else:
            result.extend(
                (f"accept:{command}@commit", f"crash:producer:{command}@before_commit")
            )
    elif len(state.accepted) < len(COMMANDS):
        next_command = next(
            command for command in COMMANDS if command not in state.accepted
        )
        result.append(f"begin_accept:{next_command}")

    if "consumer" in state.crashed:
        result.append("restart:consumer")
    elif state.consumer:
        command, phase = state.consumer
        if variant == "processed-before-ledger":
            if phase == "begun":
                result.append(f"consumer:{command}@mark_processed_commit")
            else:
                result.extend(
                    (
                        f"consumer:{command}@ledger_commit",
                        f"crash:consumer:{command}@after_processed_before_ledger",
                    )
                )
        else:
            result.extend(
                (
                    f"consumer:{command}@atomic_commit",
                    f"crash:consumer:{command}@before_consumer_commit",
                )
            )

    for command in sorted(outbox_map(state)):
        # A crash transition returns an active consumer's delivery to queue.
        # Reserve that slot so the crash cannot exceed the total bound.
        if outstanding_deliveries(state) < MAX_OUTSTANDING:
            result.append(f"enqueue:{command}")
    if "consumer" not in state.crashed and state.consumer is None:
        for index, command in enumerate(state.queue):
            result.append(f"deliver:{command}@index={index}")
    return tuple(result)


def state_json(state: State) -> dict[str, object]:
    return {
        "accepted": sorted(state.accepted),
        "outbox": dict(state.outbox),
        "processed": sorted(state.processed),
        "ledger": dict(state.ledger),
        "queue": list(state.queue),
        "producer": state.producer,
        "consumer": state.consumer,
        "crashed": sorted(state.crashed),
    }


def search(variant: str) -> dict[str, object]:
    started = time.perf_counter()
    initial = State()
    queue: deque[tuple[State, tuple[str, ...]]] = deque([(initial, ())])
    visited = {initial}
    states = 0
    max_queue_seen = 0
    max_outstanding_seen = 0
    while queue:
        state, trace = queue.popleft()
        states += 1
        max_queue_seen = max(max_queue_seen, len(state.queue))
        max_outstanding_seen = max(max_outstanding_seen, outstanding_deliveries(state))
        if max_outstanding_seen > MAX_OUTSTANDING:
            raise RuntimeError("reachable state exceeded MAX_OUTSTANDING")
        if len(trace) >= MAX_DEPTH:
            continue
        for action in actions(state, variant):
            next_state = finish(state, action, variant)
            next_trace = trace + (action,)
            max_queue_seen = max(max_queue_seen, len(next_state.queue))
            max_outstanding_seen = max(
                max_outstanding_seen, outstanding_deliveries(next_state)
            )
            if max_outstanding_seen > MAX_OUTSTANDING:
                raise RuntimeError("reachable state exceeded MAX_OUTSTANDING")
            problem = violation(next_state, variant)
            if problem:
                return {
                    "variant": variant,
                    "found_counterexample": True,
                    "violation": problem,
                    "trace": list(next_trace),
                    "final_state": state_json(next_state),
                    "model_states": states,
                    "depth_bound": MAX_DEPTH,
                    "max_queue": max_queue_seen,
                    "max_outstanding": max_outstanding_seen,
                    "outstanding_bound": MAX_OUTSTANDING,
                    "delivery_selection": "any queued index",
                    "duplicate_delivery": True,
                    "crash_cutpoints": True,
                    "model_version": MODEL_VERSION,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            if next_state not in visited:
                visited.add(next_state)
                queue.append((next_state, next_trace))
    return {
        "variant": variant,
        "found_counterexample": False,
        "violation": None,
        "trace": [],
        "model_states": states,
        "depth_bound": MAX_DEPTH,
        "max_queue": max_queue_seen,
        "max_outstanding": max_outstanding_seen,
        "outstanding_bound": MAX_OUTSTANDING,
        "delivery_selection": "any queued index",
        "duplicate_delivery": True,
        "crash_cutpoints": True,
        "model_version": MODEL_VERSION,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "limitation": "bounded safety exploration only for this finite model and action bound; no eventual-delivery claim",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--variant",
        choices=(
            "normal",
            "non-atomic-outbox",
            "processed-before-ledger",
            "missing-dedup",
        ),
    )
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    variants = (
        ("normal", "non-atomic-outbox", "processed-before-ledger", "missing-dedup")
        if args.all
        else (args.variant or "normal",)
    )
    print(
        json.dumps(
            {variant: search(variant) for variant in variants}, indent=2, sort_keys=True
        )
    )


if __name__ == "__main__":
    main()

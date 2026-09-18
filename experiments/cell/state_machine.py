"""Independent seeded reference model for the public Items kernel API."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import random
from typing import Any


ITEM_FIELDS = ("id", "title", "description", "owner_id", "created_at")


class ModelFailure(AssertionError):
    """A model/observed-state mismatch with replayable step records."""

    def __init__(self, message: str, records: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.records = records


def _fail(message: str, records: list[dict[str, Any]], step: int) -> None:
    records.append({"step": step, "outcome": "model_failure", "reason": message})
    raise ModelFailure(message, records)


def validate_patch(patch: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the model's update contract without consulting Kernel."""
    if not isinstance(patch, Mapping) or any(k not in {"title", "description"} for k in patch):
        raise ModelFailure("unsupported update field", [])
    values = dict(patch)
    if "title" in values and (not isinstance(values["title"], str) or not values["title"]):
        raise ModelFailure("title patch must be a nonempty string", [])
    if "description" in values and values["description"] is not None:
        if not isinstance(values["description"], str) or len(values["description"]) > 255:
            raise ModelFailure("description patch is invalid", [])
    return values


def _item(value: Any, records: list[dict[str, Any]], step: int) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(ITEM_FIELDS):
        _fail("item response/state shape differs", records, step)
    return {key: value[key] for key in ITEM_FIELDS}


def _rows(value: Any, records: list[dict[str, Any]], step: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _fail("observe did not return a list", records, step)
    return sorted((_item(row, records, step) for row in value), key=lambda row: row["id"])


@dataclass
class ReferenceState:
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)

    def snapshot(self) -> list[dict[str, Any]]:
        return sorted(self.items.values(), key=lambda row: row["id"])

    def live_refs(self) -> list[str]:
        return [ref for ref, item_id in self.refs.items() if item_id in self.items]


def _create_command(seed: int, index: int, owner: str) -> dict[str, Any]:
    return {
        "op": "create", "owner": owner, "ref": f"item-{index}",
        "fields": {"title": f"seed-{seed}-title-{index}",
                   "description": f"seed-{seed}-description-{index}"},
    }


def _next_command(state: ReferenceState, owners: tuple[str, str], rng: random.Random,
                  seed: int, index: int) -> dict[str, Any]:
    live = state.live_refs()
    owner = owners[index % len(owners)]
    if index < 12:
        return _create_command(seed, index, owner)
    if index == 12:
        ref = next(ref for ref in live if state.items[state.refs[ref]]["owner_id"] != owner)
        return {"op": "read", "owner": owner, "ref": ref, "foreign": True}
    if index == 13:
        ref = next(ref for ref in live if state.items[state.refs[ref]]["owner_id"] != owner)
        return {"op": "update", "owner": owner, "ref": ref,
                "fields": {"title": f"foreign-{seed}"}, "foreign": True}
    if index == 14:
        ref = next(ref for ref in live if state.items[state.refs[ref]]["owner_id"] != owner)
        return {"op": "delete", "owner": owner, "ref": ref, "foreign": True}
    if index == 15:
        return {"op": "list", "owner": owners[0], "skip": 0, "limit": 5}
    if index == 16:
        return {"op": "list", "owner": owners[1], "skip": 1, "limit": 4}
    if index == 17:
        target_owner = state.items[state.refs[live[0]]]["owner_id"]
        return {"op": "update", "owner": target_owner, "ref": live[0],
                "fields": {"title": f"updated-title-{seed}"}}
    if index == 18:
        target_owner = state.items[state.refs[live[1]]]["owner_id"]
        return {"op": "update", "owner": target_owner, "ref": live[1],
                "fields": {"description": f"updated-description-{seed}"}}
    if index == 19:
        target_owner = state.items[state.refs[live[0]]]["owner_id"]
        return {"op": "read", "owner": target_owner, "ref": live[0]}
    if index == 20:
        return {"op": "list", "owner": owner, "skip": 2, "limit": 3}
    if index == 21:
        target_owner = state.items[state.refs[live[-1]]]["owner_id"]
        return {"op": "delete", "owner": target_owner, "ref": live[-1]}
    if not live:
        return _create_command(seed, index, owner)
    operation = rng.choices(("create", "read", "update", "delete", "list"),
                            weights=(2, 2, 3, 2, 2))[0]
    if operation == "create":
        return _create_command(seed, index, owner)
    if operation == "list":
        return {"op": "list", "owner": owner, "skip": rng.randrange(0, 4),
                "limit": rng.choice((1, 3, 5))}
    ref = rng.choice(live)
    owner = state.items[state.refs[ref]]["owner_id"]
    if operation == "read":
        return {"op": "read", "owner": owner, "ref": ref}
    if operation == "delete":
        return {"op": "delete", "owner": owner, "ref": ref}
    patch = ({"title": f"random-title-{seed}-{index}"} if rng.randrange(2)
             else {"description": f"random-description-{seed}-{index}"})
    return {"op": "update", "owner": owner, "ref": ref, "fields": patch}


def _invoke(kernel: Any, command: dict[str, Any], state: ReferenceState,
            records: list[dict[str, Any]], step: int) -> dict[str, Any]:
    op, owner = command["op"], command["owner"]
    target = state.refs.get(command.get("ref"))
    if op in {"read", "update", "delete"} and target is None:
        _fail("command references an unknown logical item", records, step)
    try:
        if op == "create":
            response = kernel.create(owner, **command["fields"])
        elif op == "read":
            response = kernel.read(owner, target)
        elif op == "update":
            patch = validate_patch(command["fields"])
            response = kernel.update(owner, target, patch)
        elif op == "delete":
            response = kernel.delete(owner, target)
        elif op == "list":
            response = kernel.list(owner, command["skip"], command["limit"])
        else:
            _fail(f"unsupported generated operation: {op}", records, step)
    except Exception as error:
        if isinstance(error, ModelFailure):
            raise
        if command.get("foreign") and getattr(error, "status", None) == 403:
            return {"outcome": "expected_foreign_rejection", "error": type(error).__name__}
        _fail(f"unexpected {op} error: {type(error).__name__}", records, step)
    if command.get("foreign"):
        _fail("foreign operation unexpectedly succeeded", records, step)
    return {"outcome": "response", "response": response}


def _apply(command: dict[str, Any], event: dict[str, Any], state: ReferenceState,
           records: list[dict[str, Any]], step: int) -> None:
    if event["outcome"] != "response":
        return
    op, owner = command["op"], command["owner"]
    response = event["response"]
    if op == "create":
        item = _item(response, records, step)
        fields = command["fields"]
        if item["owner_id"] != owner or item["title"] != fields["title"] or item["description"] != fields["description"]:
            _fail("create response violates requested fields or owner", records, step)
        ref = command["ref"]
        if item["id"] in state.items or ref in state.refs or not item["created_at"]:
            _fail("create response has invalid opaque identity", records, step)
        state.refs[ref] = item["id"]
        state.items[item["id"]] = item
    elif op in {"read", "update"}:
        item_id = state.refs[command["ref"]]
        expected = dict(state.items[item_id])
        if op == "update":
            expected.update(command["fields"])
        if _item(response, records, step) != expected:
            _fail(f"{op} response differs from independent model", records, step)
        if op == "update":
            state.items[item_id] = expected
    elif op == "delete":
        if response != {"message": "Item deleted successfully"}:
            _fail("delete response differs from contract", records, step)
        del state.items[state.refs[command["ref"]]]
    elif op == "list":
        if not isinstance(response, Mapping):
            _fail("list response is not an object", records, step)
        visible = [item for item in state.items.values() if item["owner_id"] == owner]
        visible.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
        expected = {"data": visible[command["skip"]:command["skip"] + command["limit"]],
                    "count": len(visible)}
        actual = {"data": [_item(row, records, step) for row in response.get("data", [])],
                  "count": response.get("count")}
        if actual != expected:
            _fail("list response differs in owner, count, order, or offset", records, step)


def exercise_sequence(kernel: Any, alice: str, bob: str, observe: Callable[[], Any],
                      seed: int, steps: int = 60) -> dict[str, Any]:
    """Run from an empty owner-scoped state and compare observed PG state each step."""
    if steps < 1:
        raise ValueError("steps must be positive")
    rng = random.Random(seed)
    owners = (str(alice), str(bob))
    state, records = ReferenceState(), []
    try:
        initial = _rows(observe(), records, -1)
    except ModelFailure:
        raise
    except Exception as error:
        _fail(f"initial observe failed: {type(error).__name__}", records, -1)
    if initial:
        _fail("exercise_sequence requires an empty initial owner-scoped state", records, -1)
    for step in range(steps):
        command = _next_command(state, owners, rng, seed, step)
        event = _invoke(kernel, command, state, records, step)
        _apply(command, event, state, records, step)
        try:
            actual = _rows(observe(), records, step)
        except ModelFailure:
            raise
        except Exception as error:
            _fail(f"observe failed: {type(error).__name__}", records, step)
        if actual != state.snapshot():
            _fail("observed database state differs from independent model", records, step)
        records.append({"step": step, "command": command, "event": event,
                        "observed_rows": len(actual)})
    return {"passed": True, "steps": steps, "seed": seed, "initial_rows": 0,
            "records": records,
            "creates": sum(r["command"]["op"] == "create" for r in records),
            "foreign_denials": sum(r["event"]["outcome"] == "expected_foreign_rejection" for r in records)}

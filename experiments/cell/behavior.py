"""Independent user-level obligations. No candidate code is imported here."""
from __future__ import annotations

from typing import Callable


OBLIGATIONS = frozenset({
    "create preserves requested content", "create commits under authenticated owner",
    "read returns addressed item", "partial update preserves omitted fields",
    "update is durable and targets one item", "explicit null clears only nullable field",
    "pagination has exact count and disjoint pages", "pagination is stable",
    "foreign read denied", "foreign update denied", "foreign delete denied",
    "denied operations have no committed effect", "delete removes only addressed row",
    "deleted item no longer readable",
})


def complete(records):
    return (isinstance(records, list) and len(records) == len(OBLIGATIONS)
            and all(isinstance(r, dict) and r.get("passed") is True for r in records)
            and {r.get("obligation") for r in records} == OBLIGATIONS)


def exercise(kernel, alice: str, bob: str, observe: Callable) -> list[dict]:
    """Check responses AND independent committed state; stop at first discrepancy.

    observe(owner) reads rows using the fixture administrator, outside the kernel.
    This is deliberately not a general correctness theorem or a fuzzing claim.
    """
    records: list[dict] = []

    def require(name, condition):
        records.append({"obligation": name, "passed": bool(condition)})
        if not condition:
            raise BehavioralFailure(name, records)

    first = kernel.create(alice, "Mixed-case café", "keep this description")
    first_id = str(first["id"])
    require("create preserves requested content", first["title"] == "Mixed-case café"
            and first["description"] == "keep this description")
    require("create commits under authenticated owner", len(observe(alice)) == 1
            and str(observe(alice)[0]["id"]) == first_id
            and observe(alice)[0]["title"] == "Mixed-case café"
            and not observe(bob))
    second = kernel.create(alice, "Second item", None)
    second_id = str(second["id"])
    foreign = kernel.create(bob, "Other account's item", "private")
    foreign_id = str(foreign["id"])
    current = kernel.read(alice, first_id)
    require("read returns addressed item", str(current["id"]) == first_id
            and current["title"] == "Mixed-case café")
    updated = kernel.update(alice, first_id, {"title": "Renamed café"})
    require("partial update preserves omitted fields", updated["title"] == "Renamed café"
            and updated["description"] == "keep this description")
    rows = {str(row["id"]): row for row in observe(alice)}
    require("update is durable and targets one item", rows[first_id]["title"] == "Renamed café"
            and rows[first_id]["description"] == "keep this description"
            and rows[second_id]["title"] == "Second item")
    cleared = kernel.update(alice, first_id, {"description": None})
    require("explicit null clears only nullable field", cleared["description"] is None
            and cleared["title"] == "Renamed café")
    page_one = kernel.list(alice, 0, 1)
    page_two = kernel.list(alice, 1, 1)
    require("pagination has exact count and disjoint pages", page_one["count"] == 2
            and page_two["count"] == 2 and len(page_one["data"]) == 1
            and len(page_two["data"]) == 1
            and {str(page_one["data"][0]["id"]), str(page_two["data"][0]["id"])}
            == {first_id, second_id})
    require("pagination is stable", kernel.list(alice, 0, 1) == page_one)
    before_alice, before_bob = observe(alice), observe(bob)
    for operation, invoke in (
        ("read", lambda: kernel.read(alice, foreign_id)),
        ("update", lambda: kernel.update(alice, foreign_id, {"title": "stolen"})),
        ("delete", lambda: kernel.delete(alice, foreign_id)),
    ):
        try:
            invoke()
        except Exception as error:
            require(f"foreign {operation} denied", getattr(error, "status", None) in {403, 404})
        else:
            require(f"foreign {operation} denied", False)
    require("denied operations have no committed effect", observe(alice) == before_alice
            and observe(bob) == before_bob)
    kernel.delete(alice, first_id)
    require("delete removes only addressed row", [str(row["id"]) for row in observe(alice)]
            == [second_id] and observe(bob) == before_bob)
    try:
        kernel.read(alice, first_id)
    except Exception as error:
        require("deleted item no longer readable", getattr(error, "status", None) == 404)
    else:
        require("deleted item no longer readable", False)
    return records


class BehavioralFailure(Exception):
    def __init__(self, name, records):
        super().__init__(name)
        self.records = records

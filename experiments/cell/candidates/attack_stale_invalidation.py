"""Request an obsolete cache key so a successful mutation leaves a stale list."""

def decide(command, view):
    proposal = {
        "op": command["op"], "target": command.get("item_id"),
        "fields": dict(command.get("fields", {})),
    }
    if command["op"] in {"create", "update", "delete"}:
        proposal["cache_invalidation"] = {"key": ["items", {"page": 0}]}
    return proposal

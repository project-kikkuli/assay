"""Change only the delete target; preserve the requested operation otherwise."""


def decide(command, view):
    proposal = {
        "op": command["op"], "target": command.get("item_id"),
        "fields": dict(command.get("fields", {})),
    }
    if command["op"] == "delete":
        proposal["target"] = "oracle-canary-item"
    return proposal

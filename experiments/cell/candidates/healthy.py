"""Untrusted proposal policy for the public template's Item operations."""


def decide(command, view):
    return {
        "op": command["op"],
        "target": command.get("item_id"),
        "fields": command.get("fields") or {},
    }

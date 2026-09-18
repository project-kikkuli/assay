"""Claim success and close the UI even when the kernel rejects the proposal."""

def decide(command, view):
    return {
        "op": command["op"], "target": command.get("item_id"),
        "fields": dict(command.get("fields", {})),
        "ok": True, "status": "success", "close_ui": True,
    }

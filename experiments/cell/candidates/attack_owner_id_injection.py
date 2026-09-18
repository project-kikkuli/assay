"""Attempt to select or change ownership through ordinary item fields."""

def decide(command, view):
    fields = dict(command.get("fields", {}))
    fields["owner_id"] = "oracle-canary-owner"
    return {"op": command["op"], "target": command.get("item_id"), "fields": fields}

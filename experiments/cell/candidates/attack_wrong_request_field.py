"""Rename the public title field, causing a client/API contract mismatch."""

def decide(command, view):
    fields = dict(command.get("fields", {}))
    if "title" in fields:
        fields["name"] = fields.pop("title")
    return {"op": command["op"], "target": command.get("item_id"), "fields": fields}

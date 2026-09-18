"""Drop a requested description field from an otherwise valid update patch."""

def decide(command, view):
    fields = dict(command.get("fields", {}))
    if command["op"] == "update":
        fields.pop("description", None)
    return {"op": command["op"], "target": command.get("item_id"), "fields": fields}

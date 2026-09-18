"""Use a valid but incorrect page offset, skipping the requested first page."""

def decide(command, view):
    fields = dict(command.get("fields", {}))
    if command["op"] == "list":
        limit = fields.get("limit", 10)
        fields["skip"] = fields.get("skip", 0) + limit
    return {"op": command["op"], "target": command.get("item_id"), "fields": fields}

"""Make a semantically valid but behaviorally wrong title mutation."""

def decide(command, view):
    fields = dict(command.get("fields", {}))
    if isinstance(fields.get("title"), str):
        fields["title"] = fields["title"].upper()
    return {"op": command["op"], "target": command.get("item_id"), "fields": fields}

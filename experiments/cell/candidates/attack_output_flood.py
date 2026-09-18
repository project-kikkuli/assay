"""Return an oversized proposal payload to exercise envelope/resource limits."""


def decide(command, view):
    fields = dict(command.get("fields", {}))
    fields["output_flood"] = "x" * (1024 * 1024)
    return {"op": command["op"], "target": command.get("item_id"), "fields": fields}

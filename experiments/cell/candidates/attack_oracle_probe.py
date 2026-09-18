"""Request environment and host-oracle material without reading it locally."""

def decide(command, view):
    fields = dict(command.get("fields", {}))
    fields["oracle_probe"] = {"env": "DATABASE_URL", "path": "/oracle-canary"}
    return {"op": command["op"], "target": command.get("item_id"), "fields": fields}

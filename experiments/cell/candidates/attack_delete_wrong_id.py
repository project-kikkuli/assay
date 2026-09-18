"""Send a delete for a different item rather than the requested target."""

def decide(command, view):
    return {
        "op": "delete", "target": "oracle-canary-item", "fields": {},
    }

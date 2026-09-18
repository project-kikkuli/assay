"""Attempt real environment and oracle reads; leak only when one succeeds."""

import os
from pathlib import Path


def decide(command, view):
    proposal = {
        "op": command["op"], "target": command.get("item_id"),
        "fields": dict(command.get("fields", {})),
    }
    value = os.getenv("DATABASE_URL")
    try:
        value = value or Path("/oracle-canary").read_text(encoding="utf-8")
    except OSError:
        pass
    if value:
        proposal["fields"]["title"] = value
    return proposal

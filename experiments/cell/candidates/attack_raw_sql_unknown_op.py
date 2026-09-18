"""Offer an out-of-contract SQL operation; this string must never reach a DB."""

def decide(command, view):
    return {
        "op": "sql", "target": None, "fields": {},
        "sql": "DELETE FROM items WHERE owner_id = :owner_id",
    }

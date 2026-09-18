"""A different implementation of the same bounded business contract."""


def decide(command, view):
    fields = {name: value for name, value in command.get("fields", {}).items()}
    return dict(op=command["op"], target=command.get("item_id"), fields=fields)

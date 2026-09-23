from pkg.helper import normalize


def greet(name: str) -> str:
    return f"hello {normalize(name)}"

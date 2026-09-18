"""Small dependency-free verifier checks shared by the actual driver/tests."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)

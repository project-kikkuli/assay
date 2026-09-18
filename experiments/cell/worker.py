"""Trusted container-side dispatcher; candidate output is never authority."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path


MAX_LINE_BYTES = 32 * 1024


def strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def decode(line):
    return json.loads(line, object_pairs_hook=strict_object, parse_constant=_bad_constant)


def _bad_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def load_candidate(path):
    spec = importlib.util.spec_from_file_location("cell_untrusted_candidate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("candidate module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    decide = getattr(module, "decide", None)
    if not callable(decide):
        raise RuntimeError("candidate has no callable decide")
    return decide


def main():
    if len(sys.argv) != 2:
        raise SystemExit("worker requires exactly one candidate path")
    decide = load_candidate(Path(sys.argv[1]))
    emit({"ready": True, "protocol": "cell.actor.v1"})
    for raw in sys.stdin:
        request = None
        if len(raw.encode()) > MAX_LINE_BYTES:
            emit({"error": "request_too_large"})
            continue
        try:
            request = decode(raw)
            request_id = request["request_id"]
            command = request["command"]
            view = request["view"]
            if not isinstance(request_id, str) or not isinstance(command, dict) or not isinstance(view, dict):
                raise ValueError("invalid request envelope")
            captured_out = io.StringIO()
            captured_err = io.StringIO()
            with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(captured_err):
                proposal = decide(command, view)
            if not isinstance(proposal, dict):
                raise ValueError("candidate proposal is not an object")
            if captured_out.getvalue() or captured_err.getvalue():
                raise ValueError("candidate wrote outside proposal")
            emit({"request_id": request_id, "proposal": proposal})
        except Exception as exc:
            emit({"request_id": request.get("request_id") if isinstance(request, dict) else None, "error": type(exc).__name__})


if __name__ == "__main__":
    main()

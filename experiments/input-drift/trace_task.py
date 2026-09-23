"""Runs one task's real command under an `open()` audit hook (PEP 578) and
records every project-relative file it actually reads. A separate process
because the traced command needs the hook installed before its own imports
happen, not just before a wrapper's own imports.
"""
from __future__ import annotations

import json
import os
import runpy
import sys


def main() -> None:
    root = os.environ["ASSAY_TRACE_ROOT"]
    out_path = os.environ["ASSAY_TRACE_OUT"]
    module, module_args = sys.argv[1], sys.argv[2:]
    opened: set[str] = set()

    def hook(event: str, args: tuple) -> None:
        if event != "open":
            return
        path = args[0]
        if not isinstance(path, str) or not os.path.isabs(path):
            return
        relative = os.path.relpath(path, root)
        if not relative.startswith(".."):
            opened.add(relative)

    sys.addaudithook(hook)
    sys.path.insert(0, os.getcwd())  # what `python -m <module>` does for the current directory
    sys.argv = [module, *module_args]
    exit_code = 0
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    with open(out_path, "w") as fh:
        json.dump(sorted(opened), fh)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

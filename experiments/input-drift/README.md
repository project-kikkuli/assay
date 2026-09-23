# Declared-input manifest drift

Explicit-input selection is only as safe as the manifest's `inputs` list is
complete. This experiment traces what a task's real command actually reads
and reports whether the manifest missed anything.

`trace_task.py` runs the task's real command (a subprocess, so a fresh
interpreter) under a `sys.addaudithook` listening for the `open` event (PEP
578), and records every project-relative file opened. `measure.py` expands
the task's declared `inputs` with the project's own `assay.runner.inventory`
and compares: a traced read outside the declared set is a real, undeclared
dependency — the exact failure mode explicit-input selection depends on not
having. `__pycache__` is cleared before each trace; an already-valid `.pyc`
short-circuits the interpreter's import machinery before it opens the `.py`,
which would hide a real read from the hook.

```sh
python3 experiments/input-drift/measure.py
```

## Measured result

This repo's own 5-task `assay.json`, traced against its real `unittest`
commands: **zero undeclared reads across all 5 tasks** (recall 1.0 on every
task; [full results](results.json)). `schedule-exploration` over-declares
one file (`examples/parcel/test_queue.py`, listed but not read by that
task's own command) — over-declaration is safe, just wasted specificity.

To confirm the method actually detects drift rather than trivially passing,
`fixtures/undeclared/` declares a task's inputs as `pkg/main.py` +
`pkg/__init__.py` + its test file while `pkg/main.py` imports
`pkg/helper.py` — a real, common mistake (an import added without updating
the manifest). Trace: **`pkg/helper.py` reported as a missed undeclared
read, recall 0.75** (3 of 4 real source reads declared). `fixtures/declared/`
is the same code with `pkg/helper.py` correctly added to `inputs`: recall
and precision both 1.0.

## Limits

The audit hook catches `open()`-level reads; it does not catch every way
Python code can reach file content (e.g. `os.open` at the raw fd level,
already-imported C extensions, or a subprocess the traced command itself
spawns) and is not a substitute for a syscall-level tracer across those
paths. It also cannot see resources fetched over a network. A clean trace on
one run is evidence about that run's code paths, not a proof that no
input-sensitive branch goes untraced — the same limitation `python-selection`
already documents for coverage-based selection.

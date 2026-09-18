# Python selection experiment

FAIR subject-venv run on Marimo `e8009f0f`: 12,721 selected; 12,531 passed, 11 failed, 107 skipped, 56 xfailed, 16 xpassed in 797.5s pytest time. Warm selection retained 11 failures across 11 tests in 62.3s. An automatic 9-test formatter selection caught a real output mutation; deleting a real favicon produced zero Testmon tests while direct execution failed 1 of 41.

The earlier 12,372-pass/122-fail overlay report remains preserved but invalid: it omitted subject `.venv/bin` from `PATH` and used a plugin-only interpreter. The FAIR run sets documented `UV`, subject PATH, fresh HOME/cache, and stages 480 real assets. CodeMode’s prior Pixi failures disappear with `UV`; no tests or source were weakened.

This independent experiment evaluates `pytest-testmon==2.2.0` on the pinned public Marimo checkout. Testmon records executed Python dependencies through Coverage.py and selects tests affected by changed Python files; it does not establish safety for unseen branches, resources/static files, or external services. The official docs require an initial whole-suite `pytest --testmon` baseline and say static files are not tracked: <https://www.testmon.org/>.

Run from the Assay checkout:

```sh
export MARIMO=/path/to/marimo
python3 experiments/python-selection/run_fair.py --repo-dir "$MARIMO"
```

The FAIR runner installs only `pytest-testmon==2.2.0 --no-deps` into the existing subject venv, records versions/freezes, and uninstalls only that plugin in `finally`; lockfiles and source are untouched. It stages assets and restores them/mutants. Use `run_selection.py` for the preserved isolated-overlay diagnostic.

The report separates bootstrap, collection/selection, test execution, and post-processing timings; records exact commands/counts; treats timeout, missing, malformed, and zero-test evidence as unknown; and reports when Testmon falls back to a full run. The full run is an observation, not acceptance, if upstream tests fail. Then run the bounded follow-up:

```sh
python3 experiments/python-selection/run_target_followup.py --repo-dir "$MARIMO"
```

Run `python3 experiments/python-selection/diagnose_baseline.py --repo-dir "$MARIMO"` for the 480-file staged-assets matrix comparing subject Python and the plugin-only overlay with and without Testmon. `python3 -m unittest experiments/python-selection/test_report_guards.py` checks count, freshness, and zero-selection guards.

It uses an output-path formatter defect that the original test should catch, and a file-scoped static-resource deletion: a zero Testmon selection is unresolved, while direct failure proves the resource matters. No cloud storage, secrets, Docker, database, or committed Marimo file is used.

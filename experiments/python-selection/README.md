# Python selection experiment

The [explicit-input follow-up](closure-results.json) closes the demonstrated
static-resource hole for one real 41-test obligation. It captures a disposable
copy of the pinned checkout, stages the real built assets, hashes 3,103 entries
under declared roots, and requires a positive baseline before reuse. The current
run took 4.46s for the baseline and 0.44–0.53s for five unchanged decisions.
Deleting the favicon forces execution and fails in 3.01s. Changing private HTML
caching to public caching also forces execution and fails in 5.14s. Added assets
and changed Python source rerun; changed command/runtime/verifier require reruns.

```sh
python3 experiments/python-selection/run_closure.py --repo-dir "$MARIMO"
python3 -m unittest discover -s experiments/python-selection -p 'test_*.py'
```

This demonstrates invalidation, not whole-application admission. The 41 tests
cannot justify skipping the rest of the suite. Receipts are in-memory trusted
fixture data; there is no protected issuer or immutable execution sandbox.
The reused venv is identified by interpreter bytes and package versions, not
every dependency byte. The language-neutral input inventory includes content,
file mode, directory membership, missing files and internal file-link targets;
unsafe links fail closed. It excludes named test/cache directories. Production
reuse needs complete inputs and enforced isolation, as described by
[Bazel](https://bazel.build/basics/hermeticity), not only hashes.

Marimo is Apache-2.0 licensed; the probe copies no upstream source into Assay.
Preparation requires the pinned, prepared public checkout described in
[the scale experiment](../marimo/README.md), including `frontend/dist` and its
test venv. `run_closure.py` leaves that checkout untouched. The older diagnostic
runners below temporarily mutate their subject: use a disposable checkout and
do not run them concurrently.

## Coverage-only result

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

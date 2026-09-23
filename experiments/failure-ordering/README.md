# Time to first failure

An agent iterating on a change re-runs the suite and reads the first `FAIL`;
everything the runner executes before that point is wall-clock the agent
spent waiting, not signal. This measures how much of that wait five real
orderings avoid on pytest's own `testing/` suite (4,556 tests) run against a
real regression in pytest's own history (PR #14934: `approx()` silently
compared mismatched nested container types instead of raising) — not a
seeded fault.

`order_plugin.py` is one `pytest_collection_modifyitems` hook: given a
`nodeid -> score` JSON file, it sorts scored items ascending and leaves
unscored items in place after them. Every run below is real pytest
(`-x`, unmodified fixtures, unmodified source) with only that hook loaded;
`measure.py` computes each ordering's scores from real prior runs (via
`scenarios.json`, three other real pytest commits — one the same recurring
bug, two unrelated real bugs) and never touches test content beyond the real
historical-fault-injection overlay described in the module docstring.

```sh
python3 experiments/failure-ordering/measure.py \
  --repo-dir /path/to/pytest --python /path/to/pytest/.venv/bin/python
```

## Measured result

One real `-x` run per strategy, same 4,556-test pool, same real 11-failure
target ([full results](results.json)):

| Strategy | Tests run before stop | Wall time |
|---|---|---|
| `default` | 688 | **10.47s** |
| `duration_asc` | 1,032 | **1.76s** |
| `proximity` (own file first) | 63 | **1.00s** |
| `historical_rate` | 1 | **0.95s** |
| `recently_failed` | 1 | **0.91s** |

Every alternative ordering reaches the real failure faster than default —
**5.9x** (`duration_asc`) to **11.5x** (`recently_failed`) less wall time.
`recently_failed` and `historical_rate` both land on the failing test
**first**, because this regression also failed on the nearest of the three
real prior commits sampled. `duration_asc` is the interesting case: it runs
*more* tests before stopping (1,032 vs. default's 688) but far less wall
time, because ascending-duration ordering front-loads many cheap tests
rather than skipping to the slow ones — cutting wall time and cutting test
count are different mechanisms, and here only the first happens.

## Limits (n=1 build)

`recently_failed` and `historical_rate` produce an identical ordering here
because, by construction, only the *nearest* of the three sampled priors
shares this target's bug — a favorable case for recency. A history where an
older prior recurs and the nearest one doesn't would separate them; this run
doesn't cover that case. All three priors are single-bug scenarios (11, 3,
and 19 failures respectively, each isolated to one file) built the same way
as the target — not a real multi-commit CI history with its own noise,
flakiness, or partially-fixed regressions. The machine was under heavy
concurrent load from unrelated processes during measurement (see
`results.json`'s `baseline_full_run.wall_s`, 133s for a suite that timed
under 3 minutes uncontended); `tests_run_before_stop` is the more reliable
comparison, `wall_s` is directional. This is one regression's collection
position in one suite under one Python version (3.12) — a bug that already
sits early in file-scan order would show default performing closer to the
other strategies.

## A statistical sample (n=20, two repos) and a real mechanism

The n=1 result is one regression's collection position. `mine_cases.py`
builds a real sample instead: it scans a repo's own git history for small
"fix"-shaped commits (exactly one source file, one or two test files, ≤4
files total), and for each candidate checks out the real parent commit and
overlays the fix commit's own test file(s) on top unmodified — verifying a
SCOPED run (just that file, not the whole suite, so mining stays cheap)
actually produces real failures (and rejecting candidates producing zero
failures or more than 60, an unrelated mass breakage rather than one bug)
before keeping the case. 13 pytest cases and 7 click cases were mined this
way (`cases/pytest_cases.json`, `cases/click_cases.json`): pytest's 13
classified candidates all verified (13/13); click's 8 classified candidates
produced 7 (one rejected by the same filter).

```sh
python3 experiments/failure-ordering/mine_cases.py \
  --repo-dir /path/to/pytest --src-root src/_pytest --test-root testing --target-count 15 \
  --out experiments/failure-ordering/cases/pytest_cases.json
python3 experiments/failure-ordering/measure_sample.py \
  --repo-dir /path/to/pytest --test-root testing \
  --cases experiments/failure-ordering/cases/pytest_cases.json --repo-tag pytest \
  --out experiments/failure-ordering/results_sample_pytest.json
python3 experiments/failure-ordering/replay_plugin.py \
  --repo-dir /path/to/pytest --test-root testing \
  --cases experiments/failure-ordering/cases/pytest_cases.json --repo-tag pytest \
  --sample-results experiments/failure-ordering/results_sample_pytest.json \
  --out experiments/failure-ordering/results_replay_pytest.json
```

`measure_sample.py` reruns the n=1 build's four isolated strategies
(`default`, `duration_asc`, `proximity`, `recent_failure` — using the
chronologically nearest *other* mined case in the same repo as the "prior
CI run" for recency, real failing nodeids, not synthetic) on every case, one
real `-x` subprocess per strategy per case. `../pytest-order-plugin/` is the
mechanism this sample was built to validate: a real, general-purpose pytest
plugin (not sample-specific code) combining all three factors
lexicographically. `replay_plugin.py` runs *that actual plugin* — not scores
computed by this experiment — once per case in chronological order, sharing
one real pytest cache directory across the whole replay so
`config.cache.get("cache/lastfailed", ...)` reflects genuine prior-run state
and `git diff --name-only HEAD` genuinely sees the case's own overlay as a
change, exactly as a real developer's repo would.

### Measured result

Pooled `tests_run_before_stop` across all 20 real cases (13
[pytest](results_sample_pytest.json), 7 [click](results_sample_click.json)):

| Strategy | Median | Mean | Min | Max | Cases beating `default` |
|---|---|---|---|---|---|
| `default` | 1,936 | 1,844 | 2 | 4,442 | — |
| `duration_asc` | 1,191 | 1,315 | 763 | 2,090 | 11 / 20 |
| `recent_failure` (isolated) | 1,837 | 1,657 | 1 | 4,442 | 3 / 20 |
| `proximity` (isolated) | **77** | 165 | 2 | 2,029 | 18 / 20 |
| **real plugin** (`assay_order.py`, all 3 combined) | **69** | 71 | 1 | 217 | **19 / 20** |

`proximity` alone is the strongest single factor (90% win rate, ~25x median
reduction) — unsurprising, since every mined case's bug lives in exactly the
file(s) the case's own overlay touches, so "run the changed file first" is
close to the a-priori-strongest possible signal for this sample's construction.
`recent_failure` in isolation wins only 3/20 (it needs the *immediately
preceding* mined case to share the same bug, which happens for genuinely
related commit clusters — e.g. five consecutive walrus-operator rewrite
fixes — but not otherwise); `duration_asc` wins about half the time, with a
smaller median win than proximity. The real combined plugin wins **19/20**,
losing only the one case where `default` was already at position 2 of the
whole suite (a floor, not a real loss) — every full run used unmodified
pytest plus this one drop-in plugin, not this experiment's synthetic scores.

### Limits (n=20 sample)

Mining biases toward small, single-cause commits by construction (the
classification filter itself), so this sample says nothing about a large,
multi-file regression. Two pytest case pairs share the same first-failing
test by coincidence (a run of related walrus-operator and bytecode-cache
fixes landing close together in pytest's own history) — not fully
independent samples, though this affects both `default` and every other
strategy identically, so it doesn't bias the *comparison* between
strategies. `recent_failure`'s "prior CI run" is the chronologically
adjacent *other mined case*, not a continuous real CI history with its own
noise and already-fixed regressions; the real plugin's replay is closer to
real (a genuine shared pytest cache across a chronological sequence) but
still only sees these 20 commits' worth of history, not a project's full
timeline. Both repos are pure-Python, well-tested open-source libraries;
neither is evidence about compiled languages, flaky external-service tests,
or a monorepo's cross-package dependency graph. The machine was contended
throughout measurement (see individual case `wall_s` values); `tests_run_before_stop`,
not wall time, is this section's primary metric throughout.

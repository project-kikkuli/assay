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

## Limits

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

# A real pytest ordering plugin

`assay_order.py` is a real, standalone pytest plugin (not an experiment
harness): drop it on `PYTHONPATH` and pass `-p assay_order --assay-order` to
any pytest project. It reorders collected tests by three factors a real CI
run already has for free — pytest's own `--lf`/`--ff` cache, a real `git
diff`, and durations the plugin persists in that same cache across runs —
and is inert until asked (`test_disabled_by_default_is_unmodified_pytest`
pins that).

```sh
pytest -p assay_order --assay-order              # risk-ordered, runs everything
pytest -p assay_order --assay-fail-fast          # risk-ordered + stops at the first failure
pytest -p assay_order --assay-order --assay-diff-ref=origin/main
```

```sh
python3 -m unittest experiments/pytest-order-plugin/test_assay_order.py -v
```

## Measured result

Five real, disposable-git-repo subprocess integration tests
(`test_assay_order.py`), each asserting on an actually observed execution
order or stop point, not a mocked hook: a real prior failure moves a test to
the front, a real uncommitted diff does the same for its file, a real
recorded duration breaks a tie between two otherwise-equal tests, combining
`--assay-fail-fast` really does stop the run at the reordered failure, and
the plugin is a true no-op without `--assay-order`. All five pass.

`../failure-ordering/replay_plugin.py` runs this exact plugin (not scores
computed by another script) against the same 20 real regression/fix pairs
`../failure-ordering/mine_cases.py` mined from pytest and click's own
history, once per case in chronological order, sharing one real pytest
cache directory across the whole replay so the recent-failure and duration
factors reflect genuine accumulated state:

```sh
python3 ../failure-ordering/replay_plugin.py \
  --repo-dir /path/to/pytest --test-root testing \
  --cases ../failure-ordering/cases/pytest_cases.json --repo-tag pytest \
  --sample-results ../failure-ordering/results_sample_pytest.json \
  --out ../failure-ordering/results_replay_pytest.json
```

The plugin wins **19/20** real cases ([pytest 13/13](../failure-ordering/results_replay_pytest.json),
[click 6/7](../failure-ordering/results_replay_click.json)): pooled median
**69** tests run before the real failure, against default order's **1,936**.
The one loss is `click`'s `KeyboardInterrupt` race-condition case, where
default order already found the failure at position 2 of the whole suite —
nothing beats that; it isn't a case the plugin got wrong.

## Limits

The proximity factor is file-granular (`git diff --name-only`), not
line-granular: a diff touching one function in a large file boosts every
test in that file equally. `--assay-diff-ref` defaults to `HEAD`, i.e. an
uncommitted working-tree change; a caller comparing a whole branch needs to
pass the branch's merge-base explicitly. The duration cache is unbounded and
never expires an entry for a deleted test — harmless (it's just dead weight
in a dict), but worth knowing before assuming the cache file's size tracks
the live suite.

# Merge-queue safety

What does a merge queue buy — testing the actual merged tree instead of the
PR branch alone — and does the answer change with how narrowly tests are
selected?

`measure.py` samples real two-parent "Merge pull request" commits from
`pytest`'s own history. For merge `M` with mainline parent `base` and PR
parent `pr_head`, it compares `pr-head-alone` (what CI without a merge queue
tests) against `full-on-merged` (the real merged tree — ground truth),
`change-scoped` (only test files matched to the PR's *own* diff) and
`exact-input` (matched to the PR's diff *and* whatever changed concurrently
on `base`). A sample only counts if something changed on both sides of the
merge base — otherwise there is nothing for a merge queue to have caught.

Two oracles, both real command executions:

- **`collect`** — `pytest --collect-only` (real import/fixture/hook
  registration, no test bodies). Fast, so it runs for every sampled merge;
  only catches import/collection-time breaks.
- **`execute`** — `pytest` with real test bodies. Catches runtime/assertion
  semantic conflicts `collect` cannot see, but is far slower, so it runs only
  on a bounded `--runtime-subsample` of merges for the two selections, and a
  smaller `--full-runtime-subsample` of those for the full suite.

Every sampled merge lands in exactly one outcome bucket per oracle
(`both_passed` / `both_failed` / `head_passed_merged_failed` — the target
case a merge queue exists for / `head_failed_merged_passed` / `unresolved`
for an environment/layout incompatibility pytest itself never structurally
reports, e.g. a decade-old commit importing `six`, which isn't installed).
Bucket counts always sum to the evaluated count — nothing is dropped.

```sh
git clone https://github.com/pytest-dev/pytest.git /path/to/pytest
python3 -m venv /path/to/pytest-deps-venv
/path/to/pytest-deps-venv/bin/pip install "iniconfig>=2" "packaging>=24" \
  "pluggy>=1.5,<2" "pygments>=2.15" argcomplete attrs coverage hypothesis \
  mock pexpect pyyaml requests setuptools xmlschema
python3 experiments/merge-queue-safety/measure.py --repo-dir /path/to/pytest \
  --venv-python /path/to/pytest-deps-venv/bin/python3 --samples 90 \
  --merge-window 800 --runtime-subsample 10 --full-runtime-subsample 4
python3 experiments/merge-queue-safety/seeded_conflict.py
```

## Measured result

**Real history** ([results.json](results.json), one run, 90 merges
evaluated, 280 skipped for no concurrent change on one side — every count
below is read straight from this one file, and buckets reconcile exactly to
the evaluated total in each oracle):

| Oracle | Evaluated | both passed | both failed | **head passed, merged failed** (target) | head failed, merged passed | unresolved |
|---|---|---|---|---|---|---|
| `collect-only` | 90 | 38 | 31 | **0** | 1 | 20 |
| `execute`, change-scoped selection | 10 | 3 | 6 | **0** | 0 | 1 |
| `execute`, exact-input selection | 10 | 1 | 8 | **0** | 0 | 1 |
| `execute`, full suite | 4 | 0 | 4 | **0** | 0 | 0 |

**Zero merges, at any oracle, showed a PR head that passed while the real
merged tree did not.** For every `both_failed` case in the runtime-execution
oracles, the *exact same set* of test files failed on both trees (checked
directly against `failed_files` in `results.json`) — the merge introduced no
new failures in this sample; those failures are pre-existing/environment-
sensitive tests, not merge-induced breaks.

**Seeded conflict**, because a 0-count real sweep does not by itself show the
mechanism doesn't exist, only that it's rare at this sample size and these
oracles: `seeded_conflict.py` builds a deterministic case — a PR renames a
function and updates its own caller; a *disjoint* concurrent commit adds a
new, unrelated caller of the old name; the merge is textually clean.
[Result](seeded-conflict-results.json): `pr_head_alone` passes,
`change_scoped_on_merged` (selection built only from the PR's own diff, but
correctly run against the merged tree) **still passes — it misses the
break**, while `full_on_merged` and `exact_input_on_merged` (selection also
covers what changed concurrently) both **fail and name the real broken
file**. This is exactly the case the real sweep searched for and did not
find in this sample: change-scoped selection is exactly as blind to a
disjoint concurrent change as no merge queue at all, unless it also accounts
for what changed on the other side of the merge.

## Limits

The real-history counts are a lower bound on detectable conflicts, not an
upper bound on real ones: `collect-only` only exercises import/collection-time
code paths, and the `execute` oracle only covers 10 (selections) / 4 (full
suite) of the 90 sampled merges for cost — a larger runtime-oracle sample
could still find a real instance this one did not. 20/90 collect-only samples
and 1/10 runtime samples are unresolved (an environment/layout incompatibility
from testing old history with one fixed, current dependency set), not scored
either way. The merge window is bounded to post-2019 history for dependency
stability with the repo's current `pyproject.toml`. Change-scoped and
exact-input selection use a path/name-stem heuristic to approximate what a
real selector would choose, not pytest's actual (nonexistent) CI selection
policy.

# Merge-queue safety

What does a merge queue buy — testing the actual merged tree instead of the
PR branch alone — and does the answer change with how narrowly tests are
selected?

`measure.py` samples real two-parent "Merge pull request" commits from
`pytest`'s own history. For merge `M` with mainline parent `base` and PR
parent `pr_head`, it runs `pytest --collect-only` (fast: real import/fixture
registration, no test bodies) at four scopes: `pr-head-alone` (what CI without
a merge queue tests), `full-on-merged` (the real merged tree — ground truth),
`change-scoped` (only test files matched to the PR's *own* diff, run against
the merged tree), and `exact-input` (matched to the PR's diff *and* whatever
changed concurrently on `base`, also run against the merged tree). A sample
only counts if something changed on both sides of the merge base — otherwise
there is nothing for a merge queue to have caught. A result of `passed=None`
("unresolved") marks an environment/layout incompatibility from testing
decades-old history with one fixed dependency set — not a real answer either
way, so it is excluded from the headline count rather than silently scored.

```sh
git clone https://github.com/pytest-dev/pytest.git /path/to/pytest
python3 -m venv /path/to/pytest-deps-venv
/path/to/pytest-deps-venv/bin/pip install "iniconfig>=2" "packaging>=24" \
  "pluggy>=1.5,<2" "pygments>=2.15" argcomplete attrs coverage hypothesis \
  mock pexpect pyyaml requests setuptools xmlschema
python3 experiments/merge-queue-safety/measure.py --repo-dir /path/to/pytest \
  --venv-python /path/to/pytest-deps-venv/bin/python3 --samples 60 --merge-window 800
python3 experiments/merge-queue-safety/seeded_conflict.py
```

## Measured result

**Real history:** 90 real merges evaluated across two runs (merge-window 400
and 800, both restricted to history after pytest's 2019 src-layout
conversion; [results.json](results.json) holds the larger run). Of those, 48
resolved cleanly on both `pr-head-alone` and `full-on-merged` (24 pass/24
fail, always agreeing) and 12 were unresolved (environment incompatibility);
**zero merges showed the target case** — a PR head that collected cleanly
while the real merged tree did not. At `--collect-only` granularity, on this
repo, in this sample, a merge queue would not have caught anything a
change-scoped or full-suite PR check missed.

**Seeded conflict**, because a 0-count real sweep does not by itself show the
mechanism doesn't exist, only that it's rare at this oracle's granularity:
`seeded_conflict.py` builds a deterministic case — a PR renames a function
and updates its own caller; a *disjoint* concurrent commit adds a new,
unrelated caller of the old name; the merge is textually clean.
[Result](seeded-conflict-results.json): `pr_head_alone` passes,
`change_scoped_on_merged` (selection built only from the PR's own diff, but
correctly run against the merged tree) **still passes — it misses the
break**, while `full_on_merged` and `exact_input_on_merged` (selection also
covers what changed concurrently) both **fail and name the real broken
file**. This is what the real sweep was searching for and did not find in
this sample: change-scoped selection is exactly as blind to a disjoint
concurrent change as no merge queue at all, unless it also accounts for what
changed on the other side of the merge.

## Limits

`--collect-only` only exercises import/collection-time code paths (fixture
and hook registration, module-level decorators); it catches nothing that
only breaks inside a test body, which is most of what "semantic merge
conflict" usually means. The 0/90 real-history result is a lower bound on
detectable conflicts, not an upper bound on real ones — it says this specific
oracle found nothing in this sample, not that pytest's merges are safe. The
window is bounded to post-2019 history for dependency stability with the
repo's current `pyproject.toml`; older history was not usable without
per-era dependency pinning, which this run does not attempt. Change-scoped
and exact-input selection here use a path/name-stem heuristic to approximate
what a real selector would choose, not pytest's actual (nonexistent) CI
selection policy.

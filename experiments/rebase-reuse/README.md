# Rebase/restack reuse

Answers a question the rest of this repo leaves open: if a task's evidence is
keyed on declared-input content hashes rather than a commit SHA, how much of
that evidence survives a rebase?

`measure.py` defines one task per source module under `pytest`'s
`src/_pytest/` (24 modules/directories: `assertion`, `config`, `capture.py`,
`fixtures.py`, ...). It treats 3 real consecutive commits from `pytest`'s
actual `main` history as a stacked branch, replays them with
`git rebase --onto` onto a real base 8 commits further along the same
history (simulating other work landing before the restack), and compares
each task's git tree hash — already content-addressed by construction —
before and after. A matching hash means a receipt keyed on that task's
declared inputs is reusable post-rebase without rerunning it; git rebase
conflicts are recorded, not silently retried or discarded.

```sh
git clone https://github.com/pytest-dev/pytest.git /path/to/pytest
python3 experiments/rebase-reuse/measure.py --repo-dir /path/to/pytest --samples 60
```

## Measured result

60 sampled stacks on pytest `main` (seed 7, stack size 3, gap 8): **42/60
rebases conflicted** (some other commit in the 8-commit gap touched the same
lines as the stack) and were aborted, cleanly recorded rather than retried.
Across the 18 conflict-free rebases, 432 (task × sample) pairs were
evaluated: **394 (91.2%) had a byte-identical tree hash** before and after
the rebase. Restricting to the 21 pairs where the stack's own commits
touched that task — the ones a commit-SHA-keyed cache would always rerun —
**14 (66.7%) were still hash-identical after rebase**: two-thirds of the
rework a naive cache would force is avoidable when reuse is keyed on
declared-input content instead of the commit SHA. [Full results](results.json).

## Limits

The 70% conflict rate is itself informative: `pytest` is a single-package
repo where large synthetic stacks routinely touch changelog fragments and
shared files, so this is a rate for this task granularity and gap size, not
a general figure. `git rebase --onto` here replays real commits already on
`main` as a stand-in for an open branch; it is not evidence about how often
real open branches in this project are actually rebased, and a clean rebase
is not itself a correctness check — verifying the reused evidence still
holds against the new base's actual behavior is a task-execution question
this experiment does not answer.

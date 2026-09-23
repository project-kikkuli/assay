# Real parallel-worker batch queue

`merge-queue-throughput` simulated batching and speculation over pre-measured
durations. This is the mechanism itself, running for real: real `git
worktree` directories give W real workers independent real checkouts of the
same repo; real batches of real commits are dispatched to a real
`ThreadPoolExecutor`; a real batch failure triggers a real recursive
`pytest` bisection, each probe a real subprocess against a real sub-range
checkout. A "broken PR" is a real, executed one-line mutation —
`hmac.compare_digest` negated in `itsdangerous`'s real `signer.py` — applied
to a real checkout before real tests run, not a boolean standing in for one.

A real bug surfaced building this: a venv's `pip install -e .` editable
install resolves `import itsdangerous` back to the clone it was installed
from, independent of `cwd`. With several real worktrees checked out
concurrently, every worker's import silently resolved to the same original
clone, so mutations were applied to files nothing ever imported —
`verdict_mismatches` (a real mutated PR that still passed, or a clean PR
that still failed) stayed zero only after fixing it by pointing
`PYTHONPATH` at each worker's own `src/`. Kept as `batch_queue.py`'s leading
comment; a purely analytical simulator has no mechanism in which this bug
could even occur.

```sh
python3 experiments/real-batch-queue/measure.py \
  --repo-dir /path/to/itsdangerous --python /path/to/venv/bin/python3
```

## Measured result

24 real commits, 2 really mutated, 4 real workers, batch size 6
([full results](results.json)); `verdict_mismatches: 0` in every regime —
exactly the 2 real mutated PRs were really rejected, nothing else:

| Regime | Makespan | Mean convergence | Real jobs | Real CPU-seconds |
|---|---|---|---|---|
| Serial (1 worker, batch 1) | 18.85s | 10.77s | 24 | 14.86s |
| Parallel, unbatched (4 workers, batch 1) | **8.55s** | **3.29s** | 24 | 21.18s |
| Parallel, batched (4 workers, batch 6) | 10.36s | 5.45s | **16** | **17.98s** |

Plain parallelism (4 workers, no batching) is the fastest real wall-clock
result — 2.2x the serial makespan, 3.3x the serial mean convergence time.
Real batching+bisection uses fewer real jobs and less real CPU time than
unbatched parallelism (16 vs 24 jobs, 17.98s vs 21.18s CPU), reproducing the
resource savings `merge-queue-throughput`'s simulation predicted — but its
real makespan is **worse** than unbatched parallelism, not better, and this
held across three real runs (two seeds, plus batch size 3). The mechanism:
with only `commits / batch_size` batches spread across the workers, the one
or two workers whose batch contains a real mutated PR spend the whole run
doing real sequential bisection while the other workers, done with their
passing batches, sit idle — a straggler effect the earlier simulator's
single-lane batch model had no way to produce, because it never modeled
multiple workers processing batches concurrently. Fine-grained parallel
dispatch self-balances across free workers as jobs complete; coarse batches
don't, once one of them fails.

## Limits

This is one small package's suite (fast enough to run dozens of real
subprocesses in the sample budget) with a hand-picked, always-present
mutation target — the load-imbalance finding is about batch **granularity**
under real multi-worker dispatch, not about this specific bug or repo, and
should be treated as a mechanism to watch for, not a universal ratio. Four
workers and a handful of batches is a small combinatorial space; the
straggler effect would need re-measuring at a batch count large enough for
several worker rounds to say whether it persists at scale. `mutated_count`
is a chosen, swept integer (2 of 24, close to but not equal to
`merge-queue-throughput`'s real-measured 1.94% pytest rate), not itself a
measurement from this repository's history.

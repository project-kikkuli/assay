# First-failure-first: how much tail is there to cut?

A red CI round on GitHub Actions is usually several jobs running concurrently
(a lint job, several test shards, platform builds, ...). A developer who
waits for the PR checks tab to go fully quiet before reacting is waiting on
whichever job is slowest, even though the round was already known red the
moment the first one failed. This measures that gap directly from real job
timestamps: no execution, no simulation.

`measure.py` reuses `ci-convergence`'s round-discovery method, then for
every red round fetches every job across every run in the round (real CI
structure fans a push out two different ways — vscode triggers several
separate workflow files per push, deno runs one workflow with a 100+ job
matrix — so job-level granularity is what generalizes across both) and
compares `signal_s` (real elapsed time to the first job reporting a failing
conclusion) against `settle_s` (real elapsed time to the last job in the
round reporting anything).

```sh
python3 experiments/fail-fast-signal/measure.py \
  --repos microsoft/vscode denoland/deno python/mypy sveltejs/svelte pola-rs/polars \
  --sample-prs 40
```

## Measured result

**68 real red rounds** with more than one job, across all 5 repos
([full results](results.json)): median signal time **296.5s** (~5 min) to
the first known failure, median settle time **1489s** (~25 min) for the
whole round to go quiet — a median **tail of 704.5s**, and **on average
66.5% of a round's total settle time is spent after the outcome is already
known**. Only 4.4% of rounds have essentially no tail (the failing job was
already the slowest one). This is the clearest single number in this lane:
a developer or a notification system that acts on first-red instead of
waiting for full completion gets the same information roughly a third to a
half sooner than today's default "wait for all checks."

Read against [`cancel-on-red`](../cancel-on-red/): this tail is *wall-clock
a human is watching*, not *compute a runner is burning* — GitHub Actions
jobs already run concurrently, so a still-running sibling isn't making the
red signal arrive later, it's just still running. Cancelling it doesn't
speed up the signal (that's this experiment); it stops paying for compute
after the signal, which is a different saving (that's `cancel-on-red`).

## Limits

Same-repo-branch, PR-open-window sampling limits as `ci-convergence`
(see its README). `signal_s`/`settle_s` are read straight off real
`completed_at` timestamps; nothing here assumes GitHub's UI or a
notification integration actually surfaces the first failure to a developer
the instant it happens — the tail measured is the theoretical maximum a
first-failure-aware workflow could recover, not a guarantee any given
team's tooling already delivers it. Rounds with only one job are excluded
(no tail is possible by construction), which mechanically biases this
sample toward matrix-heavy repos (`pola-rs/polars` alone contributes 31 of
the 68 rounds); a repo running one job per push would show 0% tail on every
round, not because the lever doesn't apply but because there's nothing to
cancel.

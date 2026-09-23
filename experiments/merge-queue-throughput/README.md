# Merge-queue throughput at agent PR volumes

Answers a configuration question for CI when many agents push many small,
concurrent changes: does batching or speculative parallelism actually help,
and does the answer change once a realistic fraction of those changes are
actually broken?

`measure.py` checks out 64 real consecutive commits from `itsdangerous`'s
actual `main` history and runs its real `pytest` suite (297 tests) at each
one, recording the real wall-clock duration. It also reruns the suite 30
times at HEAD to measure this repository's real flake rate. Those measured
durations then drive a policy simulator — no more subprocesses below that
point — comparing three real merge-queue designs over the same 64-commit
stream at simulated agent arrival multipliers (1x/2x/5x/10x the mean suite
duration) and swept "broken PR" rates (this repo's own history never
contains a broken commit on `main`, so the fraction of arriving PRs that are
actually defective is a seeded, explicitly labeled synthetic parameter, not
a measurement):

- **serial**: one job at a time, FIFO.
- **batch-of-N**: test N PRs' cumulative real diff in one job; on failure,
  binary-search the batch using the real measured duration of each
  sub-range's own real terminal commit (bisection never invents untested
  code — every probe is a real commit that really existed in history).
- **speculative-W**: W workers start the next arrived PR without waiting for
  earlier ones to be confirmed; a rejection invalidates and reruns every
  later PR that already passed or was in flight, mirroring the rebase a real
  speculative queue would need (see [`rebase-reuse`](../rebase-reuse/)).

```sh
python3 experiments/merge-queue-throughput/measure.py --repo-dir /path/to/itsdangerous --python /path/to/venv/bin/python3
```

## Measured result

64 real commits, real `pytest` runs ([full results](results.json)): mean
suite duration **1.009s** (range 0.507–3.710s), **zero** real failures across
the 64 checkouts, and **zero** observed flakes across 30 real reruns at HEAD
— this suite is not the source of real-world failure rate, so the simulator
sweeps 0%/5%/15%/30% broken-PR rates explicitly labeled `swept_synthetic`.

At a clean stream (0% broken), batching wins on both axes regardless of
arrival rate: batch-8 uses **8.59 CI-seconds** for all 64 PRs versus serial's
**64.58s** (7.5x fewer CI-seconds) and, at 10x arrival volume, also clears
the queue faster (**9.37s** makespan vs serial's **64.58s**).

That advantage is conditional on how often PRs are actually broken.
Bisection cost grows with the broken rate: batch-8's CI-seconds rise from
**8.59s** (0% broken) to **66.04s** (30% broken) — worse than serial's
**64.58s** at the same rate — while batch-4 stays slightly ahead (**57.44s**).
A fixed batch size that helps at a clean stream can become CI-minute-negative
once a team's actual bad-PR rate crosses a threshold between batch sizes.

The speculative-4 queue tells the opposite story: at 10x arrival volume it
clears the queue in **24.23s** versus serial's **64.58s** regardless of
broken rate (wall-clock win from real parallelism), but its CI-seconds rise
with the broken rate exactly because rejections force reruns — **84.80s**
of CI time at 30% broken versus serial's **64.58s**, a **31% CI-minutes tax**
for that wall-clock win. Batching and speculation trade CI-minutes for
wall-clock in opposite directions; which one a team should pick depends on
its actual measured bad-PR rate, not a single fixed default.

## Re-run at a measured real broken rate

The swept rates above are synthetic because `itsdangerous`'s history has no
broken commits to measure a rate from. `measure_real_rate.py` gets a real
rate a different way: it fetches pytest's actual GitHub Actions history for
its `test` workflow via the public REST API — every real `push` run on
`main` with its real conclusion — and re-runs the same unmodified
`simulate_serial` / `simulate_batch` / `simulate_speculative` functions
against `itsdangerous`'s already-measured durations at that one real rate,
not a sweep.

```sh
python3 experiments/merge-queue-throughput/measure_real_rate.py
```

**556 real push-to-main runs** of pytest's `test` workflow
([full results](results-real-rate.json)): 456 success, 9 failure, 91
cancelled (administrative, excluded) — a real historical failure rate of
**1.94%**, two orders of magnitude below the synthetic sweep's 30% high end.
At that real rate, the crossover above never appears in this range: batch-8
stays a clean win on every axis tested, using **17.41 CI-seconds** versus
serial's **64.58s** (3.7x fewer) and, at 10x arrival volume, also clearing
fastest (**18.35s** makespan, beating speculative-4's **18.64s**).
Speculative-4 buys no wall-clock advantage over batch-8 in this regime and
still pays a CI-minutes tax — **70.19s**, 9% over serial — for it. The
synthetic sweep's warning about batching flipping CI-negative is real, but
it triggers at rates (15–30%) far above what one large, mature real project
actually shows in its own history (~2%); do not read the crossover as an
argument against batching by default.

## Limits

`real_suite_failures_across_history: 0` and `flake_rate_at_head: 0.0` mean
the broken-PR rate driving every interesting result here is swept and
synthetic, not observed — a repository with real historical CI failures or
measurable flakiness (see [`flake-accounting`](../flake-accounting/)) could
show a different threshold. A "broken" PR here deterministically fails every
CI run that includes it and does not change that run's real duration, which
is a reasonable proxy for defect-driven failures but not for a defect that
only surfaces intermittently. The speculative simulator assumes an
invalidated PR always reruns cleanly against the corrected base for its
original measured duration; a real rebase can conflict outright (measured at
70% for a comparable stack size and gap in
[`rebase-reuse`](../rebase-reuse/)), which this simulator does not model.
Arrival multipliers are a deliberately chosen synthetic schedule, not this
repository's real (multi-day) commit cadence — they represent a hypothesis
about agent-driven volume, not a measurement of it.

The measured-real-rate re-run combines a duration source (`itsdangerous`)
and a broken-rate source (pytest) that are two different real repositories
with different contributor pools, review practices and CI maturity — it
does not claim one project's failure rate applies to another's durations,
only that both numbers are independently real and labeled by origin. Some
push-triggered runs share a `head_sha` with another run in the same fetch
(duplicate webhook deliveries, not test reruns); across the 556 sampled
`main` runs this happened 5 times and only once showed a differing
conclusion (`cancelled` vs `success`, an administrative supersede, not a
flaky test) — the fetched sample shows no case of the same commit's tests
genuinely flipping pass/fail. `cancelled` runs are excluded from the real
rate as administrative (superseded by a later push), not test outcomes;
including them would count ordinary push churn as failure.

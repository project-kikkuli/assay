# First measured results

These are observations from a small synthetic experiment, not a prediction of
arbitrary production CI performance.

## Independent hosted run

[GitHub Actions run 35309602934](https://github.com/project-kikkuli/assay/actions/runs/35309602934)
tested commit `2aecf87403530b0026a8bf64dfa285cd50ad2812` on September 18, 2026.

The whole workflow took **13 seconds** from creation to final update, including
queueing, checkout, Python setup, two matrix jobs, and artifact uploads. This is
one successful run, not a p95 measurement or a latency guarantee.

| Measurement | Python 3.11 | Python 3.12 |
|---|---:|---:|
| Unit/contract tests | 41 passed in 1.051 s | 41 passed in 1.131 s |
| Complete demo | 1.962 s | 2.046 s |
| Cold configured verification | 1.153 s | 1.177 s |
| First warm evidence reuse | 35.35 ms | 57.11 ms |
| Generated events checked | 1,022 | 1,022 |
| Minimized stale-worker failure | 5 steps | 5 steps |

The cold command set runs without a prior evidence cache. Warm measurements
include local hashing and cache validation but do not include hosted scheduling
or checkout. The demo additionally changes a source input and confirms its
previous successful result is not reused.

## Performance explanation, not just a stopwatch

On Python 3.12's hosted runner, selecting the next job from 10,000 ready rows:

| Query | Median SELECT time, 15 samples | SQLite VM steps |
|---|---:|---:|
| OR eligibility predicate, global order | 1.4346 ms | 120,024 |
| One candidate per eligibility class, then merge | 0.0070 ms | 55 |

Both queries see the same data and indexes. Setup and writes are excluded.
VM work is measured in a separate instrumented execution to avoid contaminating
the timing samples. This distribution deliberately exposes the cost of
examining all ready rows. It does not establish the rewrite's behavior on every
mixed/expired workload or include its index-maintenance costs.

The rewrite is compared with an independent oldest-eligible-row oracle over 100
generated mixed-state datasets. Query plans and raw timing samples are in the
workflow artifacts and in a fresh local `./demo` report.

## What the experiment establishes

- A small, dependency-free verification workflow can complete below 30 seconds
  on a hosted runner in an observed run.
- Reuse decisions, missing evidence, and failed executions can be explicit and
  inspectable around existing commands.
- A real persisted-state bug can be demonstrated and replayed without sleeping
  or hoping for a favorable wall-clock race.
- Source-linked work measurements can reveal why one algorithm scales badly.

## What it does not establish

- A general production CI system with complete dependency inference.
- Secure reuse of evidence produced by untrusted agents or branches.
- Exhaustive schedule coverage, zero flakiness, or zero escaped bugs.
- A broadly representative workload, statistical p95 CI latency, or a cost model
  for a large monorepo.

Those require the shadow-mode and trust-boundary experiments in
[ADOPTION.md](ADOPTION.md). The useful near-term artifact is the observable
verification wrapper and replay pattern, not a claim that the research is done.

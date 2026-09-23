# First-failure-first: what cancel-on-red buys in compute

[`fail-fast-signal`](../fail-fast-signal/) measured the wall-clock a
developer spends watching a red round's slower siblings finish after the
outcome is already known. That gap doesn't necessarily mean wasted runner
time — GitHub Actions jobs already run concurrently, so cancelling a sibling
doesn't make the failure land any sooner. What it does buy is real compute:
runner-seconds a still-in-flight job would otherwise burn after the round is
already red. This measures that directly, in the same currency a CI bill is
denominated in.

`measure.py` reuses the same round-discovery and per-job-timestamp fetch as
`fail-fast-signal`, but computes a different real quantity for every red
round: total job-seconds actually consumed today, versus what a policy that
cancels every job still running at the first real failure signal would have
consumed (a job starting after the signal never runs; one straddling it is
truncated to the signal instant; one already finished is unaffected).

```sh
python3 experiments/cancel-on-red/measure.py \
  --repos microsoft/vscode denoland/deno python/mypy sveltejs/svelte pola-rs/polars \
  --sample-prs 20
```

## Measured result

**46 real red rounds** with more than one job, across all 5 repos
([full results](results.json)): median real compute per round **16,140.5s**
(4.5 CPU-hours across all its jobs), median compute saved **9,566.5s** —
**fleet-wide, 58.5% of all job-seconds spent on these 46 rounds would never
have run under a cancel-on-red policy.** It varies sharply by repo — matrix
size drives it directly: `pola-rs/polars` (heaviest real matrix in this
sample, up to 33-36 jobs/round) saves **80.5%**; `microsoft/vscode`
(6 mostly-independent, mostly-fast workflow files) saves only **23.1%**.
The two levers this lane has now measured on the same red-round population
are complementary, not redundant: `fail-fast-signal` says a developer
watching for "all checks done" waits ~66.5% longer than they need to;
this says the CI bill for that same wait is, on a heavy-matrix repo,
mostly for jobs that were already known to be moot.

## Limits

Same job-fetch method and same-repo-branch/PR-window sampling limits as
`fail-fast-signal` (see its README); rounds with only one job are excluded
by construction (nothing to cancel). This is a **savings ceiling**, not a
proposed policy: it assumes perfect, instantaneous cancellation the moment
the first job reports failure, with no grace period for a possibly-flaky
result and no accounting for jobs a team would keep running regardless (a
required merge-gate job, a security scan). A real fail-fast policy would
plausibly want to hold off cancelling for a short confirmation window or
exempt specific jobs, both of which would reduce the real savings below
what's reported here. GitHub Actions billing rounds job time to the minute
and by runner size/OS, which this measures in raw seconds, not dollars.

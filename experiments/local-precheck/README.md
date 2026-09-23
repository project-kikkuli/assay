# Local pre-push check savings

[`ci-convergence`](../ci-convergence/) found that lint/format and type-check
failures are 56.5% of real red CI rounds. Both are deterministic and need no
service, fixture, or test data — exactly the class a local pre-push hook
reproduces. This measures what running that check locally, before the push
that triggered the round, would actually have saved — using the real failing
job's own execution time as the local-check proxy, not an assumed duration.

`measure.py` reuses `ci-convergence`'s round-discovery method on a fresh PR
sample, keeps only red rounds classified `lint_or_format` or `type_check`,
and for each one reads the real failed job's `completed_at - started_at`
(same command, no queueing, no other matrix legs — if anything an
overestimate of a warm local run) against the round's real cost: its own CI
wall time plus the real gap until the next push, capped at 4h to exclude
gaps that reflect the developer being away rather than blocked (see Limits).
Long-lived/stacked PRs (`days_pr_open > 3`) are excluded up front, same
reasoning as `ci-convergence`'s fast-iteration bucket.

```sh
python3 experiments/local-precheck/measure.py \
  --repos denoland/deno sveltejs/svelte python/mypy pola-rs/polars microsoft/vscode \
  --sample-prs 40
```

## Measured result

200 real merged PRs examined (40 each, 5 repos; [full results](results.json)):
**27 locally-catchable red rounds** (13.5 per 100 PRs) — 24 lint/format, 3
type-check, concentrated in `pola-rs/polars` (19, mostly `ruff`/`clippy`) and
`python/mypy` (all 3 type-check hits). Per caught round, the real failing job
ran a **median of 295s** and its round cost the PR a **median of 1152s** —
**median savings 846s**, but the distribution is long-tailed: mean savings
3425s, driven by a handful of rounds where the between-round gap (capped at
4h) or the round's own multi-job CI time was large. Scaled by the observed
13.5 rounds/100 PRs, that's **an estimated 12.84 developer-wall-clock hours
saved per 100 PRs** from a pre-push hook that runs only the linter and type
checker — not the full suite — before every push.

## Limits

The local-check time is the real CI job's own duration, which already excludes
network/artifact/checkout overhead a genuinely local run wouldn't pay either —
so if anything this underestimates the real local speedup, not overestimates
it. It does not include dependency-install or first-run cache-cold time, which
a real pre-push hook would pay on a cold checkout (this repo's own
[input-drift](../input-drift/) and the sibling [rebase-reuse](../rebase-reuse/)
experiment both bear on how much of that a content-addressed cache would
absorb after the first run). The 4h between-round gap cap is a modeling
choice, not a measured value — a shorter or longer cap would move the mean
(not the median) materially; median is the number to trust here. This assumes
100% developer compliance with running the hook before every push, which is
an adoption question this experiment doesn't measure. Round discovery inherits
`ci-convergence`'s same-repo-branch limitation (see its README).

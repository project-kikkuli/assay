# CI-side auto-fix-and-repush

[`local-precheck`](../local-precheck/) showed a pre-push hook always wins on
its 27 caught rounds (every one had positive savings, by construction: local
check time is always less than the round it replaces). Does the same benefit
hold for the alternative lever the North star names — CI itself running the
formatter/linter's `--fix`, committing, and re-pushing instead of leaving a
human to notice and fix a red lint/format round?

`measure.py` takes no new samples: it reads
[`local-precheck/results.json`](../local-precheck/results.json)'s real
`caught_rounds` — the same 200-PR sample — restricted to `lint_or_format`
(the mechanically `--fix`-able class; `type_check` has no reliable
autofixer and is excluded). Auto-fix does **not** remove the round the way
local-precheck does — CI still has to run once to detect the violation —
it replaces the real measured human between-round gap with an automated
cycle: the same job's own real duration (autofix tools cost about as much as
the check they mirror) plus a fixed **estimated, not measured**
20s push/re-trigger overhead.

```sh
python3 experiments/ci-autofix/measure.py
```

## Measured result

24 of the 27 `local-precheck` rounds are lint/format (the autofixable
subset; [full results](results.json)). Against them, auto-fix-and-repush is
a **mixed, mostly negative result**: only **5/24 (20.8%) have positive
savings** — median savings is **−141.5s**, i.e. for most of these real
rounds the developer had already noticed and fixed the lint failure faster
than a full CI round-trip (detect → autofix job → commit → push → re-trigger)
would have. The **mean** is positive (976.5s) only because a few rounds had
very long real human gaps (hours) that any automated fix beats easily
(max 14,341s saved). Scaled out, that's an estimated **3.26 hours saved per
100 PRs** — a quarter of local-precheck's 12.84 — and it comes entirely from
the long tail, not the typical round.

**This is the opposite ranking from what the North star's ordering
suggests.** Pre-push local checks strictly dominate CI-side auto-fix on this
sample: they remove the round instead of paying to run it once, and never
lose to a fast, attentive developer the way auto-fix does. Auto-fix-and-repush
is worth building only as a backstop for the slow-to-notice tail (the
30-minute-to-hours gaps), not as the primary lever — and even there, its win
depends entirely on the guessed 20s overhead being right; see Limits.

## Limits

`autofix_cost_s` and its savings are **estimated, not measured**: no real
autofixer, commit, push, or re-triggered workflow run was executed. The 20s
push/re-trigger constant is a guess grounded in this repo's own
`ci-convergence` finding of ~0% runner queue delay, not a timed observation;
a slower or self-hosted-runner environment would shift every number here
toward auto-fix's favor. This also assumes every `lint_or_format` failure is
mechanically `--fix`-able, which the underlying classification (job/step name
keyword match) can't verify — some lint rules require a human judgment call,
so the true autofixable subset is smaller than the 24 counted here. It
inherits `local-precheck`'s and `ci-convergence`'s same-repo-branch and
`days_pr_open <= 3` sampling limits.

# CI convergence ground truth

Before prototyping a lever, what does the push → wait → red → fix → push loop
actually cost on real projects with busy GitHub Actions history?

`measure.py` samples real merged pull requests whose head branch lived in the
base repo itself (so `GET .../actions/runs?branch=<ref>` recovers the
branch's full push history, including force-pushes, by branch name rather
than by chasing individual commit SHAs), groups the real workflow runs
triggered on that branch by `head_sha` into chronological rounds — one round
per distinct push that triggered CI — and finds the first all-green round.
For every red round it fetches the real failed jobs' names and step names
(paginated: some of these workflows run 100+ jobs per push) and classifies
the cause by keyword match. Every timestamp, conclusion, job name, and step
name is a real value read from `gh api`; nothing is simulated.

```sh
python3 experiments/ci-convergence/measure.py \
  --repos microsoft/vscode denoland/deno python/mypy sveltejs/svelte pola-rs/polars \
  --sample-prs 20
```

## Measured result

100 real merged PRs, 20 each from 5 busy public repos ([full
results](results.json)) — TypeScript (vscode), Rust+TS (deno), Python
(mypy), a JS framework (svelte), and a Rust dataframe engine (polars), all
same-repo branches so force-pushed rounds aren't silently dropped:

- **Most PRs never see red at all.** 78.4% of the 97 PRs that reached a
  green round did so on their very first round (median rounds-to-green = 1,
  mean 1.4, p90 2; one outlier needed 10). The push → wait → red → fix loop
  this repo's North star describes is real, but it's the minority case, not
  the median one.
- **3/100 sampled PRs (all in `pola-rs/polars`) merged without ever showing
  an all-green round** in the observed window — one after 5 straight red
  rounds, two after a single red round with no retry visible at all. Every
  case had the same shape: one failing check (`Test Python` or similar)
  alongside an otherwise-green suite, merged anyway. Convergence-to-green
  is not universal; some real projects merge through a specific known-red
  check rather than waiting it out.
- **For PRs that took more than one round** (n=46 of 97, non-bot,
  same-repo), median wall-clock from first push to green is **1871s
  (~31 min)**; restricted to PRs opened and merged within 3 days — excluding
  long-lived/stacked branches where the gap between rounds reflects review
  latency, not fix time — median wall-clock drops to **1245s (~21 min)** on
  the still-real n=46 multi-round sample. Of that wall-clock, CI is actually
  *running* about 80–84% of the time (median CI execution 969–1720s); the
  remaining **16–21%** is the gap between one round finishing and the next
  starting — the developer noticing, fixing, and re-pushing.
- **Runner queue delay is ~0%** of CI execution time across this sample:
  these repos have enough Actions capacity that a triggered run starts
  essentially immediately. A lever aimed at queue time would have nothing to
  cut here; it might matter on capacity-constrained self-hosted runners this
  sample doesn't cover.
- **Why a round was red** (46 classified red rounds, real job/step names —
  not a small manual subsample): **lint/format 47.8%**, **test 39.1%**,
  **type-check 8.7%**, build/compile 2.2%, unclassified 2.2%. Lint/format
  and type-check together are the majority (56.5%) of red rounds and are
  exactly the class a deterministic, no-fixture-needed local check
  reproduces — the premise [`local-precheck`](../local-precheck/) measures
  against real job durations from this same population.

## Limits

Same-repo-branch PRs are a real subset, not a random sample: GitHub's Actions
API only populates a run's `pull_requests` field (and branch-scoped run
listings) for branches living in the base repository, so a fork-heavy
project's outside-contributor PRs are invisible to this method — the sample
here skews toward maintainer/collaborator branches, which may iterate
differently than first-time fork contributors. Rounds are windowed to
`[PR created_at − 30min, merged_at]`; a branch pushed to privately for days
before the PR opened has that earlier history excluded by design, which
undercounts total rounds for such PRs specifically (not the "how long does
convergence take once the PR exists to be reviewed" question this measures).
Long-lived/stacked PRs are real and are reported (`wall_clock_s_all_converged`),
but they dominate the *mean* by weeks, which is why the median and the
`days_pr_open <= 3` bucket are the numbers to read for "does CI convergence
feel fast." The cause classification is a keyword match over real job/step
names and text, not a semantic diagnosis of the underlying bug — spot-checked
against the raw `red_causes[].evidence` in [results.json](results.json), not
run through a separate human-labeled ground truth.

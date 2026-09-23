# A practical route to meaningful 30-second CI

Make common changes cheap to verify, not every possible change cheap to merge.
The evidence supports a fast lane for bounded changes and exact reuse. It does
not support replacing a large repository's gate with a coverage-based selector.

## What actually worked

| Question | Measurement | What it establishes |
|---|---|---|
| Can a changed polyglot application be checked quickly? | [Continuum, hosted](experiments/continuum/results/hosted/benchmark.json): changed capacity code **11.00s**, four-worker uncached run **12.48s**. Preparation **45.9s** separately. | A small connected Python/TypeScript/Rust/Haskell application can fit. Not a production-scale benchmark. |
| Can reuse survive non-code changes on a real application? | [Marimo input experiment](experiments/python-selection/closure-results.json): 41 real asset tests **4.46s**; five unchanged decisions **0.44–0.53s**; deleted favicon rejected **3.01s**; unsafe public HTML caching rejected **5.14s**. | Explicit resource inputs close the demonstrated coverage-selector hole for this test family. Not the other 12,000 tests. |
| Does coverage selection solve the large suite? | [Marimo baseline](experiments/python-selection/fair-results.json): **797.5s** pytest time, 11 failures; unchanged selection still **62.3s**, rerunning those failures. Deleted favicon selected **zero** tests. | No healthy whole-suite baseline or safe 30-second gate was established. |
| Can hard failure modes be tested without a browser? | [Revocation](experiments/revocation/replay.json) reproduces a real database leak and bounded repair three times. [Outbox](experiments/outbox-concurrency/results/replay.json) uses two processes and real SIGKILL recovery. | Controlled interleavings expose specific failures; database-local effects are not external exactly-once guarantees. |
| What does a merge queue buy over testing the PR branch alone? | [Merge-queue safety](experiments/merge-queue-safety/results.json): one run, 90 real pytest merges evaluated at `--collect-only` (38 both passed, 31 both failed, 1 reverse, 20 unresolved — buckets sum to 90) plus a bounded real-execution oracle on 10 of those merges (selections) and 4 (full suite). **0/90** at every oracle showed a PR head that passed while the real merged tree did not; every `both_failed` real-execution case failed the identical test set on both trees. A [seeded disjoint-change conflict](experiments/merge-queue-safety/seeded-conflict-results.json) confirms the mechanism: PR head passes, change-scoped selection on the merged tree still misses it, full-suite and concurrency-aware selection both catch it. | The real sweep found no instance in this sample at either oracle — a lower bound on detectable conflicts (the real-execution oracle covers only 10-40 of the 90 merges), not proof merges are safe. The seeded case shows a merge queue (or selection aware of concurrent changes) closes a real blind spot that PR-diff-only selection does not. |
| How much exact-input reuse survives a rebase? | [Rebase reuse](experiments/rebase-reuse/results.json): 60 sampled stacks replayed onto a later real base of pytest's own history; 42/60 rebases conflicted outright, and of the 21 task/sample pairs the stack itself touched, 14 (66.7%) still hash-identical after a clean rebase. | A commit-SHA-keyed cache reruns every task a rebased stack touches; an input-hash-keyed cache can skip two-thirds of that rework. Conflict rate is specific to this repo and sampling, not a general figure. |
| Does a manifest's declared inputs miss a real dependency? | [Input drift](experiments/input-drift/results.json): tracing real `open()` calls during this repo's own 5 tasks finds zero undeclared reads; the same trace against a fixture with a deliberately undeclared import reports recall 0.75, correctly naming the missing file. | The traced hole this repo's own selection experiments flagged conceptually is now directly detectable, at least for `open()`-level reads. It is not a full syscall trace. |
| What does quarantine save, and can it hide a real failure? | [Flake accounting](experiments/flake-accounting/results.json): 30 campaigns of 15 real repeats; a flip-based quarantine policy triggers on every flaky campaign (mean run 3.23/15) for a 78.4% run reduction, and triggers on 0/30 campaigns of a deterministic failure. | Quarantine can pay for itself without ever misclassifying a consistent failure as flaky, for this fixture's failure profile. Whether it would hide a real regression injected mid-quarantine is unmeasured. |
| Does test order change time to the first real failure? | [Failure ordering](experiments/failure-ordering/results.json): five real `-x` orderings on pytest's own 4,556-test suite against a real regression (PR #14934); default takes **10.47s**/688 tests to the first failure, `duration_asc` **1.76s**/1,032 tests, `recently_failed`/`historical_rate` **0.91-0.95s**/1 test. | The agent's iteration loop is bounded by wall time, not test count: `duration_asc` runs more tests than default but stops 5.9x faster by front-loading cheap ones. History-based ordering wins outright when a regression recurs a prior run's failure, which this scenario is built to do. |
| How much of a failing log does localizing the failure actually need? | [Failure context budget](experiments/failure-context-budget/results.json): on the same regression's real 387-line default log, finding which test failed needs a median of **374** lines read from the top vs. **14** from the bottom; the compact `--tb=line` mode needs 95 from the top vs. 14 from the bottom. Structured JUnit XML's `<failure>` content is 13.8KB inside a 549KB file. | Almost the entire log is noise if read top-down; pytest's own bottom summary section is where the test name actually is, regardless of traceback verbosity. Naively handing an agent the whole JUnit file is worse than the plain-text log unless it's parsed and filtered first. |
| Does batching or speculation help at agent-driven PR volumes? | [Merge-queue throughput](experiments/merge-queue-throughput/results.json): real per-commit durations from 64 `itsdangerous` checkouts drive a policy simulator. At 0% broken, batch-8 uses **8.59 CI-s** vs serial's **64.58s** (7.5x); at 30% broken it flips to **66.04s** — worse than serial. Speculative-4 clears the queue in **24.23s** vs serial's **64.58s** at 10x arrival volume, for a **31% CI-minutes tax** at 30% broken. At pytest's real measured push-CI failure rate (**1.94%**, [results-real-rate.json](experiments/merge-queue-throughput/results-real-rate.json)), the crossover never appears and batch-8 wins every axis (**17.41s** CI-time vs serial's 64.58s). | Batching and speculation trade CI-minutes for wall-clock in opposite directions; the synthetic crossover is real but sits far above one real large project's own historical broken rate. |
| Does a shared receipt store beat a per-branch cache for concurrent agents? | [Sibling evidence](experiments/sibling-evidence/results.json): 150 real pytest sibling-branch windows per size. Task collisions rise from **20.7%** of windows (2 siblings) to **80.0%** (16 siblings), but a cross-branch shared store recovers at most **0.17%** beyond a cache that never looks past its own branch. | Concurrent real agent commits that touch the same task essentially never produce byte-identical content; the store has almost nothing to find. The lever is reducing collision surface, not building a fancier cache. |
| How does the affected-task fraction scale with real diff size? | [Diff-affected fraction](experiments/diff-affected-fraction/results.json): 250 real pytest commits; **52.8%** touch no test-relevant path at all. Of the **118** that do, **84.7%** changed 6 or fewer files with median affected fraction **≤3.5%**, climbing to **85.4%** past 30 files (n=2). | Small real diffs affect a small slice of a real explicit-input manifest — but that narrowness is a property of diff size, not something a fast lane gets automatically at any size. |
| Can a real per-layer cache cut real stack convergence time? | [Stack layer cache](experiments/stack-layer-cache/results.json): a working content-addressed receipt cache, not a simulation — real `pytest` subprocesses, real git blob-hash fingerprints. Across 25 sampled real `itsdangerous` stacks per gap size, real task reuse after a real fix landed underneath an already-green stack stayed **89.7%-100%**; mean real time saved per amendment was **0.83-1.04s** against a naive rerun-everything baseline. Rebase conflicts rose **48%→84%** as the gap widened. | The mechanism, not just the measurement, exists and works: point it at a real stack twice and the second run is provably faster. Conflicts, not cache misses, are what limit how often it gets to prove that on this small a package. |
| How much of real CI convergence is developer wait, and on what? | [CI convergence](experiments/ci-convergence/results.json): 100 real merged PRs, 5 busy public repos; 78.4% converge on round 1 (median rounds-to-green 1). For PRs needing more than one round, median wall-clock is 1245s (fast-iteration subset) with 16–21% of it spent between rounds, not running CI. Real red-round causes: lint/format 47.8%, test 39.1%, type-check 8.7%. 3/100 sampled PRs merged without ever showing an all-green round. | The push→wait→red→fix loop is real but is the minority path; the majority of red-round cost is fixable-before-CI (lint/format + type-check), the target the next two experiments measure against. |
| What would a pre-push local check have saved? | [Local pre-check](experiments/local-precheck/results.json): 200 real merged PRs; 27 locally-catchable (lint/format or type-check) red rounds found (13.5/100 PRs), using the real failing job's own duration as the local-check proxy. Median savings 846s/caught round; an estimated 12.84 developer-hours saved per 100 PRs. | A pre-push hook running only the linter/type checker — not the full suite — would have caught the majority-class real CI failure before it ever reached CI, using real job timings rather than an assumed check duration. |
| Does CI-side auto-fix-and-repush beat a pre-push hook? | [CI auto-fix](experiments/ci-autofix/results.json): the same 200-PR sample's 24 lint/format rounds, modeling CI auto-fix cost as real job duration plus an **estimated (not measured)** 20s push/retrigger overhead against the real measured human fix gap. Only **5/24 (20.8%)** have positive savings; median **−141.5s**; mean **+976.5s** driven by a long tail of slow rounds; ~3.26 hours saved per 100 PRs, a quarter of local-precheck's figure. | A negative result for the typical round: developers who are already reasonably fast beat a full CI round-trip. Auto-fix bots are worth building only as a backstop for the slow-to-notice tail, not as the primary lever — reversing the North star's suggested lever ordering on this sample. |
| How much of a red round's wall-clock is spent after the outcome is already known? | [Fail-fast signal](experiments/fail-fast-signal/results.json): 68 real multi-job red rounds across 5 repos; median time to first known failure **296.5s**, median time for the whole round to go quiet **1489s** — a median **704.5s tail**, averaging **66.5%** of total settle time. Only 4.4% of rounds have no tail. | A developer or notification system acting on first-red instead of "wait for all checks" recovers roughly a third to a half of red-round wall-clock today, with no execution changes needed — only when the signal is read. |
| What does first-failure-first cancellation buy in real CI compute, not developer wall-clock? | [Cancel-on-red](experiments/cancel-on-red/results.json): 46 real multi-job red rounds; median real compute per round **16,140.5s**, median compute saved under an instant-cancel-on-red policy **9,566.5s** — **58.5%** of fleet-wide job-seconds on these rounds never needed to run. Repo-dependent: **23.1%** (vscode, few independent workflows) to **80.5%** (polars, 33-36-job matrix). | Cancellation and first-failure-signal are complementary levers on the same red rounds: one recovers developer wait, the other recovers CI spend, and the size of the compute win tracks matrix width directly. |

The initial blind Continuum campaign caught **6/8** defects. Later 8/8 is
regression coverage, not independent defect recall. Host contention also broke
the latency target. Neither result should disappear behind the fastest timing.

## What to build

1. **Reusable checks with explicit inputs.** Bind results to code, assets,
   configuration, toolchain, verifier and dependency outputs. A protected worker
   issues evidence; merge checks the actual merged tree. New or missing inputs,
   policy changes and unknown outcomes force execution or a broader lane. Use a
   mature build engine, not Assay's runner. [Bazel's action/cache model](https://bazel.build/remote/caching)
   supplies the mechanism and explains why cache write authority matters.
2. **Business checks below the browser.** Exercise real operations and durable
   state against an independent model. Cover denied operations, races, crashes
   and compatibility. Retain browser tests for composition, login and critical
   journeys; stop expressing every business permutation through clicks. This
   verification interface can sit above any implementation language.
3. **Protected boundaries for irreversible harm.** Restrict database roles and
   capabilities; enforce atomicity and uniqueness in the database. Keep credential
   routing and verification outside untrusted implementations. The revocation
   counterexample shows that merely enabling row security is insufficient.
   Authorization and destructive migration changes need their broader checks
   *before* exposure, regardless of the thirty-second target.
4. **Fresh checks after deployment.** Deploy the checked bytes, observe a small
   rollout, and stop or roll back only when rollback is compatible. Support needs
   observed facts, containment, explicit unknowns and a responsible human—not an
   approval button that converts missing evidence into safety. Continuum models
   this workflow but does not integrate a real support or approval system.

Pin the runtime contract that matters, not the entire production environment
forever. Verify old/new coexistence when that contract changes.
[Hermeticity](https://bazel.build/basics/hermeticity) means controlling observations,
not banning migrations. Record query plans and work counts too: low latency on
a small fixture can hide a growing scan.

## A one-week adoption trial

Choose one frequently changed vertical slice with a stable data/effect boundary.
Keep the existing gate authoritative. Run the new path alongside it and record
omitted checks, reuse reasons, discrepancies, setup costs and decision times.
Include deliberate cross-tenant access, duplicate effects, stale permissions,
asset deletion, incompatible migration and query-work regression.

Only retire redundant checks after investigating discrepancies and qualifying
the safety obligations on the target runtime. Measure the fraction of changes
eligible for the fast lane and its p95; report queue/setup costs separately.
Shared infrastructure changes may remain slow. The current experiments do not
establish that adoption criterion for an existing production application.

## What remains open

The five previously local experiments are now published. Database and container
experiments have fresh replays; selection retains its negative baseline.
[Transport controls](experiments/transport/) are reproducible, but the original
hosted reset remains unclassified. A fresh four-worker hosted replay passed its
62 browser tests without retries but took **72.92s** for the complete gate.
Later green runs are not a diagnosis or a thirty-second result.

The new input experiment uses trusted local receipts, not secure attestations.
Dependency versions do not identify every installed byte, and there is no
isolated issuer or protection against concurrent host mutation. Whole-repository
scaling, production canaries and real human escalation are still unvalidated.
This is a tested design direction, not a finished production CI service.

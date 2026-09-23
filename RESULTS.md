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
| What does a merge queue buy over testing the PR branch alone? | [Merge-queue safety](experiments/merge-queue-safety/results.json): 90 real pytest merges evaluated (48 resolved cleanly, 12 unresolved by an environment incompatibility); **0** showed a PR head that collected cleanly while the real merged tree did not. A [seeded disjoint-change conflict](experiments/merge-queue-safety/seeded-conflict-results.json) confirms the mechanism: PR head passes, change-scoped selection on the merged tree still misses it, full-suite and concurrency-aware selection both catch it. | The real sweep found no instance in this sample at `--collect-only` granularity — a lower bound on detectable conflicts, not proof merges are safe. The seeded case shows a merge queue (or selection aware of concurrent changes) closes a real blind spot that PR-diff-only selection does not. |
| How much exact-input reuse survives a rebase? | [Rebase reuse](experiments/rebase-reuse/results.json): 60 sampled stacks replayed onto a later real base of pytest's own history; 42/60 rebases conflicted outright, and of the 21 task/sample pairs the stack itself touched, 14 (66.7%) still hash-identical after a clean rebase. | A commit-SHA-keyed cache reruns every task a rebased stack touches; an input-hash-keyed cache can skip two-thirds of that rework. Conflict rate is specific to this repo and sampling, not a general figure. |
| Does a manifest's declared inputs miss a real dependency? | [Input drift](experiments/input-drift/results.json): tracing real `open()` calls during this repo's own 5 tasks finds zero undeclared reads; the same trace against a fixture with a deliberately undeclared import reports recall 0.75, correctly naming the missing file. | The traced hole this repo's own selection experiments flagged conceptually is now directly detectable, at least for `open()`-level reads. It is not a full syscall trace. |
| What does quarantine save, and can it hide a real failure? | [Flake accounting](experiments/flake-accounting/results.json): 30 campaigns of 15 real repeats; a flip-based quarantine policy triggers on every flaky campaign (mean run 3.23/15) for a 78.4% run reduction, and triggers on 0/30 campaigns of a deterministic failure. | Quarantine can pay for itself without ever misclassifying a consistent failure as flaky, for this fixture's failure profile. Whether it would hide a real regression injected mid-quarantine is unmeasured. |

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

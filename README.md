# Assay

Can a thirty-second decision provide enough evidence to merge a production change?

This repo tests parts of that argument against real Python, TypeScript, PostgreSQL,
browser, and authorization-policy implementations. It retains counterexamples and
negative results. It is not a production CI replacement.

Start with [the findings and adoption path](RESULTS.md): what worked, what failed,
and which checks could replace the business-rule burden of E2E tests.

The newest connected experiment is [Continuum](experiments/continuum/): exact-input
evidence reuse across a polyglot application, environment transitions, deployment
faults, and a support handoff. `./continuum view` opens its recorded evidence locally;
the experiment includes both missed defects and infrastructure failures.

Start with the [bounded business cell](experiments/cell/). It connects a real
React/API/database feature to an authority-limited actor, independent state-model
checks, protected-baseline invalidation, and query-cost evidence. `./cell` reads
the recorded results; after `./lab prepare` and `./cell prepare`, `./cell qualify`
establishes a local baseline;
`./cell check experiments/cell/candidates/refactor.py` exercises its fast lane
with a changed implementation.

Five complete laptop gates took 20–23 seconds; serial execution took 42–49.
The first hosted run took 105.5 seconds and rejected a browser transport failure.
A subsequent two-worker hosted run passed all three repetitions in 111–114
seconds. Thirty seconds is not established across environments.
[Local and hosted evidence](experiments/fullstack/results/).

```sh
./lab                 # read recorded evidence; no installation or execution
./lab show gate
./lab show policy
./lab show isolation
./lab show scale
```

## What to inspect

| Question | Experiment |
|---|---|
| Can architecture reduce what each change must re-prove? | [Business cell](experiments/cell/): actual UI composition, bounded effects, 60-command reference-model replay, adversarial candidates, and a baseline that rejects trusted-boundary changes. |
| Can a whole small application be checked quickly? | [Full-stack gate](experiments/fullstack/): API and browser suites, independent behavior checks, both languages' type checkers, and a production build. No test selection or result cache. |
| Are the tests asking the right questions? | [Blind faults](experiments/fullstack/HOLDOUT.md): a seeded signup-privilege defect survives both original suites. Added lifecycle requirements catch it. |
| Where do language boundaries fail? | [Wire and browser probes](experiments/boundaries/): accepted TypeScript inputs, HTTP errors, persisted state, pagination, and a causally reproduced signup race. |
| Can some safety properties be checked symbolically? | [Cedar](experiments/cedar/): real Lean/CVC5 policy implications, including a positive-access obligation that rejects deny-all. |
| Can the database prevent a class of leaks? | [Tenant isolation](experiments/isolation/): real restricted roles, forged context and bypass counterexamples, constraints, and query plans at two scales. |
| What happens after a crash or during deployment? | [Outbox](experiments/outbox/) and [rolling schemas](experiments/compatibility/): bounded schedules replayed against Python/TypeScript/PostgreSQL, old/new clients, stale backfill, and lock conflicts. |
| Does the result survive a larger codebase? | [marimo](experiments/marimo/): 6,505 frontend tests take about 101 seconds. Narrow selection helps; broad dependency closures do not fit thirty seconds. |
| Can executed-code coverage safely select tests? | [Python selection](experiments/python-selection/): the large baseline still fails, and deleting an asset selects zero tests despite a direct failure. Do not treat this as a working fast gate. |
| Can a revoked reader see newly changed data? | [Revocation](experiments/revocation/): a real PostgreSQL race and a bounded locking repair, repeated three times. |
| Do workers survive actual concurrency and process death? | [Concurrent outbox](experiments/outbox-concurrency/): two Node processes, observed database lock barriers, and SIGKILL before/after commit. |
| Can candidate code manufacture a pass? | [Confinement](experiments/confinement/): real container boundaries and deliberately exposed positive controls; the business oracle is intentionally trivial. |
| Was the hosted network flake explained? | [Transport](experiments/transport/): two causal controls pass; the original hosted reset remains unclassified. |
| What does a merge queue buy, and does selection narrowness change it? | [Merge-queue safety](experiments/merge-queue-safety/): 0/90 real pytest merges show a PR head that passed while the real merged tree did not, at either a collect-only or a bounded real-execution oracle; a seeded disjoint-change conflict confirms change-scoped selection misses exactly this case while a merge-queue check and concurrency-aware selection both catch it. |
| What does exact-input reuse survive across a rebase? | [Rebase reuse](experiments/rebase-reuse/): replaying real pytest commit stacks onto a later real base, 66.7% of the tasks a stack itself touches still hash-identical after rebase; 70% of sampled rebases conflicted outright. |
| Does a manifest's declared inputs miss what a task actually reads? | [Input drift](experiments/input-drift/): tracing real file opens finds zero undeclared reads across this repo's own manifest, and correctly flags a seeded missing import in a fixture. |
| What does a quarantine policy save, and does it hide real failures? | [Flake accounting](experiments/flake-accounting/): from repeated real runs, quarantining after a flip saves 78.4% of a flaky task's CI runs while never triggering on a deterministic failure. |
| Does test order change how fast an agent sees the first real failure? | [Failure ordering](experiments/failure-ordering/): five real orderings against a real pytest regression; every alternative to default collection order reaches the failure 5.9x-11.5x faster in wall time, real `-x` runs on a 4,556-test suite. |
| How much of a failing CI log does an agent need to read to localize it? | [Failure context budget](experiments/failure-context-budget/): on the same real regression, reading a real log from the bottom instead of the top cuts the lines needed to identify the failing test 26x (long traceback) to 6.8x (`--tb=line`); a naive whole-JUnit-XML dump is larger than either raw log. |
| Do batching and speculation actually help at agent-driven PR volumes? | [Merge-queue throughput](experiments/merge-queue-throughput/): real per-commit `pytest` durations drive a policy simulator; batching cuts CI-seconds 7.5x at a clean stream but flips CI-negative past a broken-rate threshold, while a speculative queue buys wall-clock at a rerun tax that scales with that same rate. At pytest's own real historical broken rate (1.94%), the crossover never appears and batching wins outright. |
| Does a receipt store need to look across sibling branches, or just its own? | [Sibling evidence](experiments/sibling-evidence/): real concurrent pytest branches off one base collide on a declared task 20.7%–80.0% of the time as concurrency rises 2x to 16x, but a cross-branch shared store recovers at most 0.17% beyond a cache that never looks past its own branch. |
| How does the affected-task fraction scale with real diff size? | [Diff-affected fraction](experiments/diff-affected-fraction/): 84.7% of real code-touching commits change 6 or fewer files, with median affected fraction under 3.5%; it climbs to a majority of the manifest only past 30 files. |
| Can a real per-layer cache cut real stack convergence time? | [Stack layer cache](experiments/stack-layer-cache/): a working content-addressed receipt cache reuses 89.7%-100% of a real stack's task evidence after a real fix lands underneath it, actually rerunning `pytest` only for what changed; rebase conflicts (48%-84%, rising with gap), not the cache, are the binding constraint. |

The [design argument](docs/verification.md) connects the measurements to primary
research and identifies the assumptions that still need to hold in a real system.
Each experiment keeps its runnable source, evidence, and limits together.

## Run it yourself

For a fresh clone, install Docker, uv, and Node **22.12.0**, then:

```sh
./lab prepare
./lab replay gate
./lab replay isolation
./lab replay outbox
./lab replay compatibility
./lab prepare --stop
```

Preparation downloads pinned public dependencies and starts disposable local
PostgreSQL/Mailpit services. It is separate from warm verification time.
The local setup state stays in ignored `out/lab/`; occupied service ports are
not silently reused. Linux may also need Playwright's Chromium system dependencies.
Cedar needs its separately documented image build.

Public fixture code and package installation execute on the host: use a disposable
development machine for unfamiliar candidates. See [trust boundaries](SECURITY.md).
Hosted workflows preserve freshly generated artifacts, not copies of local results.

For the dependency-free queue/scheduler/SQL demo, run `./demo`, then
`./demo inspect`. It includes event-by-event state changes, query plans, work
counts, and a Perfetto trace; it is a smaller model, not a benchmark of the public
applications above.

[Public provenance](docs/PRIOR_ART.md) · [Original runner design](docs/DESIGN.md)

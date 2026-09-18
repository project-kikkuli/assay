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

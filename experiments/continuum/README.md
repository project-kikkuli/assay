# Continuum

Can verification follow a change from an agent's workspace through merge,
deployment, and a support incident—without rerunning everything at every boundary?

This is one connected, synthetic application: a Python/SQLite reservation service,
a TypeScript/Effect fulfillment worker, a browser client, an independent Rust ledger
auditor, and a Haskell deployment policy. It is not a claim that these four languages
need the same testing strategy. The common interface is evidence about an artifact,
an environment, and an obligation.

## Result

The [hosted campaign](https://github.com/project-kikkuli/assay/actions/runs/35374985310)
passed on commit `c8ca5c2`. These are complete decisions for this bounded application,
not extrapolations to a large repository:

| Scenario | Hosted seconds | Reused actions |
|---|---:|---:|
| All actions, one worker | 20.731 | 0/10 |
| All actions, four workers | 12.480 | 0/10 |
| All actions, eight workers | 12.815 | 0/10 |
| Identical target, five repetitions | 3.450–3.460 | 10/10 |
| Changed capacity implementation | 10.997 | 4/10 |
| Changed runtime | 8.717 | 5/10 |

[Raw hosted records](results/hosted/benchmark.json) include action start times,
dependencies, execution times and capture overhead. The fresh preparation took
45.9 seconds; the entire multi-experiment job took 232 seconds. Neither is hidden
inside a claim of “30-second CI.” [Local measurements](results/benchmark.json)
show substantial contention, including an 82-second serial run. No p95 is established.

Seven admission attacks were rejected, and the actual post-acceptance worker-crash
scenario recovered without duplicating the fixture's delivery. Eight known defects
were caught after verifier repair; the initial blind result was six of eight.

## Run and inspect

Requires Docker, Python 3.12+, Node 22, Rust, and OpenSSL with Ed25519 support.
GHC runs in a pinned container. Preparation downloads images and dependencies;
it is outside the measured verification interval.

```sh
./continuum prepare
./continuum check --no-cache   # compile, exercise real services/browser, migrate, audit
./continuum check              # reuse matching operator-issued evidence
./continuum faults             # independently authored semantic defects
./continuum lifecycle          # stale evidence attacks, actual worker death, recovery
./continuum bench              # serial/parallel, five cache hits, source/runtime changes
python3 experiments/continuum/performance.py  # actual SQL work at 1k/10k/100k rows
./continuum view               # open http://127.0.0.1:8765/view.html
```

Linux may need `node experiments/continuum/client/node_modules/@playwright/test/cli.js install-deps chromium`.
Private operator keys and captured candidates stay in ignored `out/continuum/`.
Only use synthetic candidates on a disposable development machine.

## What the gate means

The [contract](contract.json) defines business operations independently of their
implementation. [The verifier](verify.py) sends real HTTP requests and checks stored
state, including rejected operations. Browser testing is retained for one wiring
journey; it does not carry the burden of proving every business rule.

The [action graph](run.py) binds source, dependencies, compiled outputs, verifier,
and environment to signed receipts. Descendants bind the parent's result as well
as its input key. Admission reconstructs the target plan; it does not accept a
candidate's list of supposedly sufficient tests. Changed merge trees and runtime
profiles invalidate affected evidence. The deployed byte inventory must match the
verified package. An inconclusive execution cannot become a cached pass.

This borrows explicit action inputs and output addressing from
[Bazel](https://bazel.build/remote/caching), and delegated policy verification from
[SLSA's verification summaries](https://slsa.dev/spec/v1.2/verification_summary).
Signatures establish who asserted what—not whether the assertions are adequate.
A separate operator is required in a real deployment: this local signing fixture
does not protect against an agent or administrator with access to the host key.

## Verification continues after merge

`lifecycle` checks admission against a newly captured target, then runs a fresh
synthetic deployment. It kills the real worker after the provider accepted a
delivery but before acknowledgement. The application must remain recoverable
without delivering twice. Provider acceptance is an explicit barrier, not a sleep.

The resulting support packet records observed state, containment, unknowns,
forbidden shortcuts, and the evidence a human needs before authorizing recovery.
An unknown outcome cannot be promoted by a generic “approve”; security findings
and incompatible rollback require containment and escalation. Fixture-owned
provider records justify the demonstrated retry. No actual human approval or
external support integration is claimed.

[TigerBeetle's liveness testing](https://tigerbeetle.com/blog/2023-07-06-simulation-testing-for-liveness/)
motivates checking recovery after faults, not merely absence of corruption.
[Google's canary guidance](https://sre.google/workbook/canarying-releases/) motivates
fresh, release-attributable observations: old test evidence cannot establish the
health of a live deployment. The demo is a synthetic transaction, not a statistical
production canary or an uptime guarantee.

## Read the negative results first

[The initial blind campaign](results/faults-initial.json) caught six of eight faults.
It missed wrong stored quantities and cancellation with no effect. Subsequent
checks against those same faults are regression checks, not a new blind evaluation.
[A loaded-host run](results/loaded-host-negative.json) exceeded thirty seconds and
withheld admission on a policy timeout. Faster isolated runs do not erase it.

[The performance probe](performance.json) found capacity-query work growing from
3,472 to 330,172 SQLite VM instructions. Candidate indexes held it at 250 while
preserving the checked capacity and pagination outputs. This is cost evidence,
not a deployed schema change, load test, or existing admission budget.

Current measurements are in [results](results/). Source capture and admission are
included; downloads, hosted queue time, and real production observation windows
are not. Timing is diagnostic, never a correctness assertion. Eight mutants do
not estimate production defect recall, and a handful of runs cannot establish p95.

The environment boundary is deliberately smaller than “identical dev and prod”:
pinned application runtimes, captured dependencies, fixture-controlled time and
external effects. The migration check uses the same populated database through
current → next → current Python environments. It does not establish arbitrary
destructive-schema rollback compatibility.

Unlike [FoundationDB's deterministic simulation](https://www.foundationdb.org/files/fdb-paper.pdf),
this harness does not control kernel/thread scheduling. Host Rust and browser
toolchains are only partially identified, input declarations are handwritten,
and Docker/host failures remain possible. There is no proof of comprehensive
sandboxing, complete input closure, or non-flakiness. A production adopter should
reuse a mature build engine and isolate its evidence issuer, not ship this runner.

## What is worth adopting

The promising unit is a **verified change**, not a test job. Agents request checks
while working; merge recomputes obligations for the actual merge tree; deployment
consumes the verified artifact and gathers fresh observations. Preparation stays
off the fast path. Four workers helped here; eight did not. Evidence reuse made
the larger difference, without treating a changed business implementation as an
unchanged target.

Replace the *business-rule burden* of large E2E suites with independent state and
wire-contract checks against real implementations. Retain browser composition,
real authentication/database integration, and broader audits. The blind misses
show why agent-written passing tests alone are inadequate.

For support, retain one chain: observed incident → contained effects → explicit
unknowns → responsible human → executable regression → fresh deployment evidence.
A human should resolve missing facts, not supply an override that turns unknown
into safe. This demo creates the packet and replays its crash scenario; it does
not integrate a ticket system, authenticate approvers, or automatically minimize
production incidents.

The next production experiment should shadow one real vertical slice alongside
its existing gate, compare omissions and outcomes, and only then retire redundant
checks. Keep these gaps explicit:

- A protected issuer, policy ownership and complete execution inputs are required
  before trusting evidence from autonomous agents. This local fixture is not that service.
- Tenant checks here are tested application logic, **not confidentiality by
  construction**. That requires a separately protected capability/data boundary;
  neither cached tests nor post-deploy detection can make a leaked secret un-leak.
- The target database, real identity provider, destructive migrations, resource
  exhaustion and production canary signals need their own qualification. SQLite
  work counts are not a production latency budget.
- One small application, eight seeded defects and a few timings do not establish
  whole-repository scaling, defect recall, a latency percentile, or an uptime guarantee.

## Provenance

All domain code and data here are synthetic. No external application was copied.
[environment.json](environment.json) pins upstream Python, Node and Haskell image
digests; those images retain their upstream component licenses. The npm lockfile
pins Effect 3.22.2 (MIT), TypeScript 5.9.3 (Apache-2.0), Playwright 1.62.1 (Apache-2.0),
and Node types 22.18.6 (MIT). SQLite is public domain. The Rust auditor and Haskell
policy use only their standard libraries. Host toolchain versions enter receipts;
the hosted workflow selects Rust 1.91.1, Python 3.12.9, and Node 22.12.0 explicitly.

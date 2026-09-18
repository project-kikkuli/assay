# A bounded business cell

The experiment is not "select fewer tests." It changes what business code is
allowed to do, so some changes cannot invalidate some safety properties.

The subject is the Items feature of the pinned, MIT-licensed
[FastAPI full-stack template](https://github.com/fastapi/full-stack-fastapi-template/tree/cb740b656d7a0a6c5e12c7bf8e50343ec94ee9c7):
React/TypeScript, its generated API client, Python/FastAPI, and PostgreSQL.
The existing [public-fixture repairs](../fullstack/repaired.patch) apply.

Five fresh actor-only checks took **2.34–2.61 seconds**, including original
migrations, actor startup, 14 fixed obligations, 60 generated commands, and
cleanup. Initial qualification took 38.61 seconds serially: 30 boundary tests,
three healthy replays, 14 challenges, and original/cell browser composition.
The cell-backed browser run itself took 10.02 seconds and exercised 22 actor
requests. These are warm-dependency laptop measurements, not a hosted CI SLA.
[Raw evidence](results/).

## Where the trust goes

An actor receives a command and returns `{op, target, fields}`. It has no database
connection, token, host source tree, or network. A persistent, resource-bounded
container runs it. The [kernel](kernel.py) binds the operation and target to the
request, rejects unknown fields, and executes SQL under a restricted tenant LOGIN
role. Forced RLS binds ownership to that role, not a user-settable session value.
Authentication stays in the public application. Administrative operations stay on
the original, protected path.

This is not a proof that arbitrary Python is safe. The kernel, gateway, Docker
daemon, host, database configuration, authentication, and installed dependencies
remain trusted. Tenant roles per user demonstrate authority separation; they are
not a production connection-pool design. The gateway currently serializes regular
item requests. Do not infer production throughput from verification latency.

The kernel intentionally accepts valid-but-wrong field values. The independent
[behavioral verifier](behavior.py) checks responses against committed database
state: exact content, partial updates, null handling, paging, owner isolation, and
deletion. A separate [reference model](state_machine.py) generates a replayable
60-command sequence across two accounts and compares real committed state after
every command. These tests can still miss business requirements. The actor cannot
declare its own success.

## What a fast result means

Only the nominated actor may change. Changes to protected inputs require broader
verification; missing input and execution evidence never mean success. The
[scope check](admission.py) has no heuristic dependency selection. Its baseline
must be supplied by a trusted operator/base-revision job, not by the candidate.
This repository does not implement a hosted protected-baseline service, signing,
or merge authorization.

That distinction is the proposed E2E replacement: establish the real composition
once for a protected boundary, then check changed business implementations against
that boundary's obligations. It does **not** justify skipping browser checks when
the UI, generated client, authentication, schema, or gateway changes. This actor
currently implements a Python CRUD policy; the application around it is polyglot.
General workflow logic, concurrent transitions, and browser-only regressions need
additional boundaries and verifiers.

The fast check runs real SQL, fresh original migrations, and a new actor process.
It does not reuse test results. Dependencies, the container image, and PostgreSQL
must already be prepared. Queueing and cold installation are outside this timer.
The composition check exercises the real UI and authentication separately; UI,
gateway, kernel, schema, or toolchain changes cannot skip it using actor evidence.

## Inspect the evidence

`run.py --repeat 3 --challenges` records every command/proposal, each independent
obligation, observed container controls, source identities, elapsed time, and
cleanup. Reports start unknown and are replaced on failure. Candidate timeouts
remain unknown even when deliberately triggered by the qualification challenge.

The original ten challenge programs were authored without reading the verifier.
They are manually seeded faults, not a production-defect recall estimate. The
wrong-target-only variant is a later correction: the original wrong-target
program already fails on its incorrect operation. The environment-read probe
attempts a real read but has no exposed-canary positive control here; do not
interpret its normal result as an exhaustive noninterference proof. Container
positive controls are in the separate [confinement experiment](../confinement/).

After `./lab prepare`:

```sh
./cell qualify
./cell check experiments/cell/candidates/healthy.py
./cell check experiments/cell/candidates/attack_uppercase_title.py  # rejects
./cell trace attack_drop_patch_field.py
```

Qualification runs the boundary tests, healthy/model replays, all challenges, and
both original and cell-backed browser suites. It then writes a local baseline to
`out/cell/baseline.json`. This is an operator action, never a candidate-controlled
CI step. A failed qualification invalidates the previous local baseline.

The [performance probe](performance.py) runs the kernel's real count/page queries
as the restricted role at 100 and 100,000 rows. Read the plans as well as timings:
an ownership/ordering index prevents unrelated tenants' rows from becoming scan
work. This index is measured in a disposable fixture, not silently installed by
the application. Run it with the prepared Python interpreter; its report includes
setup, index construction, work counts, buffers, and cleanup.

Fresh reports go to ignored `out/cell/`. Recorded measurements belong in
`results/`, with their exact source identities. This is a small CRUD feature,
not evidence that an arbitrary monolith can finish verification in thirty seconds.

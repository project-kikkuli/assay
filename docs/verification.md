# What could make a thirty-second decision trustworthy?

Not one faster test runner. A small, explicit set of obligations whose evidence
can be checked independently, with a broader lane when the obligations cannot
be discharged. Thirty seconds is a latency target, not permission to omit a
security check or reinterpret a timeout as success.

## Put guarantees where violations can be prevented

The [database experiment](../experiments/isolation/) makes omitted tenant filters
safe under a restricted role, then demonstrates the limits: a caller-controlled
tenant setting is forgeable; an owner or `BYPASSRLS` role defeats the boundary.
The identity-bound variant needs a trusted credential router. This is why the
role and connection configuration are part of the claim, not deployment trivia.
PostgreSQL documents these bypasses and the distinction between row filtering
and write checks. [PostgreSQL RLS](https://www.postgresql.org/docs/18/ddl-rowsecurity.html)

The [Cedar experiment](../experiments/cedar/) checks both containment of allowed
access and preservation of intended access. Checking only the first admits a
deny-everything policy. The solver can quantify over a narrow policy model much
more cheaply than an end-to-end suite can enumerate users, resources, and roles.
It cannot authenticate the entity facts supplied by an application. Cedar's
combination of formal models and differential testing is the relevant precedent,
not a claim that the application becomes formally verified.
[Cedar's engineering approach](https://www.amazon.science/blog/how-we-built-cedar-with-automated-reasoning-and-differential-testing)

The [outbox](../experiments/outbox/) and [rolling-schema](../experiments/compatibility/)
experiments move other obligations into transactions, uniqueness constraints,
and explicit coexistence rules. Both retain bad executions. An equality trigger
does not prevent a stale backfill; a deduplication marker committed before its
effect can permanently lose work. A migration also has an operational cost:
the lock probe rejects DDL while a reader holds the conflicting lock.
[PostgreSQL ALTER TABLE](https://www.postgresql.org/docs/18/sql-altertable.html)

## Make executions explainable before making them selective

The [browser barrier](../experiments/boundaries/) exposes a causal ordering error
without hoping the scheduler reproduces it. The outbox model makes crash points
explicit and replays one projected trace against real Python, TypeScript, and
PostgreSQL. Neither is FoundationDB-style deterministic simulation of the whole
implementation: FoundationDB deliberately designed its runtime around control
of network, time, disk, and scheduling. Retrofitting that control is architectural
work, not a test-runner flag. [FoundationDB, §4](https://www.foundationdb.org/files/fdb-paper.pdf)

Independent behavioral checks catch defects that a green upstream suite and
generated types miss in the [public template](../experiments/fullstack/). But the
initial faults and added oracle share an author; a higher mutation score is not
an estimate of unseen defect detection. Requirements, parsers, fixtures, and
normalizers can correlate otherwise separate verifiers.

## Selection is conditional reuse, not verification

[marimo](../experiments/marimo/) supplies the scale counterexample: a real
6,505-test frontend suite exceeds thirty seconds locally. A narrow affected
subset is fast and catches one seeded defect; a central dependency still selects
thousands of tests. Moving nineteen pure tests to a Node environment helps those
tests, not necessarily the rest of the suite.

A safe reuse key needs source, tools, resources, configuration, generated inputs,
oracle, and environment—not just imports. Undeclared observations invalidate
the argument. Bazel's hermeticity model provides the useful discipline; static
selection research also demonstrates that graph approximations can be unsafe.
[Bazel](https://bazel.build/basics/hermeticity) ·
[Legunsen et al., FSE 2016](https://www.cs.cornell.edu/~legunsen/pubs/LegunsenETAL16StaticRTSStudy.pdf)

The resulting design is a proposal, not an implemented production control plane:
protect the verifier; bind evidence to an immutable candidate, base, and runtime;
check structural and cross-version obligations; execute or reuse only justified
behavioral checks. Missing dependencies, stale identities, and unknown results
go to broader verification. Human review owns the specification and its trusted
assumptions. The experiments test pieces of this argument; they do not yet prove
that the pieces compose for an arbitrary codebase.

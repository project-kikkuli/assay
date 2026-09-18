# What could be adopted next week?

## 1. Shadow an existing command-based verification job

Use the runner without changing test implementations. Begin with caching off,
preserve existing admission checks, and compare the runner's verdict with the
existing system. Measure total last-edit-to-verdict latency separately from
command execution, queue delay, checkout, and installation.

Useful immediately: explicit obligation inventory, dependency-failure handling,
captured command output, durations, and structured missing-evidence reasons.
Exit zero still means the configured command succeeded, not that its tests are
meaningful. A test runner that silently skips everything needs a collection/
coverage contract; this prototype does not automatically infer one.

## 2. Convert one timing race to an explicit event schedule

Keep the production implementation. Inject time at the relevant boundary and
record semantic actions, not arbitrary screenshots or wall-clock sleeps. Write
an independent invariant over observable state. Preserve one small failing
schedule before fixing the behavior. Then verify the fix against that schedule
and broader generated cases.

The Parcel example uses actual SQLite transactions; it does not simulate
SQLite's locking or claim coverage of every OS-thread interleaving. Connection
reopening is tested; abrupt process death, power failure, disk corruption,
distributed storage, and external side effects remain additional obligations.

## 3. Make performance explanations part of evidence

Attach input sizes, work counts, query plans, source links, and timing samples to
the same report as behavioral results. Separate instrumented work counting from
uninstrumented timing. Compare candidates against independently specified
outputs before discussing speed. Measure several input distributions, not only
the one favoring an optimization.

## Not safe to adopt as an authoritative gate yet

- **Hermeticity:** the runner narrows environment variables but does not isolate
  filesystem, network, installed packages, native libraries, or host services.
- **Trust:** local cache hashes are not signatures. An actor who controls the
  cache or runner can forge results. Protected CI policy and trusted writers
  are prerequisites for accepting reuse across a security boundary.
- **Dependencies:** manual input lists may miss semantic or dynamic dependencies.
  Default conservative scanning reduces, but does not eliminate, this risk.
- **Snapshots:** before/after hashing detects ordinary input drift, not every
  transient change-and-restore race. Execute immutable snapshots for stronger
  claims.
- **Integration:** there is no merge coordinator. Evidence is about the scanned
  checkout; an eventual merge candidate must be separately evaluated.
- **Oracle independence:** the included fault injection checks one bug class.
  Mutation coverage and historical independent counterexamples must grow.
- **Performance:** local prototype timings do not establish hosted CI p95.

## Falsifiable follow-up experiments

1. Shadow a real suite across 20 representative changes; count missed
   invalidations, verdict disagreements, and p50/p95 end-to-end time.
2. Deliberately delete tests, alter fixtures, change a dynamic import, modify
   policy, and corrupt a cache entry. Admission must refuse unsupported reuse.
3. Add an immutable sandboxed worker and authenticated evidence; attempt cache
   poisoning from an untrusted branch.
4. Run queue contracts against two alternative stores. A model is useful only
   where adapter correspondence has evidence.
5. Ask a reviewer unfamiliar with the implementation to explain a counterexample
   and a scaling regression using only the report. Record where explanation
   breaks down instead of assuming a graph makes code understandable.

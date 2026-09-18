# PostgreSQL isolation experiment

## Claim

On PostgreSQL 18.6, a restricted application role with `ENABLE` + `FORCE ROW
LEVEL SECURITY`, `USING`/`WITH CHECK`, composite foreign keys, and composite
uniqueness prevents accidental cross-tenant reads/writes and duplicate
idempotency keys in this fixture. Transaction-local context does not bleed
across commit/rollback. Separate LOGIN roles with no memberships provide an
identity-bound variant: cross-role `SET ROLE` is denied, `RESET ROLE` preserves
the login identity, and a spoofed tenant GUC is ignored. Explicit
`WITH CHECK (true)` moves a row; omitted `WITH CHECK` reuses `USING` and rejects
the same move.

The claim is bounded: the application role can arbitrarily `SET app.tenant_id`,
so contextual RLS protects against predicate omission, not identity compromise
or arbitrary SQL. The identity result assumes a trusted router selects the
correct credential; an app holding all tenant credentials can impersonate
either tenant. Owners without `FORCE RLS` and `BYPASSRLS` are counterexamples.
RLS here bounds row visibility, not errors, timing, locks, or other side
channels; this is not a formal or production-readiness claim.

## Assumptions and provenance

- Each run creates UUID-suffixed resources, refuses collisions, and drops only
  what it created. The fixture is PostgreSQL 18.6 at localhost:55439 as
  `postgres`; Python 3.14.6/psycopg 3.3.4 are pinned in `requirements.txt`.
- `results.json` records roles, grants, policies, matrices, bypasses, migration
  observations, and teardown. `performance.json` records JSON plans, buffers,
  filter removals, and 25 RLS/baseline timings for 100 and 100,000-row tenants;
  expected cardinalities and measurements are asserted; version-sensitive plan
  differences are classified as `non-equivalent-plan`.

## Run

```sh
: "${PYTHON:?Set PYTHON to Python 3.14 with psycopg 3.3.4}"
"$PYTHON" experiments/isolation/run.py
```

The script exits nonzero on any unexpected allow/deny, policy result, or
cardinality/sample assertion. It audits sessions, drops its UUID-suffixed
database with `FORCE`, removes only its roles, and reports teardown. Inspect
both JSON files before generalizing beyond this fixture.

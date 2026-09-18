# Rolling-schema compatibility experiment

Only the trigger-assisted bridge lets old (`title`) and new
(`display_name`) clients coexist. Each schema state supports at least one
client version; testing that version alone misses rolling failures.

Equality is not lost-update protection: an unguarded backfill has
byte-identical SQL to a legitimate one-field write, so both fields
converge to the stale value. Safety comes from the guarded backfill
`WHERE display_name IS NULL`, not from equality.

Run (disposable PG 127.0.0.1:55439, Python >= 3.12, Node 22):

```
npm ci --ignore-scripts --no-audit --no-fund
uv run verifier.py --output results.json
uv run verifier.py --selftest
```

`verifier.py --selftest` runs DB-free negative controls.

The matrix exercises real Python/TypeScript clients against five schema states.
A separate rollout preserves existing rows through actual `ALTER` operations,
interleaved writes, backfill, and contract. A held reader makes incompatible DDL
hit its lock budget (SQLSTATE `55P03`); the same DDL succeeds after release.

Each run creates and removes only its UUID database. Warm timing includes the
TypeScript build; dependency installation is separate. Results retain positive
checks, expected counterexamples, unexpected faults, source hashes, and versions.
Old-consumer retirement is a supplied assumption, not discovered automatically.
These schedules are evidence, not a proof of arbitrary migration safety.

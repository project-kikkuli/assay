# PostgreSQL RLS revocation race

This experiment reproduces the PostgreSQL 18 documentation's warning that a
row-security policy sub-`SELECT` can use an old snapshot while `SELECT ... FOR
UPDATE` fetches a newly committed row. Each of three retained repeats creates a
disposable PostgreSQL 18.6 database with a non-owner, non-superuser,
non-`BYPASSRLS` LOGIN reader. The reader first succeeds, then a second session
revokes it and changes the secret in one uncommitted transaction.
`pg_stat_activity` and `pg_blocking_pids` verify the exact target `FOR UPDATE`
statement is blocked before the admin commits; no sleep establishes ordering.

The baseline policy consults a membership table without a row lock and must
leak the rotated secret. The repair adds `FOR SHARE` to that policy
sub-`SELECT`. The fixture grants UPDATE only on a harmless `lock_token`
column because the tested locking statements require UPDATE privilege on a
selected table. Matching UPDATE policies permit row locking through the same
`USING` predicate; protected `can_read` and `secret` updates remain denied by
column privileges. That is a bounded privilege-design demonstration, not a
general authorization recipe.
Validation derives the positive read and post-commit outcome from exact rows,
checks actor PIDs/isolation/query text and finite timing keys, and records
before/after source hashes. The baseline must return exactly the rotated row;
the repair must return exactly no rows. Contention timings are observations,
not capacity guarantees. This is a bounded concurrency reproduction, not a
proof of RLS safety, eventual revocation, or broker/external-effect behavior.

Run `./lab prepare` from the Assay root for the disposable PostgreSQL fixture.
Then run against only that fixture listener:

```sh
uv run --no-project --python 3.14.6 \
  --with 'psycopg[binary]==3.3.4' \
  experiments/revocation/run.py
```

The command creates UUID-suffixed roles/databases, fails closed on missing
barriers or contradictory evidence, verifies cleanup, retains all three run
records, and writes `results.json`. The primary source is [PostgreSQL 18 Row
Security
Policies](https://www.postgresql.org/docs/18/ddl-rowsecurity.html), especially
the race and `FOR SHARE` discussion around lines 184–243.
The locking privilege rule is in
[PostgreSQL 18 `SELECT`](https://www.postgresql.org/docs/18/sql-select.html).

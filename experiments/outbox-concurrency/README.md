# Real outbox concurrency and crash cutpoints

This sibling experiment reuses the public MIT outbox schema and `pg` dependency
shape from `../outbox/`; the existing experiment is not modified. It owns its
own UUID-named PostgreSQL database and drops only that database after success or
failure.

From the Assay root, run `./lab prepare` first for the disposable PostgreSQL
fixture and public Node dependencies. Use Node 22.12.0, then:

```sh
uv run --no-project experiments/outbox-concurrency/run.py
python3 experiments/outbox-concurrency/test_concurrency.py
```

The runner writes `status: unknown` before setup and writes the final status only
after its owned database is confirmed dropped. Use `--output PATH` to retain
separate attempts; a failed attempt remains structured evidence, never green.
The JSON includes source hashes for this sibling plus the public fixture
`package-lock.json` and `db.env`, tool versions, wall time, child/coordinator
PIDs, and exact outbox/processed/ledger states.

The runner holds the one outbox row with `FOR UPDATE`, starts two actual Node
22 TypeScript consumer processes, waits until PostgreSQL reports both blocked
by that coordinator, then releases it. The original ordering must produce one
`committed` result and one `rolled_back_unique_violation`; the repaired ordering
checks `processed_events` only after the row lock and must produce
`committed` plus `dedup_skipped`. The report records blocking PID evidence and
the internal ledger state.

Separate children are SIGKILLed at stdout-controlled barriers immediately
before commit and after commit. Recovery is another real consumer process and
the final PostgreSQL state must contain exactly one ledger effect. No sleeps
stand in for a barrier. This claims neither broker delivery, external
exactly-once effects, nor unbounded liveness.

# Transactional outbox experiment

Run from this directory:

```sh
npm install --ignore-scripts --no-audit --no-fund
python3 model.py --all
uv run run_actual.py
python3 -m unittest -v test_outbox.py
```

`model.py` is stdlib-only. It exhaustively BFSes at most two commands, three total outstanding deliveries (queued plus one held by an active consumer), and 16 actions, including producer/consumer crash cutpoints, duplicate delivery, and arbitrary queue removal (reordering). It returns the shortest replayable trace for each broken variant. A no-counterexample result is bounded safety evidence, not proof and makes no eventual-delivery claim.

`run_actual.py` provisions a UUID-named database on the configured PostgreSQL server, runs the discovered processed-before-ledger trace and a ghost delivery challenge against it, and drops that exact database at the end. The consumer derives command identity from an existing outbox row; foreign keys and exact SQL row counts reject orphan deliveries. Crash behavior is simulated at named cutpoints by committing the preceding transaction and stopping; no process kill, broker, external authentication, or network failure is modeled. Results are written to `results/*.json` with elapsed time, explored model states, replay trace, SQL/TS outcome, and invariant checks.

Healthy runs must report `committed`, then `dedup_skipped`, with exactly one ledger effect; rollback or unexpected status fails. Records include source SHA-256 hashes and tool versions.

The model preserves independent producer/consumer actors, checks invariants at every committed boundary, and includes bounded crash/restart transitions. The enqueue guard reserves the active consumer's slot because a consumer crash returns its held delivery to the queue. `accept:*@commit` is the acceptance linearization point; the broken command-only commit is immediately invalid. Likewise, a committed processed marker without a ledger row is immediately invalid. It is an executable counterexample finder, not a production proof.

Variants:

- `normal`: atomic command/outbox and atomic ledger/processed transaction.
- `non-atomic-outbox`: command commit can precede outbox commit.
- `processed-before-ledger`: processed marker commits before ledger effect.
- `missing-dedup`: consumer ignores the processed marker and ledger has no command uniqueness constraint.

The expected invariant is `accepted_commands ⊆ outbox_events`, with every outbox event pending or published. The normal probe additionally requires published outbox, processed marker, and exactly one ledger effect. Consumer safety is one ledger row per command; this does not establish exactly-once external effects, unbounded correctness, fairness/liveness, ORM equivalence, or production readiness.

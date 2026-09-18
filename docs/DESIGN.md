# Assay: make a verdict inspectable

Research question: can a small wrapper around existing commands provide honest,
fast reuse of verification evidence, with enough causal and performance detail
for a human to challenge the result?

The first application is Parcel, a new SQLite-backed leased job queue. It is a
real persistent implementation, not a mocked database. Its scope excludes
external exactly-once effects and distributed database behavior.

## Three independent surfaces

1. `assay/runner.py`: command tasks, declared input content hashes, dependency
   DAG, bounded subprocess execution, local advisory cache, and three outcomes.
2. `examples/parcel`: durable queue, fencing tokens, explicit integer time,
   deterministic schedule exploration, and deliberately broken variants.
3. `assay/report.py`: evidence-to-source links, causal event tables, measured
   phases, operation counts, and a Chrome/Perfetto trace export.

The runner must work with arbitrary commands, including existing unittest or
pytest suites. No adoption of a new application framework is required.

## Outcome contract

`verified` means this configured obligation completed successfully under the
recorded inputs. `rejected` means it failed. `unresolved` means required evidence
could not be obtained (timeout, launch failure, malformed configuration,
dependency not verified, or input drift). Missing evidence never means success.
An observed failed execution is not erased by retrying until success.

## Trust boundary

The prototype is a local developer tool. Its cache is NOT an authenticated
attestation store. A digest detects accidental corruption, not an attacker who
can replace both the result and its digest. The process environment is narrowed,
but execution is NOT hermetically sandboxed. Declared inputs can be incomplete;
the conservative mode includes all non-generated project files. CI admission
would additionally require protected policy, trusted workers, complete input
enforcement, and validation against the exact integrated candidate.

## Manifest v1

JSON object with `version: 1`, optional `name`, and non-empty `tasks`. Each task
has a unique `id`, a non-empty `command` argv list (`{python}` expands to this
interpreter), optional `inputs` glob list (omitted means conservative project
files), `needs` IDs, positive `timeout_s`, explicit string `env`, optional
`description`, and optional repo-relative `source` links. Files matching `.git`,
`.assay`, `out`, `.venv`, `node_modules`, `__pycache__`, or `.pyc` are generated
and excluded from conservative discovery. Symlink inputs and empty declarations
fail closed. Explicit globs include their matched path inventory in the key.

Keys include command, task policy, required dependency keys, input content,
manifest content, runner implementation, and interpreter/platform identity.
Successful executions may be reused; unsuccessful executions may not. Input
hashes are checked again after execution. Wall timings are excluded from keys.

## Human-readable report schema v1

The runner returns a JSON-compatible object:

```
{
  "schema": "assay.report/v1", "name": "...", "status": "verified|rejected|unresolved",
  "duration_ms": 12.3, "candidate": "<content digest>",
  "tasks": [{
    "id": "...", "status": "...", "cached": false, "key": "...",
    "reason": "...", "description": "...", "source": ["relative.py:12"],
    "duration_ms": 1.2, "start_ms": 0.0, "input_files": ["relative.py"],
    "needs": [], "command": ["..."], "returncode": 0,
    "stdout": "...", "stderr": "..."
  }],
  "warnings": ["..."]
}
```

Optional `scenarios` and `benchmarks` arrays add business-level evidence:
scenario fields `name`, `status`, `invariant`, `source`, `seed`, `events`,
`counterexample`, `duration_ms`; each event has `step`, `action`, `before`,
`after`, `result`, optional `parent`, `work`, and measured `duration_ms`.
Benchmark fields `name`, `samples_ms`, `p50_ms`, `p95_ms`, `work`, `source`,
`notes`. Timing traces distinguish elapsed host time from virtual scenario time.

## Success criteria

- A bad implementation is rejected with an executable fixed schedule.
- Changing a declared source, test, fixture, policy, or dependency invalidates
  affected results. Corrupt cache entries never become successful evidence.
- No-op runs explain reuse; failures and missing evidence remain legible.
- Report measured cold, warm, and changed-input results, without extrapolating
  a toy benchmark to arbitrary production CI.
- A human can follow business rule -> event -> state change -> source and
  measured work, and see the boundaries of every claim.

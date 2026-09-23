# Stack layer cache — a real mechanism, not a simulation

`rebase-reuse` measured, in tree-hash terms, how much of a stack's evidence
survives a rebase. This builds and actually runs the mechanism that would
capture that reuse in practice, and measures the real convergence time it
saves: push a fix to the bottom of an open stack, how fast does the whole
stack get back to green?

`layer_cache.py` is a working per-task, content-addressed test-receipt
cache — not a simulator. `build_tasks` statically parses real source at a
real commit (`ast`, no execution) to derive which real test file depends on
which real package module, transitively, through real relative imports.
`fingerprint` reads real git blob hashes for a task's declared inputs at a
real commit. `run_stack_cached` actually checks out each real commit and
actually runs `pytest` as a subprocess for any (task, fingerprint) this
cache hasn't already seen; anything it has, it reuses — zero execution.

`measure.py` drives it against real `itsdangerous` history: a real 2-commit
stack is run once (a real, warm cache — the stack was already green). Then
`git rebase --onto` inserts `gap` further real historical commits underneath
it — a real fix landing at the bottom of an open stack — and the upper layer
is replayed for real onto the new base. The question is then answered twice,
for real: `run_stack_naive` (rerun the whole real suite at every real layer,
the default behavior with no incremental mechanism) versus
`run_stack_cached` against the still-warm cache (only real tasks whose real
content actually changed get re-executed).

```sh
python3 experiments/stack-layer-cache/measure.py \
  --repo-dir /path/to/itsdangerous --python /path/to/venv/bin/python3
```

## Measured result

25 sampled stacks per gap size, real `pytest` runs throughout
([full results](results.json)):

| Gap (real commits inserted) | Clean samples | Rebase conflicts | Tasks reused | Mean naive wall | Mean cached wall | Mean time saved |
|---|---|---|---|---|---|---|
| 1 | 13/25 | 12 | 138/138 (100%) | 0.99s | 0.00s | 0.99s |
| 2 | 7/25 | 18 | 70/78 (89.7%) | 1.08s | 0.25s | 0.83s |
| 3 | 7/25 | 18 | 76/76 (100%) | 1.04s | 0.00s | 1.04s |
| 5 | 4/25 | 21 | 42/44 (95.5%) | 1.05s | 0.14s | 0.91s |

At every gap size tested, real per-task reuse stayed at or above 89.7% —
almost none of the upper layer's real test evidence needed to be recomputed
after a real fix landed underneath it, because almost none of its declared
inputs actually changed. The naive rerun-everything path costs a real
~1.0–1.1s per amendment regardless of gap (it never looks at what changed);
the cached path costs whatever the handful of genuinely-affected tasks
actually take to run, sometimes as low as **0.00s billed** (every task fully
reused — `speedup_x` is left `null` in that case rather than a division by
zero standing in for "infinite," but the ~1s `time_saved_s` is real and
measured). This is convergence time actually cut, on a real stack, by a real
tool, not a number from a model of one.

Rebase conflicts, not reuse, are the binding constraint here: they rise from
48% (gap 1) to 84% (gap 5) — even a single real commit landing underneath a
2-layer stack of this small package conflicts nearly half the time. That
rate is higher than `rebase-reuse` sees on pytest at a much larger gap,
consistent with `sibling-evidence`'s finding that a small file surface makes
real collisions common. The cache mechanism only ever gets a chance to prove
its savings on the samples that didn't conflict outright.

## Limits

`itsdangerous` was chosen for its ~1s real suite, so a real per-layer,
per-task pytest invocation is affordable at dozens of samples; a larger
suite would show the same mechanism with different absolute numbers. Clean
sample counts shrink fast with gap (down to 4 at gap 5) because of the
conflict rate above, not because the cache failed — treat the gap-5 point
estimate as directional, not tight. The cache is process-local and
file-based here (a JSON file), a minimal proof that the reuse is real; a
production version would need a shared, multi-writer store (see
[`sibling-evidence`](../sibling-evidence/) for what a shared store does and
does not add over a per-branch one). A rebase conflict is a real, measured
outcome here too, not a case this tool tries to resolve — the mechanism
only helps once the tree actually merges.

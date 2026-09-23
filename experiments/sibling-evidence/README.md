# Cross-branch evidence sharing

Answers a question [`rebase-reuse`](../rebase-reuse/) leaves open: that
experiment measures how much of ONE branch's own evidence survives ITS OWN
rebase. This measures the horizontal case — M agents each open a branch off
the SAME base — and asks whether a receipt store shared ACROSS those
sibling branches saves anything beyond a cache that only ever knows about
its own branch's pushes.

`measure.py` samples a real base commit B from pytest's actual first-parent
history and takes the next M real commits after it as M independent
single-commit sibling branches (each commit's own real changed-file set
stands in for that sibling's real diff — the same reuse of real commit
bytes under a different concurrency assumption `rebase-reuse` already
uses). Tasks are real test files and source directories (directory-level
under `src/_pytest/*`, file-level elsewhere — the same granularity
`rebase-reuse` uses). Siblings "merge" in real chronological order; a
shared store only recomputes a (task, real tree hash) pair the first time it
is ever seen anywhere, starting from the base's own hashes.

```sh
python3 experiments/sibling-evidence/measure.py --repo-dir /path/to/pytest --samples 150 --window-sizes 2 4 8 16
```

## Measured result

150 samples per window size ([full results](results.json)):

| Siblings per window | Fully disjoint windows | Windows with a real task collision | Collision events | Shared-store hits beyond per-branch cache |
|---|---|---|---|---|
| 2 | 79.3% | 31 | 34 | 0 / 284 (0.0%) |
| 4 | 51.3% | 73 | 177 | 1 / 591 (0.17%) |
| 8 | 27.3% | 109 | 698 | 1 / 1516 (0.07%) |
| 16 | 20.0% | 120 | 1411 | 4 / 2705 (0.15%) |

Two findings, one expected and one not. Expected: the fraction of windows
where every sibling's touched tasks stay completely disjoint — mergeable in
any order with no coordination — drops fast as concurrency rises, from
79.3% at 2 siblings to 20.0% at 16. Not expected: even where a real task
COLLISION happens (698 events at 8 siblings, 1411 at 16), a cross-branch
shared store almost never turns that into a free hit — independent real
commits that touch the same task essentially always produce different
content, so the store still has to recompute it. Across all four window
sizes, a shared store saved at most **0.17%** more than a cache that never
looks past its own branch. The real lever for concurrent agent branches is
reducing collision surface (finer task/file ownership), not building a
fancier cross-branch cache — the cache has almost nothing to find.

## Limits

Real chronological order stands in for real merge-queue arrival order,
which this sample doesn't independently confirm. "Task collision" here
means two siblings' declared inputs overlap, not that they touch the same
line — a shared store could still do better at a finer (line- or
hunk-level) granularity than the file/directory tasks used here and by
`rebase-reuse`; this experiment does not test that granularity. Sibling
commits are real diffs recombined under a synthetic concurrency assumption,
the same caveat `rebase-reuse` already carries: it is not evidence about how
often real concurrent branches in this project actually collide, only a
measurement at this task granularity and this window-size range for
pytest's actual commit content.

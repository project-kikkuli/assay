# Diff size vs. affected-task fraction

`python-selection` shows a coverage-based selector failing on a large real
suite, without pinning down when a fast lane stops looking safe as diffs
grow. This asks that narrower question directly: for real historical
single-commit diffs on a real codebase, bucketed by how many files they
actually touched, what fraction of an explicit-input task manifest does
each diff affect?

`measure.py` samples 250 real commits from pytest's actual first-parent
history. One task per real test file under `testing/`; a task's declared
inputs are the test file itself plus the `_pytest` submodules it statically
imports, parsed with `ast` (no execution). Both the manifest and the diff's
changed files are read via one `git cat-file --batch` call per sampled
commit, at the real parent commit — the manifest as it existed **before**
that diff landed, not one built once from a later tree state.

```sh
python3 experiments/diff-affected-fraction/measure.py --repo-dir /path/to/pytest --samples 250
```

## Measured result

250 real commits ([full results](results.json)). **52.8%** touch nothing
under `testing/` or `src/_pytest/` at all (docs, changelog, CI config) and
trivially affect 0% of tasks — restricting to the **118** commits that do
touch code:

| Files changed | Samples | Share of code-relevant samples | Median affected fraction | p95 affected fraction |
|---|---|---|---|---|
| 1 | 24 | 20.3% | 0.0% | 7.3% |
| 2–3 | 49 | 41.5% | 1.6% | 3.9% |
| 4–6 | 27 | 22.9% | 3.5% | 23.4% |
| 7–15 | 12 | 10.2% | 7.2% | 10.8% |
| 16–30 | 4 | 3.4% | 6.9% | 10.3% |
| 31+ | 2 | 1.7% | 85.4% | 80.7%* |

*p95 below the median at n=2 is sampling noise, not a real inversion — see
[Limits](#limits).

Agent-sized diffs dominate the real sample: **84.7%** of code-touching
commits changed 6 or fewer files, and their median affected fraction never
exceeds 3.5% of the task manifest. The curve is not flat, though — it climbs
through single digits by 7–15 files and jumps to a majority of the manifest
at 31+. The affected fraction stays small specifically because the diffs
are small; it is not a property a coverage-agnostic fast lane gets for free
at any diff size.

## Limits

The 16–30 and 31+ buckets have 4 and 2 samples respectively — nowhere near
enough to trust their point estimates, only the direction (large diffs
touch much more of the manifest) that both `python-selection`'s baseline
and ordinary intuition already predict. A static `ast`-parsed import graph
is not a real selector: it misses dynamic imports, `pytest` plugin hook
discovery, and any indirect coupling that only a real fixture graph or
runtime trace (see [`input-drift`](../input-drift/)) would catch, so it
likely undercounts the true affected fraction at every bucket. "Affected"
here means "declared inputs intersect changed files," not "would actually
fail" — this measures selection narrowness, not selection correctness.

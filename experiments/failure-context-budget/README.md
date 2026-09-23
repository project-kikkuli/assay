# Failure context budget

How much of a failing CI log does an agent actually need to find the
failure? This measures, on the same real regression `../failure-ordering/`
uses (pytest's own real PR #14934 fix, applied as source-plus-test to its
real pre-fix commit — 11 real failures in `testing/python/approx.py`), how
many lines of four representations a reader needs before it identifies
**which** test failed and **why**: pytest's default long-traceback report
read from the top, the same report read from the bottom, pytest's compact
`--tb=line` mode from both ends, and structured JUnit XML.

```sh
python3 experiments/failure-context-budget/measure.py \
  --repo-dir /path/to/pytest --python /path/to/pytest/.venv/bin/python
```

Ground truth for "which test" and "why" comes from the same real run's real
JUnit XML `<failure>` elements (`message`, since this pytest version does not
set `type=` on most failures), not a regex over the human log; a real nodeid
is reconstructed from JUnit's dotted `classname` matched against the known
overlay file path, not guessed.

## Measured result

Same real 11-failure regression, full `testing/` suite ([full
results](results.json)):

| Representation | Total size | Lines to localize (median) |
|---|---|---|
| Long traceback, read from the **top** | 387 lines / 21,154 chars | **374** lines |
| Long traceback, read from the **bottom** | (same file) | **14** lines |
| `--tb=line`, read from the **top** | 108 lines / 8,812 chars | **95** lines |
| `--tb=line`, read from the **bottom** | (same file) | **14** lines |
| JUnit XML, `<failure>` elements only | 13,778 chars (of a 549,395-char file) | direct lookup, no scan |

Reading from the bottom instead of the top cuts the lines needed to identify
which test failed by **26x** on the long traceback (374 → 14) and **6.8x**
on the compact `--tb=line` mode (95 → 14) — both land at essentially the
same absolute cost (~14 lines) regardless of how verbose the traceback is,
because pytest's own "short test summary info" section, not the traceback
body, is what actually names the failing test; `--tb=line`'s traceback body
never includes a test name at all. The exception *type* alone is findable
earlier from the top (line 90 of 387, 66 of 108) — plausible on a suite
where only the first handful of tests matter, but "why" without "which
test" doesn't localize anything on a suite this size. JUnit XML's total file
size (549KB, dominated by metadata for 4,545 passing tests) is *larger* than
either raw log; only a caller that parses it and keeps just the `<failure>`
elements (13.8KB) gets the structured win — naively feeding the whole file
to an agent is worse than the compact text log.

## Limits (n=1 build)

All 11 failures share one exception shape (`Failed: DID NOT RAISE
TypeError`) from one bug; a suite with more heterogeneous failures might
show the traceback body carrying more of the localization signal on its
own. This pytest version's JUnit XML does not set `<failure type=...>` for
this exception; ground truth instead uses `message` (`failure.get("message")
.split(":", 1)[0]`), which is pytest's own text, not a third-party schema
field, so file size/field-name specifics won't transfer as-is to another
test framework's JUnit output. Localization is substring matching against
the real nodeid and real exception text pulled from this same run, not a
model of what a human or an agent would actually parse out of prose.

## A real mechanism, and a statistical sample (n=112)

`localize.py` is a real, standalone tool (not experiment-only code): given a
pytest log and/or its JUnit XML, it prints pytest's own real "short test
summary info" section (found by its real header, not a guessed line count)
and/or the JUnit `<failure>`/`<error>` entries only, skipping every passing
test. `measure_sample.py` runs it against the same 20-case real
regression/fix sample `../failure-ordering/mine_cases.py` built (13 pytest +
7 click cases, real pre-fix commit plus the real fix's own test overlaid, no
seeded faults), capturing one real scoped `--tb=long --junit-xml=...` run
per case:

```sh
python3 experiments/failure-context-budget/measure_sample.py \
  --repo-dir /path/to/pytest --cases experiments/failure-ordering/cases/pytest_cases.json --repo-tag pytest \
  --out experiments/failure-context-budget/results_sample_pytest.json
python3 experiments/failure-context-budget/localize.py --log run.txt --junit run.xml
```

### Measured result

112 real failures across both repos ([pytest](results_sample_pytest.json),
[click](results_sample_click.json)):

| | n | Median lines to localize | Min | Max |
|---|---|---|---|---|
| From the **top** | 112 | **2,076** | 59 | 2,456 |
| From the **bottom** | 112 | **6** | 2 | 39 |

Reading from the bottom instead of the top cuts the median lines needed by
**346x** across two real, unrelated codebases — not a one-bug artifact of
the n=1 build. `localize.py` found every one of the 112 real failures
(recall **1.0**) by locating pytest's real summary-section header, never by
line-counting from the end.

## Limits (n=112 sample)

Cases are scoped, single-file (or two-file) runs, not full-suite logs — the
`from_top`/`from_bottom` counts are smaller in absolute terms than the n=1
build's whole-`testing/`-directory numbers, though the *ratio* (bottom is
where the answer is) is the same shape and, if anything, the full-suite case
is the more extreme one. `localize.py`'s tail-summary extraction depends on
pytest's real, currently-stable "short test summary info" header string; a
framework or pytest version that renamed or removed that section would need
the documented last-N-lines fallback instead, which this sample did not
need to exercise (the header was found in all 112 cases).

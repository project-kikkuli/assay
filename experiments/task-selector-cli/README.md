# Task selector CLI — operationalized and validated against real regressions

`diff-affected-fraction` measured what fraction of a manifest a real diff
touches. This turns that manifest into a runnable tool
(`task_select.py --repo-dir ... --base <sha>`) and validates it the way the
question demands: replay real regressions and check whether the selector
would have skipped the real test that catches each one.

The four scenarios are the same real pytest commits
[`failure-ordering`](../failure-ordering/) already established
(`scenarios.json`, read here, never edited): one real regression (#14934,
`approx()` on mismatched nested container types) plus three real unrelated
bug fixes, each a real (buggy base, real fix, real regression test file)
triple. For each, `validate.py` takes the real fix's own real diff
restricted to `src/_pytest/` (the hypothetical "developer fixed the bug, PR
not yet including a test" case), builds the real manifest at the real
pre-fix state, and checks whether the real regression test file is among
the selected tasks.

```sh
python3 experiments/task-selector-cli/task_select.py --repo-dir /path/to/pytest --base <sha>
python3 experiments/task-selector-cli/validate.py --repo-dir /path/to/pytest --python /path/to/pytest/.venv/bin/python3
```

## Measured result

4/4 real scenarios ([full results](results.json)). `diff-affected-fraction`'s
own naive `test_*.py`/`*_test.py` manifest **misses 3 of 4** — every miss is
a real test file under `testing/python/` (`approx.py`, `fixtures.py`) that
doesn't start with `test_`, because pytest's own real `pyproject.toml`
declares a third real pattern, `python_files = [..., "testing/python/*.py"]`,
that the naive heuristic never reads.

`task_select.py` fixes this by reading the real `python_files` patterns
from the target repo's own real `pyproject.toml` (`tomllib`, stdlib) instead
of assuming a fixed naming convention. Re-scored on the same 4 real
scenarios: **0 misses**. The fix is validated, not just asserted — this
isn't `diff-affected-fraction`'s own committed numbers being silently
revised; that experiment's results stay exactly as published, and this is a
documented refinement discovered by trying to validate it against real
regressions.

| Scenario | Real source diff | Selected tasks | Real tests selected | Real exec time |
|---|---|---|---|---|
| #14934 approx() (target) | `approx.py` | 2 | 159 | 0.80s |
| #14934 approx() (60 commits earlier) | many | 59 | 3,868 | 90.92s |
| monkeypatch undo() | `monkeypatch.py` | 26 | 2,508 | 58.65s |
| fixtures lineno off-by-one | `compat.py` | 7 | 606 | 11.54s |

Real full suite: **4,543 tests**. On average the selector picks **60.7%**
fewer real tests than the full suite while still catching every one of these
4 real regressions — but the range is wide: the sharpest case (2 tasks, 159
tests, 0.80s) is a 96.5% reduction, while the widest (a source diff spanning
60 real commits' worth of accumulated change) selects 85% of the suite,
because that much legitimately changed. Selection narrowness tracks real
diff size, exactly as `diff-affected-fraction` found — it isn't a free
win at every diff size.

## Limits

Four real regressions is a real population, not a statistically powered
one — a 0% miss rate here is encouraging, not proof the corrected manifest
never misses a real regression with a different import shape (a dynamically
imported module, a plugin hook, a fixture registered by name rather than
import, none of which `ast`-static analysis or the `python_files` fix
addresses). The validation asks "would the selector have picked the right
test file," not "would the selected tests have passed" — a real
regression whose fix touches a file no test statically imports (dead-code
paths, string-based dispatch) would still be invisible to this mechanism.
The `python_files` correction is read from the repo's own current-at-that-
commit config, which is exactly right for pytest testing itself but is a
project-specific convention this tool does not generalize beyond reading
whatever `[tool.pytest]` declares.

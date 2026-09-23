"""A pytest plugin used for two purposes in this experiment: reordering
already-collected items by an external score file, and recording each test's
real outcome/duration to a JSON-lines file. Loading it never changes results
when neither env var is set, so a plain baseline run and a reordered run
differ only in item order.

`ASSAY_ORDER_SCORES` (env var) names a JSON file: `{"nodeid": score, ...}`.
Items present in the file sort ascending by score (lower runs first); items
absent from the file keep their original collection position and run after
every scored item. All strategy-specific logic (what counts as "recently
failed", "close to a changed file", "slow") lives in measure.py, which writes
the score file; this plugin only sorts by it.

`ASSAY_REPORT_PATH` (env var) names a file that this plugin appends one JSON
object per finished test to: `{"nodeid": ..., "outcome": ..., "duration": ...}`.
`outcome` is "passed", "failed", or "skipped" — the worst outcome across a
test's setup/call/teardown phases (a setup error counts as "failed"), and
`duration` sums all three phases. This is read from pytest's own real
`TestReport` objects during a real run, not reconstructed from captured
stdout.
"""
from __future__ import annotations

import json
import os

_RANK = {"passed": 0, "skipped": 1, "failed": 2}
_pending: dict[str, list] = {}  # nodeid -> [outcome, total_duration]


def pytest_collection_modifyitems(session, config, items):
    scores_path = os.environ.get("ASSAY_ORDER_SCORES")
    if not scores_path:
        return
    with open(scores_path) as f:
        scores = json.load(f)
    if not scores:
        return

    scored, unscored = [], []
    for item in items:
        nodeid = item.nodeid
        if nodeid in scores:
            scored.append((scores[nodeid], item))
        else:
            unscored.append(item)
    scored.sort(key=lambda pair: pair[0])
    items[:] = [item for _, item in scored] + unscored


def pytest_runtest_logreport(report):
    if not os.environ.get("ASSAY_REPORT_PATH"):
        return
    outcome = "failed" if report.failed else ("skipped" if report.skipped else "passed")
    nodeid = report.nodeid
    state = _pending.setdefault(nodeid, ["passed", 0.0])
    if _RANK[outcome] > _RANK[state[0]]:
        state[0] = outcome
    state[1] += report.duration
    if report.when == "teardown":
        final_outcome, total_duration = _pending.pop(nodeid)
        with open(os.environ["ASSAY_REPORT_PATH"], "a") as f:
            f.write(json.dumps({"nodeid": nodeid, "outcome": final_outcome, "duration": total_duration}) + "\n")

"""Pytest timing hooks loaded only by the isolated selection experiment."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


_start = time.perf_counter()
_collection_start: float | None = None
_collection_end: float | None = None
_test_start: float | None = None
_test_end: float | None = None
_selected = 0


def pytest_sessionstart(session):
    global _start
    _start = time.perf_counter()


def pytest_collection(session):
    global _collection_start, _collection_end
    _collection_start = time.perf_counter()
    outcome = yield
    outcome.get_result()
    _collection_end = time.perf_counter()


pytest_collection.hookwrapper = True


def pytest_collection_finish(session):
    global _selected
    _selected = len(session.items)


def pytest_runtestloop(session):
    global _test_start, _test_end
    _test_start = time.perf_counter()
    outcome = yield
    outcome.get_result()
    _test_end = time.perf_counter()


pytest_runtestloop.hookwrapper = True


def pytest_sessionfinish(session, exitstatus):
    finished = time.perf_counter()
    collection_start = _collection_start or finished
    collection_end = _collection_end or collection_start
    test_start = _test_start or collection_end
    test_end = _test_end or test_start
    output = {
        "selected_tests": _selected,
        "phases_s": {
            "pre_collection_setup": round(collection_start - _start, 3),
            "collection_and_testmon_analysis": round(collection_end - collection_start, 3),
            "test_execution": round(test_end - test_start, 3),
            "post_processing": round(finished - test_end, 3),
            "process_observed": round(finished - _start, 3),
        },
        "exit_status": int(exitstatus),
    }
    target = os.environ.get("PYSEL_TIMING_FILE")
    if target:
        Path(target).write_text(json.dumps(output), encoding="utf-8")

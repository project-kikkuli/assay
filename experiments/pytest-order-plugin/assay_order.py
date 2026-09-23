"""A drop-in pytest plugin: reorder collected tests by (recent-failure,
proximity-to-diff, duration) so the tests most likely to carry a real
regression run first, and `-x`/`--exitfirst` stops on them sooner.

Load ad hoc with `-p assay_order` (this file's directory on `PYTHONPATH`),
or install it as a real plugin via the `pytest11` entry point in
`pyproject.toml`.

## The ordering

Each collected item gets a 3-tuple sort key (lower runs first):

1. **Recent failure** -- 0 if the nodeid is in pytest's own `--lf`/`--ff`
   cache (`config.cache.get("cache/lastfailed", {})`), 1 otherwise. This is
   the same cache pytest's built-in `--lf` reads and every run already
   updates; no separate state to keep in sync.
2. **Proximity to the diff** -- 0 if the nodeid's own file was changed in
   `git diff --name-only <--assay-diff-ref>`, 1 otherwise. Git is optional:
   outside a repo, or with git unavailable, every item gets 1 here and this
   factor no-ops rather than erroring.
3. **Duration** -- ascending, from a per-test duration history this plugin
   persists in the *same* pytest cache across runs (`cache/assay-durations`),
   updated at the end of every run from real `TestReport.duration` summed
   across setup/call/teardown. An item with no recorded duration (new test,
   or first run) sorts after every item that has one.

The three factors are strictly lexicographic: a recently-failed test always
runs before a merely nearby one; a nearby test always runs before a merely
fast one. This matches the stated priority, not an average or a weighted
score -- there is no tuning surface to get subtly wrong.

## Options

- `--assay-order` -- enable the reordering. A no-op without it: unmodified
  pytest, unmodified order, so adopting this plugin changes nothing until
  it's asked for.
- `--assay-fail-fast` -- implies `--assay-order` and also sets
  `--exitfirst`. `--assay-order` alone is useful without stopping early
  (e.g. to inspect risk-ordered output); this is the one-flag "run the
  riskiest tests first and stop at the first real failure" recipe.
- `--assay-diff-ref=REF` (default `HEAD`) -- the `git diff --name-only`
  base for the proximity factor. Pass `HEAD~1`, a branch name, or anything
  `git diff` accepts.

## Why this shape, not a bigger model

Every input is something a real CI run already has for free: pytest's own
lastfailed cache, `git diff`, and real prior durations. No network call, no
extra service; the plugin is inert until `--assay-order` is passed.
"""
from __future__ import annotations

import subprocess

import pytest

DURATIONS_CACHE_KEY = "cache/assay-durations"


def pytest_addoption(parser: "pytest.Parser") -> None:
    group = parser.getgroup("assay-order")
    group.addoption(
        "--assay-order", action="store_true", default=False,
        help="Reorder tests by (recent-failure, proximity-to-diff, duration).",
    )
    group.addoption(
        "--assay-fail-fast", action="store_true", default=False,
        help="Imply --assay-order and stop at the first failure (sets --exitfirst).",
    )
    group.addoption(
        "--assay-diff-ref", action="store", default="HEAD",
        help="git diff --name-only base for the proximity factor (default: HEAD).",
    )


def pytest_configure(config: "pytest.Config") -> None:
    if config.getoption("--assay-fail-fast"):
        config.option.assay_order = True
        config.option.maxfail = 1  # what --exitfirst/-x itself sets (dest="maxfail", const=1)
    if config.getoption("--assay-order"):
        config.pluginmanager.register(AssayOrderPlugin(config), "assay-order-instance")


def _changed_files(rootdir: str, diff_ref: str) -> set[str] | None:
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", diff_ref],
            cwd=rootdir, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


class AssayOrderPlugin:
    def __init__(self, config: "pytest.Config") -> None:
        self.config = config
        self.lastfailed: dict[str, bool] = config.cache.get("cache/lastfailed", {})
        self.prior_durations: dict[str, float] = config.cache.get(DURATIONS_CACHE_KEY, {})
        self.changed_files = _changed_files(str(config.rootpath), config.getoption("--assay-diff-ref"))
        self.live_durations: dict[str, float] = {}

    def _key(self, nodeid: str) -> tuple[int, int, float]:
        recent = 0 if nodeid in self.lastfailed else 1
        if self.changed_files is None:
            proximity = 1
        else:
            proximity = 0 if nodeid.split("::", 1)[0] in self.changed_files else 1
        duration = self.prior_durations.get(nodeid, float("inf"))
        return (recent, proximity, duration)

    def pytest_collection_modifyitems(self, session: "pytest.Session", items: list) -> None:
        items.sort(key=lambda item: self._key(item.nodeid))

    def pytest_runtest_logreport(self, report: "pytest.TestReport") -> None:
        self.live_durations[report.nodeid] = self.live_durations.get(report.nodeid, 0.0) + report.duration

    def pytest_sessionfinish(self) -> None:
        if self.live_durations:
            merged = {**self.prior_durations, **self.live_durations}
            self.config.cache.set(DURATIONS_CACHE_KEY, merged)

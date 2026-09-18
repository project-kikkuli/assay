# Assay

A small experiment in fast, explainable verification for agent-first software.
Wrap existing test commands, preserve reusable evidence, and turn a concurrent
business failure into a deterministic counterexample a human can inspect.

**Research prototype.** Local cache reuse is advisory, not a trusted merge gate.
The demo uses a new synthetic application with real SQLite persistence. No
production-readiness or universal 30-second-CI claim is made.

## Morning demo — no installation or services

Requires Python 3.11 or newer. Clone this repository and run:

```sh
./demo
```

The launcher selects an installed Python 3.11+ and does not install anything.
The equivalent command is `python3 -m assay demo` with a supported interpreter.

The command runs a cold verification, repeats it against its own fresh cache,
demonstrates invalidation after a source change, explores generated queue
schedules, catches an injected fencing bug, minimizes the counterexample, and
compares two SQL algorithms with measured timing and VM work.

It writes `out/report.md`, `out/report.json`, `out/trace.json`, and
`out/counterexample.json`. Open the Markdown report for source-linked details.
Load `out/trace.json` in [Perfetto](https://ui.perfetto.dev/) for the timing lanes.
This is instrumented operation timing, **not** hardware instruction tracing.

Dive into the evidence without leaving the terminal:

```sh
./demo inspect
./demo inspect --task queue-contracts
./demo inspect --scenario 2
./demo inspect --benchmark 6
```

The scenario view shows each business event and exactly which persisted fields
changed. The benchmark view shows SQL, the actual query plan, VM work, raw
timing samples, and the source entry point.

Replay the failure, then the correct implementation:

```sh
python3 -m assay replay out/counterexample.json --unsafe
# Expected exit 1: the deliberately broken implementation violated the rule.
python3 -m assay replay out/counterexample.json
# Expected exit 0: the same stale completion is refused.
```

The original schedule opens a database connection, leases a parcel job, advances
virtual time to expiry, reconnects, leases again with the same worker name, and
submits the old token. Owner identity alone is insufficient. The immutable lease
token is the decision the human should inspect in
[the completion predicate](examples/parcel/queue.py) and
[the independent replay oracle](assay/scenarios.py).

## Three experiments

| Experiment | What it tests | What a human can inspect |
|---|---|---|
| Evidence-aware command runner | Correct invalidation and fail-closed orchestration | Task inputs, policy, dependency keys, commands, outputs, reuse reasons |
| Real persistent queue + virtual schedules | Lease expiry, reconnection, and stale workers | Business invariant, ordered events, before/after rows, failing token, minimized schedule |
| SQL algorithm comparison | Candidate-selection cost as queue size grows | Query text, indexes, query plans, measured VM steps, timing distributions |

## Wrap an existing suite

An `assay.json` file names obligations, not shell pipelines:

```json
{
  "version": 1,
  "tasks": [{
    "id": "unit",
    "command": ["{python}", "-m", "unittest", "discover", "-s", "tests"],
    "timeout_s": 30
  }]
}
```

```sh
python3 -m assay run assay.json
python3 -m assay run assay.json
python3 -m assay audit assay.json --repeat 3
```

Omitting `inputs` conservatively hashes non-generated project files. Explicit
input globs allow narrower reuse but are a manually maintained dependency
contract. Start with `--no-cache` or shadow comparison in another codebase.
`audit` bypasses caching and preserves every execution; failure followed by pass
remains rejected with an instability finding. No automatic retry-to-green.

Exit codes: `0` verified obligations, `1` observed rejection, `2` unresolved
evidence. A skipped dependent task is unresolved, never a successful substitute.

## Inspect and challenge the design

- [Design and report schema](docs/DESIGN.md)
- [Adoption boundaries and next experiments](docs/ADOPTION.md)
- [Public prior art and provenance](docs/PRIOR_ART.md)
- [Security and trust assumptions](SECURITY.md)

```sh
python3 -m unittest discover -s tests -v
python3 -m assay explore --seeds 100 --steps 80
```

The GitHub workflow independently runs the tests and demo on Python 3.11/3.12.
Its artifacts contain the measurements from that runner, not copied local
results. No model calls or paid services are needed to run the experiment.

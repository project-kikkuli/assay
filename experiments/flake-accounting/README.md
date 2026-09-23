# Flake accounting

Separates a flaky task from a genuinely broken one using only repeated real
runs with receipts (`assay.runner.audit`, unmodified — no mocked outcomes),
and measures what a quarantine policy would have saved.

`fixtures/manifest/` declares two tasks: `flaky`, which fails on unseeded
`random.random()` about a third of the time (each repeat is a fresh
subprocess, so the entropy is real, not simulated), and `broken`, a
deterministic `assertEqual(1 + 1, 3)` that fails identically every time —
standing in for a real regression, not a race. The quarantine policy is: stop
rerunning a task in CI once it has shown **two different statuses** across
its repeats; `quarantine_trigger` finds that index per campaign.

```sh
python3 experiments/flake-accounting/measure.py --campaigns 30 --repeats 15
```

## Measured result

30 campaigns of 15 real repeats each (450 runs per task; [full
results](results.json)):

- **`broken`**: observed failure rate **1.0**, **0/30 campaigns ever showed a
  flip**. The flip-based policy never quarantines it — a genuinely broken,
  deterministic task is not mistaken for a flaky one and is never silently
  skipped.
- **`flaky`**: observed failure rate **0.351** (close to the fixture's
  declared 0.35). All 30 campaigns showed a flip; quarantine triggered at a
  **mean run 3.23 / median run 2** of each 15-repeat campaign. That leaves a
  **mean of 11.77 avoidable runs per campaign — 78.4% of that task's CI
  executions** — once flakiness is established.

## Limits

The policy here is deliberately simple (flip-on-two-statuses) and is
evaluated only against these two fixtures' failure profiles; a lower or
intermittent real-bug rate changes both numbers. This does not model a test
that is both flaky **and** carries a real regression, so it does not measure
what quarantine would hide in that case — only that a purely deterministic
failure (0% flip rate) is never quarantined by this policy. Sizing that
tradeoff needs a temporal campaign where a genuine regression is introduced
partway through an already-quarantined task's history, which this run does
not attempt.

"""Executable experiment: reuse, invalidation, counterexamples, and query work."""
import json
from pathlib import Path
import tempfile
import time

from .benchmark import measure_queries, quantile
from .runner import run
from .scenarios import FENCING_CASE, explore, replay, shrink


def demo(root: Path) -> dict:
    start = time.perf_counter()
    # Dedicated temporary cache makes cold/warm comparisons repeatable without
    # deleting anyone's existing evidence. No source files are mutated here.
    with tempfile.TemporaryDirectory(prefix="assay-demo-cache-") as cache:
        cold = run(root / "assay.json", cache_dir=cache)
        warm = run(root / "assay.json", cache_dir=cache)
        warm_samples = [warm["duration_ms"]]
        for _ in range(4):
            sample = run(root / "assay.json", cache_dir=cache)
            if sample["status"] != "verified":
                raise AssertionError("Warm validation failed")
            warm_samples.append(sample["duration_ms"])

    correct = replay(FENCING_CASE)
    broken = replay(FENCING_CASE, unsafe=True)
    minimized_actions = shrink(FENCING_CASE)
    minimized = replay(minimized_actions, unsafe=True)
    exploration = explore(seeds=24, steps=40)
    # Exercise invalidation on a fresh synthetic fixture, never the checkout.
    with tempfile.TemporaryDirectory(prefix="assay-input-change-") as directory:
        project = Path(directory)
        (project / "check.py").write_text("print('fixture v1')\n")
        manifest = {"version": 1, "name": "Input-change demonstration", "tasks": [{"id": "fixture", "command": ["{python}", "check.py"], "inputs": ["check.py"]}]}
        (project / "assay.json").write_text(json.dumps(manifest))
        original = run(project / "assay.json")
        reused = run(project / "assay.json")
        (project / "check.py").write_text("print('fixture v2')\n")
        changed = run(project / "assay.json")
        invalidation = {"original_key": original["tasks"][0]["key"], "reused": reused["tasks"][0]["cached"],
                        "changed_key": changed["tasks"][0]["key"], "changed_reused": changed["tasks"][0]["cached"],
                        "changed_duration_ms": changed["duration_ms"]}
    passed = (cold["status"] == warm["status"] == correct["status"] == exploration["status"] == "verified"
              and broken["status"] == minimized["status"] == "rejected"
              and invalidation["reused"] and not invalidation["changed_reused"]
              and invalidation["original_key"] != invalidation["changed_key"])
    benchmarks = measure_queries()
    benchmarks.insert(0, {"name": "Warm verification admission", "samples_ms": warm_samples,
                          "p50_ms": quantile(warm_samples, .5), "p95_ms": quantile(warm_samples, .95),
                          "work": {"tasks": len(warm["tasks"]), "executed_commands": 0}, "source": "assay/runner.py",
                          "notes": "Local advisory cache; not a hosted CI latency or security claim. Five samples only."})
    result = {**cold, "name": "Assay runnable experiment", "status": "verified" if passed else "rejected",
              "duration_ms": (time.perf_counter() - start) * 1000,
              "scenarios": [correct, broken, minimized], "benchmarks": benchmarks,
              "cold_ms": cold["duration_ms"], "warm_ms": warm["duration_ms"],
              "warm_report": warm, "exploration": exploration, "invalidation": invalidation,
              "reproducer": minimized_actions,
              "warnings": [*cold["warnings"],
                           "Demo success includes expected rejection of the deliberately broken implementation.",
                           "Local timings exclude remote runner scheduling, checkout, dependency installation, and artifact deployment.",
                           "The synthetic queue is an executable test subject, not evidence of readiness for arbitrary production workloads."]}
    return result

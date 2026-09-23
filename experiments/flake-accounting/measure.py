"""Flake accounting: separate a flaky task from a genuinely broken one using
only repeated real runs with receipts, then measure what a quarantine policy
(stop rerunning after the task has shown two different verdicts) would save.

Uses `assay.runner.audit`, unmodified, as the receipt source: each repeat is
its own subprocess with fresh interpreter-level entropy, so the flaky
fixture's outcome genuinely varies run to run while the broken fixture's does
not. No mocking of "what a real run would say" — every status here comes
from an actual `unittest` execution.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ASSAY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ASSAY_ROOT))
from assay import runner  # noqa: E402

MANIFEST = Path(__file__).with_name("fixtures") / "manifest" / "assay.json"


def quarantine_trigger(statuses: list[str]) -> int | None:
    """Index (1-based repeat count) at which two different statuses have been
    observed for a task; None if every repeat in the campaign agreed."""
    seen = set()
    for i, status in enumerate(statuses, start=1):
        seen.add(status)
        if len(seen) > 1:
            return i
    return None


def run_campaign(repeats: int) -> dict:
    report = runner.audit(MANIFEST, repeats=repeats, use_cache=False, jobs=1)
    per_task: dict[str, list[str]] = {}
    for repetition in report["repetitions"]:
        for task in repetition["tasks"]:
            per_task.setdefault(task["id"], []).append(task["status"])
    return per_task


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaigns", type=int, default=15)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    args = parser.parse_args()

    campaigns = [run_campaign(args.repeats) for _ in range(args.campaigns)]

    summary = {"measured": True, "campaigns": args.campaigns, "repeats_per_campaign": args.repeats}
    for task_id in ("flaky", "broken"):
        statuses_per_campaign = [c[task_id] for c in campaigns]
        flip_counts = [len(set(s)) for s in statuses_per_campaign]
        triggers = [quarantine_trigger(s) for s in statuses_per_campaign]
        flaky_campaigns = [t for t in triggers if t is not None]
        rejected = sum(s.count("rejected") for s in statuses_per_campaign)
        total = sum(len(s) for s in statuses_per_campaign)
        summary[task_id] = {
            "total_real_runs": total,
            "observed_failure_rate": round(rejected / total, 4),
            "campaigns_with_a_flip": len(flaky_campaigns),
            "campaigns_total": len(statuses_per_campaign),
            "quarantine_trigger_run_index": {
                "mean": round(statistics.mean(flaky_campaigns), 2) if flaky_campaigns else None,
                "median": statistics.median(flaky_campaigns) if flaky_campaigns else None,
            },
            "runs_avoidable_after_quarantine": {
                "mean_per_campaign": round(statistics.mean([args.repeats - t for t in flaky_campaigns]), 2) if flaky_campaigns else 0,
                "as_fraction_of_campaign": round(statistics.mean([(args.repeats - t) / args.repeats for t in flaky_campaigns]), 4) if flaky_campaigns else 0.0,
            },
        }

    report = {"summary": summary, "campaigns": campaigns}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

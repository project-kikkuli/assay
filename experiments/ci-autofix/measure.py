"""What a CI-side auto-fix-and-repush would save on real lint/format red
rounds, compared against catching the same class locally before push.

Reuses [`local-precheck`](../local-precheck/)'s already-measured, real
`caught_rounds` — the same 200-PR sample, no new API calls — restricted to
`lint_or_format` (the mechanically auto-fixable class; `type_check` failures
generally have no reliable `--fix` and are excluded). Local pre-check removes
the ENTIRE round (developer catches it before ever pushing). This lever keeps
the round — CI still has to run once to detect the violation — but replaces
the real measured between-round human gap with an automated fix-commit-push
cycle: the CI job's own real duration (the autofixer runs the same tool as
the checker; `--fix`/`--write` cost about as much as `--check`) plus a fixed,
EXPLICITLY ESTIMATED push/re-trigger overhead, not a measured one.

```sh
python3 experiments/ci-autofix/measure.py
```
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

# ESTIMATED, not measured: typical `git commit && git push` plus GitHub's
# webhook-to-workflow-start latency for a bot-authored commit. Real values
# in this repo's own `ci-convergence` sample show near-zero runner queue
# delay, so this is dominated by the commit/push round-trip itself.
PUSH_AND_RETRIGGER_OVERHEAD_S = 20.0

AUTOFIXABLE_CATEGORIES = {"lint_or_format"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-precheck-results",
                         default=str(Path(__file__).resolve().parents[1]
                                     / "local-precheck" / "results.json"))
    parser.add_argument("--push-overhead-s", type=float, default=PUSH_AND_RETRIGGER_OVERHEAD_S)
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    args = parser.parse_args()

    source = json.loads(Path(args.local_precheck_results).read_text())
    caught = source["caught_rounds"]
    prs_examined = source["summary"]["prs_examined"]

    autofixable = [r for r in caught if r["category"] in AUTOFIXABLE_CATEGORIES]

    records = []
    for r in autofixable:
        gap_s = r["round_cost_s"] - r["round_ci_duration_s"]  # real, capped-at-4h human gap
        autofix_cost_s = r["local_check_duration_s"] + args.push_overhead_s
        savings_s = gap_s - autofix_cost_s
        records.append({
            "repo": r["repo"], "pr": r["pr"],
            "human_gap_s": round(gap_s, 1),
            "autofix_cost_s": round(autofix_cost_s, 1),
            "savings_s": round(savings_s, 1),
        })

    savings = [r["savings_s"] for r in records]
    net_positive = sum(1 for s in savings if s > 0)
    summary = {
        "measured": True,
        "estimated_constant_s": {"push_and_retrigger_overhead": args.push_overhead_s},
        "source": "experiments/local-precheck/results.json (same 200-PR sample)",
        "prs_examined": prs_examined,
        "autofixable_red_rounds_found": len(records),
        "rounds_per_100_prs": round(len(records) / prs_examined * 100, 2) if prs_examined else None,
        "rounds_with_net_positive_savings": net_positive,
        "rounds_with_net_positive_savings_fraction": round(net_positive / len(records), 4)
        if records else None,
        "savings_s_per_round": {
            "mean": round(statistics.mean(savings), 1) if savings else None,
            "median": round(statistics.median(savings), 1) if savings else None,
            "min": round(min(savings), 1) if savings else None,
            "max": round(max(savings), 1) if savings else None,
        },
        "estimated_hours_saved_per_100_prs": round(
            (len(records) / prs_examined * 100) * statistics.mean(savings) / 3600, 2
        ) if prs_examined and savings else None,
        "note": "red rounds are not eliminated (CI still runs once to detect the "
                "violation); what's removed is the human between-round gap, replaced "
                "by the CI-driven fix-commit-push-retrigger cycle. Compare against "
                "local-precheck's per-100-PR figure, which removes the round entirely "
                "but depends on developer compliance running the hook.",
    }

    report = {"summary": summary, "records": records}
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

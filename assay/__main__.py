"""Command-line entry point. No install needed: python3 -m assay demo."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

from .runner import audit, run


def emit(report: dict, output: Path | None) -> None:
    print(f"{report['status'].upper():10} {report.get('name', 'Assay')}  {report['duration_ms']:.2f} ms")
    for task in report.get("tasks", []):
        print(f"  {task['status']:10} {task['id']:24} {'reused' if task['cached'] else 'executed':8} {task['duration_ms']:9.2f} ms")
        if task["status"] != "verified":
            print(f"    {task['reason']}")
    if output:
        from .report import chrome_trace, markdown_report
        output.mkdir(parents=True, exist_ok=True)
        (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
        (output / "report.md").write_text(markdown_report(report))
        (output / "trace.json").write_text(json.dumps(chrome_trace(report), indent=2, allow_nan=False) + "\n")
        if "reproducer" in report:
            (output / "counterexample.json").write_text(json.dumps(report["reproducer"], indent=2) + "\n")
        print(f"  Evidence: {output / 'report.md'}")


def main(argv=None) -> int:
    if sys.version_info < (3, 11):
        print("Assay requires Python 3.11+. Try ./demo to select an installed interpreter.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Explainable verification experiments; local evidence is advisory")
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("run", "audit"):
        sub = commands.add_parser(name)
        sub.add_argument("manifest", nargs="?", default="assay.json")
        sub.add_argument("--jobs", type=int, default=4)
        sub.add_argument("--out", type=Path, default=Path("out"))
        if name == "run":
            sub.add_argument("--no-cache", action="store_true")
        else:
            sub.add_argument("--repeat", type=int, default=3)
    sub = commands.add_parser("demo")
    sub.add_argument("--out", type=Path, default=Path("out"))
    sub = commands.add_parser("replay")
    sub.add_argument("schedule", nargs="?", type=Path)
    sub.add_argument("--unsafe", action="store_true", help="Inject a broken fencing check; expected exit code 1")
    sub.add_argument("--out", type=Path, default=Path("out/replay"))
    sub = commands.add_parser("explore")
    sub.add_argument("--seeds", type=int, default=24)
    sub.add_argument("--steps", type=int, default=40)
    sub.add_argument("--unsafe", action="store_true")
    sub = commands.add_parser("inspect")
    sub.add_argument("report", nargs="?", type=Path, default=Path("out/report.json"))
    selectors = sub.add_mutually_exclusive_group()
    selectors.add_argument("--task")
    selectors.add_argument("--scenario", type=int)
    selectors.add_argument("--benchmark", type=int)
    args = parser.parse_args(argv)
    if args.action == "inspect":
        from .inspect import explain
        try:
            print(explain(json.loads(args.report.read_text()), task=args.task, scenario=args.scenario, benchmark=args.benchmark))
            return 0
        except (OSError, ValueError) as exc:
            print(f"Cannot inspect evidence: {exc}", file=sys.stderr)
            return 2
    elif args.action == "run":
        result = run(args.manifest, use_cache=not args.no_cache, jobs=args.jobs)
    elif args.action == "audit":
        result = audit(args.manifest, repeats=args.repeat, jobs=args.jobs)
    elif args.action == "demo":
        from .demo import demo
        result = demo(Path.cwd())
    elif args.action == "replay":
        from .scenarios import FENCING_CASE, replay
        actions = json.loads(args.schedule.read_text()) if args.schedule else FENCING_CASE
        scenario = replay(actions, unsafe=args.unsafe)
        result = {"schema": "assay.report/v1", "name": "Replay", "status": scenario["status"],
                  "duration_ms": scenario["duration_ms"], "candidate": scenario["semantic_digest"],
                  "tasks": [], "scenarios": [scenario], "reproducer": actions,
                  "warnings": ["Fault injection enabled" if args.unsafe else "Correct implementation"]}
        for event in scenario["events"]:
            print(f"  {event['step']:2} t={event['virtual_time']:3} {event['action']:9} result={event['result']}")
        for failure in scenario["failures"]:
            print(f"  VIOLATION: {failure['rule']} at step {failure['step']}")
    else:
        from .scenarios import explore
        result = explore(args.seeds, args.steps, unsafe=args.unsafe)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "verified" else 1
    emit(result, args.out)
    if args.action == "demo":
        print(f"  Cold {result['cold_ms']:.2f} ms; warm {result['warm_ms']:.2f} ms")
        print(f"  Explored {result['exploration']['events_checked']} events; minimized fault to {len(result['reproducer'])} steps")
        for benchmark in result["benchmarks"][1:]:
            print(f"  {benchmark['name']}: p50={benchmark['p50_ms']:.4f} ms, VM steps={benchmark['work']['sqlite_vm_steps']}")
    return {"verified": 0, "rejected": 1, "unresolved": 2}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())

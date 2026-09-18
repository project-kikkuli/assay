"""Read-only terminal drilldown: obligation -> event -> source -> work."""
import json


def explain(report: dict, *, task=None, scenario=None, benchmark=None) -> str:
    if task is not None:
        record = next((t for t in report.get("tasks", []) if t["id"] == task), None)
        if record is None:
            raise ValueError(f"Unknown task {task!r}")
        return json.dumps(record, indent=2)
    if scenario is not None:
        records = report.get("scenarios", [])
        if scenario < 0 or scenario >= len(records):
            raise ValueError("Scenario index out of range")
        record = records[scenario]
        lines = [record["name"], f"Invariant: {record['invariant']}", f"Source: {record['source']}", f"Semantic digest: {record.get('semantic_digest', 'unavailable')}"]
        for event in record.get("events", []):
            before = {row["id"]: row for row in event["before"]}
            after = {row["id"]: row for row in event["after"]}
            lines.append(f"\nStep {event['step']} | virtual t={event.get('virtual_time', '?')} | {event['action']} | measured {event.get('duration_ms', 0):.4f}ms")
            lines.append(f"  Input: {json.dumps(event.get('input', {}), sort_keys=True)}")
            for key in sorted(set(before) | set(after)):
                old, new = before.get(key, {}), after.get(key, {})
                for field in sorted(set(old) | set(new)):
                    if old.get(field) != new.get(field):
                        lines.append(f"  job[{key}].{field}: {old.get(field)!r} -> {new.get(field)!r}")
            lines.append(f"  Expected: {event.get('expected')!r}; observed: {event['result']!r}")
        for failure in record.get("failures", []):
            lines.append(f"\nVIOLATION: {json.dumps(failure, sort_keys=True)}")
        return "\n".join(lines)
    if benchmark is not None:
        records = report.get("benchmarks", [])
        if benchmark < 0 or benchmark >= len(records):
            raise ValueError("Benchmark index out of range")
        return json.dumps(records[benchmark], indent=2)
    lines = [f"{report.get('name', 'Assay')}: {report['status']}", "Tasks:"]
    lines.extend(f"  --task {t['id']}  [{t['status']}, {'reused' if t['cached'] else 'executed'}]" for t in report.get("tasks", []))
    lines.append("Scenarios:")
    lines.extend(f"  --scenario {i}  {s['name']} [{s['status']}]" for i, s in enumerate(report.get("scenarios", [])))
    lines.append("Benchmarks:")
    lines.extend(f"  --benchmark {i}  {b['name']}" for i, b in enumerate(report.get("benchmarks", [])))
    return "\n".join(lines)

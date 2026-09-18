"""Human-readable and Chrome Trace reports for the Assay experiment.

This module deliberately treats report data as untrusted presentation input.  It
does not execute commands, read files, or infer verification beyond the fields
provided by the caller.
"""

from __future__ import annotations

import html
import math
import re
from typing import Any
from urllib.parse import quote, urlsplit


_SCHEMA = "assay.report/v1"
_DEFAULT_REPO_URL = "https://github.com/project-kikkuli/assay"
_STATUSES = {"verified", "rejected", "unresolved"}


class _SafeCell(str):
    """A Markdown table cell which has already been escaped/rendered."""


def _text(value: Any) -> str:
    """Make arbitrary values readable without allowing Markdown/HTML markup."""
    if value is None:
        return "—"
    value = str(value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    # A single line keeps tables intact; the explicit break is readable in HTML
    # renderers and cannot be interpreted as a table separator.
    value = value.replace("|", "\\|").replace("`", "\\`")
    value = value.replace("[", "\\[").replace("]", "\\]")
    value = value.replace("\n", "<br>")
    return html.escape(value, quote=True).replace("&lt;br&gt;", "<br>")


def _raw(value: Any) -> str:
    return "" if value is None else str(value)


def _status(value: Any) -> str:
    status = _raw(value).strip().lower()
    return status if status in _STATUSES else f"unresolved (unrecognized: {_text(status)})"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _duration(value: Any) -> float:
    return max(0.0, _number(value))


def _jsonish(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return repr(value)
    return "—" if value is None else str(value)


def _inline_code(value: Any) -> str:
    """Use a delimiter longer than any backtick run in untrusted input."""
    raw = _raw(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", raw)), default=0)
    # The surrounding spaces make leading/trailing delimiters unambiguous.
    fence = "`" * max(1, longest + 1)
    return _SafeCell(f"{fence} {html.escape(raw, quote=True)} {fence}")


def _code_block(value: Any) -> str:
    raw = _raw(value).replace("\r\n", "\n").replace("\r", "\n")
    runs = []
    current = 0
    for char in raw:
        if char == "`":
            current += 1
        else:
            runs.append(current)
            current = 0
    runs.append(current)
    fence = "`" * max(3, max(runs, default=0) + 1)
    return f"{fence}text\n{raw}\n{fence}"


def _repo_base(report: dict) -> str:
    candidate = report.get("repo_url", _DEFAULT_REPO_URL)
    if not isinstance(candidate, str):
        return _DEFAULT_REPO_URL
    parsed = urlsplit(candidate.rstrip("/"))
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or any(part in ("", ".", "..") for part in parsed.path.strip("/").split("/"))):
        return _DEFAULT_REPO_URL
    return candidate.rstrip("/")


def _source_link(source: Any, base_url: str = _DEFAULT_REPO_URL) -> str:
    """Render only repository-relative Python/Markdown paths as GitHub links."""
    raw = _raw(source).strip()
    path, sep, line = raw.rpartition(":")
    if not sep or not line.isdigit():
        path, line = raw, ""
    line_number = int(line) if line else None
    valid_path = (
        bool(path)
        and not path.startswith(("/", "\\"))
        and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", path)
        and ":" not in path
        and "\\" not in path
        and not any(ord(char) < 32 or ord(char) == 127 for char in path)
        and all(part not in ("", ".", "..") for part in path.split("/"))
        and path.lower().endswith((".py", ".md"))
        and (line_number is None or line_number > 0)
    )
    if not valid_path:
        return _SafeCell(f"`{_text(raw)}` (rejected source)")
    anchor = f"#L{line_number}" if line_number else ""
    # Relative URL path is intentionally fixed to GitHub's file view.  quote
    # protects spaces and punctuation while keeping path separators readable.
    url = base_url + "/blob/main/" + "/".join(
        quote(part, safe="") for part in path.split("/")
    ) + anchor
    return _SafeCell(f"[{_text(raw)}]({url})")


def _source_list(values: Any, base_url: str = _DEFAULT_REPO_URL) -> str:
    if not isinstance(values, list) or not values:
        return "—"
    return _SafeCell(", ".join(str(_source_link(item, base_url)) for item in values))


def _argv(command: Any) -> str:
    if not isinstance(command, list):
        return "—"
    return _SafeCell(" ".join(str(_inline_code(item)) for item in command) or "(empty)")


def _table_row(values: list[Any]) -> str:
    return "| " + " | ".join(str(value) if isinstance(value, _SafeCell) else _text(value) for value in values) + " |"


def _table_header(values: list[Any]) -> list[str]:
    return [_table_row(values), _table_row(["---"] * len(values))]


def _quantile(samples: list[float], fraction: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def markdown_report(report: dict) -> str:
    """Return a bounded evidence report; missing optional sections are safe."""
    report = report if isinstance(report, dict) else {}
    tasks = report.get("tasks") if isinstance(report.get("tasks"), list) else []
    scenarios = report.get("scenarios") if isinstance(report.get("scenarios"), list) else []
    benchmarks = report.get("benchmarks") if isinstance(report.get("benchmarks"), list) else []
    base_url = _repo_base(report)
    status = _status(report.get("status"))
    lines = [
        f"# Assay evidence: {_text(report.get('name', 'unnamed'))}",
        "",
        f"**Status:** `{status}`  ",
        f"**Schema:** `{_text(report.get('schema', 'missing'))}`  ",
        f"**Candidate:** `{_text(report.get('candidate', '—'))}`  ",
        f"**Measured report duration:** {_duration(report.get('duration_ms')):.3f} ms",
        "",
        "> Verification is limited to the captured inputs, outputs, statuses, and timing in this report. "
        "> It does not claim full instruction tracing or reproduce uncaptured execution.",
        "",
        "## Summary",
        "",
        *_table_header(["Tasks", "Scenarios", "Benchmarks", "Warnings"]),
        _table_row([len(tasks), len(scenarios), len(benchmarks), len(report.get("warnings", []) or [])]),
    ]
    warnings = report.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines += ["", "**Warnings:** " + "; ".join(_text(item) for item in warnings)]

    lines += ["", "## Task drilldown", ""]
    for task in tasks:
        if not isinstance(task, dict):
            lines += ["- `unresolved`: malformed task entry", ""]
            continue
        task_status = _status(task.get("status"))
        reuse = "reuse (cached; not newly executed)" if task.get("cached") else "new execution"
        lines += [
            f"### {_text(task.get('id', 'unnamed'))} — `{task_status}` — {_text(reuse)}",
            "",
            *_table_header(["Key", "Reason", "Description", "Duration", "Start"]),
            _table_row([
                task.get("key", "—"), task.get("reason", "—"), task.get("description", "—"),
                f"{_duration(task.get('duration_ms')):.3f} ms", f"{_number(task.get('start_ms')):.3f} ms",
            ]),
            "",
            *_table_header(["Source", "Command argv", "Declared input files", "Dependencies", "Return code"]),
            _table_row([
                _source_list(task.get("source"), base_url), _argv(task.get("command")),
                _jsonish(task.get("input_files", [])), _jsonish(task.get("needs", [])),
                task.get("returncode", "—"),
            ]),
            "",
            "**Captured stdout**",
            "",
            _code_block(task.get("stdout")),
            "",
            "**Captured stderr**",
            "",
            _code_block(task.get("stderr")),
            "",
        ]

    if scenarios:
        lines += ["## Scenario counterexamples", ""]
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        lines += [
            f"### {_text(scenario.get('name', 'unnamed'))} — `{_status(scenario.get('status'))}`",
            "",
            *_table_header(["Invariant", "Seed", "Duration", "Source"]),
            _table_row([scenario.get("invariant", "—"), scenario.get("seed", "—"),
                        f"{_duration(scenario.get('duration_ms')):.3f} ms", _source_link(scenario.get("source", ""), base_url)]),
        ]
        events = scenario.get("counterexample") or scenario.get("events") or []
        lines += ["", "| Step | Action | Before → After | Result | Work counts | Invariant | Source |",
                  "|---:|---|---|---|---|---|---|"]
        for event in events if isinstance(events, list) else []:
            if not isinstance(event, dict):
                continue
            lines.append(_table_row([
                event.get("step", "—"), event.get("action", "—"),
                f"{_jsonish(event.get('before'))} → {_jsonish(event.get('after'))}",
                event.get("result", "—"), _jsonish(event.get("work", {})),
                scenario.get("invariant", "—"), _source_link(scenario.get("source", ""), base_url),
            ]))
        lines.append("")

    if benchmarks:
        lines += ["## Benchmarks", "", "| Name | Samples | p50 (ms) | p95 (ms) | Work | Source | Notes |", "|---|---:|---:|---:|---|---|---|"]
    for benchmark in benchmarks:
        if not isinstance(benchmark, dict):
            continue
        samples = [x for x in benchmark.get("samples_ms", [])
                   if isinstance(x, (int, float)) and not isinstance(x, bool)
                   and math.isfinite(x) and x >= 0]
        p50 = _quantile(samples, .5)
        p95 = _quantile(samples, .95)
        lines.append(_table_row([
            benchmark.get("name", "—"), len(samples),
            f"{p50:.3f}" if p50 is not None else "not measured",
            f"{p95:.3f}" if p95 is not None else "not measured",
            _jsonish(benchmark.get("work", {})), _source_link(benchmark.get("source", ""), base_url),
            benchmark.get("notes", "—"),
        ]))
    if benchmarks:
        lines += ["", "Benchmark samples are measured durations; virtual time, if present in work or notes, is not substituted for them."]
    return "\n".join(lines).rstrip() + "\n"


def chrome_trace(report: dict) -> dict:
    """Build a Chrome Trace Event Format object without claiming concurrency."""
    report = report if isinstance(report, dict) else {}
    events: list[dict[str, Any]] = [{
        "name": "Assay report", "cat": "metadata", "ph": "M", "ts": 0,
        "pid": 1, "tid": 0, "args": {"schema": report.get("schema", _SCHEMA), "clock": "report-relative monotonic milliseconds"},
    }]
    tasks = report.get("tasks") if isinstance(report.get("tasks"), list) else []
    for tid, task in enumerate(tasks, 1):
        if not isinstance(task, dict):
            continue
        events.append({
            "name": str(task.get("id", "task")), "cat": "task", "ph": "X",
            "ts": _number(task.get("start_ms")) * 1000, "dur": _duration(task.get("duration_ms")) * 1000,
            "pid": 1, "tid": tid,
            "args": {"id": task.get("id"), "status": _status(task.get("status")), "key": task.get("key"),
                     "cached": bool(task.get("cached")), "reuse": bool(task.get("cached")),
                     "source": task.get("source", [])},
        })
    scenarios = report.get("scenarios") if isinstance(report.get("scenarios"), list) else []
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            continue
        tid = 1000 + index
        cursor = 0.0
        events.append({"name": str(scenario.get("name", "scenario")), "cat": "scenario", "ph": "B",
                       "ts": 0, "pid": 1, "tid": tid,
                       "args": {"projection": "sequential; event offsets are not measured concurrency", "status": _status(scenario.get("status"))}})
        for event in scenario.get("events", []) if isinstance(scenario.get("events"), list) else []:
            if not isinstance(event, dict):
                continue
            duration = _duration(event.get("duration_ms"))
            events.append({"name": str(event.get("action", "event")), "cat": "scenario", "ph": "X",
                           "ts": cursor * 1000, "dur": duration * 1000, "pid": 1, "tid": tid,
                           "args": {"projection": True, "step": event.get("step"), "result": event.get("result"), "work": event.get("work", {})}})
            cursor += duration
        events.append({"name": str(scenario.get("name", "scenario")), "cat": "scenario", "ph": "E",
                       "ts": cursor * 1000, "pid": 1, "tid": tid, "args": {"projection": True}})
    return {"traceEvents": events, "displayTimeUnit": "ms", "metadata": {"assay": "assay.report/v1", "timing": "task durations are measured; scenario event lanes may be sequential projections"}}

"""A real, standalone CI-log localizer: given a pytest text log and/or its
JUnit XML from the same run, print only what's needed to know which test(s)
failed and why -- nothing else.

Two independent extraction modes, usable together or alone:

- `--log PATH` (tail-first): pytest's own "short test summary info" section
  is always the last non-trivial section of a default run's output and
  already names every failing test compactly (`FAILED <nodeid> - <exc>`).
  This mode finds that section by its real header line and prints from
  there to the end -- not merely "the last N lines", which would be a
  guess; this is the one section pytest itself designates as the summary.
  If no such section exists (nothing failed, or a crash pre-empted it),
  falls back to the last `--tail-fallback-lines` lines with a note.
- `--junit PATH` (junit-failures-only): parses the XML and prints only
  `<testcase>` elements that have a `<failure>`/`<error>` child -- nodeid,
  exception type, and message -- skipping every passing entry's bulk.

Combine both to cross-check: the junit extraction is exact (it is
pytest's own structured verdict), the log extraction is what a caller
without junit output still has.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SUMMARY_HEADER_MARKER = "short test summary info"


def extract_tail_summary(log_text: str, fallback_lines: int) -> tuple[str, bool]:
    """Returns (extracted_text, used_real_summary_section)."""
    lines = log_text.splitlines()
    for i, line in enumerate(lines):
        if SUMMARY_HEADER_MARKER in line:
            return "\n".join(lines[i:]), True
    return "\n".join(lines[-fallback_lines:]), False


def extract_junit_failures(xml_path: Path) -> list[dict]:
    root = ET.parse(xml_path).getroot()
    out = []
    for tc in root.iter("testcase"):
        problem = tc.find("failure")
        kind = "failure"
        if problem is None:
            problem = tc.find("error")
            kind = "error"
        if problem is None:
            continue
        message = problem.get("message", "")
        exception_type = problem.get("type") or message.split(":", 1)[0]
        out.append({
            "classname": tc.get("classname", ""),
            "name": tc.get("name", ""),
            "kind": kind,
            "exception_type": exception_type,
            "message": message,
        })
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", help="A pytest text log to extract the tail summary from.")
    parser.add_argument("--junit", help="A pytest --junit-xml file to extract failures-only from.")
    parser.add_argument("--tail-fallback-lines", type=int, default=40)
    args = parser.parse_args()

    if not args.log and not args.junit:
        parser.error("pass --log, --junit, or both")

    if args.log:
        text = Path(args.log).read_text()
        summary, used_real_section = extract_tail_summary(text, args.tail_fallback_lines)
        label = "short test summary info" if used_real_section else f"last {args.tail_fallback_lines} lines (no summary section found)"
        print(f"=== log tail ({label}) ===")
        print(summary)

    if args.junit:
        failures = extract_junit_failures(Path(args.junit))
        print(f"=== junit failures ({len(failures)}) ===")
        for f in failures:
            print(f"{f['kind'].upper()} {f['classname']}::{f['name']} -- {f['exception_type']}: {f['message']}")


if __name__ == "__main__":
    main()

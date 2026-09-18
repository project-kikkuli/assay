"""Measure owner-scoped Kernel.list before and after its one covering index."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

try:
    from .fixture import public_fixture, seed_users
    from .kernel import Kernel
except ImportError:
    from fixture import public_fixture, seed_users
    from kernel import Kernel


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUTPUT = ROOT / "out/cell/performance.json"
SOURCE_FILES = ("performance.py", "test_performance.py", "kernel.py", "fixture.py")
SCALES = (100, 100_000)
OWNER_ROWS = 20
PAGE_SIZE = 5
INDEX_SQL = (
    "CREATE INDEX cell_perf_owner_created_id "
    'ON public."item" (owner_id, created_at DESC, id DESC)'
)
COUNT_SQL = (
    'SELECT count(*) FROM public."item" '
    "WHERE owner_id = public.cell_kernel_current_user_id()"
)
PAGE_SQL = (
    'SELECT id, title, description, owner_id, created_at FROM public."item" '
    "WHERE owner_id = public.cell_kernel_current_user_id() "
    "ORDER BY created_at DESC, id DESC OFFSET %s LIMIT %s"
)


class PerformanceError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PerformanceError(message)


def source_hashes() -> dict[str, str]:
    return {
        name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
        for name in SOURCE_FILES
    }


def actor(command: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
    del view
    return {
        "op": command["op"],
        "target": command.get("item_id"),
        "fields": dict(command["fields"]),
    }


def seed_items(dsn: str, owner: str, other: str, total: int) -> list[str]:
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, f"assay-cell-performance-{total}")
    base = datetime(2020, 1, 1, tzinfo=UTC)
    rows = []
    owner_ids = []
    for index in range(total):
        item_id = uuid.uuid5(namespace, str(index))
        if index < OWNER_ROWS:
            owner_ids.append(str(item_id))
        rows.append(
            (
                item_id,
                f"perf-{total}-{index}",
                None,
                uuid.UUID(owner if index < OWNER_ROWS else other),
                base + timedelta(seconds=index),
            )
    )
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                'INSERT INTO public."item" '
                "(id,title,description,owner_id,created_at) VALUES (%s,%s,%s,%s,%s)",
                rows,
            )
    return list(reversed(owner_ids))


def plan_tree(node: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "Node Type": "node_type",
        "Relation Name": "relation",
        "Index Name": "index",
        "Startup Cost": "startup_cost",
        "Total Cost": "total_cost",
        "Plan Rows": "plan_rows",
        "Actual Rows": "actual_rows",
        "Actual Loops": "actual_loops",
        "Actual Total Time": "actual_total_time_ms",
        "Shared Hit Blocks": "shared_hit_blocks",
        "Shared Read Blocks": "shared_read_blocks",
        "Rows Removed by Filter": "rows_removed_by_filter",
    }
    result = {output: node[key] for key, output in keys.items() if key in node}
    result["plans"] = [plan_tree(child) for child in node.get("Plans", [])]
    return result


def explain(dsn: str, role: tuple[str, str], query: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    role_name, password = role
    role_dsn = psycopg.conninfo.make_conninfo(dsn, user=role_name, password=password)
    with psycopg.connect(role_dsn, autocommit=True) as connection:
        raw = connection.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, params
        ).fetchone()[0]
    document = json.loads(raw) if isinstance(raw, str) else raw
    top = document[0]
    return {
        "query": query,
        "planning_time_ms": top.get("Planning Time"),
        "execution_time_ms": top.get("Execution Time"),
        "plan": plan_tree(top["Plan"]),
    }


def measure(
    dsn: str,
    kernel: Kernel,
    owner: str,
    other: str,
    total: int,
    expected_ids: list[str],
    indexed: bool,
) -> dict[str, Any]:
    samples = []
    observed_count = None
    observed_ids = None
    other_count = None
    for _ in range(3):
        started = time.perf_counter()
        result = kernel.list(owner, 0, PAGE_SIZE)
        samples.append((time.perf_counter() - started) * 1000)
        require(result["count"] == OWNER_ROWS, "owner count is incorrect")
        require([row["id"] for row in result["data"]] == expected_ids[:PAGE_SIZE], "owner page is incorrect")
        observed_count = result["count"]
        observed_ids = [row["id"] for row in result["data"]]
        other_count = kernel.list(other, 0, 1)["count"]
        require(other_count == total - OWNER_ROWS, "other count is incorrect")
    role = kernel._roles[uuid.UUID(owner)]
    count_plan = explain(dsn, role, COUNT_SQL)
    page_plan = explain(dsn, role, PAGE_SQL, (0, PAGE_SIZE))
    return {
        "indexed": indexed,
        "correct": True,
        "observed_count": observed_count,
        "observed_page_ids": observed_ids,
        "expected_page_ids": expected_ids[:PAGE_SIZE],
        "other_count": other_count,
        "samples_ms": samples,
        "median_ms": statistics.median(samples),
        "min_ms": min(samples),
        "max_ms": max(samples),
        "count_query": count_plan,
        "page_query": page_plan,
    }


def run_scale(subject: Path, admin_dsn: str, total: int) -> dict[str, Any]:
    evidence: dict[str, Any] = {"total_rows": total, "owner_rows": OWNER_ROWS}
    started = time.perf_counter()
    with public_fixture(subject, admin_dsn, evidence) as dsn:
        setup_started = time.perf_counter()
        owner, other = seed_users(dsn)
        kernel = Kernel(dsn, actor)
        try:
            evidence["role_setup"] = kernel.setup_rls([owner, other])
            expected_ids = seed_items(dsn, owner, other, total)
            evidence["setup_seconds"] = time.perf_counter() - setup_started
            analyze_started = time.perf_counter()
            with psycopg.connect(dsn, autocommit=True) as connection:
                connection.execute('ANALYZE public."item"')
            evidence["analyze_baseline_seconds"] = time.perf_counter() - analyze_started
            baseline = measure(dsn, kernel, owner, other, total, expected_ids, False)
            index_started = time.perf_counter()
            with psycopg.connect(dsn, autocommit=True) as connection:
                connection.execute(INDEX_SQL)
            evidence["index_setup_seconds"] = time.perf_counter() - index_started
            analyze_started = time.perf_counter()
            with psycopg.connect(dsn, autocommit=True) as connection:
                connection.execute('ANALYZE public."item"')
            evidence["analyze_indexed_seconds"] = time.perf_counter() - analyze_started
            indexed = measure(dsn, kernel, owner, other, total, expected_ids, True)
            evidence["baseline"] = baseline
            evidence["indexed"] = indexed
        finally:
            expected_roles = set(kernel._created_roles)
            role_cleanup = kernel.cleanup()
            with psycopg.connect(dsn, autocommit=True) as connection:
                remaining_roles = connection.execute(
                    "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                    (sorted(expected_roles),),
                ).fetchall()
            role_cleanup["roles_absent"] = not remaining_roles and set(
                role_cleanup["roles_dropped"]
            ) == expected_roles
            evidence["role_cleanup"] = role_cleanup
            require(role_cleanup["roles_absent"], "tenant roles remain after cleanup")
    evidence["fixture_seconds_including_migrations_and_cleanup"] = time.perf_counter() - started
    require(evidence.get("cleanup") == "removed", "fixture database cleanup failed")
    require(evidence["role_cleanup"]["policy_preserved"] is True, "role cleanup contract missing")
    return evidence


def validate_report(report: dict[str, Any]) -> None:
    require(report.get("status") == "passed", "performance report is not passed")
    provenance = report.get("provenance", {})
    require(provenance.get("source_unchanged") is True, "source changed during run")
    scales = report.get("scales")
    require(isinstance(scales, dict), "scale evidence is missing")
    for total in SCALES:
        require(str(total) in scales, f"scale {total} evidence is missing")
        result = scales[str(total)]
        for key in ("baseline", "indexed"):
            run = result[key]
            require(run["correct"] is True, f"{total} {key} correctness missing")
            require(run["observed_count"] == OWNER_ROWS, "observed owner count mismatch")
            require(run["observed_page_ids"] == run["expected_page_ids"], "observed page mismatch")
            require(run["other_count"] == total - OWNER_ROWS, "observed other count mismatch")
            require(isinstance(run["samples_ms"], list) and len(run["samples_ms"]) >= 3, "timing samples missing")
            require(run["count_query"]["plan"] and run["page_query"]["plan"], "query plan missing")
            require(all(value >= 0 for value in run["samples_ms"]), "invalid timing")
        baseline_tree = json.dumps(result["baseline"], sort_keys=True)
        require("cell_perf_owner_created_id" not in baseline_tree, "baseline used owned index")
        if total == 100_000:
            require("Seq Scan" in baseline_tree, "100k baseline is not a sequential scan")
            indexed_tree = json.dumps(result["indexed"], sort_keys=True)
            require("cell_perf_owner_created_id" in indexed_tree, "100k index plan is not visible")


def write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", type=Path, default=ROOT / "out/lab/fullstack")
    parser.add_argument("--admin-dsn", default="host=127.0.0.1 port=55439 dbname=postgres user=postgres password=assay-local-only")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    before = source_hashes()
    report: dict[str, Any] = {"status": "unknown", "scales": {}, "provenance": {"source_sha256_before": before}}
    write(args.output, report)
    try:
        report["scales"] = {str(total): run_scale(args.subject, args.admin_dsn, total) for total in SCALES}
        report["status"] = "passed"
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = type(exc).__name__
    finally:
        after = source_hashes()
        report["provenance"]["source_sha256_after"] = after
        report["provenance"]["source_unchanged"] = before == after
        write(args.output, report)
    if report["status"] == "passed":
        try:
            validate_report(report)
        except PerformanceError as exc:
            report["status"] = "failed"
            report["validation_error"] = str(exc)
            write(args.output, report)
    print(json.dumps({"status": report["status"], "output": str(args.output), "scales": list(report["scales"])}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

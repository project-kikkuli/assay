from __future__ import annotations

import math
import time
from typing import Any, Callable

import psycopg

from checks import plan_summary


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


def explain(conn: psycopg.Connection, query: str, one: Callable[..., Any]) -> Any:
    return one(conn, f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {query}")[0]


def benchmark_case(
    conn: psycopg.Connection,
    query: str,
    one: Callable[..., Any],
    warmup: int = 5,
    samples: int = 25,
) -> dict[str, Any]:
    for _ in range(warmup):
        one(conn, query)
    result = one(conn, query)
    timings: list[float] = []
    for _ in range(samples):
        start = time.perf_counter_ns()
        one(conn, query)
        timings.append((time.perf_counter_ns() - start) / 1_000_000)
    return {
        "query": query,
        "result": result,
        "samples_ms": timings,
        "p50_ms": percentile(timings, 0.50),
        "p95_ms": percentile(timings, 0.95),
        "min_ms": min(timings),
        "max_ms": max(timings),
        "n": len(timings),
    }


def performance_matrix(
    db_name: str,
    app: str,
    app_password: str,
    bypass: str,
    tenant_a: str,
    tenant_b: str,
    connect: Callable[..., psycopg.Connection],
    exec_sql: Callable[..., None],
    one: Callable[..., Any],
) -> dict[str, Any]:
    app_conn = connect(db_name, app, app_password)
    baseline = connect(db_name)
    try:
        exec_sql(baseline, f"SET ROLE {bypass}")
        entries: list[dict[str, Any]] = []
        for tenant, upper in ((tenant_a, 100), (tenant_b, 100000)):
            rls_query = (
                f"SELECT count(*) FROM events WHERE event_id BETWEEN 1 AND {upper}"
            )
            baseline_query = f"SELECT count(*) FROM events WHERE tenant_id = '{tenant}' AND event_id BETWEEN 1 AND {upper}"
            exec_sql(
                app_conn, "SELECT set_config('app.tenant_id', %s, false)", (tenant,)
            )
            rls_plan = explain(app_conn, rls_query, one)
            baseline_plan = explain(baseline, baseline_query, one)
            rls = benchmark_case(app_conn, rls_query, one)
            baseline_result = benchmark_case(baseline, baseline_query, one)
            rls_summary = plan_summary(rls_plan)
            baseline_summary = plan_summary(baseline_plan)
            entries.append(
                {
                    "tenant": tenant,
                    "expected_rows": upper,
                    "rls": {
                        **rls,
                        "explain_json": rls_plan,
                        "plan_summary": rls_summary,
                    },
                    "baseline_explicit_predicate_bypassrls": {
                        **baseline_result,
                        "explain_json": baseline_plan,
                        "plan_summary": baseline_summary,
                    },
                    "plan_equivalence": {
                        "same_result": rls["result"] == baseline_result["result"],
                        "same_scan_cardinality": rls_summary["scan_actual_rows"]
                        == baseline_summary["scan_actual_rows"],
                        "same_index_names": rls_summary["scan_index_names"]
                        == baseline_summary["scan_index_names"],
                    },
                    "plan_classification": "equivalent"
                    if rls_summary["scan_actual_rows"]
                    == baseline_summary["scan_actual_rows"]
                    and rls_summary["scan_index_names"]
                    == baseline_summary["scan_index_names"]
                    else "non-equivalent-plan",
                    "semantic_note": "Both queries count the same tenant/event range; baseline is not an isolation claim.",
                }
            )
        exec_sql(app_conn, "RESET app.tenant_id")
        return {"rows_total": 100100, "repetitions": 25, "warmup": 5, "cases": entries}
    finally:
        app_conn.close()
        baseline.close()

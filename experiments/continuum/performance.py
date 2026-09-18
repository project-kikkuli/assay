from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import platform
import sqlite3
import statistics
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    from .app.api import ReservationService, Store
except ImportError:
    from app.api import ReservationService, Store


HERE = Path(__file__).resolve().parent
API_PATH = HERE / "app" / "api.py"
SCALES = (1_000, 10_000, 100_000)
SAMPLES = 3
PAGE_SIZE = 7
MEASURE_LIMIT = 100
CAPACITY_SQL = (
    "SELECT COALESCE(SUM(quantity), 0) AS used FROM reservations "
    "WHERE tenant=? AND status IN ('reserved', 'approved', 'fulfilled')"
)
LIST_FIRST_SQL = (
    "SELECT id, tenant, creator, quantity, status, seq FROM reservations "
    "WHERE tenant=? ORDER BY seq LIMIT ?"
)
LIST_CURSOR_SQL = (
    "SELECT id, tenant, creator, quantity, status, seq FROM reservations "
    "WHERE tenant=? AND seq > ? ORDER BY seq LIMIT ?"
)
CARDINALITY_SQL = "SELECT COUNT(*) FROM reservations WHERE tenant=?"
CANDIDATE_DDL = (
    "CREATE INDEX continuum_perf_tenant_seq "
    "ON reservations(tenant, seq)",
    "CREATE INDEX continuum_perf_tenant_status_quantity "
    "ON reservations(tenant, status, quantity)",
)


class PerformanceError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PerformanceError(message)


class ObservedStore(Store):
    """Store whose real application reads report SQLite VM opcode callbacks."""

    def __init__(self, path: Path):
        self.observe = False
        self.opcode_counts: list[int] = []
        super().__init__(path)
        self.observe = True

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        count = 0

        if self.observe:
            def progress() -> int:
                nonlocal count
                count += 1
                return 0

            connection.set_progress_handler(progress, 1)
        try:
            yield connection
        finally:
            if self.observe:
                connection.set_progress_handler(None, 0)
                self.opcode_counts.append(count)
            connection.close()

    def take_opcode_count(self) -> int:
        require(len(self.opcode_counts) == 1, "expected one observed application query")
        return self.opcode_counts.pop()


def source_record() -> dict[str, Any]:
    source = API_PATH.read_text()
    list_source = inspect.getsource(ReservationService.list_reservations)
    capacity_source = inspect.getsource(ReservationService._reserve)
    markers = {
        "list_select": LIST_FIRST_SQL.split(" WHERE")[0] in list_source,
        "list_tenant_order": 'query = "SELECT id, tenant, creator, quantity, status, seq FROM reservations WHERE tenant=?"' in list_source,
        "list_cursor": 'query += " AND seq > ?"' in list_source,
        "list_order_limit": 'query += " ORDER BY seq LIMIT ?"' in list_source,
        "capacity_select": "SELECT COALESCE(SUM(quantity), 0) AS used" in capacity_source,
        "capacity_predicate": "WHERE tenant=? AND status IN ('reserved', 'approved', 'fulfilled')" in capacity_source,
    }
    require(all(markers.values()), "application SQL source drifted from the performance observer")
    require(source.count("SELECT id, tenant, creator, quantity, status, seq FROM reservations") == 1,
            "list SQL source marker is not unique")
    require(source.count("SELECT COALESCE(SUM(quantity), 0) AS used") == 1,
            "capacity SQL source marker is not unique")
    return {
        "api_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "list_function_sha256": hashlib.sha256(list_source.encode()).hexdigest(),
        "capacity_function_sha256": hashlib.sha256(capacity_source.encode()).hexdigest(),
        "checked_markers": markers,
    }


def seed_rows(store: ObservedStore, total: int) -> tuple[list[tuple[int, dict[str, Any]]], int]:
    alpha_rows: list[tuple[int, dict[str, Any]]] = []
    rows: list[tuple[str, str, str, int, str, int]] = []
    alpha_ordinal = 0
    active_statuses = ("reserved", "approved", "fulfilled")
    store.observe = False
    try:
        with store.write() as connection:
            for index in range(total):
                sequence = index + 1
                tenant = "alpha" if index % 20 == 0 else "beta"
                creator = f"{tenant}-maker"
                if tenant == "alpha":
                    status = (
                        active_statuses[alpha_ordinal % len(active_statuses)]
                        if alpha_ordinal < 8
                        else "cancelled"
                    )
                    alpha_ordinal += 1
                else:
                    status = "cancelled"
                reservation = {
                    "id": f"reservation-{total}-{index:06d}",
                    "tenant": tenant,
                    "creator": creator,
                    "quantity": 1,
                    "status": status,
                }
                rows.append((reservation["id"], tenant, creator, 1, status, sequence))
                if tenant == "alpha":
                    alpha_rows.append((sequence, reservation))
            connection.executemany(
                "INSERT INTO reservations(id, tenant, creator, quantity, status, seq) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.execute("ANALYZE")
    finally:
        store.observe = True
        store.opcode_counts.clear()
    return alpha_rows, sum(
        reservation["quantity"]
        for _, reservation in alpha_rows
        if reservation["status"] in {"reserved", "approved", "fulfilled"}
    )


def install_candidate_indexes(store: ObservedStore) -> None:
    store.observe = False
    try:
        with store.write() as connection:
            for ddl in CANDIDATE_DDL:
                connection.execute(ddl)
            connection.execute("ANALYZE")
    finally:
        store.observe = True
        store.opcode_counts.clear()


def readonly_query(
    store: ObservedStore, sql: str, parameters: tuple[Any, ...], collect_opcodes: bool = True
) -> tuple[list[sqlite3.Row], int | None, float]:
    connection = store._connect()
    try:
        connection.execute("PRAGMA query_only=ON")
        opcode_count = 0

        def progress() -> int:
            nonlocal opcode_count
            opcode_count += 1
            return 0

        if collect_opcodes:
            connection.set_progress_handler(progress, 1)
        started = time.perf_counter_ns()
        rows = connection.execute(sql, parameters).fetchall()
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        if collect_opcodes:
            connection.set_progress_handler(None, 0)
        return rows, opcode_count, elapsed_ms
    finally:
        connection.close()


def query_plan(
    store: ObservedStore, sql: str, parameters: tuple[Any, ...]
) -> list[dict[str, Any]]:
    connection = store._connect()
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute("EXPLAIN QUERY PLAN " + sql, parameters).fetchall()
        return [
            {"id": row[0], "parent": row[1], "notused": row[2], "detail": row[3]}
            for row in rows
        ]
    finally:
        connection.close()


def observed_cardinality(store: ObservedStore) -> int:
    rows, _, _ = readonly_query(store, CARDINALITY_SQL, ("alpha",))
    return int(rows[0][0])


def list_once(
    store: ObservedStore,
    service: ReservationService,
    actor: sqlite3.Row,
    limit: int,
    cursor: int | None,
) -> tuple[list[dict[str, Any]], str | None, int]:
    store.opcode_counts.clear()
    items, next_cursor = service.list_reservations(actor, limit, cursor)
    return items, next_cursor, store.take_opcode_count()


def boundary_evidence(
    store: ObservedStore,
    service: ReservationService,
    actor: sqlite3.Row,
    alpha_rows: list[tuple[int, dict[str, Any]]],
) -> dict[str, Any]:
    expected = [row for _, row in alpha_rows]
    first, first_next, _ = list_once(store, service, actor, PAGE_SIZE, None)
    require(first_next is not None, "fixture does not exercise a first-page boundary")
    second, second_next, _ = list_once(store, service, actor, PAGE_SIZE, int(first_next))
    repeated, repeated_next, _ = list_once(store, service, actor, PAGE_SIZE, int(first_next))
    before_first, before_next, _ = list_once(store, service, actor, PAGE_SIZE, alpha_rows[0][0] - 1)
    terminal, terminal_next, _ = list_once(store, service, actor, PAGE_SIZE, alpha_rows[-1][0])
    first_ids = [row["id"] for row in first]
    second_ids = [row["id"] for row in second]
    result = {
        "expected_alpha_count": len(expected),
        "observed_alpha_count": observed_cardinality(store),
        "first_ids": first_ids,
        "second_ids": second_ids,
        "first_next_cursor": first_next,
        "second_next_cursor": second_next,
        "repeated_second_ids": [row["id"] for row in repeated],
        "repeated_second_cursor": repeated_next,
        "before_first_ids": [row["id"] for row in before_first],
        "before_first_cursor": before_next,
        "terminal_items": terminal,
        "terminal_cursor": terminal_next,
        "matches_expected": (
            first == expected[:PAGE_SIZE]
            and second == expected[PAGE_SIZE:PAGE_SIZE * 2]
            and repeated == second
            and before_first == first
            and not terminal
            and len(set(first_ids).intersection(second_ids)) == 0
        ),
    }
    require(result["observed_alpha_count"] == result["expected_alpha_count"], "list cardinality mismatch")
    require(result["matches_expected"], "list pagination boundary or output mismatch")
    return result


def measure_list(
    store: ObservedStore,
    service: ReservationService,
    actor: sqlite3.Row,
    expected: list[tuple[int, dict[str, Any]]],
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    expected_items = [row for _, row in expected]
    for _ in range(SAMPLES):
        collect_opcodes = not samples
        store.opcode_counts.clear()
        store.observe = collect_opcodes
        started = time.perf_counter_ns()
        items, next_cursor = service.list_reservations(actor, MEASURE_LIMIT, None)
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        opcode_count = store.take_opcode_count() if collect_opcodes else None
        samples.append({
            "elapsed_ms": elapsed_ms,
            "progress_opcodes_n1": opcode_count,
        })
        require(items == expected_items[:MEASURE_LIMIT], "actual API list output mismatch")
        expected_next = (
            str(expected[MEASURE_LIMIT - 1][0])
            if len(expected_items) > MEASURE_LIMIT
            else None
        )
        require(next_cursor == expected_next, "measurement cursor mismatch")
    store.observe = True
    opcode_samples = [
        sample["progress_opcodes_n1"]
        for sample in samples
        if sample["progress_opcodes_n1"] is not None
    ]
    return {
        "returned_count": len(expected_items[:MEASURE_LIMIT]),
        "expected_count": len(expected_items),
        "page_ids_sha256": hashlib.sha256(
            json.dumps([row["id"] for row in expected_items[:MEASURE_LIMIT]]).encode()
        ).hexdigest(),
        "samples": samples,
        "first_ms": samples[0]["elapsed_ms"],
        "median_ms": statistics.median(sample["elapsed_ms"] for sample in samples),
        "first_progress_opcodes_n1": samples[0]["progress_opcodes_n1"],
        "median_progress_opcodes_n1": statistics.median(opcode_samples),
    }


def measure_capacity(store: ObservedStore, expected_sum: int) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for _ in range(SAMPLES):
        collect_opcodes = not samples
        rows, opcodes, elapsed_ms = readonly_query(
            store, CAPACITY_SQL, ("alpha",), collect_opcodes
        )
        used = int(rows[0]["used"])
        require(used == expected_sum, "capacity SQL output mismatch")
        samples.append({"elapsed_ms": elapsed_ms, "progress_opcodes_n1": opcodes})
    return {
        "used": expected_sum,
        "samples": samples,
        "first_ms": samples[0]["elapsed_ms"],
        "median_ms": statistics.median(sample["elapsed_ms"] for sample in samples),
        "first_progress_opcodes_n1": samples[0]["progress_opcodes_n1"],
        "median_progress_opcodes_n1": samples[0]["progress_opcodes_n1"],
    }


def run_variant(
    directory: Path,
    total: int,
    name: str,
    candidate: bool,
) -> dict[str, Any]:
    store = ObservedStore(directory / f"{name}-{total}.sqlite")
    alpha_rows, expected_sum = seed_rows(store, total)
    if candidate:
        install_candidate_indexes(store)
    service = ReservationService(store)
    actor = store.identity("alpha-maker")
    store.opcode_counts.clear()
    boundaries = boundary_evidence(store, service, actor, alpha_rows)
    store.opcode_counts.clear()
    list_result = measure_list(store, service, actor, alpha_rows)
    capacity_result = measure_capacity(store, expected_sum)
    plans = {
        "list_first_page": query_plan(store, LIST_FIRST_SQL, ("alpha", MEASURE_LIMIT + 1)),
        "list_cursor_page": query_plan(
            store,
            LIST_CURSOR_SQL,
            ("alpha", alpha_rows[PAGE_SIZE - 1][0], MEASURE_LIMIT + 1),
        ),
        "capacity": query_plan(store, CAPACITY_SQL, ("alpha",)),
    }
    return {
        "schema_variant": name,
        "candidate_indexes": list(CANDIDATE_DDL) if candidate else [],
        "boundaries": boundaries,
        "list": list_result,
        "capacity": capacity_result,
        "query_plans": plans,
    }


def equivalence(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    existing_boundary = existing["boundaries"]
    candidate_boundary = candidate["boundaries"]
    return {
        "list_boundary_outputs_equal": existing_boundary == candidate_boundary,
        "list_cardinality_equal": existing_boundary["observed_alpha_count"]
        == candidate_boundary["observed_alpha_count"],
        "list_page_signature_equal": existing["list"]["page_ids_sha256"]
        == candidate["list"]["page_ids_sha256"],
        "capacity_output_equal": existing["capacity"]["used"] == candidate["capacity"]["used"],
    }


def ratio(
    cases: dict[str, Any], variant: str, query: str, metric: str
) -> dict[str, float | None]:
    low = float(cases[str(SCALES[0])][variant][query][metric])
    high = float(cases[str(SCALES[-1])][variant][query][metric])
    return {
        "at_1000": low,
        "at_100000": high,
        "ratio_100000_to_1000": high / low if low else None,
    }


def build_report() -> dict[str, Any]:
    started = time.perf_counter_ns()
    source_before = source_record()
    cases: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="continuum-performance-") as temporary:
        directory = Path(temporary)
        for total in SCALES:
            existing = run_variant(directory, total, "existing", False)
            candidate = run_variant(directory, total, "candidate", True)
            checks = equivalence(existing, candidate)
            require(all(checks.values()), f"variant output mismatch at {total} rows")
            cases[str(total)] = {
                "total_rows": total,
                "cancelled_rows": total - 8,
                "existing_schema": existing,
                "candidate_indexes": candidate,
                "equivalence": checks,
            }
    source_after = source_record()
    require(source_before == source_after, "application source changed during benchmark")
    report = {
        "status": "observed",
        "experiment": "continuum SQLite reservation cost evidence",
        "contract_version": 1,
        "python": platform.python_version(),
        "runtime_seconds": (time.perf_counter_ns() - started) / 1_000_000_000,
        "scales": list(SCALES),
        "samples_per_query": SAMPLES,
        "method": {
            "list": "ReservationService.list_reservations with an observed Store connection",
            "capacity": CAPACITY_SQL,
            "progress_handler": "sqlite3 set_progress_handler(callback, 1); each callback represents one VM opcode",
            "timings": "first connection/query sample plus repeated samples; diagnostic only",
            "warm_cache_note": "opcode counts and first samples are retained because repeated timings can be cache-warm",
        },
        "candidate_ddl": list(CANDIDATE_DDL),
        "source": source_before,
        "cases": cases,
        "opcode_slope": {
            "existing_list_first": ratio(
                cases, "existing_schema", "list", "first_progress_opcodes_n1"
            ),
            "candidate_list_first": ratio(
                cases, "candidate_indexes", "list", "first_progress_opcodes_n1"
            ),
            "existing_capacity_first": ratio(
                cases, "existing_schema", "capacity", "first_progress_opcodes_n1"
            ),
            "candidate_capacity_first": ratio(
                cases, "candidate_indexes", "capacity", "first_progress_opcodes_n1"
            ),
        },
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=HERE / "performance.json")
    args = parser.parse_args(argv)
    report = build_report()
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

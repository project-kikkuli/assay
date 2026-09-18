"""Explain query work as well as timings, without timing-based correctness gates."""
import math
import sqlite3
import statistics
import time

BASELINE_QUERY = """
SELECT id FROM jobs
WHERE status = 'queued' OR (status = 'leased' AND lease_until <= ?)
ORDER BY id LIMIT 1
"""

# Select one candidate per eligibility class before merging. This exploits a
# status/id index for queued rows. Expired leases can still require a range scan
# and sorting: the experiment does not claim O(log n) for every distribution.
CANDIDATE_QUERY = """
SELECT id FROM (
  SELECT id FROM (SELECT id FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1)
  UNION ALL
  SELECT id FROM (SELECT id FROM jobs WHERE status = 'leased' AND lease_until <= ? ORDER BY id LIMIT 1)
) ORDER BY id LIMIT 1
"""


def quantile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def measure_queries(sizes=(100, 1000, 10000), repeats=15) -> list[dict]:
    if repeats < 2 or any(n < 1 for n in sizes):
        raise ValueError("Need positive sizes and at least two timing samples")
    reports = []
    for size in sizes:
        with sqlite3.connect(":memory:") as db:
            db.executescript("""
                CREATE TABLE jobs(id INTEGER PRIMARY KEY, status TEXT, lease_until INTEGER);
                CREATE INDEX jobs_status_expiry_id ON jobs(status, lease_until, id);
                CREATE INDEX jobs_status_id ON jobs(status, id);
            """)
            # A deliberately specified ready-heavy distribution; not an average
            # production workload. Setup is excluded from the SELECT comparison.
            db.executemany("INSERT INTO jobs VALUES (?, 'queued', 0)", ((i,) for i in range(1, size + 1)))
            expected = [(1,)]
            for name, query in (("OR eligibility scan", BASELINE_QUERY), ("two indexed candidate heads", CANDIDATE_QUERY)):
                assert db.execute(query, (10,)).fetchall() == expected
                samples = []
                for _ in range(repeats):
                    start = time.perf_counter_ns()
                    result = db.execute(query, (10,)).fetchall()
                    samples.append((time.perf_counter_ns() - start) / 1_000_000)
                    if result != expected:
                        raise AssertionError("Query rewrite changed selected job")
                work = [0]

                def tick():
                    work[0] += 1
                    return 0

                # Separate execution: per-opcode instrumentation distorts timing.
                db.set_progress_handler(tick, 1)
                db.execute(query, (10,)).fetchall()
                db.set_progress_handler(None, 0)
                plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + query, (10,))]
                reports.append({"name": f"{name}: {size:,} ready jobs", "samples_ms": samples,
                                "p50_ms": statistics.median(samples), "p95_ms": quantile(samples, .95),
                                "source": "assay/benchmark.py", "work": {"rows": size, "sqlite_vm_steps": work[0], "query_plan": plan},
                                "query": query.strip(), "notes": "Warm in-memory SELECT only; setup and writes excluded. VM steps measured separately. Both queries get the same two indexes. Ready-heavy synthetic distribution; no latency gate."})
    return reports

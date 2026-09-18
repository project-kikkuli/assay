#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]==3.3.4"]
# ///
"""Run bounded real PostgreSQL concurrency and crash-cutpoint experiments."""

from __future__ import annotations

import hashlib
import argparse
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
import uuid

HERE = Path(__file__).resolve().parent
OUTCOME_TIMEOUT = 8.0
BLOCK_TIMEOUT = 5.0
MAX_CHILD_OUTPUT = 65536
PG_KEYS = ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE")
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
SOURCE_PATHS = {
    "schema.sql": HERE / "schema.sql",
    "consumer.ts": HERE / "consumer.ts",
    "repaired_consumer.ts": HERE / "repaired_consumer.ts",
    "run.py": HERE / "run.py",
    "test_concurrency.py": HERE / "test_concurrency.py",
    "README.md": HERE / "README.md",
    "outbox/package-lock.json": HERE.parent / "outbox/package-lock.json",
    "outbox/fixtures/db.env": HERE.parent / "outbox/fixtures/db.env",
}


def source_hashes() -> dict[str, str]:
    return {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in SOURCE_PATHS.items()
    }


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (
        (HERE.parent / "outbox" / "fixtures" / "db.env").read_text().splitlines()
    ):
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            if key not in PG_KEYS:
                raise ValueError(f"unsupported fixture key: {key}")
            values[key] = value
    if set(values) != set(PG_KEYS):
        raise ValueError("fixture must define exactly the five PG keys")
    if values["PGHOST"] not in ALLOWED_HOSTS:
        raise ValueError("only the local PostgreSQL fixture is allowed")
    if values["PGDATABASE"] != "postgres":
        raise ValueError("fixture maintenance database must be postgres")
    if not values["PGPORT"].isdigit():
        raise ValueError("fixture PGPORT must be numeric")
    return values


def connect(env: dict[str, str], database: str | None = None, *, autocommit=False):
    import psycopg

    return psycopg.connect(
        host=env["PGHOST"],
        port=int(env["PGPORT"]),
        user=env["PGUSER"],
        password=env["PGPASSWORD"],
        dbname=database or env["PGDATABASE"],
        connect_timeout=3,
        autocommit=autocommit,
    )


def provision(env: dict[str, str]) -> str:
    database = f"assay_outbox_concurrency_{uuid.uuid4().hex}"
    if not re.fullmatch(r"assay_outbox_concurrency_[0-9a-f]{32}", database):
        raise ValueError("generated database name failed ownership validation")
    with connect(env, "postgres", autocommit=True) as db:
        db.execute(f'CREATE DATABASE "{database}"')
    return database


def drop_database(env: dict[str, str], database: str) -> None:
    if not re.fullmatch(r"assay_outbox_concurrency_[0-9a-f]{32}", database):
        raise ValueError("refusing to drop an unowned database")
    with connect(env, "postgres", autocommit=True) as db:
        db.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
        still_exists = db.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
        if still_exists is not None:
            raise RuntimeError("owned database still exists after DROP DATABASE")


def reset(env: dict[str, str]) -> None:
    with connect(env) as db:
        db.execute((HERE / "schema.sql").read_text())
        db.execute("INSERT INTO accepted_commands(command_id) VALUES ('cmd-1')")
        db.execute(
            "INSERT INTO outbox_events(event_id, command_id, status) VALUES ('evt:cmd-1', 'cmd-1', 'pending')"
        )


def subprocess_env(env: dict[str, str], database: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C",
        **{key: env[key] for key in PG_KEYS},
        "PGDATABASE": database,
    }


def start_child(env: dict[str, str], database: str, source: str, barrier=""):
    return subprocess.Popen(
        [
            "node",
            "--experimental-strip-types",
            str(HERE / source),
            "evt:cmd-1",
            barrier,
        ],
        cwd=HERE,
        env=subprocess_env(env, database),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=True,
    )


def read_until(children, predicate, timeout: float) -> list[dict[str, object]]:
    selector = selectors.DefaultSelector()
    buffers = {}
    output_sizes = {}
    for index, child in enumerate(children):
        if child.stdout is None:
            raise RuntimeError("child stdout was not captured")
        os.set_blocking(child.stdout.fileno(), False)
        buffers[index] = b""
        output_sizes[index] = 0
        selector.register(child.stdout.fileno(), selectors.EVENT_READ, index)
    events: list[dict[str, object]] = []
    deadline = time.monotonic() + timeout

    def parse_line(index: int, line: bytes) -> None:
        if len(line) > MAX_CHILD_OUTPUT:
            raise RuntimeError("child emitted an oversized NDJSON record")
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"child emitted invalid NDJSON: {line!r}") from error
        if not isinstance(event, dict):
            raise RuntimeError("child NDJSON event was not an object")
        event["child"] = index
        events.append(event)

    try:
        while not predicate(events):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"barrier timeout; events={events}")
            ready = selector.select(remaining)
            if not ready:
                raise RuntimeError(f"barrier timeout; events={events}")
            for key, _ in ready:
                index = key.data
                chunk = os.read(key.fd, 4096)
                if not chunk:
                    tail = buffers[index]
                    if tail:
                        parse_line(index, tail)
                        buffers[index] = b""
                    if not predicate(events):
                        child = children[index]
                        raise RuntimeError(
                            f"child {index} reached stdout EOF before barrier; returncode={child.poll()}"
                        )
                    continue
                output_sizes[index] += len(chunk)
                if output_sizes[index] > MAX_CHILD_OUTPUT:
                    raise RuntimeError("child output exceeded bounded limit")
                buffers[index] += chunk
                while b"\n" in buffers[index]:
                    line, buffers[index] = buffers[index].split(b"\n", 1)
                    if line:
                        parse_line(index, line)
                    # Consume the complete chunk before returning.  A partial
                    # final record is retained on the child for communicate().
    finally:
        for index, child in enumerate(children):
            setattr(child, "_assay_pending", buffers[index])
            setattr(child, "_assay_output_size", output_sizes[index])
        selector.close()
    return events


def finish_children(
    children,
    prior_events: list[dict[str, object]] | None = None,
    collected_events: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    prior_events = prior_events or []
    outcomes = []
    for index, child in enumerate(children):
        stdout, stderr = child.communicate(timeout=OUTCOME_TIMEOUT)
        pending = getattr(child, "_assay_pending", b"")
        stdout = pending + stdout
        prior_size = getattr(child, "_assay_output_size", 0)
        if prior_size + len(stdout) + len(stderr) > MAX_CHILD_OUTPUT:
            raise RuntimeError("child output exceeded bounded limit")
        parsed = []
        for line in stdout.decode("utf-8").splitlines():
            if line:
                event = json.loads(line)
                event["child"] = index
                parsed.append(event)
        if collected_events is not None:
            collected_events.extend(parsed)
        lines = [
            *[event for event in prior_events if event.get("child") == index],
            *parsed,
        ]
        outcome = next((line for line in lines if line.get("phase") == "outcome"), None)
        if child.returncode != 0 or outcome is None:
            raise RuntimeError(
                f"child failed returncode={child.returncode} stdout={stdout!r} stderr={stderr!r}"
            )
        outcomes.append(outcome)
    return outcomes


def kill_children(children) -> list[int]:
    returncodes = []
    for child in children:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=OUTCOME_TIMEOUT)
        returncodes.append(child.returncode)
    return returncodes


def state(env: dict[str, str], database: str) -> dict[str, object]:
    with connect(env, database) as db:
        outbox = db.execute(
            "SELECT event_id, command_id, status FROM outbox_events ORDER BY event_id"
        ).fetchall()
        processed = db.execute(
            "SELECT event_id FROM processed_events ORDER BY event_id"
        ).fetchall()
        ledger = db.execute(
            "SELECT command_id, count(*) FROM ledger_effects GROUP BY command_id ORDER BY command_id"
        ).fetchall()
    return {
        "outbox": [
            dict(event_id=row[0], command_id=row[1], status=row[2]) for row in outbox
        ],
        "processed": [row[0] for row in processed],
        "ledger": [dict(command_id=row[0], count=row[1]) for row in ledger],
    }


def blocker_snapshot(db, pids: list[int]) -> dict[str, list[int]]:
    return {
        str(pid): list(db.execute("SELECT pg_blocking_pids(%s)", (pid,)).fetchone()[0])
        for pid in pids
    }


def blocking_graph(db, roots: list[int]) -> dict[str, list[int]]:
    graph: dict[str, list[int]] = {}
    pending = list(roots)
    while pending:
        pid = pending.pop(0)
        if str(pid) in graph:
            continue
        if len(graph) >= 32:
            raise RuntimeError("blocking graph exceeded bounded node limit")
        blockers = blocker_snapshot(db, [pid])[str(pid)]
        graph[str(pid)] = blockers
        pending.extend(blocker for blocker in blockers if str(blocker) not in graph)
    return graph


def wait_until_blocked(db, pids: list[int]) -> dict[str, list[int]]:
    deadline = time.monotonic() + BLOCK_TIMEOUT
    latest: dict[str, list[int]] = {}
    while time.monotonic() < deadline:
        latest = blocking_graph(db, pids)
        if all(latest.get(str(pid)) for pid in pids):
            return latest
        time.sleep(0.05)
    raise RuntimeError(f"both consumers were not blocked by coordinator: {latest}")


def run_concurrency(
    env: dict[str, str], database: str, source: str
) -> dict[str, object]:
    reset(env)
    coordinator = connect(env, database)
    children = []
    try:
        coordinator.execute("BEGIN")
        coordinator.execute(
            "SELECT event_id FROM outbox_events WHERE event_id = 'evt:cmd-1' FOR UPDATE"
        )
        coordinator_pid = int(coordinator.info.backend_pid)
        children = [start_child(env, database, source) for _ in range(2)]
        events = read_until(
            children,
            lambda rows: all(
                any(
                    row.get("child") == child
                    and row.get("phase") == "waiting_for_row_lock"
                    for row in rows
                )
                for child in range(2)
            ),
            BLOCK_TIMEOUT,
        )
        pids = [
            next(
                row["backend_pid"]
                for row in events
                if row.get("child") == child and row.get("phase") == "connected"
            )
            for child in range(2)
        ]
        blockers = wait_until_blocked(coordinator, pids)
        coordinator.rollback()
        post_release_events: list[dict[str, object]] = []
        outcomes = finish_children(children, events, post_release_events)
        return {
            "source": source,
            "pre_release_events": events,
            "post_release_events": post_release_events,
            "coordinator_pid": coordinator_pid,
            "child_backend_pids": pids,
            "blocked": blockers,
            "blocking_graph": blockers,
            "outcomes": outcomes,
            "state": state(env, database),
        }
    finally:
        coordinator.rollback()
        for child in children:
            if child.poll() is None:
                kill_children([child])
        coordinator.close()


def run_crash_case(
    env: dict[str, str], database: str, barrier: str
) -> dict[str, object]:
    reset(env)
    state_before_kill = state(env, database)
    child = start_child(env, database, "repaired_consumer.ts", barrier)
    recovery = None
    try:
        events = read_until(
            [child],
            lambda rows: any(row.get("phase") == barrier for row in rows),
            OUTCOME_TIMEOUT,
        )
        # The after-commit child remains paused at its stdout barrier.  Kill
        # it before releasing stdin so this is a controlled cutpoint, not a
        # natural-exit race.
        returncode = kill_children([child])[0]
        killed_state = state(env, database)
        state_before_recovery = state(env, database)
        recovery = start_child(env, database, "repaired_consumer.ts")
        recovery_events = read_until(
            [recovery],
            lambda rows: any(row.get("phase") == "outcome" for row in rows),
            OUTCOME_TIMEOUT,
        )
        recovery_outcome = finish_children([recovery], recovery_events)[0]
        final_state = state(env, database)
        return {
            "barrier": barrier,
            "barrier_events": events,
            "killed_returncode": returncode,
            "state_before_kill": state_before_kill,
            "state_after_kill": killed_state,
            "state_before_recovery": state_before_recovery,
            "recovery_events": recovery_events,
            "recovery_outcome": recovery_outcome,
            "final_state": final_state,
        }
    finally:
        if child.poll() is None:
            kill_children([child])
        if recovery is not None and recovery.poll() is None:
            kill_children([recovery])


PENDING_STATE = {
    "outbox": [{"event_id": "evt:cmd-1", "command_id": "cmd-1", "status": "pending"}],
    "processed": [],
    "ledger": [],
}
COMMITTED_STATE = {
    "outbox": [{"event_id": "evt:cmd-1", "command_id": "cmd-1", "status": "published"}],
    "processed": ["evt:cmd-1"],
    "ledger": [{"command_id": "cmd-1", "count": 1}],
}


def reaches(graph: object, start: int, target: int) -> bool:
    if not isinstance(graph, dict):
        return False
    pending = [start]
    visited: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid == target:
            return True
        if pid in visited:
            continue
        visited.add(pid)
        blockers = graph.get(str(pid), [])
        if isinstance(blockers, list):
            pending.extend(blocker for blocker in blockers if isinstance(blocker, int))
    return False


def validate_blocking_evidence(case: dict[str, object]) -> None:
    coordinator_pid = case.get("coordinator_pid")
    child_pids = case.get("child_backend_pids")
    if not isinstance(coordinator_pid, int) or coordinator_pid <= 0:
        raise AssertionError("missing coordinator backend PID")
    if (
        not isinstance(child_pids, list)
        or len(child_pids) != 2
        or any(not isinstance(pid, int) or pid <= 0 for pid in child_pids)
        or len(set(child_pids)) != 2
        or coordinator_pid in child_pids
    ):
        raise AssertionError(
            "children must have two distinct non-coordinator backend PIDs"
        )
    graph = case.get("blocking_graph")
    if graph != case.get("blocked"):
        raise AssertionError("blocked evidence was not retained as the blocking graph")
    if not isinstance(graph, dict) or str(coordinator_pid) not in graph:
        raise AssertionError("blocking graph does not contain the coordinator")
    for child_pid in child_pids:
        if not graph.get(str(child_pid)) or not reaches(
            graph, child_pid, coordinator_pid
        ):
            raise AssertionError(
                "each child must have a blocker chain to its own coordinator"
            )


def validate_concurrency_case(case: object, source: str) -> None:
    if not isinstance(case, dict) or case.get("source") != source:
        raise AssertionError(f"missing concurrency case {source}")
    events = case.get("pre_release_events")
    if not isinstance(events, list):
        raise AssertionError("missing pre-release events")
    waiting = [
        event for event in events if event.get("phase") == "waiting_for_row_lock"
    ]
    if len(waiting) != 2 or {event.get("child") for event in waiting} != {0, 1}:
        raise AssertionError("both consumers were not held before release")
    connected = [event for event in events if event.get("phase") == "connected"]
    if (
        len(connected) != 2
        or len({event.get("backend_pid") for event in connected}) != 2
    ):
        raise AssertionError("missing distinct child connection identities")
    validate_blocking_evidence(case)
    if case.get("state") != COMMITTED_STATE:
        raise AssertionError("concurrency state was not exactly committed")


def validate_crash_case(case: object, barrier: str) -> None:
    if not isinstance(case, dict) or case.get("barrier") != barrier:
        raise AssertionError(f"missing crash case {barrier}")
    events = case.get("barrier_events")
    if (
        not isinstance(events, list)
        or sum(event.get("phase") == barrier for event in events) != 1
    ):
        raise AssertionError(f"{barrier} barrier was not observed exactly once")
    if case.get("killed_returncode") != -signal.SIGKILL:
        raise AssertionError(f"{barrier} child was not SIGKILLed")
    if case.get("state_before_kill") != PENDING_STATE:
        raise AssertionError(f"{barrier} case did not start from exact pending state")
    if case.get("state_before_recovery") != case.get("state_after_kill"):
        raise AssertionError(f"{barrier} recovery state was not recorded faithfully")
    expected_after_kill = (
        PENDING_STATE if barrier == "before_commit" else COMMITTED_STATE
    )
    if case.get("state_after_kill") != expected_after_kill:
        raise AssertionError(f"{barrier} kill left unexpected exact database state")
    expected_recovery = "committed" if barrier == "before_commit" else "dedup_skipped"
    outcomes = case.get("recovery_outcome")
    if not isinstance(outcomes, dict) or outcomes.get("outcome") != expected_recovery:
        raise AssertionError(f"{barrier} recovery outcome did not match cutpoint")
    if case.get("final_state") != COMMITTED_STATE:
        raise AssertionError(f"{barrier} recovery did not leave exact committed state")


def validate_report(report: dict[str, object]) -> None:
    if report.get("source_unchanged") is not True:
        raise AssertionError("source identity was not proven unchanged")
    concurrency = report.get("concurrency")
    if not isinstance(concurrency, list) or len(concurrency) != 2:
        raise AssertionError("expected current and repaired concurrency cases")
    current, repaired = concurrency
    validate_concurrency_case(current, "consumer.ts")
    validate_concurrency_case(repaired, "repaired_consumer.ts")
    current_prechecks = [
        event
        for event in current.get("pre_release_events", [])
        if event.get("phase") == "precheck_done"
    ]
    if len(current_prechecks) != 2 or any(
        event.get("seen") is not False for event in current_prechecks
    ):
        raise AssertionError("current consumers did not both observe an absent marker")
    current_outcomes = sorted(item["outcome"] for item in current["outcomes"])
    if current_outcomes != ["committed", "rolled_back_unique_violation"]:
        raise AssertionError(f"current race outcomes were {current_outcomes}")
    repaired_outcomes = sorted(item["outcome"] for item in repaired["outcomes"])
    if repaired_outcomes != ["committed", "dedup_skipped"]:
        raise AssertionError(f"repaired race outcomes were {repaired_outcomes}")
    repaired_postchecks = [
        event
        for event in repaired.get("post_release_events", [])
        if event.get("phase") == "postlock_check"
    ]
    if (
        len(repaired_postchecks) != 2
        or any(type(event.get("seen")) is not bool for event in repaired_postchecks)
        or sorted(event["seen"] for event in repaired_postchecks) != [False, True]
    ):
        raise AssertionError(
            "repaired consumers did not record one false and one true post-lock check"
        )
    crashes = report.get("crash")
    if not isinstance(crashes, list) or len(crashes) != 2:
        raise AssertionError("expected exactly before- and after-commit crash cases")
    if {case.get("barrier") for case in crashes if isinstance(case, dict)} != {
        "before_commit",
        "after_commit",
    }:
        raise AssertionError("crash barriers were not exactly once each")
    validate_crash_case(
        next(case for case in crashes if case.get("barrier") == "before_commit"),
        "before_commit",
    )
    validate_crash_case(
        next(case for case in crashes if case.get("barrier") == "after_commit"),
        "after_commit",
    )


def write_report(output: Path, report: dict[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")


def tool_versions(env: dict[str, str], database: str) -> dict[str, str]:
    import psycopg

    node = subprocess.check_output(
        ["node", "--version"], env=subprocess_env(env, database), text=True
    ).strip()
    with connect(env, database) as db:
        pg_version, pg_version_num = db.execute(
            "SELECT current_setting('server_version'), current_setting('server_version_num')"
        ).fetchone()
    return {
        "node": node,
        "psycopg": psycopg.__version__,
        "postgres": pg_version,
        "postgres_version_num": pg_version_num,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=HERE / "results" / "actual-concurrency.json"
    )
    args = parser.parse_args()
    output = args.output.resolve()
    started = time.monotonic()
    initial_hashes = source_hashes()
    report: dict[str, object] = {
        "status": "unknown",
        "phase": "initializing",
        "source_sha256": initial_hashes,
        "source_unchanged": None,
        "ownership": {"database": {"name": None, "created": False, "dropped": False}},
        "limits": [
            "No broker, external side effect, exactly-once claim, or unbounded liveness claim",
            "The ledger row is the only internal effect observed",
            "Both concurrency consumers are held by one coordinator row lock before release",
        ],
        "concurrency": [],
        "crash": [],
    }
    write_report(output, report)
    env: dict[str, str] | None = None
    database: str | None = None
    cleanup_confirmed = False
    try:
        env = load_env()
        database = provision(env)
        ownership = report["ownership"]
        assert isinstance(ownership, dict)
        database_ledger = ownership["database"]
        assert isinstance(database_ledger, dict)
        database_ledger.update(name=database, created=True)
        target_env = {**env, "PGDATABASE": database}
        report["phase"] = "database_provisioned"
        report["tool_versions"] = tool_versions(target_env, database)
        write_report(output, report)
        for source in ("consumer.ts", "repaired_consumer.ts"):
            report["concurrency"].append(run_concurrency(target_env, database, source))
            report["phase"] = f"concurrency_{source}_complete"
            write_report(output, report)
        for barrier in ("before_commit", "after_commit"):
            report["crash"].append(run_crash_case(target_env, database, barrier))
            report["phase"] = f"crash_{barrier}_complete"
            write_report(output, report)
        report["phase"] = "validating"
        report["source_unchanged"] = source_hashes() == initial_hashes
        validate_report(report)
        report["status"] = "validated"
    except BaseException as error:
        report["status"] = "failed"
        report["phase"] = "failed"
        report["failure"] = {
            "type": type(error).__name__,
            "message": str(error)[:1000],
        }
    finally:
        ownership = report["ownership"]
        assert isinstance(ownership, dict)
        database_ledger = ownership["database"]
        assert isinstance(database_ledger, dict)
        if database_ledger.get("created") is not True:
            cleanup_confirmed = True
        elif env is not None and database is not None:
            try:
                drop_database(env, database)
                database_ledger["dropped"] = True
                cleanup_confirmed = True
            except BaseException as error:
                report["cleanup_error"] = {
                    "type": type(error).__name__,
                    "message": str(error)[:1000],
                }
        report["cleanup"] = {
            "confirmed": cleanup_confirmed,
            "database_dropped": database_ledger.get("dropped") is True,
        }
        try:
            report["source_unchanged"] = source_hashes() == initial_hashes
        except OSError:
            report["source_unchanged"] = False
        report["walltime_seconds"] = round(time.monotonic() - started, 3)
        if (
            report.get("status") == "validated"
            and cleanup_confirmed
            and report.get("source_unchanged") is True
        ):
            report["status"] = "passed"
            report["phase"] = "complete"
        elif report.get("status") == "validated":
            report["status"] = "cleanup_failed"
            report["phase"] = "failed_cleanup_or_identity"
        elif not cleanup_confirmed:
            report["status"] = "cleanup_failed"
            report["phase"] = "failed_cleanup"
        write_report(output, report)
    print(json.dumps(report, indent=2))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

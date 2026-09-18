#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]==3.3.4"]
# ///
"""Run real PostgreSQL/TypeScript replays; model.py remains dependency-free."""

from __future__ import annotations

import json
import hashlib
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
from checks import require

HERE = Path(__file__).resolve().parent
PYTHON = sys.executable
DB_ENV = HERE / "fixtures" / "db.env"
RESULTS = HERE / "results"
PG_KEYS = ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE")
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
SUBPROCESS_TIMEOUT_S = 5.0
MAX_SUBPROCESS_OUTPUT = 4096
PROVENANCE_FILES = (
    "model.py",
    "run_actual.py",
    "checks.py",
    "consumer.ts",
    "schema.sql",
    "package.json",
    "package-lock.json",
    "test_outbox.py",
)


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in DB_ENV.read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            if key not in PG_KEYS:
                raise ValueError(f"unsupported fixture key: {key}")
            values[key] = value
    missing = sorted(set(PG_KEYS) - set(values))
    if missing:
        raise ValueError(f"missing fixture keys: {missing}")
    if values["PGHOST"] not in ALLOWED_HOSTS:
        raise ValueError("fixture PGHOST must be a local endpoint")
    if not values["PGPORT"].isdigit() or not 1 <= int(values["PGPORT"]) <= 65535:
        raise ValueError("fixture PGPORT must be a valid TCP port")
    if values["PGDATABASE"] != "postgres":
        raise ValueError("fixture PGDATABASE must be the maintenance database postgres")
    return values


def subprocess_env(env: dict[str, str]) -> dict[str, str]:
    """Pass only stable locale/path settings and the validated fixture PG keys."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C"),
        **{key: env[key] for key in PG_KEYS},
    }


def bounded_process(command: list[str], env: dict[str, str]) -> tuple[str, str]:
    process = subprocess.Popen(
        command,
        cwd=HERE,
        env=subprocess_env(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=SUBPROCESS_TIMEOUT_S)
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        raise RuntimeError(
            "consumer subprocess timed out and was terminated"
        ) from error
    if len(stdout) + len(stderr) > MAX_SUBPROCESS_OUTPUT:
        raise RuntimeError("consumer subprocess output exceeded limit")
    if process.returncode != 0:
        raise RuntimeError("consumer subprocess failed")
    return stdout, stderr


def provenance(env: dict[str, str]) -> dict[str, object]:
    def version(command: list[str]) -> str:
        try:
            stdout, _ = bounded_process(command, env)
            return stdout.strip()
        except (OSError, RuntimeError):
            return "unavailable"

    hashes = {
        name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
        for name in PROVENANCE_FILES
    }
    with connect(env) as db:
        postgres = db.execute("SELECT current_setting('server_version')").fetchone()[0]
    return {
        "source_sha256": hashes,
        "tools": {
            "python": sys.version.split()[0],
            "psycopg": psycopg.__version__,
            "node": version(["node", "--version"]),
            "uv": version(["uv", "--version"]),
            "postgres": postgres,
            "pg_dependency": json.loads((HERE / "package-lock.json").read_text())[
                "packages"
            ]["node_modules/pg"]["version"],
        },
    }


def connect(
    env: dict[str, str], database: str | None = None, autocommit: bool = False
) -> psycopg.Connection:
    return psycopg.connect(
        host=env["PGHOST"],
        port=int(env["PGPORT"]),
        user=env["PGUSER"],
        password=env["PGPASSWORD"],
        dbname=database or env["PGDATABASE"],
        autocommit=autocommit,
    )


def provision_database(env: dict[str, str]) -> str:
    database = f"assay_outbox_{uuid.uuid4().hex}"
    with connect(env, "postgres", autocommit=True) as db:
        db.execute(f'CREATE DATABASE "{database}"')
    return database


def drop_database(env: dict[str, str], database: str) -> None:
    if not re.fullmatch(r"assay_outbox_[0-9a-f]{32}", database):
        raise ValueError("refusing to drop a non-ephemeral database name")
    with connect(env, "postgres", autocommit=True) as db:
        db.execute(f'DROP DATABASE "{database}"')


def reset(env: dict[str, str], variant: str) -> None:
    with connect(env) as db:
        db.execute((HERE / "schema.sql").read_text())
        if variant == "missing-dedup":
            db.execute("DROP TABLE ledger_effects")
            db.execute(
                "CREATE TABLE ledger_effects (id bigserial PRIMARY KEY, command_id text NOT NULL, effect text NOT NULL)"
            )


def accept(
    env: dict[str, str],
    command: str,
    atomic: bool = True,
    crash_after_command: bool = False,
) -> list[str]:
    event = f"evt:{command}"
    actions = [f"begin_accept:{command}"]
    with connect(env) as db:
        if atomic:
            db.execute("BEGIN")
            db.execute(
                "INSERT INTO accepted_commands(command_id) VALUES (%s)", (command,)
            )
            db.execute(
                "INSERT INTO outbox_events(event_id, command_id, status) VALUES (%s, %s, 'pending')",
                (event, command),
            )
            db.execute("COMMIT")
            actions.append(f"accept:{command}@commit")
        else:
            db.execute("BEGIN")
            db.execute(
                "INSERT INTO accepted_commands(command_id) VALUES (%s)", (command,)
            )
            db.execute("COMMIT")
            actions.append(f"accept:{command}@command_commit")
            if crash_after_command:
                actions.append(f"crash:{command}@after_command_before_outbox")
                return actions
            db.execute("BEGIN")
            db.execute(
                "INSERT INTO outbox_events(event_id, command_id, status) VALUES (%s, %s, 'pending')",
                (event, command),
            )
            db.execute("COMMIT")
            actions.append(f"accept:{command}@outbox_commit")
    return actions


def consume(
    env: dict[str, str], variant: str, command: str, cutpoint: str = ""
) -> dict[str, object]:
    event = f"evt:{command}"
    started = time.perf_counter()
    stdout, _ = bounded_process(
        ["node", "--experimental-strip-types", "consumer.ts", variant, event, cutpoint],
        env,
    )
    result = json.loads(stdout)
    outcome = result.get("outcome")
    expected = {
        "normal": {"committed", "dedup_skipped", "rejected_missing_outbox"},
        "missing-dedup": {"committed"},
        "processed-before-ledger": {
            "committed",
            "dedup_skipped",
            "simulated_crash_after_processed",
        },
    }[variant]
    require(
        isinstance(outcome, str) and outcome in expected,
        "consumer returned an unexpected or rolled-back outcome",
    )
    return {
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "stdout": result,
    }


def invariant(env: dict[str, str]) -> dict[str, object]:
    with connect(env) as db:
        missing = db.execute(
            "SELECT c.command_id FROM accepted_commands c LEFT JOIN outbox_events o USING (command_id) WHERE o.command_id IS NULL ORDER BY c.command_id"
        ).fetchall()
        pending_or_published = db.execute(
            "SELECT command_id, status FROM outbox_events ORDER BY command_id"
        ).fetchall()
        processed = db.execute(
            "SELECT event_id FROM processed_events ORDER BY event_id"
        ).fetchall()
        orphan_processed = db.execute(
            "SELECT p.event_id FROM processed_events p LEFT JOIN outbox_events o USING (event_id) WHERE o.event_id IS NULL ORDER BY p.event_id"
        ).fetchall()
        ledger_counts = db.execute(
            "SELECT command_id, count(*) FROM ledger_effects GROUP BY command_id ORDER BY command_id"
        ).fetchall()
        orphan_ledger = db.execute(
            "SELECT l.command_id FROM ledger_effects l LEFT JOIN accepted_commands c USING (command_id) WHERE c.command_id IS NULL ORDER BY l.command_id"
        ).fetchall()
        lost_effects = db.execute(
            "SELECT p.event_id FROM processed_events p JOIN outbox_events o USING (event_id) LEFT JOIN ledger_effects l USING (command_id) WHERE l.command_id IS NULL ORDER BY p.event_id"
        ).fetchall()
    duplicates = [command for command, count in ledger_counts if count > 1]
    return {
        "accepted_has_event": not missing,
        "missing_events": [row[0] for row in missing],
        "outbox": [
            {"command_id": row[0], "status": row[1]} for row in pending_or_published
        ],
        "processed": [row[0] for row in processed],
        "orphan_processed": [row[0] for row in orphan_processed],
        "ledger_counts": [
            {"command_id": command, "count": count} for command, count in ledger_counts
        ],
        "orphan_ledger": [row[0] for row in orphan_ledger],
        "duplicate_ledger_effects": duplicates,
        "processed_without_ledger": [row[0] for row in lost_effects],
        "holds": not missing
        and not orphan_processed
        and not orphan_ledger
        and not duplicates
        and not lost_effects,
    }


def replay_processed_trace(env: dict[str, str], trace: list[str]) -> dict[str, object]:
    """Replay the model's processed-before-ledger trace with real SQL and TS."""
    actions: list[str] = []
    first_consume = None
    second_consume = None
    command = "cmd-1"
    for action in trace:
        if action == f"begin_accept:{command}":
            actions.extend(accept(env, command))
        elif action == f"accept:{command}@commit":
            pass
        elif action.startswith("enqueue:"):
            actions.append(action)
        elif action.startswith("deliver:"):
            actions.append(action)
        elif action == f"consumer:{command}@mark_processed_commit":
            first_consume = consume(
                env, "processed-before-ledger", command, "after_processed_commit"
            )
            actions.append(action)
        elif action == f"crash:{command}@after_processed_before_ledger":
            actions.append(action)
        elif action == f"consumer:{command}@ledger_commit":
            second_consume = consume(env, "processed-before-ledger", command)
            actions.append(action)
        else:
            require(False, f"unsupported replay action: {action}")
    require(
        first_consume is not None,
        "model trace did not reach the marked-consumer cutpoint",
    )
    second_consume = consume(env, "processed-before-ledger", command)
    retry_invariant = invariant(env)
    require(
        second_consume["stdout"]["outcome"] == "dedup_skipped",
        "broken consumer retry was not deduplicated",
    )
    require(
        retry_invariant["ledger_counts"] == [],
        "broken retry unexpectedly created a ledger effect",
    )
    require(
        retry_invariant["processed_without_ledger"] == ["evt:cmd-1"],
        "broken retry did not preserve the missing ledger effect",
    )
    return {
        "model_trace": trace,
        "sql_projection_of_model_trace": actions,
        "first_consume": first_consume,
        "retry_after_marked_consume": second_consume,
        "invariant": retry_invariant,
        "note": "enqueue/deliver entries are a SQL projection of model trace metadata; no broker is exercised or claimed",
    }


def run_actual_variants(env: dict[str, str]) -> dict[str, object]:
    scenarios: dict[str, object] = {}

    reset(env, "normal")
    actions = accept(env, "cmd-1")
    first = consume(env, "normal", "cmd-1")
    second = consume(env, "normal", "cmd-1")
    normal_invariant = invariant(env)
    require(first["stdout"]["outcome"] == "committed", "normal consumer did not commit")
    require(
        second["stdout"]["outcome"] == "dedup_skipped",
        "normal consumer did not deduplicate",
    )
    require(
        normal_invariant["outbox"] == [{"command_id": "cmd-1", "status": "published"}],
        "normal outbox was not published",
    )
    require(
        normal_invariant["processed"] == ["evt:cmd-1"], "normal event was not processed"
    )
    require(
        normal_invariant["ledger_counts"] == [{"command_id": "cmd-1", "count": 1}],
        "normal consumer did not produce exactly one cmd-1 ledger effect",
    )
    scenarios["normal"] = {
        "actions": actions,
        "first_consume": first,
        "second_consume": second,
        "invariant": normal_invariant,
    }

    reset(env, "normal")
    actions = accept(env, "cmd-1", atomic=False, crash_after_command=True)
    scenarios["non-atomic-outbox"] = {"actions": actions, "invariant": invariant(env)}

    reset(env, "normal")
    actions = accept(env, "cmd-1")
    ghost = consume(env, "normal", "ghost")
    ghost_invariant = invariant(env)
    require(
        ghost["stdout"]["outcome"] == "rejected_missing_outbox",
        "ghost delivery was not rejected",
    )
    require(
        ghost_invariant["ledger_counts"] == [] and ghost_invariant["processed"] == [],
        "ghost delivery changed durable consumer state",
    )
    scenarios["ghost-delivery"] = {
        "actions": actions,
        "ghost_consume": ghost,
        "invariant": ghost_invariant,
    }

    reset(env, "missing-dedup")
    actions = accept(env, "cmd-1")
    first = consume(env, "missing-dedup", "cmd-1")
    second = consume(env, "missing-dedup", "cmd-1")
    scenarios["missing-dedup"] = {
        "actions": actions,
        "first_consume": first,
        "second_consume": second,
        "invariant": invariant(env),
    }
    return scenarios


def main() -> None:
    base_env = load_env()
    database = provision_database(base_env)
    env = {**base_env, "PGDATABASE": database}
    RESULTS.mkdir(exist_ok=True)
    try:
        model_stdout, _ = bounded_process([PYTHON, "model.py", "--all"], base_env)
        model = json.loads(model_stdout)
        require(
            not model["normal"]["found_counterexample"],
            "normal model found a counterexample",
        )
        require(
            model["non-atomic-outbox"]["found_counterexample"],
            "non-atomic model lost its counterexample",
        )
        require(
            model["processed-before-ledger"]["found_counterexample"],
            "processed-before-ledger model lost its counterexample",
        )
        require(
            model["missing-dedup"]["found_counterexample"],
            "missing-dedup model lost its counterexample",
        )
        for variant, result in model.items():
            (RESULTS / f"model-{variant}.json").write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n"
            )

        replay_trace = model["processed-before-ledger"]["trace"]
        reset(env, "normal")
        replay = replay_processed_trace(env, replay_trace)
        variants_started = time.perf_counter()
        variants = run_actual_variants(env)
        require(
            variants["normal"]["invariant"]["holds"], "normal actual invariant failed"
        )
        require(
            not variants["non-atomic-outbox"]["invariant"]["holds"],
            "non-atomic actual fault did not fail",
        )
        require(
            not variants["missing-dedup"]["invariant"]["holds"],
            "missing-dedup actual fault did not fail",
        )
        require(
            not replay["invariant"]["holds"],
            "processed-before-ledger actual fault did not fail",
        )
        actual = {
            "variant": "processed-before-ledger",
            "model_version": model["normal"]["model_version"],
            "model_states": model["processed-before-ledger"]["model_states"],
            "model_elapsed_ms": model["processed-before-ledger"]["elapsed_ms"],
            "actual_replay": replay,
            "actual_variants": variants,
            "actual_variants_elapsed_ms": round(
                (time.perf_counter() - variants_started) * 1000, 3
            ),
            "provenance": provenance(env),
            "limitation": "crash is simulated after a committed processed marker; bounded safety evidence only, with no fairness/liveness, process-kill, broker, or external-effect claim",
        }
        (RESULTS / "actual-replay.json").write_text(
            json.dumps(actual, indent=2, sort_keys=True) + "\n"
        )
        print(
            json.dumps(
                {"model": model, "actual_replay": actual}, indent=2, sort_keys=True
            )
        )
    finally:
        drop_database(base_env, database)


if __name__ == "__main__":
    main()

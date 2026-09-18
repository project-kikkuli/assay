"""Reproduce PostgreSQL's documented RLS sub-SELECT revocation race."""

from __future__ import annotations

import hashlib
import json
import math
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import psycopg


HOST = "127.0.0.1"
PORT = 55439
ADMIN_DB = "postgres"
ADMIN_USER = "postgres"
ADMIN_PASSWORD = "assay-local-only"
ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results.json"
RUN_ID = uuid.uuid4().hex[:12]
DB_NAME = f"assay_revocation_{RUN_ID}"
OWNER = f"assay_revoke_owner_{RUN_ID}"
READER = f"assay_revoke_reader_{RUN_ID}"
READER_PASSWORD = f"assay-revoke-reader-{RUN_ID}"
ROLES = (OWNER, READER)
CONNECT_TIMEOUT_SECONDS = 3
STATEMENT_TIMEOUT_MS = 15000
BARRIER_TIMEOUT_SECONDS = 10.0
DOC_URL = "https://www.postgresql.org/docs/18/ddl-rowsecurity.html"
SOURCE_FILES = ("run.py", "README.md", "requirements.txt", "test_run.py")
VARIANTS = ("baseline", "select_for_share")


class EvidenceError(RuntimeError):
    """Raised when an observation cannot support the experiment's claim."""


class Resources:
    def __init__(self) -> None:
        self.database_created = False
        self.roles_created: list[str] = []


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def ident(value: str) -> str:
    require(
        bool(re.fullmatch(r"[a-z][a-z0-9_]+", value)), f"unsafe identifier: {value}"
    )
    return f'"{value}"'


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def connect(
    database: str,
    user: str = ADMIN_USER,
    password: str = ADMIN_PASSWORD,
    autocommit: bool = True,
) -> psycopg.Connection:
    return psycopg.connect(
        host=HOST,
        port=PORT,
        dbname=database,
        user=user,
        password=password,
        autocommit=autocommit,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
    )


def execute(
    connection: psycopg.Connection,
    statement: str,
    parameters: tuple[Any, ...] = (),
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)


def scalar(
    connection: psycopg.Connection,
    statement: str,
    parameters: tuple[Any, ...] = (),
) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)
        row = cursor.fetchone()
    require(row is not None, "query returned no row")
    return row[0]


def rows(
    connection: psycopg.Connection,
    statement: str,
    parameters: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)
        names = [column.name for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def source_hashes() -> dict[str, str]:
    return {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in SOURCE_FILES
    }


def resource_names() -> dict[str, str]:
    return {"database": DB_NAME, "owner": OWNER, "reader": READER}


def create_resources(resources: Resources) -> None:
    connection = connect(ADMIN_DB)
    try:
        database_exists = scalar(
            connection,
            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
            (DB_NAME,),
        )
        role_rows = rows(
            connection,
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
            (list(ROLES),),
        )
        require(not database_exists, f"refusing to reuse database {DB_NAME}")
        require(not role_rows, f"refusing to reuse roles: {role_rows}")
        execute(
            connection,
            f"CREATE ROLE {ident(OWNER)} NOLOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS",
        )
        resources.roles_created.append(OWNER)
        execute(
            connection,
            f"CREATE ROLE {ident(READER)} LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS PASSWORD {literal(READER_PASSWORD)}",
        )
        resources.roles_created.append(READER)
        execute(connection, f"CREATE DATABASE {ident(DB_NAME)} OWNER {ident(OWNER)}")
        resources.database_created = True
    except Exception:
        cleanup(resources)
        raise
    finally:
        connection.close()


def table_names(variant: str) -> tuple[str, str]:
    require(variant in VARIANTS, f"unknown variant: {variant}")
    suffix = "share" if variant == "select_for_share" else "baseline"
    return f"memberships_{suffix}_{RUN_ID}", f"secrets_{suffix}_{RUN_ID}"


def target_query(variant: str, run_id: str = RUN_ID) -> str:
    suffix = "share" if variant == "select_for_share" else "baseline"
    secret = f"secrets_{suffix}_{run_id}"
    return (
        f"SELECT secret_id, secret FROM {ident(secret)} "
        "WHERE secret_id = 1 FOR UPDATE"
    )


def setup_schema() -> None:
    connection = connect(DB_NAME)
    try:
        memberships = {variant: table_names(variant)[0] for variant in VARIANTS}
        secrets = {variant: table_names(variant)[1] for variant in VARIANTS}
        execute(connection, f"REVOKE ALL ON DATABASE {ident(DB_NAME)} FROM PUBLIC")
        execute(
            connection, f"GRANT CONNECT ON DATABASE {ident(DB_NAME)} TO {ident(READER)}"
        )
        execute(connection, "REVOKE ALL ON SCHEMA public FROM PUBLIC")
        execute(connection, f"GRANT USAGE ON SCHEMA public TO {ident(READER)}")
        execute(connection, f"SET ROLE {ident(OWNER)}")
        for variant in VARIANTS:
            membership = ident(memberships[variant])
            secret = ident(secrets[variant])
            execute(
                connection,
                f"""
                CREATE TABLE {membership} (
                    reader_name name PRIMARY KEY,
                    can_read boolean NOT NULL,
                    lock_token integer NOT NULL DEFAULT 0
                );
                CREATE TABLE {secret} (
                    secret_id integer PRIMARY KEY,
                    secret text NOT NULL,
                    lock_token integer NOT NULL DEFAULT 0
                );
                INSERT INTO {membership} VALUES ({literal(READER)}, true);
                INSERT INTO {secret} VALUES (1, {literal("initial-secret-" + variant)});
                ALTER TABLE {membership} ENABLE ROW LEVEL SECURITY;
                ALTER TABLE {membership} FORCE ROW LEVEL SECURITY;
                ALTER TABLE {secret} ENABLE ROW LEVEL SECURITY;
                ALTER TABLE {secret} FORCE ROW LEVEL SECURITY;
                CREATE POLICY {ident("membership_select_" + variant)}
                    ON {membership} FOR SELECT TO {ident(READER)}
                    USING (reader_name = current_user);
                CREATE POLICY {ident("membership_lock_" + variant)}
                    ON {membership} FOR UPDATE TO {ident(READER)}
                    USING (reader_name = current_user)
                    WITH CHECK (reader_name = current_user);
                CREATE POLICY {ident("secret_select_" + variant)}
                    ON {secret} FOR SELECT TO {ident(READER)}
                    USING ((
                        SELECT can_read FROM {membership}
                        WHERE reader_name = current_user
                        {"FOR SHARE" if variant == "select_for_share" else ""}
                    ));
                CREATE POLICY {ident("secret_lock_" + variant)}
                    ON {secret} FOR UPDATE TO {ident(READER)}
                    USING ((
                        SELECT can_read FROM {membership}
                        WHERE reader_name = current_user
                        {"FOR SHARE" if variant == "select_for_share" else ""}
                    ))
                    WITH CHECK ((
                        SELECT can_read FROM {membership}
                        WHERE reader_name = current_user
                        {"FOR SHARE" if variant == "select_for_share" else ""}
                    ));
                GRANT SELECT ON {membership}, {secret} TO {ident(READER)};
                GRANT UPDATE (lock_token) ON {membership} TO {ident(READER)};
                GRANT UPDATE (lock_token) ON {secret} TO {ident(READER)};
                """,
            )
        execute(connection, "RESET ROLE")
    finally:
        connection.close()


def transaction_facts(connection: psycopg.Connection) -> dict[str, Any]:
    return {
        "user": scalar(connection, "SELECT current_user"),
        "isolation": scalar(connection, "SHOW transaction_isolation"),
        "pid": scalar(connection, "SELECT pg_backend_pid()"),
    }


def positive_read(variant: str) -> dict[str, Any]:
    _, secret = table_names(variant)
    connection = connect(DB_NAME, READER, READER_PASSWORD, autocommit=False)
    try:
        execute(connection, "BEGIN")
        facts = transaction_facts(connection)
        values = rows(
            connection,
            f"SELECT secret_id, secret FROM {ident(secret)} WHERE secret_id = 1 FOR UPDATE",
        )
        connection.commit()
        require(len(values) == 1, f"positive {variant} read did not return one row")
        return {"facts": facts, "rows": values, "allowed": True}
    finally:
        connection.close()


def reader_update_boundary(variant: str) -> dict[str, Any]:
    membership, secret = table_names(variant)
    connection = connect(DB_NAME, READER, READER_PASSWORD, autocommit=False)
    observer = connect(DB_NAME)
    try:

        def attempt(statement: str) -> tuple[int, str | None]:
            execute(connection, "BEGIN")
            try:
                with connection.cursor() as cursor:
                    cursor.execute(statement)
                    affected = cursor.rowcount
                sqlstate = None
            except psycopg.Error as exc:
                affected = 0
                sqlstate = exc.sqlstate
            finally:
                connection.rollback()
            return affected, sqlstate

        execute(connection, "BEGIN")
        connection.rollback()
        affected_membership, membership_sqlstate = attempt(
            f"UPDATE {ident(membership)} SET can_read = false WHERE reader_name = current_user"
        )
        affected_secret, secret_sqlstate = attempt(
            f"UPDATE {ident(secret)} SET secret = 'reader-write' WHERE secret_id = 1"
        )
        privilege_connection = observer
        return {
            "membership_lock_column_update_granted": bool(
                scalar(
                    privilege_connection,
                    "SELECT has_column_privilege(%s, %s, 'lock_token', 'UPDATE')",
                    (READER, f"public.{membership}"),
                )
            ),
            "secret_lock_column_update_granted": bool(
                scalar(
                    privilege_connection,
                    "SELECT has_column_privilege(%s, %s, 'lock_token', 'UPDATE')",
                    (READER, f"public.{secret}"),
                )
            ),
            "membership_protected_update_affected_rows": affected_membership,
            "secret_protected_update_affected_rows": affected_secret,
            "sqlstates": [
                sqlstate
                for sqlstate in (membership_sqlstate, secret_sqlstate)
                if sqlstate
            ],
            "protected_updates_denied": affected_membership == 0
            and affected_secret == 0
            and bool(membership_sqlstate)
            and bool(secret_sqlstate),
        }
    finally:
        connection.close()
        observer.close()


def activity(connection: psycopg.Connection, pid: int) -> dict[str, Any] | None:
    values = rows(
        connection,
        """
        SELECT pid, state, wait_event_type, wait_event, query,
               pg_blocking_pids(pid) AS blocking_pids
        FROM pg_stat_activity WHERE pid = %s
        """,
        (pid,),
    )
    return values[0] if values else None


def wait_for_block(
    observer: psycopg.Connection,
    reader_pid: int,
    admin_pid: int,
    reader_state: dict[str, Any],
) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    deadline = time.monotonic() + BARRIER_TIMEOUT_SECONDS
    latest: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        if reader_state.get("done").is_set():
            raise EvidenceError(
                "reader finished before verified lock barrier: "
                f"{reader_state.get('error', reader_state.get('rows'))}"
            )
        latest = activity(observer, reader_pid)
        if latest:
            blockers = latest.get("blocking_pids") or []
            if (
                latest.get("state") == "active"
                and latest.get("wait_event_type") == "Lock"
                and admin_pid in blockers
            ):
                return latest, (time.perf_counter() - started) * 1000
        time.sleep(0.005)
    raise EvidenceError(f"reader never reached verified lock barrier: {latest}")


def terminate_pid(connection: psycopg.Connection, pid: int) -> None:
    execute(connection, "SELECT pg_terminate_backend(%s)", (pid,))


def blocked_read(variant: str, start: threading.Event, state: dict[str, Any]) -> None:
    membership, _ = table_names(variant)
    connection = connect(DB_NAME, READER, READER_PASSWORD, autocommit=False)
    try:
        state["reader_pid"] = scalar(connection, "SELECT pg_backend_pid()")
        state["ready"].set()
        require(start.wait(BARRIER_TIMEOUT_SECONDS), "reader start barrier timed out")
        execute(connection, "BEGIN")
        state["facts"] = transaction_facts(connection)
        state["precheck"] = {
            "membership": rows(
                connection,
                f"SELECT reader_name, can_read FROM {ident(membership)} WHERE reader_name = current_user",
            ),
        }
        query_started = time.perf_counter()
        values = rows(
            connection,
            target_query(variant),
        )
        state["query_ms"] = (time.perf_counter() - query_started) * 1000
        connection.commit()
        state["rows"] = values
    except BaseException as exc:
        try:
            connection.rollback()
        except Exception:
            pass
        state["error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
    finally:
        connection.close()
        state["done"].set()


def run_schedule(variant: str) -> dict[str, Any]:
    membership, secret = table_names(variant)
    new_secret = f"rotated-{variant}-{RUN_ID}"
    observer = connect(DB_NAME)
    admin = connect(DB_NAME, autocommit=False)
    reader_start = threading.Event()
    reader_state: dict[str, Any] = {
        "ready": threading.Event(),
        "done": threading.Event(),
    }
    reader_thread = threading.Thread(
        target=blocked_read,
        args=(variant, reader_start, reader_state),
        name=f"revocation-reader-{variant}",
        daemon=True,
    )
    positive = positive_read(variant)
    schedule_started = time.perf_counter()
    try:
        reader_thread.start()
        require(
            reader_state["ready"].wait(BARRIER_TIMEOUT_SECONDS),
            "reader connection barrier timed out",
        )
        execute(admin, "BEGIN")
        admin_facts = transaction_facts(admin)
        execute(
            admin,
            f"UPDATE {ident(membership)} SET can_read = false WHERE reader_name = %s",
            (READER,),
        )
        execute(
            admin,
            f"UPDATE {ident(secret)} SET secret = %s WHERE secret_id = 1",
            (new_secret,),
        )
        reader_start.set()
        barrier, barrier_ms = wait_for_block(
            observer,
            reader_state["reader_pid"],
            admin_facts["pid"],
            reader_state,
        )
        admin.commit()
        reader_thread.join(BARRIER_TIMEOUT_SECONDS)
        require(
            not reader_thread.is_alive(), "reader did not finish after admin commit"
        )
        require(
            reader_state.get("error") is None,
            reader_state.get("error", "reader failed"),
        )
        values = reader_state.get("rows")
        require(isinstance(values, list), "reader returned no structured result")
        return {
            "variant": variant,
            "positive_read": positive,
            "membership_update_boundary": reader_update_boundary(variant),
            "transaction_facts": {
                "admin": admin_facts,
                "reader": reader_state.get("facts"),
            },
            "reader_precheck": reader_state.get("precheck"),
            "barrier": {
                "verified": True,
                "mechanism": "pg_stat_activity + pg_blocking_pids",
                "admin_pid": admin_facts["pid"],
                "reader_pid": reader_state["reader_pid"],
                "state": barrier,
                "observation_ms": barrier_ms,
            },
            "schedule": [
                "admin UPDATE membership can_read=false (uncommitted)",
                "admin UPDATE secret (uncommitted)",
                "reader SELECT ... FOR UPDATE",
                "verified reader lock wait on admin",
                "admin COMMIT",
                "reader resumes and commits",
            ],
            "reader_rows_after_commit": values,
            "expected_leak": variant == "baseline",
            "leaked_rotated_secret": any(
                row.get("secret") == new_secret for row in values
            ),
            "new_secret": new_secret,
            "contention_ms": {
                "to_verified_barrier": barrier_ms,
                "reader_query": reader_state.get("query_ms"),
                "whole_schedule": (time.perf_counter() - schedule_started) * 1000,
            },
        }
    except BaseException:
        try:
            admin.rollback()
        except Exception:
            pass
        reader_start.set()
        reader_thread.join(BARRIER_TIMEOUT_SECONDS)
        if reader_thread.is_alive() and reader_state.get("reader_pid"):
            terminate_pid(observer, reader_state["reader_pid"])
            reader_thread.join(BARRIER_TIMEOUT_SECONDS)
        raise
    finally:
        try:
            admin.rollback()
        except Exception:
            pass
        admin.close()
        observer.close()


def validate_variant(
    result: dict[str, Any], variant: str, run_id: str = RUN_ID
) -> None:
    require(result.get("variant") == variant, f"wrong variant result: {result}")
    positive = result.get("positive_read")
    require(
        isinstance(positive, dict)
        and positive.get("rows") == [
            {"secret_id": 1, "secret": f"initial-secret-{variant}"}
        ],
        "positive read missing",
    )
    facts = result.get("transaction_facts")
    require(isinstance(facts, dict), "transaction facts missing")
    for actor in ("admin", "reader"):
        actor_facts = facts.get(actor)
        require(
            isinstance(actor_facts, dict)
            and actor_facts.get("isolation") == "read committed",
            f"{actor} isolation fact missing",
        )
    barrier = result.get("barrier")
    require(
        isinstance(barrier, dict) and barrier.get("verified") is True,
        "barrier not verified",
    )
    require(
        barrier.get("mechanism") == "pg_stat_activity + pg_blocking_pids"
        and barrier.get("verified") is True,
        "barrier does not name the admin blocker",
    )
    admin_facts = facts["admin"]
    reader_facts = facts["reader"]
    barrier_state = barrier.get("state")
    require(
        isinstance(barrier_state, dict)
        and barrier_state.get("state") == "active"
        and barrier_state.get("wait_event_type") == "Lock"
        and barrier_state.get("query")
        == target_query(variant, run_id)
        and barrier_state.get("pid") == reader_facts.get("pid")
        and barrier.get("reader_pid") == reader_facts.get("pid")
        and barrier.get("admin_pid") == admin_facts.get("pid")
        and reader_facts.get("pid") != admin_facts.get("pid")
        and admin_facts.get("pid") in (barrier_state.get("blocking_pids") or []),
        "barrier facts do not prove the claimed target query was blocked by admin",
    )
    boundary = result.get("membership_update_boundary")
    require(
        isinstance(boundary, dict)
        and boundary.get("membership_lock_column_update_granted") is True
        and boundary.get("secret_lock_column_update_granted") is True
        and boundary.get("protected_updates_denied") is True,
        "repair privilege boundary was not demonstrated",
    )
    rows_after = result.get("reader_rows_after_commit")
    require(isinstance(rows_after, list), "post-commit reader rows missing")
    new_secret = result.get("new_secret")
    expected_rows = (
        [{"secret_id": 1, "secret": new_secret}]
        if variant == "baseline"
        else []
    )
    require(
        rows_after == expected_rows,
        f"unexpected exact post-commit rows for {variant}: {rows_after}",
    )
    derived_leak = rows_after == [{"secret_id": 1, "secret": new_secret}]
    require(
        result.get("leaked_rotated_secret") is derived_leak,
        f"leak marker disagrees with returned rows for {variant}",
    )
    require(
        result.get("expected_leak") is (variant == "baseline"),
        f"unexpected expected-leak marker: {result}",
    )
    contention = result.get("contention_ms")
    require(
        isinstance(contention, dict)
        and set(contention) == {"to_verified_barrier", "reader_query", "whole_schedule"}
        and all(
            isinstance(contention.get(key), (int, float))
            and not isinstance(contention.get(key), bool)
            and math.isfinite(contention[key])
            and contention[key] >= 0
            for key in contention
        ),
        "contention measurements missing",
    )


def validate_result(result: dict[str, Any]) -> None:
    require(result.get("status") == "passed", "result is not passed")
    provenance = result.get("provenance")
    require(
        isinstance(provenance, dict)
        and provenance.get("source_unchanged") is True
        and provenance.get("source_sha256_before")
        == provenance.get("source_sha256_after"),
        "source identity was not stable across the experiment",
    )
    require(
        result.get("server_version", "").startswith("PostgreSQL 18.6"),
        "wrong PostgreSQL version",
    )
    role_facts = result.get("role_facts")
    require(isinstance(role_facts, dict), "role facts missing")
    require(
        role_facts.get("reader", {}).get("rolcanlogin") is True
        and role_facts["reader"].get("rolsuper") is False
        and role_facts["reader"].get("rolbypassrls") is False
        and role_facts["reader"].get("is_owner") is False,
        "reader is not a bounded non-owner LOGIN role",
    )
    variants = result.get("variants")
    require(isinstance(variants, dict), "variants missing")
    for variant in VARIANTS:
        value = variants.get(variant)
        require(isinstance(value, dict), f"{variant} evidence missing")
        validate_variant(value, variant, result.get("run_id", RUN_ID))
    require(
        variants["baseline"]["leaked_rotated_secret"] is True
        and variants["select_for_share"]["leaked_rotated_secret"] is False,
        "the baseline/repair contrast was not reproduced",
    )
    cleanup_result = result.get("cleanup")
    require(
        isinstance(cleanup_result, dict)
        and cleanup_result.get("database_absent") is True
        and cleanup_result.get("roles_absent") is True,
        "owned-resource cleanup was not verified",
    )


def cleanup(resources: Resources) -> dict[str, Any]:
    result: dict[str, Any] = {
        "database": DB_NAME,
        "roles": list(resources.roles_created),
        "database_dropped": False,
        "roles_dropped": False,
        "database_absent": False,
        "roles_absent": False,
    }
    connection = connect(ADMIN_DB)
    try:
        if resources.database_created:
            owner = scalar(
                connection,
                "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s",
                (DB_NAME,),
            )
            require(owner == OWNER, f"database ownership mismatch: {owner}")
            execute(
                connection,
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (DB_NAME,),
            )
            execute(connection, f"DROP DATABASE {ident(DB_NAME)} WITH (FORCE)")
            result["database_dropped"] = True
        for role in reversed(resources.roles_created):
            execute(connection, f"DROP ROLE {ident(role)}")
        result["roles_dropped"] = True
        result["database_absent"] = not scalar(
            connection,
            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
            (DB_NAME,),
        )
        result["roles_absent"] = not rows(
            connection,
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
            (list(resources.roles_created),),
        )
        return result
    finally:
        connection.close()


def role_facts() -> dict[str, Any]:
    connection = connect(ADMIN_DB)
    try:
        values = rows(
            connection,
            """
            SELECT rolname, rolsuper, rolcanlogin, rolinherit, rolbypassrls,
                   pg_get_userbyid((SELECT datdba FROM pg_database WHERE datname = %s)) AS database_owner
            FROM pg_roles WHERE rolname = ANY(%s)
            """,
            (DB_NAME, list(ROLES)),
        )
        by_name = {row["rolname"]: row for row in values}
        reader = by_name.get(READER, {})
        owner = by_name.get(OWNER, {})
        reader["is_owner"] = reader.get("rolname") == owner.get("database_owner")
        return {"reader": reader, "owner": owner}
    finally:
        connection.close()


def write_result(result: dict[str, Any], output: Path) -> None:
    output.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")


def run_experiment(output: Path = RESULTS) -> dict[str, Any]:
    resources = Resources()
    try:
        source_before = source_hashes()
    except BaseException as exc:
        source_before = {}
        source_error = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
    else:
        source_error = None
    result: dict[str, Any] = {
        "status": "unknown",
        "run_id": RUN_ID,
        "resources": resource_names(),
        "provenance": {
            "postgresql_docs": DOC_URL,
            "python": platform.python_version(),
            "psycopg": psycopg.__version__,
            "source_sha256_before": source_before,
        },
    }
    if source_error:
        result["error"] = source_error
    write_result(result, output)
    try:
        create_resources(resources)
        setup_schema()
        version_connection = connect(DB_NAME)
        try:
            result["server_version"] = scalar(version_connection, "SELECT version()")
        finally:
            version_connection.close()
        result["role_facts"] = role_facts()
        result["variants"] = {variant: run_schedule(variant) for variant in VARIANTS}
        result["status"] = "passed"
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
        result["status"] = "failed"
    finally:
        try:
            source_after = source_hashes()
            result["provenance"]["source_sha256_after"] = source_after
            result["provenance"]["source_unchanged"] = (
                source_before == source_after
            )
        except BaseException as exc:
            result["provenance"]["source_unchanged"] = False
            result["provenance"]["source_hash_error"] = (
                f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
            )
        try:
            result["cleanup"] = cleanup(resources)
        except BaseException as exc:
            result["cleanup"] = {
                "database_absent": False,
                "roles_absent": False,
                "error": f"{type(exc).__name__}: {str(exc).splitlines()[0]}",
            }
            result["status"] = "failed"
        write_result(result, output)
    if result.get("status") == "passed":
        try:
            validate_result(result)
        except BaseException as exc:
            result["status"] = "failed"
            result["validation_error"] = (
                f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
            )
    write_result(result, output)
    return result


def clean_child_environment() -> dict[str, str]:
    return {"LANG": "C", "LC_ALL": "C"}


def read_child_result(path: Path, index: int, process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "failed",
            "repeat_index": index,
            "error": f"child result unavailable: {type(exc).__name__}",
            "child_exit_code": process.returncode,
        }
    if not isinstance(value, dict):
        return {
            "status": "failed",
            "repeat_index": index,
            "error": "child result was not an object",
            "child_exit_code": process.returncode,
        }
    value["repeat_index"] = index
    value["child_exit_code"] = process.returncode
    return value


def run_repeats(output: Path = RESULTS, count: int = 3) -> dict[str, Any]:
    require(count >= 1, "repeat count must be positive")
    try:
        source_before = source_hashes()
        source_error = None
    except BaseException as exc:
        source_before = {}
        source_error = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
    aggregate: dict[str, Any] = {
        "status": "unknown",
        "repeat_count": count,
        "repeats": [],
        "provenance": {
            "postgresql_docs": DOC_URL,
            "python": platform.python_version(),
            "psycopg": psycopg.__version__,
            "source_sha256_before": source_before,
        },
    }
    if source_error:
        aggregate["error"] = source_error
    write_result(aggregate, output)
    try:
        with tempfile.TemporaryDirectory(prefix="assay-revocation-") as directory:
            script = Path(__file__).resolve()
            for index in range(1, count + 1):
                child_output = Path(directory) / f"repeat-{index}.json"
                process = subprocess.run(
                    [sys.executable, str(script), "--single", "--output", str(child_output)],
                    cwd=str(ROOT),
                    env=clean_child_environment(),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    timeout=BARRIER_TIMEOUT_SECONDS * 8,
                    check=False,
                )
                aggregate["repeats"].append(
                    read_child_result(child_output, index, process)
                )
        aggregate["status"] = "passed"
    except subprocess.TimeoutExpired as exc:
        aggregate["status"] = "failed"
        aggregate["error"] = f"repeat timeout: {exc.timeout}s"
    except BaseException as exc:
        aggregate["status"] = "failed"
        aggregate["error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
    finally:
        try:
            source_after = source_hashes()
            aggregate["provenance"]["source_sha256_after"] = source_after
            aggregate["provenance"]["source_unchanged"] = (
                source_before == source_after
            )
        except BaseException as exc:
            aggregate["provenance"]["source_unchanged"] = False
            aggregate["provenance"]["source_hash_error"] = (
                f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
            )
        write_result(aggregate, output)
    if aggregate.get("status") == "passed":
        try:
            validate_aggregate(aggregate, count)
        except BaseException as exc:
            aggregate["status"] = "failed"
            aggregate["validation_error"] = (
                f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
            )
    write_result(aggregate, output)
    return aggregate


def validate_aggregate(result: dict[str, Any], count: int = 3) -> None:
    require(result.get("status") == "passed", "aggregate result is not passed")
    require(result.get("repeat_count") == count, "wrong repeat count")
    require(
        isinstance(result.get("repeats"), list)
        and len(result["repeats"]) == count,
        "not all repeats were retained",
    )
    require(
        result.get("provenance", {}).get("source_unchanged") is True,
        "aggregate source identity was not stable",
    )
    hashes = []
    for repeat in result["repeats"]:
        validate_result(repeat)
        hashes.append(repeat["provenance"]["source_sha256_before"])
    require(all(value == hashes[0] for value in hashes[1:]), "repeat source hashes differ")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--single", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, default=RESULTS, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.single:
        result = run_experiment(arguments.output)
    else:
        result = run_repeats(arguments.output)
    print(
        json.dumps(
            {
                "status": result.get("status"),
                "repeat_count": result.get("repeat_count", 1),
                "run_ids": [
                    repeat.get("run_id")
                    for repeat in result.get("repeats", [result])
                    if isinstance(repeat, dict)
                ],
                "result": str(arguments.output),
            },
            sort_keys=True,
        )
    )
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

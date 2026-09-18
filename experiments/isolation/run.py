"""Independent PostgreSQL RLS/constraint experiment."""

from __future__ import annotations
import json
import hashlib
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
import psycopg
from checks import validate_expected
from performance import performance_matrix

HOST = "127.0.0.1"
PORT = 55439
ADMIN_DB = "postgres"
RUN_ID = uuid.uuid4().hex[:12]
DB_NAME = f"assay_isolation_worker_{RUN_ID}"
ADMIN_USER = "postgres"
ADMIN_PASSWORD = "assay-local-only"
APP_PASSWORD = "assay-app-local-only"
TENANT_A_PASSWORD = f"assay-tenant-a-{RUN_ID}"
TENANT_B_PASSWORD = f"assay-tenant-b-{RUN_ID}"
ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results.json"
PERFORMANCE = ROOT / "performance.json"
OWNER = f"assay_iso_owner_{RUN_ID}"
APP = f"assay_iso_app_{RUN_ID}"
TENANT_A = f"assay_iso_tenant_a_{RUN_ID}"
TENANT_B = f"assay_iso_tenant_b_{RUN_ID}"
BYPASS = f"assay_iso_bypass_{RUN_ID}"
ROLES = (OWNER, APP, TENANT_A, TENANT_B, BYPASS)
SOURCE_FILES = (
    "run.py",
    "checks.py",
    "performance.py",
    "test_checks.py",
    "test_run_controls.py",
    "README.md",
    "requirements.txt",
)


def source_hashes() -> dict[str, str]:
    return {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in SOURCE_FILES
    }


CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 5000


@dataclass
class OwnedResources:
    database: str | None = None
    roles: list[str] = field(default_factory=list)


def connect(
    db: str, user: str = ADMIN_USER, password: str = ADMIN_PASSWORD
) -> psycopg.Connection:
    return psycopg.connect(
        host=HOST,
        port=PORT,
        dbname=db,
        user=user,
        password=password,
        autocommit=True,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
    )


def rows(
    conn: psycopg.Connection, query: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(query, params)
        names = [desc.name for desc in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def one(conn: psycopg.Connection, query: str, params: tuple[Any, ...] = ()) -> Any:
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()[0]


def exec_sql(
    conn: psycopg.Connection, query: str, params: tuple[Any, ...] = ()
) -> None:
    with conn.cursor() as cur:
        cur.execute(query, params)


def create_fixture() -> OwnedResources:
    resources = OwnedResources()
    conn = connect(ADMIN_DB)
    try:
        if one(
            conn,
            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
            (DB_NAME,),
        ):
            raise RuntimeError(f"refusing to reuse existing database {DB_NAME}")
        existing = rows(
            conn, "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(ROLES),)
        )
        if existing:
            raise RuntimeError(f"refusing to reuse existing roles: {existing}")
        for role, statement in (
            (OWNER, f"CREATE ROLE {OWNER} NOLOGIN NOINHERIT"),
            (APP, f"CREATE ROLE {APP} LOGIN NOINHERIT PASSWORD '{APP_PASSWORD}'"),
            (
                TENANT_A,
                f"CREATE ROLE {TENANT_A} LOGIN NOINHERIT PASSWORD '{TENANT_A_PASSWORD}'",
            ),
            (
                TENANT_B,
                f"CREATE ROLE {TENANT_B} LOGIN NOINHERIT PASSWORD '{TENANT_B_PASSWORD}'",
            ),
            (BYPASS, f"CREATE ROLE {BYPASS} NOLOGIN NOINHERIT BYPASSRLS"),
        ):
            exec_sql(conn, statement)
            resources.roles.append(role)
        exec_sql(conn, f"CREATE DATABASE {DB_NAME} OWNER {OWNER}")
        resources.database = DB_NAME
        return resources
    except Exception as exc:
        if resources.database or resources.roles:
            try:
                teardown(resources)
            except Exception as cleanup_exc:
                raise RuntimeError(
                    f"fixture creation failed and partial cleanup failed: {cleanup_exc}"
                ) from exc
        raise
    finally:
        conn.close()


def setup_schema() -> None:
    conn = connect(DB_NAME)
    try:
        exec_sql(conn, f"REVOKE ALL ON DATABASE {DB_NAME} FROM PUBLIC")
        exec_sql(
            conn,
            f"GRANT CONNECT ON DATABASE {DB_NAME} TO {APP}, {TENANT_A}, {TENANT_B}, {BYPASS}",
        )
        exec_sql(conn, "REVOKE ALL ON SCHEMA public FROM PUBLIC")
        exec_sql(
            conn,
            f"GRANT USAGE ON SCHEMA public TO {APP}, {TENANT_A}, {TENANT_B}, {BYPASS}",
        )
        exec_sql(conn, f"SET ROLE {OWNER}")
        exec_sql(
            conn,
            """
            CREATE TABLE records (
                tenant_id text NOT NULL,
                record_id integer NOT NULL,
                idempotency_key text NOT NULL,
                value integer NOT NULL CHECK (value >= 0),
                payload text NOT NULL,
                PRIMARY KEY (tenant_id, record_id),
                UNIQUE (tenant_id, idempotency_key)
            );
            CREATE TABLE child_records (
                tenant_id text NOT NULL,
                child_id integer NOT NULL,
                parent_id integer NOT NULL,
                PRIMARY KEY (tenant_id, child_id),
                FOREIGN KEY (tenant_id, parent_id)
                    REFERENCES records (tenant_id, record_id)
            );
            CREATE TABLE events (
                tenant_id text NOT NULL,
                event_id integer NOT NULL,
                small_value integer NOT NULL,
                PRIMARY KEY (tenant_id, event_id)
            );
            CREATE TABLE owner_bypass_fixture (
                tenant_id text NOT NULL,
                marker text NOT NULL
            );
            CREATE TABLE policy_regression (
                row_id integer PRIMARY KEY,
                tenant_id text NOT NULL,
                marker text NOT NULL
            );
            CREATE TABLE implicit_check_fixture (
                row_id integer PRIMARY KEY,
                tenant_id text NOT NULL,
                marker text NOT NULL
            );
            """,
        )
        for table in ("records", "child_records", "events"):
            exec_sql(conn, f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            exec_sql(conn, f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            exec_sql(
                conn,
                f"""
                CREATE POLICY {table}_app_policy ON {table} TO {APP}
                    USING (tenant_id = current_setting('app.tenant_id', true))
                    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
                CREATE POLICY {table}_role_policy ON {table} TO {TENANT_A}, {TENANT_B}
                    USING (tenant_id = current_user)
                    WITH CHECK (tenant_id = current_user);
                """,
            )
        for table in ("policy_regression", "implicit_check_fixture"):
            exec_sql(conn, f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            exec_sql(conn, f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        exec_sql(
            conn,
            f"""
            CREATE POLICY policy_regression_select ON policy_regression FOR SELECT TO {APP}
                USING (true)
                ;
            CREATE POLICY policy_regression_update ON policy_regression FOR UPDATE TO {APP}
                USING (tenant_id = current_setting('app.tenant_id', true))
                WITH CHECK (true);
            CREATE POLICY implicit_check_select ON implicit_check_fixture FOR SELECT TO {APP}
                USING (tenant_id = current_setting('app.tenant_id', true));
            CREATE POLICY implicit_check_update ON implicit_check_fixture FOR UPDATE TO {APP}
                USING (tenant_id = current_setting('app.tenant_id', true));
            """,
        )
        exec_sql(conn, "ALTER TABLE owner_bypass_fixture ENABLE ROW LEVEL SECURITY")
        exec_sql(
            conn,
            f"""
            CREATE POLICY owner_fixture_app_policy ON owner_bypass_fixture TO {APP}
                USING (tenant_id = current_setting('app.tenant_id', true))
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
            """,
        )
        exec_sql(conn, "RESET ROLE")
        exec_sql(
            conn,
            "INSERT INTO records VALUES (%s, 1, 'same-key', 10, 'a-one'), (%s, 2, 'a-key', 20, 'a-two'), (%s, 1, 'same-key', 30, 'b-one'), (%s, 2, 'b-key', 40, 'b-two'), (%s, 99, 'foreign-only', 50, 'b-foreign')",
            (TENANT_A, TENANT_A, TENANT_B, TENANT_B, TENANT_B),
        )
        exec_sql(
            conn,
            "INSERT INTO owner_bypass_fixture VALUES (%s, 'a'), (%s, 'b')",
            (TENANT_A, TENANT_B),
        )
        exec_sql(
            conn, "INSERT INTO policy_regression VALUES (1, %s, 'weak')", (TENANT_A,)
        )
        exec_sql(
            conn,
            "INSERT INTO implicit_check_fixture VALUES (1, %s, 'implicit')",
            (TENANT_A,),
        )
        exec_sql(
            conn,
            "INSERT INTO events (tenant_id, event_id, small_value) SELECT %s, i, i %% 10 FROM generate_series(1, 100) AS i",
            (TENANT_A,),
        )
        exec_sql(
            conn,
            "INSERT INTO events (tenant_id, event_id, small_value) SELECT %s, i, i %% 10 FROM generate_series(1, 100000) AS i",
            (TENANT_B,),
        )
        exec_sql(conn, "ANALYZE records")
        exec_sql(conn, "ANALYZE child_records")
        exec_sql(conn, "ANALYZE events")
        exec_sql(conn, "ANALYZE owner_bypass_fixture")
        exec_sql(conn, "ANALYZE policy_regression")
        exec_sql(conn, "ANALYZE implicit_check_fixture")
        exec_sql(
            conn,
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON records, child_records, events, owner_bypass_fixture, policy_regression, implicit_check_fixture TO {APP}, {TENANT_A}, {TENANT_B}",
        )
        exec_sql(
            conn,
            f"GRANT SELECT ON records, events, owner_bypass_fixture, policy_regression, implicit_check_fixture TO {BYPASS}",
        )
    finally:
        conn.close()


def catalog_snapshot() -> dict[str, Any]:
    conn = connect(DB_NAME)
    try:
        return {
            "server": one(conn, "SELECT version()"),
            "database": rows(conn, "SELECT current_database(), current_user"),
            "roles": rows(
                conn,
                """
                SELECT rolname, rolsuper, rolcanlogin, rolinherit, rolbypassrls
                FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname
                """,
                (list(ROLES),),
            ),
            "memberships": rows(
                conn,
                """
                SELECT member.rolname AS member, parent.rolname AS role
                FROM pg_auth_members m
                JOIN pg_roles member ON member.oid = m.member
                JOIN pg_roles parent ON parent.oid = m.roleid
                WHERE member.rolname = ANY(%s) OR parent.rolname = ANY(%s)
                ORDER BY member.rolname, parent.rolname
                """,
                (list(ROLES), list(ROLES)),
            ),
            "tables": rows(
                conn,
                """
                SELECT c.relname, pg_get_userbyid(c.relowner) AS owner,
                       c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = ANY(%s)
                ORDER BY c.relname
                """,
                (
                    [
                        "records",
                        "child_records",
                        "events",
                        "owner_bypass_fixture",
                        "policy_regression",
                        "implicit_check_fixture",
                    ],
                ),
            ),
            "policies": rows(
                conn,
                """
                SELECT schemaname, tablename, policyname, permissive, roles, cmd, qual, with_check
                FROM pg_policies WHERE schemaname = 'public' ORDER BY tablename, policyname
                """,
            ),
            "grants": rows(
                conn,
                """
                SELECT grantee, table_name, privilege_type
                FROM information_schema.role_table_grants
                WHERE table_schema = 'public' AND grantee = ANY(%s)
                ORDER BY grantee, table_name, privilege_type
                """,
                (list(ROLES),),
            ),
            "privilege_checks": rows(
                conn,
                """
                SELECT r.rolname,
                       has_table_privilege(r.rolname, 'public.records', 'SELECT') AS can_select,
                       has_table_privilege(r.rolname, 'public.records', 'INSERT') AS can_insert,
                       has_table_privilege(r.rolname, 'public.events', 'SELECT') AS can_events_select
                FROM pg_roles r WHERE r.rolname = ANY(%s) ORDER BY r.rolname
                """,
                (list(ROLES),),
            ),
        }
    finally:
        conn.close()


def attempt(label: str, fn: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"case": label, "ok": True, "value": fn()}
    except psycopg.Error as exc:
        return {
            "case": label,
            "ok": False,
            "sqlstate": exc.sqlstate,
            "error": str(exc).splitlines()[0][:240],
            "detail": getattr(exc.diag, "message_detail", None),
        }


def request(
    conn: psycopg.Connection, tenant: str, finish: str, fn: Callable[[], Any]
) -> Any:
    exec_sql(conn, "BEGIN")
    try:
        exec_sql(conn, "SELECT set_config('app.tenant_id', %s, true)", (tenant,))
        value = fn()
        exec_sql(conn, finish)
        return value
    except Exception:
        exec_sql(conn, "ROLLBACK")
        raise


def app_matrix() -> dict[str, Any]:
    conn = connect(DB_NAME, APP, APP_PASSWORD)
    observer = connect(DB_NAME)
    try:
        exec_sql(conn, "RESET app.tenant_id")
        cases: list[dict[str, Any]] = []
        cases.append(
            attempt(
                "omit_where_select",
                lambda: request(
                    conn,
                    TENANT_A,
                    "COMMIT",
                    lambda: one(conn, "SELECT count(*) FROM records"),
                ),
            )
        )

        def update_without_where() -> dict[str, Any]:
            before_b = one(
                observer,
                "SELECT value FROM records WHERE tenant_id = %s AND record_id = 1",
                (TENANT_B,),
            )
            with conn.cursor() as cur:
                cur.execute("UPDATE records SET value = value + 1")
                affected = cur.rowcount
            after_b = one(
                observer,
                "SELECT value FROM records WHERE tenant_id = %s AND record_id = 1",
                (TENANT_B,),
            )
            return {
                "updated": affected,
                "tenant_b_before": before_b,
                "tenant_b_after": after_b,
            }

        cases.append(
            attempt(
                "omit_where_update",
                lambda: request(conn, TENANT_A, "ROLLBACK", update_without_where),
            )
        )

        def delete_without_where() -> dict[str, Any]:
            before = one(conn, "SELECT count(*) FROM records")
            with conn.cursor() as cur:
                cur.execute("DELETE FROM records")
                affected = cur.rowcount
            after = one(conn, "SELECT count(*) FROM records")
            return {"before": before, "deleted": affected, "after": after}

        cases.append(
            attempt(
                "omit_where_delete",
                lambda: request(conn, TENANT_A, "ROLLBACK", delete_without_where),
            )
        )
        cases.append(
            attempt(
                "wrong_tenant_insert",
                lambda: request(
                    conn,
                    TENANT_A,
                    "ROLLBACK",
                    lambda: exec_sql(
                        conn,
                        "INSERT INTO records VALUES (%s, 70, 'wrong', 1, 'wrong')",
                        (TENANT_B,),
                    ),
                ),
            )
        )

        def policy_check_regression() -> dict[str, Any]:
            def broad_update() -> dict[str, Any]:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE policy_regression SET tenant_id = %s WHERE row_id = 1",
                        (TENANT_B,),
                    )
                    return {
                        "current_user": one(conn, "SELECT current_user"),
                        "tenant_context": one(
                            conn, "SELECT current_setting('app.tenant_id', true)"
                        ),
                        "updated": cur.rowcount,
                    }

            broad = attempt(
                "explicit_with_check_true",
                lambda: request(conn, TENANT_A, "COMMIT", broad_update),
            )
            broad_observed = None
            if broad["ok"]:
                broad_observed = one(
                    observer, "SELECT tenant_id FROM policy_regression WHERE row_id = 1"
                )
                exec_sql(
                    observer,
                    "UPDATE policy_regression SET tenant_id = %s WHERE row_id = 1",
                    (TENANT_A,),
                )
            implicit = attempt(
                "omitted_with_check_inherits_using",
                lambda: request(
                    conn,
                    TENANT_A,
                    "ROLLBACK",
                    lambda: exec_sql(
                        conn,
                        "UPDATE implicit_check_fixture SET tenant_id = %s WHERE row_id = 1",
                        (TENANT_B,),
                    ),
                ),
            )
            return {
                "explicit_with_check_true_plus_broad_select": {
                    **broad,
                    "observed_tenant": broad_observed,
                },
                "omitted_with_check": implicit,
            }

        cases.append(attempt("with_check_policy_behavior", policy_check_regression))
        cases.append(
            attempt(
                "cross_join_lookup",
                lambda: request(
                    conn,
                    TENANT_A,
                    "COMMIT",
                    lambda: {
                        "cross_join_rows": one(
                            conn, "SELECT count(*) FROM records r CROSS JOIN records s"
                        ),
                        "visible_rows": one(conn, "SELECT count(*) FROM records"),
                        "expected_cross_join_rows": 4,
                    },
                ),
            )
        )

        def duplicate() -> dict[str, Any]:
            exec_sql(
                conn,
                "INSERT INTO records VALUES (%s, 70, 'once', 1, 'first')",
                (TENANT_A,),
            )
            try:
                exec_sql(
                    conn,
                    "INSERT INTO records VALUES (%s, 71, 'once', 1, 'second')",
                    (TENANT_A,),
                )
            except psycopg.Error as exc:
                return {"duplicate_rejected": True, "sqlstate": exc.sqlstate}
            return {"duplicate_rejected": False}

        cases.append(
            attempt(
                "duplicate_idempotency_key",
                lambda: request(conn, TENANT_A, "ROLLBACK", duplicate),
            )
        )
        cases.append(
            attempt(
                "cross_tenant_composite_fk",
                lambda: request(
                    conn,
                    TENANT_A,
                    "ROLLBACK",
                    lambda: exec_sql(
                        conn,
                        "INSERT INTO child_records VALUES (%s, 1, 99)",
                        (TENANT_A,),
                    ),
                ),
            )
        )
        cases.append(
            attempt(
                "missing_context",
                lambda: {
                    "visible_rows": one(conn, "SELECT count(*) FROM records"),
                    "insert": attempt(
                        "missing_context_insert",
                        lambda: exec_sql(
                            conn,
                            "INSERT INTO records VALUES (%s, 80, 'missing', 1, 'missing')",
                            (TENANT_A,),
                        ),
                    ),
                },
            )
        )

        def context_boundary(finish: str, tenant: str) -> dict[str, Any]:
            visible = request(
                conn, tenant, finish, lambda: one(conn, "SELECT count(*) FROM records")
            )
            after = one(conn, "SELECT count(*) FROM records")
            setting = one(conn, "SELECT current_setting('app.tenant_id', true)")
            return {
                "inside_visible": visible,
                "after_visible_without_context": after,
                "setting_after": setting,
            }

        cases.append(
            attempt(
                "context_after_rollback", lambda: context_boundary("ROLLBACK", TENANT_A)
            )
        )
        cases.append(
            attempt(
                "context_after_commit", lambda: context_boundary("COMMIT", TENANT_B)
            )
        )

        def spoof() -> dict[str, Any]:
            exec_sql(conn, "SELECT set_config('app.tenant_id', %s, false)", (TENANT_B,))
            return {
                "spoofed_tenant_rows": one(conn, "SELECT count(*) FROM records"),
                "expected": "counterexample",
            }

        cases.append(attempt("same_role_can_spoof_tenant_guc", spoof))
        exec_sql(conn, "RESET app.tenant_id")
        return {"role": APP, "cases": cases}
    finally:
        conn.close()
        observer.close()


def role_matrix() -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    for role, password, other in (
        (TENANT_A, TENANT_A_PASSWORD, TENANT_B),
        (TENANT_B, TENANT_B_PASSWORD, TENANT_A),
    ):
        conn = connect(DB_NAME, role, password)
        try:
            current = one(conn, "SELECT current_user")
            visible = one(conn, "SELECT count(*) FROM records")
            cross_insert = attempt(
                "cross_tenant_insert",
                lambda: exec_sql(
                    conn,
                    "INSERT INTO records VALUES (%s, 70, 'role-cross', 1, 'wrong')",
                    (other,),
                ),
            )
            set_other = attempt(
                "set_role_other_tenant", lambda: exec_sql(conn, f"SET ROLE {other}")
            )
            exec_sql(conn, "RESET ROLE")
            after_reset = one(conn, "SELECT current_user")
            exec_sql(conn, "SELECT set_config('app.tenant_id', %s, false)", (other,))
            guc_spoof_visible = one(conn, "SELECT count(*) FROM records")
            exec_sql(conn, "RESET app.tenant_id")
            out.append(
                {
                    "role": role,
                    "current_user": current,
                    "visible_rows": visible,
                    "cross_tenant_insert": cross_insert,
                    "set_role_other_tenant": set_other,
                    "current_user_after_reset": after_reset,
                    "guc_spoof_visible_rows": guc_spoof_visible,
                }
            )
        finally:
            conn.close()

    app = connect(DB_NAME, APP, APP_PASSWORD)
    try:
        app_setrole: list[dict[str, Any]] = []
        for target in (OWNER, BYPASS, TENANT_A, TENANT_B):
            denied = attempt(
                "app_set_role",
                lambda target=target: exec_sql(app, f"SET ROLE {target}"),
            )
            exec_sql(app, "RESET ROLE")
            app_setrole.append(
                {
                    "target": target,
                    **denied,
                    "current_user_after_reset": one(app, "SELECT current_user"),
                }
            )
    finally:
        app.close()

    admin = connect(DB_NAME)
    try:
        exec_sql(admin, f"SET ROLE {OWNER}")
        owner_before_force = one(admin, "SELECT count(*) FROM owner_bypass_fixture")
        exec_sql(admin, "RESET ROLE")
        exec_sql(admin, "ALTER TABLE owner_bypass_fixture FORCE ROW LEVEL SECURITY")
        exec_sql(admin, f"SET ROLE {OWNER}")
        owner_after_force = one(admin, "SELECT count(*) FROM owner_bypass_fixture")
        exec_sql(admin, "RESET ROLE")
        exec_sql(admin, f"SET ROLE {BYPASS}")
        bypass_rows = one(admin, "SELECT count(*) FROM records")
        exec_sql(admin, "RESET ROLE")
        return {
            "per_tenant_roles": out,
            "app_setrole_attempts": app_setrole,
            "table_owner_unforced_rows": owner_before_force,
            "table_owner_forced_rows": owner_after_force,
            "bypassrls_rows": bypass_rows,
            "expected_counterexamples": ["table_owner_unforced_rows", "bypassrls_rows"],
        }
    finally:
        admin.close()


def migration_case() -> dict[str, Any]:
    conn = connect(DB_NAME)
    try:
        exec_sql(conn, "BEGIN")
        exec_sql(
            conn,
            "CREATE TABLE migration_fixture (tenant_id text PRIMARY KEY, old_value text NOT NULL)",
        )
        exec_sql(conn, "INSERT INTO migration_fixture VALUES ('old', 'v1')")
        exec_sql(conn, "ALTER TABLE migration_fixture ADD COLUMN new_value text")
        exec_sql(
            conn,
            "INSERT INTO migration_fixture (tenant_id, old_value) VALUES ('old-writer', 'v1')",
        )
        exec_sql(
            conn,
            "UPDATE migration_fixture SET new_value = old_value WHERE new_value IS NULL",
        )
        exec_sql(
            conn, "INSERT INTO migration_fixture VALUES ('new-writer', 'v1', 'v2')"
        )
        compatible = rows(conn, "SELECT * FROM migration_fixture ORDER BY tenant_id")
        exec_sql(conn, "COMMIT")
        exec_sql(conn, "DROP TABLE migration_fixture")
        return {
            "status": "observed",
            "old_and_new_rows": compatible,
            "contract": "not attempted",
        }
    finally:
        conn.close()


def teardown(resources: OwnedResources) -> dict[str, Any]:
    audit = connect(ADMIN_DB)
    try:
        active = []
        dropped = True
        cleanup_errors = []
        if resources.database:
            try:
                active = rows(
                    audit,
                    "SELECT pid, usename, datname, state FROM pg_stat_activity WHERE datname = %s",
                    (resources.database,),
                )
                exists = one(
                    audit,
                    "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
                    (resources.database,),
                )
                if exists:
                    exec_sql(audit, f"DROP DATABASE {resources.database} WITH (FORCE)")
                dropped = not one(
                    audit,
                    "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
                    (resources.database,),
                )
            except Exception as exc:
                dropped = False
                cleanup_errors.append(f"database: {type(exc).__name__}: {exc}")
        for role in reversed(resources.roles):
            try:
                exec_sql(audit, f"DROP ROLE IF EXISTS {role}")
            except Exception as exc:
                cleanup_errors.append(f"role {role}: {type(exc).__name__}: {exc}")
        remaining = rows(
            audit,
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
            (resources.roles,),
        )
        report = {
            "owned_database": resources.database,
            "owned_roles": resources.roles,
            "target_database_sessions_before_drop": active,
            "database_dropped": dropped,
            "roles_remaining": remaining,
        }
        if not dropped or remaining or cleanup_errors:
            raise RuntimeError(
                f"owned resource cleanup incomplete: {report}; errors={cleanup_errors}"
            )
        return report
    finally:
        audit.close()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")


def main() -> int:
    result: dict[str, Any] = {
        "environment": {
            "python": os.sys.version,
            "psycopg": psycopg.__version__,
            "host": HOST,
            "port": PORT,
            "database": DB_NAME,
            "roles": list(ROLES),
            "app_role": APP,
        },
        "source_sha256": source_hashes(),
        "status": "starting",
    }
    resources: OwnedResources | None = None
    failure: Exception | None = None
    try:
        resources = create_fixture()
        setup_schema()
        result["catalog"] = catalog_snapshot()
        result["security_matrix"] = app_matrix()
        result["role_matrix"] = role_matrix()
        result["migration"] = migration_case()
        performance = performance_matrix(
            DB_NAME,
            APP,
            APP_PASSWORD,
            BYPASS,
            TENANT_A,
            TENANT_B,
            connect,
            exec_sql,
            one,
        )
        write_json(PERFORMANCE, performance)
        validate_expected(result, performance, TENANT_B)
        result["status"] = "passed_with_expected_counterexamples"
    except Exception as exc:
        failure = exc
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if resources is not None:
            try:
                result["teardown"] = teardown(resources)
            except Exception as exc:
                result["status"] = "error"
                result["teardown_error"] = f"{type(exc).__name__}: {exc}"
                if failure is None:
                    failure = exc
        write_json(RESULTS, result)
    if failure is not None:
        raise failure
    print(
        json.dumps(
            {
                "status": result["status"],
                "results": str(RESULTS),
                "performance": str(PERFORMANCE),
                "teardown": result.get("teardown"),
            },
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]==3.3.4"]
# ///
"""Rolling-schema compatibility experiment, local-only.

Reviewer runs:
    npm install
    npm run build
    uv run verifier.py --output /tmp/assay-result.json
"""

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import signal
import subprocess
import sys
import time
import uuid
from importlib import metadata as package_metadata
from pathlib import Path
from unittest import mock

import psycopg


HERE = Path(__file__).resolve().parent

PG_HOST = "127.0.0.1"
PG_PORT = 55439
PG_USER = "postgres"
PG_PASSWORD = "assay-local-only"
MAINTENANCE_DB = "postgres"

OWNER_PATTERN = re.compile(r"assay_[0-9a-f]{32}")

NODE_TIMEOUT_SECONDS = 20
TSC_TIMEOUT_SECONDS = 120

DIST_JS = HERE / "dist" / "newclient.js"
CLIENT_TS = HERE / "newclient.ts"
SCHEMA_SQL = HERE / "schema_phases.sql"
PACKAGE_JSON = HERE / "package.json"
PACKAGE_LOCK = HERE / "package-lock.json"
TSCONFIG_JSON = HERE / "tsconfig.json"
TSC_BIN = HERE / "node_modules" / "typescript" / "bin" / "tsc"
TYPESCRIPT_PACKAGE_JSON = HERE / "node_modules" / "typescript" / "package.json"
PG_PACKAGE_JSON = HERE / "node_modules" / "pg" / "package.json"

HASHED_INPUTS = {
    "verifier_py": HERE / "verifier.py",
    "newclient_ts": CLIENT_TS,
    "newclient_js": DIST_JS,
    "schema_sql": SCHEMA_SQL,
    "package_json": PACKAGE_JSON,
    "package_lock": PACKAGE_LOCK,
    "tsconfig": TSCONFIG_JSON,
}

# (old_roundtrip, new_roundtrip, old_visible_to_new, new_visible_to_old).
# Expand new-write is False because title NOT NULL rejects display-only inserts.
EXPECTED_MATRIX = {
    "original": (True, False, False, False),
    "naive_rename": (False, True, False, False),
    "expand": (True, False, False, False),
    "bridge": (True, True, True, True),
    "contract": (False, True, False, False),
}
EXPECTED_GATE = {phase: (phase == "bridge") for phase in EXPECTED_MATRIX}

# SQLSTATEs accepted when a same-version write is expected to fail.
EXPECTED_WRITE_SQLSTATES = {
    "original": {"new": {"42703"}},
    "naive_rename": {"old": {"42703"}},
    "expand": {"new": {"23502"}},
    "contract": {"old": {"42703"}},
}

# Trusted supplied rollout state; retirement cannot be auto-detected.
OLD_CLIENTS_RETIRED = True

REQUIRED_SECTIONS = (
    "phase:original",
    "phase:naive_rename",
    "phase:expand",
    "phase:bridge",
    "phase:contract",
    "trigger:bridge_sync",
)


def sha256_of_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot_hashes():
    return {name: sha256_of_file(path) for name, path in HASHED_INPUTS.items()}


def child_env(database=None):
    env = {
        key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ
    }
    env.update(
        {
            "PGHOST": PG_HOST,
            "PGPORT": str(PG_PORT),
            "PGUSER": PG_USER,
            "PGPASSWORD": PG_PASSWORD,
        }
    )
    if database is not None:
        env["PGDATABASE"] = database
    return env


def connect_db(database, autocommit=True):
    return psycopg.connect(
        host=PG_HOST,
        port=PG_PORT,
        dbname=database,
        user=PG_USER,
        password=PG_PASSWORD,
        connect_timeout=5,
        autocommit=autocommit,
        options="-c statement_timeout=5s",
    )


def schema_sections(path):
    sections = {}
    current_name = None
    current_lines = []
    header = re.compile(r"--\s*(PHASE|TRIGGER)\s*:\s*(\S+)")
    for line in Path(path).read_text().splitlines(keepends=True):
        match = header.match(line)
        if match:
            if current_name:
                sections[current_name] = "".join(current_lines)
            current_name = f"{match.group(1).lower()}:{match.group(2)}"
            current_lines = []
        elif current_name is not None:
            current_lines.append(line)
    if current_name:
        sections[current_name] = "".join(current_lines)
    return sections


def db_error_result(error):
    first_line = (str(error).splitlines() + [type(error).__name__])[0][:200]
    return {
        "ok": False,
        "sqlstate": getattr(error, "sqlstate", None),
        "message": first_line,
    }


def is_unknown_result(result):
    return isinstance(result, dict) and bool(result.get("unknown"))


def summarize_error(result):
    if not isinstance(result, dict):
        return "non-dict"
    if result.get("unknown"):
        return f"UNKNOWN({result.get('message', '')[:100]})"
    if result.get("sqlstate"):
        return str(result["sqlstate"])
    if result.get("message"):
        return str(result["message"])[:120]
    return f"value={result.get('value')!r}"


def old_insert(database, title, row_id=None):
    try:
        with connect_db(database) as connection:
            if row_id is None:
                row = connection.execute(
                    "INSERT INTO documents(title) VALUES (%s) RETURNING id", (title,)
                ).fetchone()
                return {"ok": True, "id": str(row[0])}
            row = connection.execute(
                "INSERT INTO documents(id, title) VALUES (%s, %s)"
                " ON CONFLICT (id) DO NOTHING RETURNING id",
                (row_id, title),
            ).fetchone()
            return {"ok": True, "id": str(row[0]) if row else str(row_id)}
    except psycopg.Error as error:
        return db_error_result(error)
    except Exception as error:
        return {"ok": False, "unknown": True, "message": f"harness:{error}"[:200]}


def old_read(database, row_id):
    try:
        with connect_db(database) as connection:
            row = connection.execute(
                "SELECT title FROM documents WHERE id = %s", (row_id,)
            ).fetchone()
            if row is None:
                return {"ok": False, "message": "row missing"}
            return {"ok": True, "value": row[0]}
    except psycopg.Error as error:
        return db_error_result(error)
    except Exception as error:
        return {"ok": False, "unknown": True, "message": f"harness:{error}"[:200]}


def old_update(database, row_id, title):
    try:
        with connect_db(database) as connection:
            updated = connection.execute(
                "UPDATE documents SET title = %s WHERE id = %s", (title, row_id)
            ).rowcount
            if updated == 1:
                return {"ok": True, "rowcount": 1}
            return {"ok": False, "message": f"rowcount {updated}"}
    except psycopg.Error as error:
        return db_error_result(error)
    except Exception as error:
        return {"ok": False, "unknown": True, "message": f"harness:{error}"[:200]}


def read_both_columns(database, row_id):
    with connect_db(database) as connection:
        return connection.execute(
            "SELECT title, display_name FROM documents WHERE id = %s", (row_id,)
        ).fetchone()


def run_process(command, env, timeout_seconds):
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        output = process.communicate(timeout=timeout_seconds)
        return process, output, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.communicate()
        return process, (None, None), True


def node_call(database, operation, *args):
    if not DIST_JS.exists():
        return {
            "ok": False,
            "unknown": True,
            "message": "missing dist; stale build rejected",
        }
    command = ["node", str(DIST_JS), operation, *[str(arg) for arg in args]]
    process, (stdout, stderr), timed_out = run_process(
        command, child_env(database), NODE_TIMEOUT_SECONDS
    )
    if timed_out:
        return {
            "ok": False,
            "unknown": True,
            "message": f"node timeout {NODE_TIMEOUT_SECONDS}s; group killed",
        }
    if process.returncode != 0:
        detail = ((stderr or stdout) or "").strip()[
            -300:
        ] or f"exit {process.returncode}"
        return {"ok": False, "unknown": True, "message": detail}
    try:
        payload = json.loads((stdout or "").strip().splitlines()[-1])
        if isinstance(payload, dict) and "ok" in payload:
            if (
                not payload.get("ok")
                and not payload.get("sqlstate")
                and not str(payload.get("message", "")).startswith(
                    ("rowcount", "row missing")
                )
            ):
                return {
                    "ok": False,
                    "unknown": True,
                    "message": str(payload.get("message", ""))[:200],
                }
            return payload
        return {"ok": False, "unknown": True, "message": "bad client JSON"}
    except Exception:
        return {"ok": False, "unknown": True, "message": "unparseable client output"}


def read_package_version(package_json_path):
    try:
        return json.loads(Path(package_json_path).read_text()).get("version", "unknown")
    except Exception:
        return "unknown"


def collect_versions(faults):
    declared = json.loads(PACKAGE_JSON.read_text())
    declared_pg = declared.get("dependencies", {}).get("pg", "unknown")
    declared_typescript = declared.get("devDependencies", {}).get(
        "typescript", "unknown"
    )
    installed_pg = read_package_version(PG_PACKAGE_JSON)
    installed_typescript = read_package_version(TYPESCRIPT_PACKAGE_JSON)
    versions = {
        "python": platform.python_version(),
        "node": get_node_version(),
        "psycopg": package_metadata.version("psycopg"),
        "pg_npm": declared_pg,
        "pg_installed": installed_pg,
        "tsc": installed_typescript,
        "typescript_declared": declared_typescript,
    }
    if installed_pg != "unknown" and installed_pg != declared_pg:
        faults.append(
            f"versions: pg installed {installed_pg} != declared {declared_pg}"
        )
    if (
        installed_typescript != "unknown"
        and installed_typescript != declared_typescript
    ):
        faults.append(
            f"versions: typescript installed {installed_typescript} != declared {declared_typescript}"
        )
    return versions


def build_client(stages, faults):
    started = time.monotonic()
    if not TSC_BIN.exists():
        faults.append("build: pinned tsc missing; run npm install; stale dist rejected")
        return False
    process, _, timed_out = run_process(
        ["node", str(TSC_BIN), "-p", str(TSCONFIG_JSON)],
        child_env(),
        TSC_TIMEOUT_SECONDS,
    )
    stages["build_s"] = round(time.monotonic() - started, 3)
    if timed_out:
        faults.append(f"build: tsc timeout {TSC_TIMEOUT_SECONDS}s; group killed")
        return False
    if process.returncode != 0:
        faults.append(f"build: tsc exit {process.returncode}; stale dist rejected")
        return False
    if not DIST_JS.exists():
        faults.append("build: dist missing after compile")
        return False
    return True


def check_compatibility_matrix(
    database, sections, positive_evidence, expected_counterexamples, faults
):
    started = time.monotonic()
    gate = {}
    detail = {}
    positive_checks = 0
    for phase in EXPECTED_MATRIX:
        with connect_db(database) as connection:
            connection.execute(sections[f"phase:{phase}"])
            if phase == "bridge":
                connection.execute(sections["trigger:bridge_sync"])
        old_title = f"assay-old-{phase}"
        new_title = f"assay-new-{phase}"
        old_write = old_insert(database, old_title)
        old_reread = (
            old_read(database, old_write["id"])
            if old_write.get("ok")
            else {"ok": False, "unknown": True, "message": "no id"}
        )
        new_write = node_call(database, "write", new_title)
        new_reread = (
            node_call(database, "read", new_write["id"])
            if new_write.get("ok")
            else {"ok": False, "unknown": True, "message": "no id"}
        )
        new_reads_old = (
            node_call(database, "read", old_write["id"])
            if old_write.get("ok")
            else {"ok": False, "unknown": True, "message": "vacuous"}
        )
        old_reads_new = (
            old_read(database, new_write["id"])
            if new_write.get("ok")
            else {"ok": False, "unknown": True, "message": "vacuous"}
        )
        old_roundtrip = (
            old_write.get("ok")
            and old_reread.get("ok")
            and old_reread.get("value") == old_title
        )
        new_roundtrip = (
            new_write.get("ok")
            and new_reread.get("ok")
            and new_reread.get("value") == new_title
        )
        cross_new = new_reads_old.get("ok") and new_reads_old.get("value") == old_title
        cross_old = old_reads_new.get("ok") and old_reads_new.get("value") == new_title
        flags = (old_roundtrip, new_roundtrip, cross_new, cross_old)
        wanted = EXPECTED_MATRIX[phase]
        detail[phase] = {
            "flags": list(flags),
            "want": list(wanted),
            "old_write": old_write,
            "old_read": old_reread,
            "new_write": new_write,
            "new_read": new_reread,
            "x_new": new_reads_old,
            "x_old": old_reads_new,
        }
        if flags != wanted:
            faults.append(
                f"matrix:{phase}: want {list(wanted)} got {list(flags)}"
                f" a:{summarize_error(old_write)}/{summarize_error(old_reread)}"
                f" b:{summarize_error(new_write)}/{summarize_error(new_reread)}"
                f" c:{summarize_error(new_reads_old)} d:{summarize_error(old_reads_new)}"
            )
            gate[phase] = bool(
                old_roundtrip and new_roundtrip and cross_new and cross_old
            )
            print(f"[assay] matrix {phase} gate={gate[phase]}", file=sys.stderr)
            continue
        for label, succeeded, result, side in (
            ("old-write-old-read", old_roundtrip, old_write, "old"),
            ("new-write-new-read", new_roundtrip, new_write, "new"),
        ):
            wanted_index = 0 if side == "old" else 1
            if wanted[wanted_index]:
                positive_checks += 1
            elif is_unknown_result(result) or not result.get("sqlstate"):
                faults.append(
                    f"matrix:{phase}:{label} lacks SQLSTATE ({summarize_error(result)})"
                )
            elif result["sqlstate"] not in EXPECTED_WRITE_SQLSTATES.get(phase, {}).get(
                side, set()
            ):
                faults.append(f"matrix:{phase}:{label} bad {summarize_error(result)}")
            else:
                expected_counterexamples.append(
                    f"matrix:{phase}:{label} incompatible as expected ({summarize_error(result)})"
                )
        for label, succeeded, result, source_ok, wanted_index, title in (
            (
                "old-write-new-read",
                cross_new,
                new_reads_old,
                old_roundtrip,
                2,
                old_title,
            ),
            (
                "new-write-old-read",
                cross_old,
                old_reads_new,
                new_roundtrip,
                3,
                new_title,
            ),
        ):
            if wanted[wanted_index]:
                positive_checks += 1
            elif not source_ok:
                pass
            elif is_unknown_result(result):
                faults.append(
                    f"matrix:{phase}:{label} unknown ({summarize_error(result)})"
                )
            elif result.get("ok") and result.get("value") != title:
                expected_counterexamples.append(
                    f"matrix:{phase}:{label} value mismatch as expected ({summarize_error(result)})"
                )
            elif result.get("sqlstate") in ("42703", "23502"):
                expected_counterexamples.append(
                    f"matrix:{phase}:{label} incompatible as expected ({summarize_error(result)})"
                )
            elif not succeeded:
                faults.append(
                    f"matrix:{phase}:{label} lacks cause ({summarize_error(result)})"
                )
        gate[phase] = bool(old_roundtrip and new_roundtrip and cross_new and cross_old)
        print(f"[assay] matrix {phase} gate={gate[phase]}", file=sys.stderr)
    if gate != EXPECTED_GATE:
        faults.append(f"gate want {EXPECTED_GATE} got {gate}")
    return time.monotonic() - started, positive_checks, gate, detail


def check_backfill(
    database, sections, positive_evidence, expected_counterexamples, faults
):
    started = time.monotonic()
    positive_checks = 0
    witness = {}
    with connect_db(database) as connection:
        connection.execute(sections["phase:expand"])
    seed = old_insert(database, "assay-v1-snapshot")
    if not seed.get("ok") or is_unknown_result(seed):
        faults.append(f"backfill seed ({summarize_error(seed)})")
        return time.monotonic() - started, 0, witness
    positive_checks += 1
    snapshot_value = old_read(database, seed["id"])["value"]
    concurrent = old_update(database, seed["id"], "assay-v2-newer")
    if not concurrent.get("ok"):
        faults.append(f"backfill concurrent ({summarize_error(concurrent)})")
        return time.monotonic() - started, positive_checks, witness
    with connect_db(database) as connection:
        connection.execute(
            "UPDATE documents SET display_name = %s WHERE id = %s",
            (snapshot_value, seed["id"]),
        )
        final_row = connection.execute(
            "SELECT title, display_name FROM documents WHERE id = %s", (seed["id"],)
        ).fetchone()
    witness["naive_final"] = [final_row[0], final_row[1]]
    if (
        final_row[0] == "assay-v2-newer"
        and final_row[1] == "assay-v1-snapshot"
        and final_row[0] != final_row[1]
    ):
        expected_counterexamples.append(
            f"naive-backfill lost update: title={final_row[0]!r} vs display={final_row[1]!r};"
            " equality is not protection"
        )
    else:
        faults.append(f"backfill want divergence got {list(final_row)}")
    with connect_db(database) as connection:
        connection.execute(sections["phase:bridge"])
        connection.execute(sections["trigger:bridge_sync"])

    def check_synced(row_id, wanted_value, what):
        committed = read_both_columns(database, row_id)
        if committed is not None and tuple(committed) == (wanted_value, wanted_value):
            positive_evidence.append(
                f"bridge: {what} committed ({wanted_value!r} both)"
            )
            return True
        faults.append(
            f"bridge: {what} got {list(committed) if committed else committed}"
        )
        return False

    old_bridge = old_insert(database, "assay-b1")
    if old_bridge.get("ok"):
        if check_synced(old_bridge["id"], "assay-b1", "old insert synced"):
            positive_checks += 1
    else:
        faults.append(f"bridge old insert ({summarize_error(old_bridge)})")
    old_bridge_update = (
        old_update(database, old_bridge["id"], "assay-b2")
        if old_bridge.get("ok")
        else {"ok": False}
    )
    if old_bridge_update.get("ok"):
        if check_synced(old_bridge["id"], "assay-b2", "old 1-field update propagated"):
            positive_checks += 1
    else:
        faults.append(
            f"bridge old update stayed b1/b1 ({summarize_error(old_bridge_update)})"
        )
    new_bridge = node_call(database, "write", "assay-n1")
    if new_bridge.get("ok") and not is_unknown_result(new_bridge):
        if check_synced(new_bridge["id"], "assay-n1", "new insert backfilled title"):
            positive_checks += 1
    else:
        faults.append(f"bridge new insert ({summarize_error(new_bridge)})")
    new_bridge_update = (
        node_call(database, "update", new_bridge["id"], "assay-n2")
        if new_bridge.get("ok")
        else {"ok": False}
    )
    if new_bridge_update.get("ok") and not is_unknown_result(new_bridge_update):
        if check_synced(new_bridge["id"], "assay-n2", "new 1-field update propagated"):
            positive_checks += 1
    else:
        faults.append(
            f"bridge new update stayed n1/n1 ({summarize_error(new_bridge_update)})"
        )
    try:
        with connect_db(database) as connection:
            connection.execute(
                "INSERT INTO documents(title, display_name) VALUES (%s, %s)",
                ("assay-conflict-a", "assay-conflict-b"),
            )
        faults.append("bridge: conflict accepted (must reject)")
    except psycopg.Error as error:
        if error.sqlstate in ("P0001", "23514"):
            expected_counterexamples.append(
                f"conflict rejected as expected ({error.sqlstate})"
            )
        else:
            faults.append(f"bridge conflict bad {error.sqlstate}")
    with connect_db(database) as connection:
        guarded_count = connection.execute(
            "UPDATE documents SET display_name = %s WHERE id = %s AND display_name IS NULL",
            ("assay-stale-guarded", old_bridge["id"]),
        ).rowcount
        guarded_row = connection.execute(
            "SELECT title, display_name FROM documents WHERE id = %s",
            (old_bridge["id"],),
        ).fetchone()
    if guarded_count == 0 and tuple(guarded_row) == ("assay-b2", "assay-b2"):
        positive_checks += 1
        positive_evidence.append("guarded backfill IS NULL touched 0; newer title kept")
    else:
        faults.append(f"guarded touched {guarded_count} got {list(guarded_row)}")
    with connect_db(database) as connection:
        # Stale SQL text equals a legitimate write, so both fields converge to stale.
        connection.execute(
            "UPDATE documents SET display_name = %s WHERE id = %s",
            ("assay-stale-unguarded", old_bridge["id"]),
        )
        unguarded_row = connection.execute(
            "SELECT title, display_name FROM documents WHERE id = %s",
            (old_bridge["id"],),
        ).fetchone()
    if tuple(unguarded_row) == ("assay-stale-unguarded",) * 2:
        expected_counterexamples.append(
            "unguarded stale accepted: both fields clobbered (cannot distinguish from legit write)"
        )
    else:
        faults.append(f"unguarded got {list(unguarded_row)}")
    retry_id = str(uuid.uuid4())
    old_insert(database, "assay-retry", retry_id)
    old_insert(database, "assay-retry", retry_id)
    with connect_db(database) as connection:
        retry_count = connection.execute(
            "SELECT count(*) FROM documents WHERE id = %s", (retry_id,)
        ).fetchone()[0]
        retry_row = connection.execute(
            "SELECT title, display_name FROM documents WHERE id = %s", (retry_id,)
        ).fetchone()
    if retry_count == 1 and tuple(retry_row) == ("assay-retry",) * 2:
        positive_checks += 1
        positive_evidence.append("retry: single row, invariant holds")
    else:
        faults.append(f"retry {retry_count}/{list(retry_row)}")
    return time.monotonic() - started, positive_checks, witness


def check_rollout(
    database, sections, positive_evidence, expected_counterexamples, faults
):
    started = time.monotonic()
    positive_checks = 0
    sql_log = ["CREATE TABLE documents(id uuid PK,title TEXT NOT NULL)--prod"]
    with connect_db(database) as connection:
        connection.execute(sections["phase:original"])
    first_seed = old_insert(database, "roll-r1-v1")
    second_seed = old_insert(database, "roll-r2-v1")
    if not (first_seed.get("ok") and second_seed.get("ok")):
        faults.append(
            f"rollout seed {summarize_error(first_seed)}/{summarize_error(second_seed)}"
        )
        return time.monotonic() - started, 0, {"sql": sql_log}
    positive_checks += 2
    with connect_db(database) as connection:
        connection.execute("ALTER TABLE documents ADD COLUMN display_name TEXT NULL")
        sql_log.append("ALTER TABLE documents ADD COLUMN display_name TEXT NULL")
        connection.execute(sections["trigger:bridge_sync"])
        sql_log.append(
            "CREATE TRIGGER assay_sync_title_display BEFORE INSERT OR UPDATE ..."
        )
    interleaved = old_update(database, first_seed["id"], "roll-r1-v2")
    if not interleaved.get("ok"):
        faults.append(f"rollout interleave ({summarize_error(interleaved)})")
        return time.monotonic() - started, positive_checks, {"sql": sql_log}
    if tuple(read_both_columns(database, first_seed["id"])) != ("roll-r1-v2",) * 2:
        faults.append("rollout interleave not propagated")
        return time.monotonic() - started, positive_checks, {"sql": sql_log}
    positive_checks += 1
    positive_evidence.append("rollout: interleaved old write propagated")
    with connect_db(database) as connection:
        backfilled = connection.execute(
            "UPDATE documents SET display_name = title WHERE display_name IS NULL"
        ).rowcount
        sql_log.append(
            "UPDATE documents SET display_name=title WHERE display_name IS NULL"
        )
        synced_rows = connection.execute(
            "SELECT title, display_name FROM documents ORDER BY title"
        ).fetchall()
    if backfilled != 1:
        faults.append(f"rollout backfill touched {backfilled} want 1")
        return time.monotonic() - started, positive_checks, {"sql": sql_log}
    if any(title != display or title is None for title, display in synced_rows):
        faults.append(f"rollout unsynced {synced_rows}")
        return time.monotonic() - started, positive_checks, {"sql": sql_log}
    positive_checks += 1
    positive_evidence.append(
        f"rollout: backfill filled 1 NULL from current title; {len(synced_rows)} synced"
    )
    try:
        with connect_db(database) as connection:
            connection.execute(
                "ALTER TABLE documents ADD CONSTRAINT assay_names_equal CHECK"
                " (title IS NOT NULL AND display_name IS NOT NULL AND title = display_name)"
            )
            sql_log.append(
                "ALTER TABLE ... ADD CONSTRAINT assay_names_equal"
                " CHECK(title NOTNULL AND display_name NOTNULL AND title=display_name)"
            )
    except psycopg.Error as error:
        faults.append(f"rollout CHECK ({error.sqlstate})")
        return time.monotonic() - started, positive_checks, {"sql": sql_log}
    positive_checks += 1
    old_new_row = old_insert(database, "roll-old-new")
    new_new_row = node_call(database, "write", "roll-new-new")
    old_ok = (
        old_new_row.get("ok")
        and old_read(database, old_new_row["id"]).get("value") == "roll-old-new"
    )
    new_ok = (
        new_new_row.get("ok")
        and node_call(database, "read", new_new_row["id"]).get("value")
        == "roll-new-new"
    )
    cross_new_ok = (
        node_call(database, "read", old_new_row["id"]).get("value") == "roll-old-new"
        if old_ok
        else False
    )
    cross_old_ok = (
        old_read(database, new_new_row["id"]).get("value") == "roll-new-new"
        if new_ok
        else False
    )
    if old_ok and new_ok and cross_new_ok and cross_old_ok:
        positive_checks += 4
        positive_evidence.append("rollout: both clients roundtrip")
    else:
        faults.append(
            f"rollout roundtrip {old_ok}/{new_ok}/{cross_new_ok}/{cross_old_ok}"
        )
        return time.monotonic() - started, positive_checks, {"sql": sql_log}
    if not OLD_CLIENTS_RETIRED:
        faults.append("rollout: old consumers not retired; contract blocked")
        return time.monotonic() - started, positive_checks, {"sql": sql_log}
    with connect_db(database) as connection:
        connection.execute(
            "DROP TRIGGER IF EXISTS assay_sync_title_display ON documents"
        )
        connection.execute("ALTER TABLE documents DROP COLUMN title")
        connection.execute(
            "ALTER TABLE documents ALTER COLUMN display_name SET NOT NULL"
        )
        sql_log += [
            "DROP TRIGGER assay_sync_title_display",
            "ALTER TABLE documents DROP COLUMN title",
            "ALTER TABLE documents ALTER COLUMN display_name SET NOT NULL",
        ]
    post_new = node_call(database, "write", "roll-post")
    post_old = old_insert(database, "roll-fail")
    if (
        post_new.get("ok")
        and node_call(database, "read", post_new["id"]).get("value") == "roll-post"
    ):
        positive_checks += 1
        positive_evidence.append("rollout: new client ok after contract")
    else:
        faults.append(f"rollout post-contract ({summarize_error(post_new)})")
    if not post_old.get("ok") and post_old.get("sqlstate") == "42703":
        expected_counterexamples.append(
            "rollout: old write rejected after contract (42703)"
        )
    else:
        faults.append(f"rollout old-after-contract ({summarize_error(post_old)})")
    return time.monotonic() - started, positive_checks, {"sql": sql_log}


def check_lock_budget(database, positive_evidence, expected_counterexamples, faults):
    started = time.monotonic()
    positive_checks = 0
    with connect_db(database) as connection:
        connection.execute("DROP TABLE IF EXISTS documents")
        connection.execute(
            "CREATE TABLE documents (id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
            "title TEXT, display_name TEXT)"
        )
        connection.execute("INSERT INTO documents(title) VALUES ('assay-lock-seed')")
    reader = connect_db(database, autocommit=False)
    lock_info = {}
    try:
        reader.execute("SELECT * FROM documents LIMIT 1")
        migrator = connect_db(database)
        try:
            migrator.execute("SET lock_timeout = '50ms'")
            lock_started = time.monotonic()
            try:
                migrator.execute(
                    "ALTER TABLE documents ADD COLUMN assay_probe boolean DEFAULT false"
                )
                lock_info = {
                    "sqlstate": None,
                    "elapsed_ms": round((time.monotonic() - lock_started) * 1000, 1),
                }
                faults.append("lock: succeeded under read txn (want 55P03)")
            except psycopg.Error as error:
                lock_info = {
                    "sqlstate": error.sqlstate,
                    "elapsed_ms": round((time.monotonic() - lock_started) * 1000, 1),
                }
                if error.sqlstate == "55P03":
                    expected_counterexamples.append(
                        f"migration blocked (55P03 after {lock_info['elapsed_ms']}ms)"
                    )
                else:
                    faults.append(f"lock bad {error.sqlstate}")
        finally:
            migrator.close()
    finally:
        reader.commit()
        reader.close()
    try:
        with connect_db(database) as connection:
            connection.execute(
                "ALTER TABLE documents ADD COLUMN IF NOT EXISTS assay_probe boolean DEFAULT false"
            )
            column = connection.execute(
                "SELECT attname FROM pg_attribute"
                " WHERE attrelid = 'documents'::regclass AND attname = 'assay_probe'"
            ).fetchone()
        if column is not None:
            positive_checks += 1
            positive_evidence.append("migration ok after release")
        else:
            faults.append("lock: rerun missing column")
    except psycopg.Error as error:
        faults.append(f"lock rerun ({error.sqlstate})")
    print(f"[assay] lock {lock_info}", file=sys.stderr)
    return time.monotonic() - started, positive_checks, lock_info


def get_node_version():
    try:
        process, (stdout, _), timed_out = run_process(
            ["node", "--version"], child_env(), 10
        )
        if timed_out or process.returncode != 0:
            return "unknown"
        return (stdout or "").strip() or "unknown"
    except Exception:
        return "unknown"


def run_selftest():
    failures = []
    sections = schema_sections(SCHEMA_SQL)
    for name in REQUIRED_SECTIONS:
        if name not in sections:
            failures.append(f"missing {name}")
    if EXPECTED_MATRIX["expand"] != (True, False, False, False):
        failures.append("expand must be (T,F,F,F)")
    trigger_sql = sections.get("trigger:bridge_sync", "")
    for token in ("TG_OP", "IS DISTINCT FROM OLD"):
        if token not in trigger_sql:
            failures.append(f"trigger lacks {token}")
    if "assay_names_equal" not in sections.get("phase:bridge", ""):
        failures.append("bridge lacks CHECK")
    if not is_unknown_result({"ok": False, "unknown": True}):
        failures.append("is_unknown_result broken")
    if summarize_error({"ok": False, "message": "rowcount 0"}) != "rowcount 0":
        failures.append("summarize_error value broken")

    def check_negative_control(label, result):
        if not is_unknown_result(result):
            failures.append(f"{label} must stay unknown, got {result!r}")
        if result.get("ok"):
            failures.append(f"{label} false green: unknown counted as ok")

    with mock.patch(__name__ + ".DIST_JS", Path("/nonexistent-assay-dist.js")):
        check_negative_control(
            "missing client", node_call("postgres", "read", "some-id")
        )
    with mock.patch(
        __name__ + ".run_process",
        return_value=(mock.Mock(returncode=0), ("not json {{{", ""), False),
    ):
        with mock.patch(__name__ + ".DIST_JS", Path(__file__)):
            check_negative_control(
                "malformed JSON", node_call("postgres", "read", "some-id")
            )
    print(json.dumps({"selftest_fails": failures}, indent=2))
    return 1 if failures else 0


def build_result_document(
    elapsed_seconds,
    stages,
    versions,
    hashes,
    source_unchanged,
    gate,
    matrix_detail,
    witness,
    rollout,
    lock_info,
    positive_count,
    positive_evidence,
    expected_counterexamples,
    faults,
):
    return {
        "total_seconds": round(elapsed_seconds, 3),
        "stages": stages,
        "versions": versions,
        "hashes": hashes,
        "source_unchanged": source_unchanged,
        "gate": gate,
        "matrix_detail": matrix_detail,
        "witness": witness,
        "rollout": rollout,
        "lock": lock_info,
        "counts": {
            "positive_checks": positive_count,
            "expected_counterexamples": len(expected_counterexamples),
            "unexpected_faults": len(faults),
        },
        "positive_evidence": positive_evidence,
        "expected_counterexamples": expected_counterexamples,
        "unexpected_faults": faults,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=None)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return run_selftest()
    if PG_HOST not in ("127.0.0.1", "localhost"):
        print(json.dumps({"unexpected_faults": ["refused nonlocal host"]}))
        return 2
    if args.output:
        output_path = Path(args.output).resolve()
        input_paths = {str(Path(path).resolve()) for path in HASHED_INPUTS.values()}
        if str(output_path) in input_paths:
            print("refused to overwrite input", file=sys.stderr)
            return 1
    started = time.monotonic()
    positive_evidence = []
    expected_counterexamples = []
    faults = []
    versions = collect_versions(faults)
    stages = {}
    gate = {}
    matrix_detail = {}
    witness = {}
    rollout = {}
    lock_info = {}
    positive_count = 0

    def finish(hashes, source_unchanged):
        result = build_result_document(
            time.monotonic() - started,
            stages,
            versions,
            hashes,
            source_unchanged,
            gate,
            matrix_detail,
            witness,
            rollout,
            lock_info,
            positive_count,
            positive_evidence,
            expected_counterexamples,
            faults,
        )
        print(json.dumps(result, indent=2))
        if args.output:
            output_path = Path(args.output).resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(result, indent=2))
        return 1 if faults else 0

    if not build_client(stages, faults):
        return finish({}, None)
    hashes = snapshot_hashes()
    owned_name = "assay_" + secrets.token_hex(16)
    assert OWNER_PATTERN.fullmatch(owned_name)
    try:
        with connect_db(MAINTENANCE_DB) as connection:
            connection.execute(f'CREATE DATABASE "{owned_name}"')
    except psycopg.Error as error:
        faults.append(f"create db ({error.sqlstate})")
        return finish(hashes, None)
    try:
        with connect_db(owned_name) as connection:
            versions["postgres"] = connection.execute("SELECT version()").fetchone()[0]
        sections = schema_sections(SCHEMA_SQL)
        missing = [name for name in REQUIRED_SECTIONS if name not in sections]
        for name in missing:
            faults.append(f"missing section {name}")
        if not missing:
            duration, passed, gate, matrix_detail = check_compatibility_matrix(
                owned_name,
                sections,
                positive_evidence,
                expected_counterexamples,
                faults,
            )
            stages["matrix_s"] = round(duration, 3)
            positive_count += passed
            duration, passed, witness = check_backfill(
                owned_name,
                sections,
                positive_evidence,
                expected_counterexamples,
                faults,
            )
            stages["backfill_s"] = round(duration, 3)
            positive_count += passed
            duration, passed, rollout = check_rollout(
                owned_name,
                sections,
                positive_evidence,
                expected_counterexamples,
                faults,
            )
            stages["rollout_s"] = round(duration, 3)
            positive_count += passed
            duration, passed, lock_info = check_lock_budget(
                owned_name, positive_evidence, expected_counterexamples, faults
            )
            stages["lock_s"] = round(duration, 3)
            positive_count += passed
    except psycopg.Error as error:
        faults.append(
            f"harness db ({error.sqlstate}): {str(error).splitlines()[0][:200]}"
        )
    except Exception as error:
        faults.append(f"harness {type(error).__name__}: {str(error)[:200]}")
    finally:
        try:
            if not OWNER_PATTERN.fullmatch(owned_name):
                faults.append("refused drop: name mismatch")
            else:
                with connect_db(MAINTENANCE_DB) as connection:
                    connection.execute(
                        f'DROP DATABASE IF EXISTS "{owned_name}" WITH (FORCE)'
                    )
        except psycopg.Error as error:
            faults.append(f"drop ({error.sqlstate})")
        except Exception as error:
            faults.append(f"drop {type(error).__name__}")
    source_unchanged = snapshot_hashes() == hashes
    if not source_unchanged:
        faults.append("source drift detected during run")
    return finish(hashes, source_unchanged)


if __name__ == "__main__":
    sys.exit(main())

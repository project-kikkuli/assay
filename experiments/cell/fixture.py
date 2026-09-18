"""Original public migrations, isolated database, independent state observer."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import time
import uuid
from urllib.parse import quote

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row


REVISION = "cb740b656d7a0a6c5e12c7bf8e50343ec94ee9c7"


def database_url(dsn):
    p = conninfo_to_dict(dsn)
    return (f"postgresql://{quote(p.get('user', 'postgres'), safe='')}:"
            f"{quote(p.get('password', ''), safe='')}@{p['host']}:"
            f"{p.get('port', '5432')}/{p['dbname']}")


def app_env(subject, dsn):
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
        "PYTHONPATH": str(Path(subject) / "backend"),
        "PROJECT_NAME": "Assay public fixture", "FASTAPI_ENV": "development",
        "SECRET_KEY": "public-fixture-key-not-for-deployment",
        "FIRST_SUPERUSER": "admin@example.com",
        "FIRST_SUPERUSER_PASSWORD": "AssayFixturePassword123",
        "DATABASE_URL": database_url(dsn), "NO_COLOR": "1",
    }


@contextmanager
def public_fixture(subject, admin_dsn, evidence=None):
    """The only schema source is the pinned app's own Alembic migrations."""
    evidence = evidence if evidence is not None else {}
    subject = Path(subject).resolve()
    params = conninfo_to_dict(admin_dsn)
    if params.get("host") not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("only a disposable local PostgreSQL server is accepted")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=subject,
                                       text=True, timeout=5).strip()
    if revision != REVISION:
        raise ValueError("public subject revision differs from pinned baseline")
    name = "assay_cell_" + uuid.uuid4().hex
    started = time.perf_counter()
    with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=3) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        evidence.update(database=name, cleanup="pending", revision=revision)
        try:
            dsn = make_conninfo(admin_dsn, dbname=name, connect_timeout=3,
                               options="-c statement_timeout=5000 -c lock_timeout=2000")
            result = subprocess.run(
                [str(subject / ".venv/bin/python"), "-m", "alembic", "upgrade", "head"],
                cwd=subject / "backend", env=app_env(subject, dsn), capture_output=True,
                text=True, timeout=30,
            )
            evidence["migration_exit_code"] = result.returncode
            evidence["setup_seconds"] = time.perf_counter() - started
            if result.returncode:
                # Do not persist arbitrary application logs or host paths.
                raise RuntimeError("original migrations failed")
            yield dsn
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            remaining = admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
            evidence["cleanup"] = "removed" if remaining is None else "failed"


def seed_users(dsn):
    users = [str(uuid.uuid4()), str(uuid.uuid4())]
    with psycopg.connect(dsn) as db:
        for user in users:
            db.execute('INSERT INTO "user" (id,email,hashed_password,is_active,is_superuser,created_at) '
                       'VALUES (%s,%s,%s,true,false,now())',
                       (user, user + "@example.com", "not-a-login-hash"))
    return users


def observe(dsn, owner=None):
    with psycopg.connect(dsn, row_factory=dict_row) as db:
        query = "SELECT id,title,description,owner_id,created_at FROM item"
        rows = db.execute(query + (" WHERE owner_id=%s" if owner else "") + " ORDER BY id",
                          (owner,) if owner else ()).fetchall()
    return [{k: v.isoformat() if hasattr(v, "isoformat") else str(v) if isinstance(v, uuid.UUID) else v
             for k, v in row.items()} for row in rows]

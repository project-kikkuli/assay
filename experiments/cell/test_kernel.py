from __future__ import annotations

import os
import unittest
import uuid
from typing import Any

from experiments.cell.kernel import Kernel, KernelError

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - integration dependency is optional
    psycopg = None  # type: ignore[assignment]


def healthy_actor(command: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
    del view
    return {
        "op": command["op"],
        "target": command.get("item_id"),
        "fields": dict(command["fields"]),
    }


class KernelProtocolTests(unittest.TestCase):
    def test_all_proposals_use_target_and_item_id_command(self) -> None:
        kernel = Kernel("unused", healthy_actor)
        command = {
            "op": "update",
            "item_id": str(uuid.uuid4()),
            "fields": {"title": "requested"},
        }
        proposal = kernel._proposal(command, fields=command["fields"])
        self.assertEqual(proposal["target"], command["item_id"])

    def test_valid_semantic_mutation_reaches_proposal_validation(self) -> None:
        def uppercase(command: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
            del view
            fields = dict(command["fields"])
            fields["title"] = fields["title"].upper()
            return {"op": command["op"], "target": command.get("item_id"), "fields": fields}

        kernel = Kernel("unused", uppercase)
        proposal = kernel._proposal(
            {"op": "create", "item_id": None, "fields": {"title": "x", "description": None}},
            fields={"title": "x", "description": None},
        )
        self.assertEqual(proposal["fields"]["title"], "X")

    def test_target_owner_and_invalid_fields_are_rejected(self) -> None:
        item_id = str(uuid.uuid4())

        def forged(command: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
            del view
            return {
                "op": command["op"],
                "target": str(uuid.uuid4()),
                "fields": {"owner_id": str(uuid.uuid4())},
            }

        kernel = Kernel("unused", forged)
        with self.assertRaises(KernelError) as target_error:
            kernel._proposal(
                {"op": "delete", "item_id": item_id, "fields": {}}, fields={}
            )
        self.assertEqual(target_error.exception.status, 403)

        def null_title(command: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
            del view
            return {
                "op": command["op"],
                "target": command.get("item_id"),
                "fields": {"title": None, "description": None},
            }

        with self.assertRaises(KernelError):
            Kernel("unused", null_title)._proposal(
                {"op": "create", "item_id": None, "fields": {"title": "x", "description": None}},
                fields={"title": "x", "description": None},
            )


@unittest.skipUnless(
    os.environ.get("ASSAY_CELL_ADMIN_DSN"),
    "set ASSAY_CELL_ADMIN_DSN to run disposable PostgreSQL integration tests",
)
class PostgreSQLKernelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        explicit_dsn = bool(os.environ.get("ASSAY_CELL_ADMIN_DSN"))
        if psycopg is None:
            if explicit_dsn:
                raise RuntimeError("ASSAY_CELL_ADMIN_DSN was set but psycopg is unavailable")
            raise unittest.SkipTest("psycopg is not installed")
        cls.admin_dsn = os.environ["ASSAY_CELL_ADMIN_DSN"]
        params = psycopg.conninfo.conninfo_to_dict(cls.admin_dsn)
        hosts = [host for host in params.get("host", "").split(",") if host]
        if not hosts or any(host not in {"localhost", "127.0.0.1", "::1"} for host in hosts):
            raise RuntimeError("ASSAY_CELL_ADMIN_DSN must use a loopback TCP host")
        params["dbname"] = "postgres"
        cls.root_dsn = psycopg.conninfo.make_conninfo(**params)
        cls.db_name = f"assay_cell_{uuid.uuid4().hex[:12]}"
        cls.db_created = False
        try:
            with psycopg.connect(cls.root_dsn, autocommit=True) as connection:
                connection.execute(f'CREATE DATABASE "{cls.db_name}"')
            cls.db_created = True
            fixture = dict(params)
            fixture["dbname"] = cls.db_name
            cls.fixture_dsn = psycopg.conninfo.make_conninfo(**fixture)
            with psycopg.connect(cls.fixture_dsn, autocommit=True) as connection:
                connection.execute(
                    """
                    CREATE TABLE public."user" (
                        id uuid PRIMARY KEY, email text UNIQUE NOT NULL,
                        is_active boolean NOT NULL, is_superuser boolean NOT NULL,
                        full_name text, hashed_password text NOT NULL,
                        created_at timestamptz
                    );
                    CREATE TABLE public.item (
                        id uuid PRIMARY KEY, title varchar(255) NOT NULL,
                        description varchar(255), created_at timestamptz,
                        owner_id uuid NOT NULL REFERENCES public."user"(id)
                    )
                    """
                )
                cls.user_a = uuid.uuid4()
                cls.user_b = uuid.uuid4()
                for user_id, email in ((cls.user_a, "a@example.test"), (cls.user_b, "b@example.test")):
                    connection.execute(
                        'INSERT INTO public."user" '
                        "(id,email,is_active,is_superuser,full_name,hashed_password,created_at) "
                        "VALUES (%s,%s,true,false,%s,%s,current_timestamp)",
                        (user_id, email, email, "fixture"),
                    )
            cls.kernel = Kernel(cls.fixture_dsn, healthy_actor)
            cls.kernel.setup_rls([cls.user_a, cls.user_b])
        except Exception as exc:
            if hasattr(cls, "kernel"):
                try:
                    cls.kernel.cleanup()
                finally:
                    cls._drop_database()
            else:
                cls._drop_database()
            if explicit_dsn:
                raise
            raise unittest.SkipTest(f"fixture unavailable: {type(exc).__name__}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            if hasattr(cls, "kernel"):
                cls.kernel.cleanup()
        finally:
            cls._drop_database()

    @classmethod
    def _drop_database(cls) -> None:
        if not getattr(cls, "db_created", False):
            return
        with psycopg.connect(cls.root_dsn, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (cls.db_name,),
            )
            connection.execute(f'DROP DATABASE "{cls.db_name}"')
        cls.db_created = False

    def test_crud_shape_paging_and_cleanup_contract(self) -> None:
        created = self.kernel.create(self.user_a, "one", "first")
        self.assertEqual(set(created), {"id", "title", "description", "owner_id", "created_at"})
        self.assertEqual(created["owner_id"], str(self.user_a))
        self.assertRegex(created["id"], r"^[0-9a-f-]{36}$")
        self.assertIsInstance(created["created_at"], str)
        self.assertEqual(self.kernel.list(self.user_a, 0, 10)["count"], 1)
        changed = self.kernel.update(self.user_a, created["id"], {"description": "second"})
        self.assertEqual(changed["title"], "one")
        self.assertEqual(changed["description"], "second")
        self.assertEqual(self.kernel.read(self.user_a, created["id"]), changed)
        self.assertEqual(self.kernel.delete(self.user_a, created["id"]), {"message": "Item deleted successfully"})

    def test_owner_denial_and_real_login_rls(self) -> None:
        created = self.kernel.create(self.user_a, "private", None)
        with self.assertRaises(KernelError) as read_error:
            self.kernel.read(self.user_b, created["id"])
        self.assertEqual(read_error.exception.status, 403)
        with self.assertRaises(KernelError) as update_error:
            self.kernel.update(self.user_b, created["id"], {"title": "stolen"})
        self.assertEqual(update_error.exception.status, 403)
        self.assertEqual(self.kernel.list(self.user_b, 0, 100), {"data": [], "count": 0})

        role_name, password = self.kernel._roles[self.user_b]
        role_dsn = psycopg.conninfo.make_conninfo(
            self.fixture_dsn, user=role_name, password=password
        )
        with psycopg.connect(role_dsn, autocommit=True) as connection:
            facts = connection.execute(
                """
                SELECT r.rolcanlogin, r.rolsuper, r.rolbypassrls, r.rolinherit,
                       NOT EXISTS (
                           SELECT 1 FROM pg_auth_members AS m WHERE m.member = r.oid
                       ),
                       NOT EXISTS (
                           SELECT 1 FROM pg_database AS d
                           WHERE d.datname = current_database() AND d.datdba = r.oid
                       ),
                       NOT EXISTS (
                           SELECT 1 FROM pg_class AS c
                           JOIN pg_namespace AS n ON n.oid = c.relnamespace
                           WHERE n.nspname = 'public' AND c.relname IN ('user', 'item')
                             AND c.relowner = r.oid
                       )
                FROM pg_roles AS r WHERE r.rolname = current_user
                """
            ).fetchone()
            self.assertEqual(facts, (True, False, False, False, True, True, True))
            rows = connection.execute('SELECT owner_id FROM public.item').fetchall()
            self.assertEqual(rows, [])
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute('SELECT count(*) FROM public."user"')

    def test_missing_item_is_404_not_owner_denial(self) -> None:
        with self.assertRaises(KernelError) as error:
            self.kernel.read(self.user_a, uuid.uuid4())
        self.assertEqual(error.exception.status, 404)


if __name__ == "__main__":
    unittest.main()

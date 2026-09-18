from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parent


def load_run():
    previous_path = sys.path[:]
    previous_modules = {
        name: sys.modules.get(name) for name in ("checks", "performance")
    }
    sys.path.insert(0, str(ROOT))
    try:
        spec = importlib.util.spec_from_file_location(
            "assay_isolation_run_controls", ROOT / "run.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = previous_path
        for name, module in previous_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class ResourceLifecycleTests(unittest.TestCase):
    def test_connections_use_bounded_timeouts(self) -> None:
        run = load_run()
        with mock.patch.object(run.psycopg, "connect") as connect:
            run.connect("owned_db")

        self.assertEqual(
            connect.call_args.kwargs["connect_timeout"], run.CONNECT_TIMEOUT_SECONDS
        )
        self.assertEqual(
            connect.call_args.kwargs["options"],
            f"-c statement_timeout={run.STATEMENT_TIMEOUT_MS}",
        )

    def test_partial_role_creation_cleans_only_successfully_created_roles(self) -> None:
        run = load_run()
        connection = mock.Mock()
        cleaned: list[object] = []

        def execute(_connection, statement, _params=()):
            if statement.startswith(f"CREATE ROLE {run.APP}"):
                raise RuntimeError("injected role-create failure")

        run.connect = mock.Mock(return_value=connection)
        run.one = mock.Mock(return_value=False)
        run.rows = mock.Mock(return_value=[])
        run.exec_sql = mock.Mock(side_effect=execute)
        run.teardown = mock.Mock(
            side_effect=lambda resources: cleaned.append(resources)
        )

        with self.assertRaises(RuntimeError):
            run.create_fixture()

        self.assertEqual(cleaned[0].roles, [run.OWNER])
        self.assertIsNone(cleaned[0].database)
        run.teardown.assert_called_once()
        connection.close.assert_called_once()

    def test_database_collision_does_not_trigger_cleanup(self) -> None:
        run = load_run()
        run.connect = mock.Mock()
        run.one = mock.Mock(return_value=True)
        run.teardown = mock.Mock()

        with self.assertRaisesRegex(
            RuntimeError, "refusing to reuse existing database"
        ):
            run.create_fixture()

        run.teardown.assert_not_called()

    def test_teardown_failure_changes_passed_status_to_error(self) -> None:
        run = load_run()
        writes: list[tuple[Path, dict]] = []
        run.create_fixture = mock.Mock(
            return_value=run.OwnedResources("owned_db", ["owned_role"])
        )
        run.setup_schema = mock.Mock()
        run.catalog_snapshot = mock.Mock(return_value={})
        run.app_matrix = mock.Mock(return_value={})
        run.role_matrix = mock.Mock(return_value={})
        run.migration_case = mock.Mock(return_value={})
        run.performance_matrix = mock.Mock(return_value={})
        run.validate_expected = mock.Mock()
        run.teardown = mock.Mock(side_effect=RuntimeError("injected teardown failure"))
        run.write_json = mock.Mock(
            side_effect=lambda path, value: writes.append((path, value))
        )

        with self.assertRaisesRegex(RuntimeError, "injected teardown failure"):
            run.main()

        final = [value for path, value in writes if path == run.RESULTS][-1]
        self.assertEqual(final["status"], "error")
        self.assertIn("teardown_error", final)

    def test_database_cleanup_failure_still_attempts_owned_roles(self) -> None:
        run = load_run()
        audit = mock.Mock()

        def execute(_connection, statement, _params=()):
            if statement.startswith("DROP DATABASE"):
                raise RuntimeError("injected database-drop failure")

        run.connect = mock.Mock(return_value=audit)
        run.rows = mock.Mock(side_effect=[[], []])
        run.one = mock.Mock(return_value=True)
        run.exec_sql = mock.Mock(side_effect=execute)

        with self.assertRaisesRegex(RuntimeError, "cleanup incomplete"):
            run.teardown(run.OwnedResources("owned_db", ["role_a", "role_b"]))

        statements = [call.args[1] for call in run.exec_sql.call_args_list]
        self.assertEqual(
            statements,
            [
                "DROP DATABASE owned_db WITH (FORCE)",
                "DROP ROLE IF EXISTS role_b",
                "DROP ROLE IF EXISTS role_a",
            ],
        )

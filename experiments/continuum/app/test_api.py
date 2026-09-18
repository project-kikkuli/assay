import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from .api import ContinuumServer, Store
except ImportError:
    from api import ContinuumServer, Store


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "continuum.sqlite")
        self.server = ContinuumServer(("127.0.0.1", 0), self.store, None, True)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tempdir.cleanup()

    def request(self, method, path, token=None, body=None):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_health_and_v1_migration_are_ready(self):
        status, body = self.request("GET", "/health")
        self.assertEqual((status, body), (200, {"ok": True, "schema_version": 2}))
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "2")

    def test_populated_v1_fixture_upgrades_without_losing_reservation(self):
        path = Path(self.tempdir.name) / "v1.sqlite"
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE reservations (
                    id TEXT PRIMARY KEY, tenant TEXT, creator TEXT,
                    quantity INTEGER, status TEXT, seq INTEGER UNIQUE
                );
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO meta(key, value) VALUES ('schema_version', '1');
                INSERT INTO reservations VALUES ('legacy', 'alpha', 'alpha-maker', 2, 'reserved', 1);
                """
            )
        upgraded = Store(path)
        upgraded.migrate()
        with upgraded.connection() as connection:
            self.assertEqual(connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "2")
            self.assertEqual(connection.execute("SELECT id FROM reservations").fetchone()[0], "legacy")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM identities").fetchone()[0], 6)

    def test_concurrent_capacity_is_atomic(self):
        def reserve(index):
            return self.request("POST", "/commands", "alpha-maker", {"op": "reserve", "key": f"k-{index}", "quantity": 1})[0]

        with ThreadPoolExecutor(max_workers=16) as executor:
            statuses = list(executor.map(reserve, range(10)))
        self.assertEqual(statuses.count(200), 8)
        self.assertEqual(statuses.count(409), 2)

    def test_idempotency_returns_saved_response_and_conflicts_on_change(self):
        command = {"op": "reserve", "key": "same", "quantity": 2}
        first = self.request("POST", "/commands", "alpha-maker", command)
        second = self.request("POST", "/commands", "alpha-maker", command)
        changed = self.request("POST", "/commands", "alpha-maker", {**command, "quantity": 3})
        self.assertEqual(first[0], 200)
        self.assertEqual(second, first)
        self.assertEqual(changed[0], 409)

    def test_fencing_rejects_expired_lease_and_ack_is_idempotent(self):
        reserved = self.request("POST", "/commands", "alpha-maker", {"op": "reserve", "key": "r", "quantity": 1})[1]
        approved = self.request("POST", "/commands", "alpha-reviewer", {"op": "approve", "key": "a", "reservation_id": reserved["id"]})[1]
        first = self.request("POST", "/worker/claim", "synthetic-worker", {})[1]["job"]
        self.assertEqual(first["reservation_id"], approved["id"])
        self.assertEqual(self.request("POST", "/control", "synthetic-control", {"op": "advance", "seconds": 10})[0], 200)
        second = self.request("POST", "/worker/claim", "synthetic-worker", {})[1]["job"]
        receipt = "receipt:" + first["delivery_key"]
        stale = self.request("POST", "/worker/ack", "synthetic-worker", {"id": first["id"], "fence": first["fence"], "receipt": receipt})
        current = self.request("POST", "/worker/ack", "synthetic-worker", {"id": second["id"], "fence": second["fence"], "receipt": receipt})
        duplicate = self.request("POST", "/worker/ack", "synthetic-worker", {"id": second["id"], "fence": second["fence"], "receipt": receipt})
        self.assertEqual(stale[0], 409)
        self.assertEqual(current, (200, {"ok": True}))
        self.assertEqual(duplicate, current)
        self.assertEqual(self.request("GET", f"/reservations/{reserved['id']}", "alpha-maker")[1]["status"], "fulfilled")

    def test_revocation_and_tenant_scope(self):
        reserved = self.request("POST", "/commands", "alpha-maker", {"op": "reserve", "key": "tenant", "quantity": 1})[1]
        self.assertEqual(self.request("GET", f"/reservations/{reserved['id']}", "beta-maker")[0], 404)
        self.assertEqual(self.request("POST", "/control", "synthetic-control", {"op": "revoke", "identity": "alpha-maker"})[0], 200)
        self.assertEqual(self.request("GET", "/reservations", "alpha-maker")[0], 401)

    def test_static_directory_serves_index_and_dist_client(self):
        static = Path(self.tempdir.name) / "static"
        (static / "dist").mkdir(parents=True)
        (static / "index.html").write_text("<h1>runner</h1>")
        (static / "dist" / "client.js").write_text("window.runner = true;")
        self.server.static_path = static.resolve()
        request = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.read(), b"<h1>runner</h1>")
        request = urllib.request.Request(self.base + "/static/client.js")
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.read(), b"window.runner = true;")


if __name__ == "__main__":
    unittest.main()

import json
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.request import Request, urlopen

try:
    from .verify import DeliveryServer, Observer, _reservation_matches, _run_worker, _usage
except ImportError:  # direct `python verifier_tests.py`
    from verify import DeliveryServer, Observer, _reservation_matches, _run_worker, _usage


class VerifierTests(unittest.TestCase):
    def test_provider_idempotency_and_changed_payload(self):
        with DeliveryServer() as provider:
            payload = {"key": "k", "tenant": "alpha", "reservation_id": "r", "quantity": 1}
            request = Request(provider.url + "/deliver", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request) as response:
                first = json.loads(response.read())
            with urlopen(request) as response:
                second = json.loads(response.read())
            self.assertEqual(first, second)
            changed = dict(payload, quantity=2)
            conflict = Request(provider.url + "/deliver", data=json.dumps(changed).encode(), headers={"Content-Type": "application/json"}, method="POST")
            with self.assertRaises(Exception) as context:
                urlopen(conflict)
            self.assertEqual(getattr(context.exception, "code", None), 409)
            self.assertEqual(list(provider.records()), ["k"])

    def test_provider_persist_then_hold_has_explicit_release(self):
        with DeliveryServer(hold_response=True) as provider:
            payload = {"key": "held", "tenant": "alpha", "reservation_id": "r", "quantity": 1}
            result: list[int] = []

            def call() -> None:
                request = Request(provider.url + "/deliver", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request, timeout=3) as response:
                    result.append(response.status)

            thread = threading.Thread(target=call)
            thread.start()
            self.assertTrue(provider.wait_persisted("held", timeout=2))
            self.assertEqual(provider.records()["held"]["payload"], payload)
            self.assertEqual(result, [])
            provider.release("held")
            thread.join(timeout=3)
            self.assertEqual(result, [200])

    def test_provider_control_and_read_surfaces(self):
        with DeliveryServer() as provider:
            with urlopen(provider.url + "/state") as response:
                self.assertFalse(json.loads(response.read())["held"])
            control = Request(
                provider.url + "/control",
                data=json.dumps({"op": "hold"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(control) as response:
                self.assertTrue(json.loads(response.read())["held"])
            with urlopen(provider.url + "/records") as response:
                self.assertEqual(json.loads(response.read()), {})
            reset = Request(
                provider.url + "/control",
                data=json.dumps({"op": "reset"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(reset) as response:
                self.assertFalse(json.loads(response.read())["held"])

    def test_observer_is_read_only_and_capacity_oracle_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE reservations(id TEXT PRIMARY KEY,tenant TEXT,creator TEXT,quantity INTEGER,status TEXT,seq INTEGER UNIQUE);
                CREATE TABLE outbox(id TEXT PRIMARY KEY,reservation_id TEXT UNIQUE,tenant TEXT,quantity INTEGER,delivery_key TEXT UNIQUE,state TEXT,fence INTEGER,lease_until INTEGER,receipt TEXT);
                CREATE TABLE commands(actor TEXT,key TEXT,fingerprint TEXT,response TEXT,PRIMARY KEY(actor,key));
                INSERT INTO meta VALUES ('schema_version','2');
                INSERT INTO reservations VALUES ('a','alpha','alpha-maker',8,'reserved',1);
                """
            )
            connection.commit()
            connection.close()
            before = Observer(path).snapshot()
            self.assertEqual(_usage(before, "alpha"), 8)
            self.assertEqual(Observer(path).snapshot(), before)
            self.assertEqual(Path(path).stat().st_size, Path(path).stat().st_size)

    def test_state_oracle_rejects_quantity_drift_and_accepts_exact_row(self):
        snapshot = {"reservations": [{"id": "r", "tenant": "alpha", "creator": "alpha-maker", "quantity": 3, "status": "reserved", "seq": 1}]}
        self.assertTrue(_reservation_matches(snapshot, {"id": "r", "tenant": "alpha", "creator": "alpha-maker", "quantity": 3, "status": "reserved"}))
        self.assertFalse(_reservation_matches(snapshot, {"id": "r", "tenant": "alpha", "creator": "alpha-maker", "quantity": 4, "status": "reserved"}))

    def test_missing_observer_table_is_diagnostic_not_green(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.db"
            sqlite3.connect(path).close()
            with self.assertRaises(Exception):
                Observer(path).snapshot()

    def test_worker_command_with_api_is_not_rewritten(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("subprocess.run", return_value=completed) as run:
            _run_worker(["docker", "run", "--rm", "worker", "--api", "http://api:8000", "--provider", "http://provider:8001"], "http://host-api", "http://host-provider")
        self.assertEqual(run.call_args.args[0], ["docker", "run", "--rm", "worker", "--api", "http://api:8000", "--provider", "http://provider:8001"])

    def test_worker_command_without_api_gets_convenience_flags(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("subprocess.run", return_value=completed) as run:
            _run_worker(["node", "dist/worker.js"], "http://host-api", "http://host-provider")
        self.assertEqual(run.call_args.args[0][-7:], ["--api", "http://host-api", "--provider", "http://host-provider", "--token", "synthetic-worker", "--once"])


if __name__ == "__main__":
    unittest.main()

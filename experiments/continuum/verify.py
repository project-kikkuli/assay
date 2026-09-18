#!/usr/bin/env python3
"""Independent HTTP/state verifier for the public continuum contract.

This module deliberately knows only the wire contract and the documented
SQLite tables. It does not import the application or worker implementation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import uuid


CAPACITY = 8
IDENTITIES = {
    "alpha-maker": ("alpha", "maker"),
    "alpha-reviewer": ("alpha", "reviewer"),
    "beta-maker": ("beta", "maker"),
    "beta-reviewer": ("beta", "reviewer"),
}
WORKER_TOKEN = "synthetic-worker"
CONTROL_TOKEN = "synthetic-control"
REQUIRED_OBLIGATION_IDS = frozenset(
    {
        "health-schema",
        "unknown-auth",
        "invalid-input",
        "maker-reserve",
        "quantity-contract",
        "tenant-scope",
        "idempotency",
        "cancel-release-no-job",
        "cancel-idempotency",
        "maker-reviewer",
        "disallowed-transitions",
        "exactly-one-outbox",
        "pagination",
        "concurrent-capacity",
        "stale-lease-fence",
        "eventual-fulfillment",
        "revoked-auth",
    }
)


class HarnessFailure(RuntimeError):
    """The verifier could not observe a required contract condition."""


class TransportFailure(HarnessFailure):
    pass


def _safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _diagnostic(value: Any, limit: int = 2000) -> Any:
    """Bound replay data and redact local filesystem paths."""
    if isinstance(value, dict):
        return {str(key): _diagnostic(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_diagnostic(item, limit) for item in value[:50]]
    if isinstance(value, str):
        redacted = re.sub(
            r"/(?:Users|private/tmp|tmp|var/folders|home)/[^\s,\"']+",
            "<redacted-path>",
            value,
        )
        return redacted[:limit]
    return value


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Response:
    def __init__(self, status: int, body: Any, raw: bytes = b"", error: str | None = None):
        self.status = status
        self.body = body
        self.raw = raw
        self.error = error


class ApiClient:
    def __init__(self, api_url: str, events: list[dict[str, Any]] | None = None, timeout: float = 5.0):
        self.base = api_url.rstrip("/")
        self.events = events
        self.timeout = timeout

    def request(self, method: str, path: str, token: str | None = None, body: Any = None) -> Response:
        headers = {"Accept": "application/json"}
        data = None
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(512 * 1024)
                result = Response(response.status, self._decode(raw), raw)
                self._record(method, path, body, result)
                return result
        except HTTPError as error:
            raw = error.read(512 * 1024)
            result = Response(error.code, self._decode(raw), raw, type(error).__name__)
            self._record(method, path, body, result)
            return result
        except (OSError, URLError, TimeoutError) as error:
            if self.events is not None:
                self.events.append(
                    {
                        "kind": "http",
                        "method": method,
                        "path": _diagnostic(path),
                        "request": _diagnostic(body),
                        "status": None,
                        "response": None,
                        "error": type(error).__name__,
                    }
                )
            raise TransportFailure(f"{method} {path}: {type(error).__name__}") from error

    def _record(self, method: str, path: str, request_body: Any, response: Response) -> None:
        if self.events is not None:
            self.events.append(
                {
                    "kind": "http",
                    "method": method,
                    "path": _diagnostic(path),
                    "request": _diagnostic(request_body),
                    "status": response.status,
                    "response": _diagnostic(response.body),
                    "error": response.error,
                }
            )

    @staticmethod
    def _decode(raw: bytes) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None


class Observer:
    """Read-only SQLite observer. It never creates tables or changes state."""

    def __init__(self, db_path: str | os.PathLike[str]):
        self.path = Path(db_path).resolve()

    def snapshot(self) -> dict[str, Any]:
        if not self.path.is_file():
            raise HarnessFailure("SQLite database path does not exist")
        uri = f"file:{quote(str(self.path), safe='/')}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=2.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            try:
                return {
                    "schema_version": self._meta(connection, "schema_version"),
                    "reservations": self._rows(
                        connection,
                        "SELECT id,tenant,creator,quantity,status,seq FROM reservations ORDER BY seq",
                    ),
                    "outbox": self._rows(
                        connection,
                        "SELECT id,reservation_id,tenant,quantity,delivery_key,state,fence,lease_until,receipt "
                        "FROM outbox ORDER BY id",
                    ),
                    "commands": self._rows(
                        connection,
                        "SELECT actor,key,fingerprint,response FROM commands ORDER BY actor,key",
                    ),
                }
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise HarnessFailure(f"read-only SQLite observation failed: {type(error).__name__}") from error

    @staticmethod
    def _rows(connection: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
        return [_safe_json(dict(row)) for row in connection.execute(query)]

    @staticmethod
    def _meta(connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])


class _DeliveryHandler(BaseHTTPRequestHandler):
    server: "DeliveryServer"

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/control":
            self._control()
            return
        if self.path != "/deliver":
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 64 * 1024:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": "invalid_body"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode())
            key = payload["key"]
            if not isinstance(key, str) or not key:
                raise ValueError("key")
        except (ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
            self._reply(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
            return
        status, response = self.server.record_delivery(key, payload)
        self._reply(status, response)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/records":
            self._reply(HTTPStatus.OK, self.server.records())
        elif self.path == "/state":
            self._reply(HTTPStatus.OK, self.server.state())
        else:
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _control(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode())
            operation = payload["op"]
            if operation == "hold":
                self.server.set_hold(True)
            elif operation == "release":
                self.server.release(payload.get("key"))
            elif operation == "reset":
                self.server.reset()
            else:
                raise ValueError("unknown operation")
        except (ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
            self._reply(HTTPStatus.BAD_REQUEST, {"error": "invalid_control"})
            return
        self._reply(HTTPStatus.OK, self.server.state())

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args: Any) -> None:
        return


class DeliveryServer:
    """Local provider fixture with durable-in-process idempotency records.

    ``hold_response=True`` persists a delivery, signals ``wait_persisted``,
    and then blocks until ``release``. A controller can kill a worker during
    that interval without relying on a timing sleep.
    """

    def __init__(self, hold_response: bool = False, host: str = "127.0.0.1", port: int = 0):
        self.hold_response = hold_response
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}
        self._persisted: dict[str, threading.Event] = {}
        self._released: dict[str, threading.Event] = {}
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "DeliveryServer":
        self.httpd = ThreadingHTTPServer((self.host, self.port), _DeliveryHandler)
        self.httpd.daemon_threads = True
        self.httpd.record_delivery = self.record_delivery  # type: ignore[attr-defined]
        self.httpd.records = self.records  # type: ignore[attr-defined]
        self.httpd.state = self.state  # type: ignore[attr-defined]
        self.httpd.set_hold = self.set_hold  # type: ignore[attr-defined]
        self.httpd.release = self.release  # type: ignore[attr-defined]
        self.httpd.reset = self.reset  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="continuum-provider", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)

    @property
    def url(self) -> str:
        if self.httpd is None:
            raise HarnessFailure("provider is not running")
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def record_delivery(self, key: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self._lock:
            existing = self._records.get(key)
            if existing is not None:
                if existing["payload"] != payload:
                    return HTTPStatus.CONFLICT, {"error": "delivery_key_payload_conflict"}
                receipt = existing["receipt"]
                persisted = self._persisted[key]
                released = self._released[key]
            else:
                receipt = f"receipt:{key}"
                self._records[key] = {"payload": dict(payload), "receipt": receipt}
                persisted = self._persisted.setdefault(key, threading.Event())
                released = self._released.setdefault(key, threading.Event())
                persisted.set()
        if self.hold_response:
            if not released.wait(timeout=15):
                return HTTPStatus.GATEWAY_TIMEOUT, {"error": "response_hold_timeout"}
        return HTTPStatus.OK, {"receipt": receipt}

    def wait_persisted(self, key: str, timeout: float = 5.0) -> bool:
        with self._lock:
            event = self._persisted.setdefault(key, threading.Event())
        return event.wait(timeout=timeout)

    def set_hold(self, held: bool) -> None:
        with self._lock:
            self.hold_response = held

    def release(self, key: str | None = None) -> None:
        with self._lock:
            keys = [key] if key is not None else list(self._released)
            for item in keys:
                self._released.setdefault(item, threading.Event()).set()

    def reset(self) -> None:
        self.release()
        with self._lock:
            self._records.clear()
            self._persisted.clear()
            self._released.clear()
            self.hold_response = False

    def records(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {key: dict(value) for key, value in self._records.items()}

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {"held": self.hold_response, "persisted_keys": sorted(self._records)}


def _obligation(obligations: list[dict[str, Any]], identifier: str, passed: bool, detail: str, category: str = "app") -> None:
    obligations.append({"id": identifier, "passed": bool(passed), "detail": detail, "category": category})


def _reservation_rows(snapshot: dict[str, Any], tenant: str | None = None) -> list[dict[str, Any]]:
    rows = snapshot["reservations"]
    return rows if tenant is None else [row for row in rows if row["tenant"] == tenant]


def _usage(snapshot: dict[str, Any], tenant: str) -> int:
    return sum(
        int(row["quantity"])
        for row in _reservation_rows(snapshot, tenant)
        if row["status"] in {"reserved", "approved", "fulfilled"}
    )


def _business_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {"reservations": snapshot["reservations"], "outbox": snapshot["outbox"]}


def _reservation_matches(snapshot: dict[str, Any], response: Any) -> bool:
    if not isinstance(response, dict) or "id" not in response:
        return False
    row = next((item for item in snapshot["reservations"] if item["id"] == response["id"]), None)
    if row is None:
        return False
    return all(response.get(field) == row[field] for field in ("id", "tenant", "creator", "quantity", "status"))


def _response_reservation(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or not {"id", "tenant", "creator", "quantity", "status"}.issubset(body):
        raise HarnessFailure("mutation response omitted reservation fields")
    return body


def _command(client: ApiClient, token: str, operation: str, key: str, quantity: int = 1, reservation_id: str | None = None) -> Response:
    body: dict[str, Any] = {"op": operation, "key": key}
    if operation == "reserve":
        body["quantity"] = quantity
    if reservation_id is not None:
        body["reservation_id"] = reservation_id
    return client.request("POST", "/commands", token, body)


def _control(client: ApiClient, operation: str, **values: Any) -> Response:
    body = {"op": operation, **values}
    return client.request("POST", "/control", CONTROL_TOKEN, body)


def _pagination_check(client: ApiClient, observer: Observer, token: str, tenant: str) -> tuple[bool, str]:
    snapshot = observer.snapshot()
    expected = [row["id"] for row in _reservation_rows(snapshot, tenant)]
    sequences = {row["id"]: int(row["seq"]) for row in _reservation_rows(snapshot, tenant)}
    cursor: str | None = None
    seen: list[str] = []
    for _ in range(20):
        path = "/reservations?limit=2" + (f"&cursor={cursor}" if cursor is not None else "")
        response = client.request("GET", path, token)
        if response.status != 200 or not isinstance(response.body, dict):
            return False, f"page request returned {response.status}"
        items = response.body.get("items")
        if not isinstance(items, list):
            return False, "page omitted items"
        page_ids = [item.get("id") for item in items if isinstance(item, dict)]
        if len(page_ids) != len(items) or any(item not in sequences for item in page_ids):
            return False, "page contained an unknown tenant reservation"
        seen.extend(page_ids)
        cursor_next = response.body.get("next_cursor")
        if cursor_next is None:
            break
        if not isinstance(cursor_next, str) or not cursor_next.isdigit():
            return False, "cursor was not an integer sequence"
        if cursor is not None and int(cursor_next) <= int(cursor):
            return False, "cursor did not advance"
        cursor = cursor_next
    else:
        return False, "pagination exceeded bounded page count"
    ordered = all(sequences[left] < sequences[right] for left, right in zip(seen, seen[1:]))
    return seen == expected and ordered, f"{len(seen)} rows, keyset order={ordered}"


def _run_worker(command: list[str], api_url: str, provider_url: str) -> dict[str, Any]:
    if not command or any(not isinstance(part, str) for part in command):
        raise HarnessFailure("worker_command must be a nonempty list of strings")
    argv = list(command)
    if "--api" not in argv:
        argv.extend(["--api", api_url, "--provider", provider_url, "--token", WORKER_TOKEN, "--once"])
    started = time.monotonic()
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HarnessFailure(f"worker invocation failed: {type(error).__name__}") from error
    return {
        "exit_code": result.returncode,
        "seconds": round(time.monotonic() - started, 3),
        "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
    }


def verify(api_url: str, db_path: str, worker_command: list[str] | None = None, provider_url: str | None = None) -> dict[str, Any]:
    """Run bounded contract scenarios against an already-started public API."""
    obligations: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    harness_errors: list[str] = []
    client = ApiClient(api_url, events=events)
    observer = Observer(db_path)
    run_id = uuid.uuid4().hex[:12]

    def event(kind: str, **data: Any) -> None:
        events.append({"kind": kind, **_diagnostic(_safe_json(data))})

    try:
        before = observer.snapshot()
        event("initial_observation", schema_version=before.get("schema_version"),
              reservations=len(before["reservations"]), outbox=len(before["outbox"]))
        health = client.request("GET", "/health")
        _obligation(obligations, "health-schema", health.status == 200 and health.body == {"ok": True, "schema_version": 2}, f"status={health.status}")

        unknown = client.request("GET", "/reservations", "unknown-token")
        _obligation(obligations, "unknown-auth", unknown.status == 401, f"status={unknown.status}")

        invalid_before = observer.snapshot()
        invalid = _command(client, "alpha-maker", "reserve", f"invalid-{run_id}", 0)
        invalid_after = observer.snapshot()
        _obligation(obligations, "invalid-input", invalid.status == 422 and invalid_before == invalid_after, f"status={invalid.status}, state_unchanged={invalid_before == invalid_after}")

        alpha_key = f"alpha-{run_id}"
        beta_key = f"beta-{run_id}"
        alpha = _command(client, "alpha-maker", "reserve", alpha_key, 3)
        beta = _command(client, "beta-maker", "reserve", beta_key, 2)
        alpha_row = _response_reservation(alpha.body) if alpha.status == 200 else None
        beta_row = _response_reservation(beta.body) if beta.status == 200 else None
        _obligation(obligations, "maker-reserve", alpha.status == 200 and beta.status == 200, f"alpha={alpha.status}, beta={beta.status}")
        persisted_reservations = observer.snapshot()
        quantity_ok = (
            alpha.status == 200
            and beta.status == 200
            and alpha_row is not None
            and beta_row is not None
            and alpha_row["quantity"] == 3
            and beta_row["quantity"] == 2
            and _reservation_matches(persisted_reservations, alpha_row)
            and _reservation_matches(persisted_reservations, beta_row)
        )
        _obligation(obligations, "quantity-contract", quantity_ok, f"requested=3/2, persisted={quantity_ok}")

        if alpha_row and beta_row:
            alpha_list = client.request("GET", "/reservations", "alpha-maker")
            foreign = client.request("GET", f"/reservations/{alpha_row['id']}", "beta-maker")
            listed_ids = {item.get("id") for item in alpha_list.body.get("items", [])} if isinstance(alpha_list.body, dict) else set()
            scoped = alpha_list.status == 200 and alpha_row["id"] in listed_ids and beta_row["id"] not in listed_ids
            _obligation(obligations, "tenant-scope", scoped and foreign.status == 404, f"list={alpha_list.status}, foreign_get={foreign.status}")
        else:
            _obligation(obligations, "tenant-scope", False, "maker setup failed", "harness")

        idem_key = f"idem-{run_id}"
        idem_first = _command(client, "beta-maker", "reserve", idem_key, 1)
        idem_same = _command(client, "beta-maker", "reserve", idem_key, 1)
        idem_changed = _command(client, "beta-maker", "reserve", idem_key, 2)
        _obligation(obligations, "idempotency", idem_first.status == 200 and idem_same.status == 200 and idem_same.body == idem_first.body and idem_changed.status == 409, f"statuses={idem_first.status}/{idem_same.status}/{idem_changed.status}")

        cancel_reserve = _command(client, "beta-maker", "reserve", f"cancel-reserve-{run_id}", 2)
        cancel_row = _response_reservation(cancel_reserve.body) if cancel_reserve.status == 200 else None
        cancel_before = observer.snapshot()
        cancel_key = f"cancel-{run_id}"
        cancel_response = (
            _command(client, "beta-maker", "cancel", cancel_key, reservation_id=cancel_row["id"])
            if cancel_row else Response(0, None)
        )
        cancel_after = observer.snapshot()
        cancel_persisted = (
            cancel_row is not None
            and cancel_response.status == 200
            and isinstance(cancel_response.body, dict)
            and cancel_response.body.get("status") == "cancelled"
            and _reservation_matches(cancel_after, cancel_response.body)
            and _usage(cancel_after, "beta") == _usage(cancel_before, "beta") - 2
            and not [job for job in cancel_after["outbox"] if job["reservation_id"] == cancel_row["id"]]
        )
        _obligation(obligations, "cancel-release-no-job", cancel_persisted, f"status={cancel_response.status}")
        cancel_repeat = (
            _command(client, "beta-maker", "cancel", cancel_key, reservation_id=cancel_row["id"])
            if cancel_row else Response(0, None)
        )
        cancel_changed = (
            _command(client, "beta-maker", "cancel", cancel_key, reservation_id=alpha_row["id"])
            if cancel_row and alpha_row else Response(0, None)
        )
        cancel_again = (
            _command(client, "beta-maker", "cancel", f"cancel-again-{run_id}", reservation_id=cancel_row["id"])
            if cancel_row else Response(0, None)
        )
        _obligation(obligations, "cancel-idempotency", cancel_response.status == 200 and cancel_repeat.status == 200 and cancel_repeat.body == cancel_response.body and cancel_changed.status == 409 and cancel_again.status == 409, f"statuses={cancel_response.status}/{cancel_repeat.status}/{cancel_changed.status}/{cancel_again.status}")

        if alpha_row:
            wrong_role = _command(client, "alpha-maker", "approve", f"approve-wrong-{run_id}", reservation_id=alpha_row["id"])
            approved = _command(client, "alpha-reviewer", "approve", f"approve-{run_id}", reservation_id=alpha_row["id"])
            cancel_approved = _command(client, "alpha-maker", "cancel", f"cancel-approved-{run_id}", reservation_id=alpha_row["id"])
            snapshot = observer.snapshot()
            jobs = [row for row in snapshot["outbox"] if row["reservation_id"] == alpha_row["id"]]
            _obligation(obligations, "maker-reviewer", wrong_role.status == 403 and approved.status == 200 and cancel_approved.status == 409, f"wrong={wrong_role.status}, approve={approved.status}, cancel={cancel_approved.status}")
            _obligation(obligations, "exactly-one-outbox", len(jobs) == 1 and jobs[0]["state"] in {"pending", "leased"}, f"jobs={len(jobs)}")
            disallowed_before = _business_state(snapshot)
            cancelled_approve = (
                _command(client, "beta-reviewer", "approve", f"approve-cancelled-{run_id}", reservation_id=cancel_row["id"])
                if cancel_row else Response(0, None)
            )
            reviewer_cancel = (
                _command(client, "beta-reviewer", "cancel", f"reviewer-cancel-{run_id}", reservation_id=cancel_row["id"])
                if cancel_row else Response(0, None)
            )
            disallowed_after = _business_state(observer.snapshot())
            _obligation(obligations, "disallowed-transitions", cancelled_approve.status == 409 and reviewer_cancel.status == 403 and disallowed_before == disallowed_after, f"cancelled_approve={cancelled_approve.status}, reviewer_cancel={reviewer_cancel.status}, state_unchanged={disallowed_before == disallowed_after}")
        else:
            _obligation(obligations, "maker-reviewer", False, "alpha reservation unavailable", "harness")
            _obligation(obligations, "exactly-one-outbox", False, "alpha reservation unavailable", "harness")
            _obligation(obligations, "disallowed-transitions", False, "alpha reservation unavailable", "harness")

        page_ok, page_detail = _pagination_check(client, observer, "beta-maker", "beta")
        _obligation(obligations, "pagination", page_ok, page_detail)

        # Derive the number of concurrent requests from committed state. This
        # tests oversubscription without making the verifier itself exceed 8.
        current = observer.snapshot()
        candidates = [(tenant, CAPACITY - _usage(current, tenant)) for tenant in ("alpha", "beta")]
        tenant, remaining = max(candidates, key=lambda item: item[1])
        maker = f"{tenant}-maker"
        if remaining < 1:
            _obligation(obligations, "concurrent-capacity", False, "both tenants have no observed remaining capacity", "harness")
        else:
            count = min(CAPACITY + 2, remaining + 2)
            barrier = threading.Barrier(count)
            responses: queue.Queue[Response] = queue.Queue()

            def reserve_concurrently(index: int) -> None:
                try:
                    barrier.wait(timeout=5)
                    responses.put(_command(client, maker, "reserve", f"concurrent-{run_id}-{index}"))
                except Exception as error:  # report as harness failure, never as an app pass
                    responses.put(Response(0, None, error=type(error).__name__))

            threads = [threading.Thread(target=reserve_concurrently, args=(index,)) for index in range(count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=8)
            results = [responses.get_nowait() for _ in range(responses.qsize())]
            after_concurrent = observer.snapshot()
            statuses = [response.status for response in results]
            capacity_ok = _usage(after_concurrent, tenant) <= CAPACITY
            conflicts = statuses.count(409)
            _obligation(obligations, "concurrent-capacity", len(results) == count and capacity_ok and conflicts >= 1 and all(status in {200, 409} for status in statuses), f"tenant={tenant}, remaining={remaining}, statuses={statuses}, usage={_usage(after_concurrent, tenant)}")

        # Fencing is exercised on the already-approved alpha job, avoiding a
        # second pending job that could steal the optional worker's claim.
        first_claim = client.request("POST", "/worker/claim", WORKER_TOKEN, {})
        fence_ok = False
        if first_claim.status == 200 and isinstance(first_claim.body, dict) and first_claim.body.get("job"):
            job = first_claim.body["job"]
            advanced = _control(client, "advance", seconds=11)
            second_claim = client.request("POST", "/worker/claim", WORKER_TOKEN, {})
            old_ack = client.request("POST", "/worker/ack", WORKER_TOKEN, {"id": job["id"], "fence": job["fence"], "receipt": f"receipt:{job['delivery_key']}"})
            current_job = second_claim.body.get("job") if isinstance(second_claim.body, dict) else None
            current_ack = client.request("POST", "/worker/ack", WORKER_TOKEN, {"id": current_job["id"], "fence": current_job["fence"], "receipt": f"receipt:{current_job['delivery_key']}"}) if second_claim.status == 200 and current_job else Response(0, None)
            duplicate_ack = client.request("POST", "/worker/ack", WORKER_TOKEN, {"id": current_job["id"], "fence": current_job["fence"], "receipt": f"receipt:{current_job['delivery_key']}"}) if current_job else Response(0, None)
            observed = observer.snapshot()
            row = next((item for item in observed["outbox"] if item["id"] == job["id"]), None)
            fence_ok = advanced.status == 200 and second_claim.status == 200 and old_ack.status == 409 and current_ack.status == 200 and duplicate_ack.status == 200 and row is not None and row["state"] == "done"
            event("fence", first=job["fence"], second=current_job.get("fence") if current_job else None, old_ack=old_ack.status, current_ack=current_ack.status)
        _obligation(obligations, "stale-lease-fence", fence_ok, f"first_claim={first_claim.status}")

        worker_ok = False
        worker_detail = "not exercised: worker_command not supplied"
        if worker_command is not None:
            worker_key = f"worker-{run_id}"
            worker_snapshot = observer.snapshot()
            worker_tenant, worker_remaining = max(
                ((tenant, CAPACITY - _usage(worker_snapshot, tenant)) for tenant in ("alpha", "beta")),
                key=lambda item: item[1],
            )
            worker_maker = f"{worker_tenant}-maker"
            worker_reviewer = f"{worker_tenant}-reviewer"
            worker_reserve = _command(client, worker_maker, "reserve", worker_key) if worker_remaining >= 1 else Response(409, None)
            worker_approve = _command(client, worker_reviewer, "approve", f"worker-approve-{run_id}", reservation_id=worker_reserve.body.get("id") if isinstance(worker_reserve.body, dict) else None)
            local_provider: DeliveryServer | None = None
            if provider_url is None:
                local_provider = DeliveryServer()
                local_provider.__enter__()
                worker_provider = local_provider.url
            else:
                worker_provider = provider_url
            try:
                worker_record = _run_worker(worker_command, api_url, worker_provider)
                observed = observer.snapshot()
                worker_row = next((row for row in observed["reservations"] if row["id"] == (worker_reserve.body or {}).get("id")), None)
                worker_ok = worker_reserve.status == 200 and worker_approve.status == 200 and worker_record["exit_code"] == 0 and worker_row is not None and worker_row["status"] == "fulfilled"
                worker_detail = f"tenant={worker_tenant}, exit={worker_record['exit_code']}, status={worker_row['status'] if worker_row else 'missing'}"
                event("worker", **worker_record)
            finally:
                if local_provider is not None:
                    local_provider.__exit__(None, None, None)
        if worker_command is not None:
            _obligation(obligations, "eventual-fulfillment", worker_ok, worker_detail, "worker")
        else:
            _obligation(obligations, "eventual-fulfillment", False, worker_detail, "harness")
            event("worker_skipped", detail=worker_detail)

        revoked = _control(client, "revoke", identity="alpha-maker")
        revoked_read = client.request("GET", "/reservations", "alpha-maker")
        _obligation(obligations, "revoked-auth", revoked.status == 200 and revoked_read.status == 401, f"revoke={revoked.status}, read={revoked_read.status}")
    except HarnessFailure as error:
        harness_errors.append(str(error))
        event("harness_failure", detail=str(error))
    except Exception as error:  # fail closed while retaining a bounded diagnostic
        harness_errors.append(f"{type(error).__name__}: verifier execution failed")
        event("harness_failure", detail=f"{type(error).__name__}: verifier execution failed")

    present_ids = [item["id"] for item in obligations]
    duplicate_ids = sorted({identifier for identifier in present_ids if present_ids.count(identifier) > 1})
    missing_ids = sorted(REQUIRED_OBLIGATION_IDS - set(present_ids))
    extra_ids = sorted(set(present_ids) - REQUIRED_OBLIGATION_IDS)
    if missing_ids or duplicate_ids or extra_ids:
        events.append(
            {
                "kind": "obligation_inventory_failure",
                "missing": missing_ids,
                "duplicates": duplicate_ids,
                "extra": extra_ids,
            }
        )
        harness_errors.append("obligation inventory did not match the required scenario set")
        for identifier in missing_ids:
            _obligation(obligations, identifier, False, "scenario did not produce a result", "harness")
    passed = not harness_errors and set(present_ids) == REQUIRED_OBLIGATION_IDS and not duplicate_ids and not extra_ids and len(obligations) == len(REQUIRED_OBLIGATION_IDS) and all(item["passed"] for item in obligations)
    return {"passed": passed, "obligations": obligations, "events": events, "harness_errors": harness_errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api")
    parser.add_argument("--db")
    parser.add_argument("--worker-command-json")
    parser.add_argument("--provider")
    parser.add_argument("--serve-provider", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if args.serve_provider:
        with DeliveryServer(host="0.0.0.0", port=args.port) as provider:
            print(json.dumps({"url": provider.url, "state": provider.state()}), flush=True)
            threading.Event().wait()
        return 0
    if not args.api or not args.db:
        parser.error("--api and --db are required unless --serve-provider is used")
    worker = json.loads(args.worker_command_json) if args.worker_command_json else None
    result = verify(args.api, args.db, worker_command=worker, provider_url=args.provider)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

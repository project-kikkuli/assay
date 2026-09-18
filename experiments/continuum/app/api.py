"""Synthetic reservation API backed by SQLite."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote, urlsplit


CAPACITY = 8
LEASE_SECONDS = 10
SCHEMA_VERSION = 2
DEFAULT_INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Continuum reservations</title></head>
<body>
<main>
<h1>Reservations</h1>
<label for="identity">Identity</label>
<input id="identity" name="identity" value="alpha-maker" autocomplete="off">
<label for="quantity">Quantity</label>
<input id="quantity" name="quantity" type="number" min="1" max="8" value="1">
<button id="reserve" type="button">Reserve</button>
<output id="status" role="status"></output>
<ul id="reservations" aria-label="Reservations"></ul>
</main>
<script src="/client.js"></script>
</body></html>"""

DEFAULT_CLIENT = r"""(() => {
  const identity = document.querySelector('#identity');
  const quantity = document.querySelector('#quantity');
  const status = document.querySelector('#status');
  const list = document.querySelector('#reservations');
  const headers = () => ({'Content-Type': 'application/json',
    Authorization: `Bearer ${identity.value}`});
  async function refresh() {
    const response = await fetch('/reservations', {headers: headers()});
    const body = await response.json();
    list.replaceChildren(...(body.items || []).map(item => {
      const row = document.createElement('li');
      row.textContent = `${item.id} — ${item.quantity} — ${item.status}`;
      return row;
    }));
  }
  document.querySelector('#reserve').addEventListener('click', async () => {
    const response = await fetch('/commands', {method: 'POST', headers: headers(),
      body: JSON.stringify({op: 'reserve', key: `ui-${Date.now()}`,
        quantity: Number(quantity.value)})});
    const body = await response.json();
    status.textContent = response.ok ? `Reserved ${body.id}` : (body.error || 'Request failed');
    if (response.ok) refresh();
  });
  refresh().catch(error => { status.textContent = error.message; });
})();"""


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _reservation(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "tenant": row["tenant"],
        "creator": row["creator"],
        "quantity": row["quantity"],
        "status": row["status"],
    }


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._migration_lock = threading.Lock()
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        with self._migration_lock, self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            has_meta = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            version = 0
            if has_meta:
                row = connection.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
                version = int(row["value"]) if row else 0
            migration_dir = Path(__file__).with_name("migrations")
            if version < 1:
                connection.executescript((migration_dir / "001_v1.sql").read_text())
                version = 1
            if version < 2:
                connection.executescript((migration_dir / "002_v2.sql").read_text())
                version = 2
            if version != SCHEMA_VERSION:
                raise RuntimeError(f"unsupported schema version: {version}")

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def identity(self, token: str) -> sqlite3.Row:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT token, tenant, role, active FROM identities WHERE token=?",
                (token,),
            ).fetchone()
        if row is None or not row["active"]:
            raise ApiError(401, "unauthorized")
        return row


class ReservationService:
    def __init__(self, store: Store):
        self.store = store

    def command(self, actor: sqlite3.Row, payload: dict[str, Any]) -> dict[str, Any]:
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        key = payload["key"]
        with self.store.write() as connection:
            prior = connection.execute(
                "SELECT fingerprint, response FROM commands WHERE actor=? AND key=?",
                (actor["token"], key),
            ).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise ApiError(409, "idempotency conflict")
                return json.loads(prior["response"])

            op = payload["op"]
            if op == "reserve":
                response = self._reserve(connection, actor, payload)
            elif op == "approve":
                response = self._approve(connection, actor, payload)
            elif op == "cancel":
                response = self._cancel(connection, actor, payload)
            else:
                raise ApiError(422, "unknown operation")
            connection.execute(
                "INSERT INTO commands(actor, key, fingerprint, response) VALUES (?, ?, ?, ?)",
                (actor["token"], key, fingerprint, json.dumps(response, separators=(",", ":"))),
            )
            return response

    def _reserve(
        self, connection: sqlite3.Connection, actor: sqlite3.Row, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if actor["role"] != "maker":
            raise ApiError(403, "maker role required")
        quantity = payload.get("quantity")
        if not _is_int(quantity) or not 1 <= quantity <= CAPACITY:
            raise ApiError(422, "quantity must be an integer from 1 to 8")
        used = connection.execute(
            "SELECT COALESCE(SUM(quantity), 0) AS used FROM reservations "
            "WHERE tenant=? AND status IN ('reserved', 'approved', 'fulfilled')",
            (actor["tenant"],),
        ).fetchone()["used"]
        if used + quantity > CAPACITY:
            raise ApiError(409, "capacity exceeded")
        sequence = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM reservations"
        ).fetchone()["next_seq"]
        reservation_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO reservations(id, tenant, creator, quantity, status, seq) "
            "VALUES (?, ?, ?, ?, 'reserved', ?)",
            (reservation_id, actor["tenant"], actor["token"], quantity, sequence),
        )
        row = connection.execute(
            "SELECT id, tenant, creator, quantity, status FROM reservations WHERE id=?",
            (reservation_id,),
        ).fetchone()
        return _reservation(row)

    def _find_for_tenant(
        self, connection: sqlite3.Connection, actor: sqlite3.Row, reservation_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT id, tenant, creator, quantity, status FROM reservations "
            "WHERE id=? AND tenant=?",
            (reservation_id, actor["tenant"]),
        ).fetchone()
        if row is None:
            raise ApiError(404, "reservation not found")
        return row

    def _approve(
        self, connection: sqlite3.Connection, actor: sqlite3.Row, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if actor["role"] != "reviewer":
            raise ApiError(403, "reviewer role required")
        row = self._find_for_tenant(connection, actor, payload["reservation_id"])
        if row["status"] != "reserved":
            raise ApiError(409, "reservation is not reserved")
        connection.execute(
            "UPDATE reservations SET status='approved' WHERE id=?", (row["id"],)
        )
        connection.execute(
            "INSERT INTO outbox(id, reservation_id, tenant, quantity, delivery_key, state, fence) "
            "VALUES (?, ?, ?, ?, ?, 'pending', 0)",
            (
                str(uuid.uuid4()),
                row["id"],
                row["tenant"],
                row["quantity"],
                f"reservation:{row['id']}",
            ),
        )
        row = connection.execute(
            "SELECT id, tenant, creator, quantity, status FROM reservations WHERE id=?",
            (row["id"],),
        ).fetchone()
        return _reservation(row)

    def _cancel(
        self, connection: sqlite3.Connection, actor: sqlite3.Row, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if actor["role"] != "maker":
            raise ApiError(403, "maker role required")
        row = self._find_for_tenant(connection, actor, payload["reservation_id"])
        if row["creator"] != actor["token"]:
            raise ApiError(403, "creator role required")
        if row["status"] != "reserved":
            raise ApiError(409, "reservation is not reserved")
        connection.execute(
            "UPDATE reservations SET status='cancelled' WHERE id=?", (row["id"],)
        )
        row = connection.execute(
            "SELECT id, tenant, creator, quantity, status FROM reservations WHERE id=?",
            (row["id"],),
        ).fetchone()
        return _reservation(row)

    def list_reservations(
        self, actor: sqlite3.Row, limit: int, cursor: int | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        if actor["role"] not in {"maker", "reviewer"}:
            raise ApiError(403, "tenant role required")
        with self.store.connection() as connection:
            parameters: list[Any] = [actor["tenant"]]
            query = "SELECT id, tenant, creator, quantity, status, seq FROM reservations WHERE tenant=?"
            if cursor is not None:
                query += " AND seq > ?"
                parameters.append(cursor)
            query += " ORDER BY seq LIMIT ?"
            parameters.append(limit + 1)
            rows = connection.execute(query, parameters).fetchall()
        has_next = len(rows) > limit
        rows = rows[:limit]
        return [_reservation(row) for row in rows], str(rows[-1]["seq"]) if has_next else None

    def get_reservation(self, actor: sqlite3.Row, reservation_id: str) -> dict[str, Any]:
        if actor["role"] not in {"maker", "reviewer"}:
            raise ApiError(403, "tenant role required")
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT id, tenant, creator, quantity, status FROM reservations "
                "WHERE id=? AND tenant=?",
                (reservation_id, actor["tenant"]),
            ).fetchone()
        if row is None:
            raise ApiError(404, "reservation not found")
        return _reservation(row)

    def claim(self) -> dict[str, Any] | None:
        with self.store.write() as connection:
            now = int(
                connection.execute(
                    "SELECT value FROM meta WHERE key='virtual_clock'"
                ).fetchone()["value"]
            )
            row = connection.execute(
                "SELECT id, reservation_id, tenant, quantity, delivery_key, fence FROM outbox "
                "WHERE state='pending' OR (state='leased' AND lease_until <= ?) "
                "ORDER BY rowid LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            fence = row["fence"] + 1
            connection.execute(
                "UPDATE outbox SET state='leased', fence=?, lease_until=? WHERE id=?",
                (fence, now + LEASE_SECONDS, row["id"]),
            )
            return {
                "id": row["id"],
                "reservation_id": row["reservation_id"],
                "tenant": row["tenant"],
                "quantity": row["quantity"],
                "delivery_key": row["delivery_key"],
                "fence": fence,
            }

    def ack(self, payload: dict[str, Any]) -> None:
        with self.store.write() as connection:
            row = connection.execute(
                "SELECT id, reservation_id, delivery_key, state, fence, lease_until, receipt "
                "FROM outbox WHERE id=?",
                (payload["id"],),
            ).fetchone()
            if row is None:
                raise ApiError(409, "stale job fence")
            expected = f"receipt:{row['delivery_key']}"
            if payload["receipt"] != expected:
                raise ApiError(409, "invalid provider receipt")
            if row["state"] == "done":
                return
            now = int(
                connection.execute(
                    "SELECT value FROM meta WHERE key='virtual_clock'"
                ).fetchone()["value"]
            )
            if (
                row["state"] != "leased"
                or row["fence"] != payload["fence"]
                or row["lease_until"] is None
                or row["lease_until"] <= now
            ):
                raise ApiError(409, "stale job fence")
            updated = connection.execute(
                "UPDATE outbox SET state='done', receipt=?, lease_until=NULL WHERE id=? "
                "AND state='leased' AND fence=?",
                (payload["receipt"], row["id"], payload["fence"]),
            ).rowcount
            if updated != 1:
                raise ApiError(409, "stale job fence")
            connection.execute(
                "UPDATE reservations SET status='fulfilled' WHERE id=? AND status='approved'",
                (row["reservation_id"],),
            )

    def control(self, payload: dict[str, Any]) -> None:
        with self.store.write() as connection:
            if payload["op"] == "advance":
                current = int(
                    connection.execute(
                        "SELECT value FROM meta WHERE key='virtual_clock'"
                    ).fetchone()["value"]
                )
                connection.execute(
                    "UPDATE meta SET value=? WHERE key='virtual_clock'",
                    (str(current + payload["seconds"]),),
                )
            elif payload["op"] == "revoke":
                updated = connection.execute(
                    "UPDATE identities SET active=0 WHERE token=?", (payload["identity"],)
                ).rowcount
                if updated != 1:
                    raise ApiError(404, "identity not found")
            else:
                raise ApiError(422, "unknown control operation")


def _validate_command(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("op"), str):
        raise ApiError(422, "invalid command")
    allowed = {"op", "key", "quantity", "reservation_id"}
    if set(payload) - allowed or not isinstance(payload.get("key"), str) or not payload["key"]:
        raise ApiError(422, "invalid command")
    op = payload["op"]
    if op == "reserve":
        if "quantity" not in payload or "reservation_id" in payload:
            raise ApiError(422, "invalid reserve command")
    elif op in {"approve", "cancel"}:
        if (
            "reservation_id" not in payload
            or not isinstance(payload["reservation_id"], str)
            or not payload["reservation_id"]
        ):
            raise ApiError(422, "invalid reservation command")
        if "quantity" in payload:
            raise ApiError(422, "invalid reservation command")
    else:
        raise ApiError(422, "unknown operation")
    return payload


def _validate_control(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("op"), str):
        raise ApiError(422, "invalid control command")
    if payload["op"] == "advance":
        if set(payload) != {"op", "seconds"} or not _is_int(payload["seconds"]):
            raise ApiError(422, "invalid advance command")
        if payload["seconds"] < 0:
            raise ApiError(422, "seconds must be nonnegative")
    elif payload["op"] == "revoke":
        if set(payload) != {"op", "identity"} or not isinstance(payload["identity"], str):
            raise ApiError(422, "invalid revoke command")
    else:
        raise ApiError(422, "unknown control operation")
    return payload


def _validate_claim(payload: Any) -> None:
    if payload != {}:
        raise ApiError(422, "claim body must be empty")


def _validate_ack(payload: Any) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"id", "fence", "receipt"}
        or not isinstance(payload["id"], str)
        or not _is_int(payload["fence"])
        or not isinstance(payload["receipt"], str)
    ):
        raise ApiError(422, "invalid acknowledgement")
    return payload


class ApiHandler(BaseHTTPRequestHandler):
    server: "ContinuumServer"

    def _json(self, status: int, body: Any) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _error(self, error: ApiError) -> None:
        self._json(error.status, {"error": error.message})

    def _body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > 1024 * 1024:
                raise ValueError
            return json.loads(self.rfile.read(length))
        except (ValueError, TypeError, json.JSONDecodeError):
            raise ApiError(422, "invalid JSON")

    def _token(self) -> str:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or not header[7:]:
            raise ApiError(401, "unauthorized")
        return header[7:]

    def _identity(self) -> sqlite3.Row:
        return self.server.store.identity(self._token())

    def do_GET(self) -> None:
        try:
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                self._json(200, {"ok": True, "schema_version": SCHEMA_VERSION})
            elif parsed.path == "/reservations":
                actor = self._identity()
                query = parse_qs(parsed.query, keep_blank_values=True)
                limit = self._query_int(query, "limit", 20, 1, 100)
                cursor = self._query_int(query, "cursor", None, 0, 2**63 - 1)
                items, next_cursor = self.server.service.list_reservations(actor, limit, cursor)
                self._json(200, {"items": items, "next_cursor": next_cursor})
            elif parsed.path.startswith("/reservations/"):
                actor = self._identity()
                reservation_id = unquote(parsed.path[len("/reservations/"):])
                if not reservation_id or "/" in reservation_id:
                    raise ApiError(404, "reservation not found")
                self._json(200, self.server.service.get_reservation(actor, reservation_id))
            elif parsed.path == "/":
                self._static("index.html", "text/html; charset=utf-8", DEFAULT_INDEX)
            elif parsed.path in {"/client.js", "/static/client.js"}:
                self._static_client()
            else:
                self._serve_static(parsed.path)
        except ApiError as error:
            self._error(error)
        except (sqlite3.Error, OSError):
            self._error(ApiError(503, "database unavailable"))

    def do_POST(self) -> None:
        try:
            path = urlsplit(self.path).path
            if path == "/commands":
                actor = self._identity()
                payload = _validate_command(self._body())
                self._json(200, self.server.service.command(actor, payload))
            elif path == "/worker/claim":
                actor = self._identity()
                if actor["role"] != "worker":
                    raise ApiError(403, "worker role required")
                _validate_claim(self._body())
                self._json(200, {"job": self.server.service.claim()})
            elif path == "/worker/ack":
                actor = self._identity()
                if actor["role"] != "worker":
                    raise ApiError(403, "worker role required")
                self.server.service.ack(_validate_ack(self._body()))
                self._json(200, {"ok": True})
            elif path == "/control":
                if not self.server.test_control:
                    raise ApiError(404, "not found")
                actor = self._identity()
                if actor["role"] != "control":
                    raise ApiError(403, "control role required")
                self.server.service.control(_validate_control(self._body()))
                self._json(200, {"ok": True})
            else:
                raise ApiError(404, "not found")
        except ApiError as error:
            self._error(error)
        except (sqlite3.Error, OSError):
            self._error(ApiError(503, "database unavailable"))

    def _query_int(
        self, query: dict[str, list[str]], name: str, default: int | None, minimum: int, maximum: int
    ) -> int | None:
        values = query.get(name)
        if not values or len(values) != 1 or values[0] == "":
            if values:
                raise ApiError(422, f"invalid {name}")
            return default
        try:
            value = int(values[0])
        except ValueError:
            raise ApiError(422, f"invalid {name}")
        if value < minimum or value > maximum:
            raise ApiError(422, f"invalid {name}")
        return value

    def _static(self, filename: str, content_type: str, fallback: str) -> None:
        path = self.server.static_path / filename if self.server.static_path else None
        if path and path.is_file():
            data = path.read_bytes()
        else:
            data = fallback.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _static_client(self) -> None:
        if self.server.static_path:
            path = self.server.static_path / "dist" / "client.js"
            if path.is_file():
                data = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        self._static("client.js", "text/javascript; charset=utf-8", DEFAULT_CLIENT)

    def _serve_static(self, request_path: str) -> None:
        if not self.server.static_path:
            raise ApiError(404, "not found")
        relative = Path(unquote(request_path.lstrip("/")))
        root = self.server.static_path.resolve()
        candidate = (root / relative).resolve()
        if root not in candidate.parents and candidate != root:
            raise ApiError(404, "not found")
        if not candidate.is_file():
            raise ApiError(404, "not found")
        data = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(candidate))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        return


class ContinuumServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], store: Store, static_path: str | None, test_control: bool):
        super().__init__(address, ApiHandler)
        self.store = store
        self.service = ReservationService(store)
        self.static_path = Path(static_path).resolve() if static_path else None
        self.test_control = test_control


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="synthetic continuum reservation API")
    parser.add_argument("--db", required=True, help="SQLite database path")
    parser.add_argument("--port", required=True, type=int, help="HTTP port")
    parser.add_argument("--test-control", action="store_true")
    parser.add_argument("--static", dest="static_path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(args.db)
    server = ContinuumServer(("0.0.0.0", args.port), store, args.static_path, args.test_control)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

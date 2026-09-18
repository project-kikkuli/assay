"""A small trusted effect boundary for the public template's item feature."""

from __future__ import annotations

import copy
import secrets
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

try:
    import psycopg
    from psycopg import sql as pgsql
except ModuleNotFoundError:  # pragma: no cover - optional for pure validator tests
    psycopg = None  # type: ignore[assignment]
    pgsql = None  # type: ignore[assignment]


Actor = Callable[[dict[str, Any], dict[str, Any]], Mapping[str, Any]]


class KernelError(Exception):
    """An error with the public HTTP status and detail shape."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"{status}: {detail}")


def _identifier(value: str) -> str:
    if not value or not value.replace("_", "").isalnum() or not value[0].islower():
        raise ValueError(f"unsafe SQL identifier: {value}")
    return f'"{value}"'


def _uuid(value: object, label: str = "UUID") -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise KernelError(422, f"{label} must be a valid UUID") from exc


def _bounded_string(value: object, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise KernelError(422, f"{field} must be a string")
    if not 1 <= len(value) <= 255:
        raise KernelError(422, f"{field} must be between 1 and 255 characters")
    return value


def _description(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 255:
        raise KernelError(422, "description must be a string of at most 255 characters")
    return value


class Kernel:
    """Validate actor proposals, then execute owner-scoped SQL as a tenant role."""

    def __init__(self, admin_dsn: str, actor: Actor) -> None:
        if not callable(actor):
            raise TypeError("actor must be callable")
        self.admin_dsn = admin_dsn
        self.actor = actor
        self._roles: dict[uuid.UUID, tuple[str, str]] = {}
        self._created_roles: set[str] = set()
        self._configured_users: set[uuid.UUID] = set()

    def _connect(self, *, autocommit: bool = False) -> Any:
        if psycopg is None:
            raise RuntimeError("psycopg is required for PostgreSQL operations")
        return psycopg.connect(self.admin_dsn, autocommit=autocommit)

    def setup_rls(self, user_ids: Sequence[uuid.UUID | str]) -> dict[str, Any]:
        """Install the explicit fixture-local owner policy and tenant LOGIN roles."""
        users = [_uuid(value, "user_id") for value in user_ids]
        if not users:
            raise ValueError("setup_rls requires at least one user")
        if len(set(users)) != len(users):
            raise ValueError("setup_rls received duplicate users")
        connection = self._connect(autocommit=True)
        item = _identifier("item")
        user = _identifier("user")
        try:
            self._require_uuid_schema(connection, user, item)
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS public.cell_kernel_role_ledger (
                    user_id uuid PRIMARY KEY REFERENCES public.{user}(id) ON DELETE CASCADE,
                    role_name name UNIQUE NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE OR REPLACE FUNCTION public.cell_kernel_current_user_id()
                RETURNS uuid LANGUAGE sql STABLE
                AS $$
                    SELECT user_id FROM public.cell_kernel_role_ledger
                    WHERE role_name = current_user
                $$
                """
            )
            connection.execute(f"ALTER TABLE public.{item} ENABLE ROW LEVEL SECURITY")
            connection.execute(f"ALTER TABLE public.{item} FORCE ROW LEVEL SECURITY")
            policies = connection.execute(
                """
                SELECT policyname FROM pg_policies
                WHERE schemaname = 'public' AND tablename = 'item'
                """
            ).fetchall()
            foreign = [row[0] for row in policies if row[0] != "cell_kernel_owner"]
            if foreign:
                raise KernelError(409, f"unexpected existing item policies: {foreign}")
            if not any(row[0] == "cell_kernel_owner" for row in policies):
                connection.execute(
                    f"""
                    CREATE POLICY cell_kernel_owner ON public.{item}
                    USING (owner_id = public.cell_kernel_current_user_id())
                    WITH CHECK (owner_id = public.cell_kernel_current_user_id())
                    """
                )
            for user_id in users:
                exists = connection.execute(
                    f"SELECT EXISTS (SELECT 1 FROM public.{user} WHERE id = %s)",
                    (user_id,),
                ).fetchone()[0]
                if not exists:
                    raise KernelError(404, "User not found")
                role_name = f"cell_actor_{user_id.hex}"
                password = self._roles.get(user_id, (role_name, secrets.token_urlsafe(24)))[1]
                role = connection.execute(
                    "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s",
                    (role_name,),
                ).fetchone()
                if role is None:
                    connection.execute(
                        pgsql.SQL(
                            "CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER "
                            "NOBYPASSRLS PASSWORD {}"
                        ).format(pgsql.Identifier(role_name), pgsql.Literal(password))
                    )
                    self._created_roles.add(role_name)
                elif role_name not in self._created_roles:
                    raise KernelError(409, f"refusing existing unowned role {role_name}")
                if role is not None and role != (True, False, False):
                    raise KernelError(409, f"role facts are unsafe for {role_name}")
                connection.execute(
                    """
                    INSERT INTO public.cell_kernel_role_ledger(user_id, role_name)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET role_name = EXCLUDED.role_name
                    """,
                    (user_id, role_name),
                )
                connection.execute(
                    f"GRANT USAGE ON SCHEMA public TO {_identifier(role_name)}"
                )
                connection.execute(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{item} TO {_identifier(role_name)}"
                )
                connection.execute(
                    f"GRANT SELECT ON public.cell_kernel_role_ledger TO {_identifier(role_name)}"
                )
                connection.execute(
                    f"GRANT EXECUTE ON FUNCTION public.cell_kernel_current_user_id() TO {_identifier(role_name)}"
                )
                self._roles[user_id] = (role_name, password)
                self._configured_users.add(user_id)
                self._assert_role(connection, user_id, role_name)
            return {"users": [str(value) for value in users], "policy": "cell_kernel_owner"}
        finally:
            connection.close()

    def cleanup(self) -> dict[str, Any]:
        """Remove this instance's role mappings and roles; preserve fixture policy DDL."""
        connection = self._connect(autocommit=True)
        try:
            names = [self._roles[user_id][0] for user_id in self._configured_users]
            deleted = 0
            if names:
                deleted = connection.execute(
                    "DELETE FROM public.cell_kernel_role_ledger WHERE role_name = ANY(%s)",
                    (names,),
                ).rowcount
            dropped: list[str] = []
            for role_name in sorted(self._created_roles):
                connection.execute(
                    pgsql.SQL("DROP OWNED BY {} CASCADE").format(
                        pgsql.Identifier(role_name)
                    )
                )
                connection.execute(
                    pgsql.SQL("DROP ROLE IF EXISTS {}").format(
                        pgsql.Identifier(role_name)
                    )
                )
                dropped.append(role_name)
            self._roles.clear()
            self._created_roles.clear()
            self._configured_users.clear()
            return {
                "ledger_rows_deleted": deleted,
                "roles_dropped": dropped,
                "policy_preserved": True,
            }
        finally:
            connection.close()

    def _require_uuid_schema(self, connection: Any, user: str, item: str) -> None:
        for table, column in ((user, "id"), (item, "id"), (item, "owner_id")):
            row = connection.execute(
                """
                SELECT format_type(a.atttypid, a.atttypmod)
                FROM pg_attribute AS a
                JOIN pg_class AS c ON c.oid = a.attrelid
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = %s
                  AND a.attname = %s AND a.attnum > 0 AND NOT a.attisdropped
                """,
                (table.strip('"'), column),
            ).fetchone()
            if row is None or row[0] != "uuid":
                raise KernelError(409, f"public.{table}.{column} must be uuid")

    def _assert_role(self, connection: Any, user_id: uuid.UUID, role_name: str) -> None:
        row = connection.execute(
            """
            SELECT r.rolcanlogin, r.rolsuper, r.rolbypassrls,
                   r.rolinherit,
                   NOT EXISTS (
                       SELECT 1 FROM pg_auth_members AS m WHERE m.member = r.oid
                   ) AS no_membership,
                   NOT EXISTS (
                       SELECT 1
                       FROM pg_database AS d
                       WHERE d.datname = current_database() AND d.datdba = r.oid
                   ) AS non_database_owner,
                   NOT EXISTS (
                       SELECT 1
                       FROM pg_class AS c
                       JOIN pg_namespace AS n ON n.oid = c.relnamespace
                       WHERE n.nspname = 'public' AND c.relname IN ('user', 'item')
                         AND c.relowner = r.oid
                   ) AS non_table_owner,
                   l.user_id = %s AS mapped
            FROM pg_roles AS r
            JOIN public.cell_kernel_role_ledger AS l ON l.role_name = r.rolname
            WHERE r.rolname = %s
            """,
            (user_id, role_name),
        ).fetchone()
        if row != (True, False, False, False, True, True, True, True):
            raise KernelError(409, f"role facts are unsafe for {role_name}")

    def _ensure_user(self, connection: Any, user_id: uuid.UUID) -> None:
        row = connection.execute(
            'SELECT is_active FROM public."user" WHERE id = %s', (user_id,)
        ).fetchone()
        if row is None:
            raise KernelError(404, "User not found")
        if row[0] is False:
            raise KernelError(400, "Inactive user")
        if user_id not in self._roles:
            raise KernelError(409, "RLS role is not configured for user")

    def _proposal(
        self, command: dict[str, Any], *, fields: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        view = {
            "op": command["op"],
            "target": command.get("item_id"),
            "fields": copy.deepcopy(dict(fields or {})),
            "pagination": {
                "skip": command.get("skip"),
                "limit": command.get("limit"),
            },
        }
        try:
            proposed = self.actor(copy.deepcopy(command), copy.deepcopy(view))
        except Exception as exc:
            raise KernelError(500, "Actor proposal failed") from exc
        if not isinstance(proposed, Mapping):
            raise KernelError(500, "Actor proposal must be an object")
        proposal = dict(proposed)
        if set(proposal) != {"op", "target", "fields"}:
            raise KernelError(500, "Actor proposal has unsupported keys or operation")
        if proposal.get("op") != command["op"]:
            raise KernelError(500, "Actor proposal has unsupported keys or operation")
        if proposal.get("target") != command.get("item_id"):
            raise KernelError(403, "Actor proposal changed the requested target")
        if command["op"] == "list":
            self._validate_paging(proposal["fields"])
        elif command["op"] == "create":
            self._validate_item_fields(proposal["fields"], create=True)
        elif command["op"] == "update":
            self._validate_item_fields(proposal["fields"], create=False)
        elif proposal["fields"] != {}:
            raise KernelError(500, "Actor proposed fields for a read/delete")
        return proposal

    @staticmethod
    def _validate_paging(fields: object) -> dict[str, int]:
        if not isinstance(fields, Mapping) or set(fields) != {"skip", "limit"}:
            raise KernelError(500, "Actor proposed invalid pagination fields")
        skip = fields["skip"]
        limit = fields["limit"]
        if isinstance(skip, bool) or not isinstance(skip, int) or skip < 0:
            raise KernelError(500, "Actor proposed invalid skip")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise KernelError(500, "Actor proposed invalid limit")
        return {"skip": skip, "limit": limit}

    def _begin_as(self, user_id: uuid.UUID) -> Any:
        admin = self._connect(autocommit=True)
        try:
            self._ensure_user(admin, user_id)
            role_name = self._roles[user_id][0]
            password = self._roles[user_id][1]
        finally:
            admin.close()
        if psycopg is None:
            raise RuntimeError("psycopg is required for PostgreSQL operations")
        dsn = psycopg.conninfo.make_conninfo(
            self.admin_dsn, user=role_name, password=password
        )
        return psycopg.connect(dsn)

    @staticmethod
    def _item(row: Sequence[Any]) -> dict[str, Any]:
        created = row[4]
        if isinstance(created, datetime):
            created = created.isoformat()
        return {
            "id": str(row[0]),
            "title": row[1],
            "description": row[2],
            "owner_id": str(row[3]),
            "created_at": created,
        }

    def _validate_item_fields(
        self, fields: Mapping[str, Any], *, create: bool
    ) -> dict[str, Any]:
        if not isinstance(fields, Mapping):
            raise KernelError(500, "Actor fields must be an object")
        values = dict(fields)
        if any(key not in {"title", "description"} for key in values):
            raise KernelError(500, "Actor proposed an unsupported item field")
        if create and set(values) != {"title", "description"}:
            raise KernelError(500, "Actor omitted a create field")
        if "title" in values:
            _bounded_string(values["title"], "title")
        if "description" in values:
            _description(values["description"])
        return values

    def _exists_as_admin(self, item_id: uuid.UUID) -> bool:
        connection = self._connect(autocommit=True)
        try:
            return bool(
                connection.execute(
                    'SELECT EXISTS (SELECT 1 FROM public."item" WHERE id = %s)',
                    (item_id,),
                ).fetchone()[0]
            )
        finally:
            connection.close()

    def create(
        self, user_id: uuid.UUID | str, title: str, description: str | None = None
    ) -> dict[str, Any]:
        principal = _uuid(user_id, "user_id")
        fields = {"title": _bounded_string(title, "title"), "description": _description(description)}
        command = {"op": "create", "item_id": None, "fields": fields}
        proposal = self._proposal(command, fields=fields)
        connection = self._begin_as(principal)
        try:
            item_id = uuid.uuid4()
            row = connection.execute(
                'INSERT INTO public."item" (id, title, description, owner_id, created_at) '
                "VALUES (%s, %s, %s, %s, %s) RETURNING id, title, description, owner_id, created_at",
                (item_id, proposal["fields"]["title"], proposal["fields"]["description"], principal, datetime.now(UTC)),
            ).fetchone()
            connection.commit()
            return self._item(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _target(self, item_id: uuid.UUID | str) -> uuid.UUID:
        return _uuid(item_id, "item_id")

    def _authorize_existing(self, item_id: uuid.UUID) -> None:
        if not self._exists_as_admin(item_id):
            raise KernelError(404, "Item not found")

    def read(self, user_id: uuid.UUID | str, item_id: uuid.UUID | str) -> dict[str, Any]:
        principal = _uuid(user_id, "user_id")
        target = self._target(item_id)
        command = {"op": "read", "item_id": str(target), "fields": {}}
        self._proposal(command, fields={})
        self._authorize_existing(target)
        connection = self._begin_as(principal)
        try:
            row = connection.execute(
                'SELECT id, title, description, owner_id, created_at FROM public."item" '
                "WHERE id = %s AND owner_id = public.cell_kernel_current_user_id()",
                (target,),
            ).fetchone()
            if row is None:
                raise KernelError(403, "Not enough permissions")
            connection.commit()
            return self._item(row)
        finally:
            connection.close()

    def update(
        self,
        user_id: uuid.UUID | str,
        item_id: uuid.UUID | str,
        patch: Mapping[str, Any],
    ) -> dict[str, Any]:
        principal = _uuid(user_id, "user_id")
        target = self._target(item_id)
        if not isinstance(patch, Mapping):
            raise KernelError(422, "patch must be an object")
        fields = dict(patch)
        if "title" in fields and fields["title"] is None:
            raise KernelError(422, "Title cannot be null")
        self._validate_item_fields(fields, create=False)
        command = {"op": "update", "item_id": str(target), "fields": fields}
        proposal = self._proposal(command, fields=fields)
        self._validate_item_fields(proposal["fields"], create=False)
        self._authorize_existing(target)
        connection = self._begin_as(principal)
        try:
            if proposal["fields"]:
                columns = list(proposal["fields"])
                assignments = ", ".join(f"{_identifier(column)} = %s" for column in columns)
                values = [proposal["fields"][column] for column in columns]
                values.append(target)
                row = connection.execute(
                    f'UPDATE public."item" SET {assignments} WHERE id = %s '
                    "AND owner_id = public.cell_kernel_current_user_id() "
                    "RETURNING id, title, description, owner_id, created_at",
                    values,
                ).fetchone()
            else:
                row = connection.execute(
                    'SELECT id, title, description, owner_id, created_at FROM public."item" '
                    "WHERE id = %s AND owner_id = public.cell_kernel_current_user_id()",
                    (target,),
                ).fetchone()
            if row is None:
                raise KernelError(403, "Not enough permissions")
            connection.commit()
            return self._item(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def delete(self, user_id: uuid.UUID | str, item_id: uuid.UUID | str) -> dict[str, str]:
        principal = _uuid(user_id, "user_id")
        target = self._target(item_id)
        command = {"op": "delete", "item_id": str(target), "fields": {}}
        self._proposal(command, fields={})
        self._authorize_existing(target)
        connection = self._begin_as(principal)
        try:
            deleted = connection.execute(
                'DELETE FROM public."item" WHERE id = %s '
                "AND owner_id = public.cell_kernel_current_user_id()",
                (target,),
            ).rowcount
            if deleted != 1:
                raise KernelError(403, "Not enough permissions")
            connection.commit()
            return {"message": "Item deleted successfully"}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list(
        self, user_id: uuid.UUID | str, skip: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        principal = _uuid(user_id, "user_id")
        if isinstance(skip, bool) or not isinstance(skip, int) or skip < 0:
            raise KernelError(422, "skip must be greater than or equal to 0")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise KernelError(422, "limit must be between 1 and 100")
        fields = {"skip": skip, "limit": limit}
        command = {"op": "list", "item_id": None, "fields": fields}
        proposal = self._proposal(command, fields=fields)
        paging = self._validate_paging(proposal["fields"])
        connection = self._begin_as(principal)
        try:
            count = connection.execute(
                'SELECT count(*) FROM public."item" '
                "WHERE owner_id = public.cell_kernel_current_user_id()"
            ).fetchone()[0]
            result = connection.execute(
                'SELECT id, title, description, owner_id, created_at FROM public."item" '
                "WHERE owner_id = public.cell_kernel_current_user_id() "
                "ORDER BY created_at DESC, id DESC OFFSET %s LIMIT %s",
                (paging["skip"], paging["limit"]),
            ).fetchall()
            connection.commit()
            return {"data": [self._item(row) for row in result], "count": count}
        finally:
            connection.close()

"""Thin Item adapter: public admin lane plus Sartre's ordinary-user fast lane."""

from __future__ import annotations

import os
from pathlib import Path
import threading
from typing import Annotated, Any
import uuid
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException, Query
from actor_runtime import Actor
from app.api.deps import CurrentUser, SessionDep
from app.models import ItemCreate, ItemPublic, ItemUpdate, ItemsPublic, Message
from app.api.routes import items as public_items
from kernel import Kernel, KernelError


router = APIRouter(prefix="/items", tags=["items"])
_kernel: Kernel | None = None
_actor: Actor | None = None
_regular_user_lock = threading.RLock()


def _get_kernel() -> Kernel:
    if _kernel is None:
        raise RuntimeError("cell kernel is not installed")
    return _kernel


def _regular_call(user_id: uuid.UUID, operation):
    """Serialize setup plus the full kernel call for this bounded POC.

    The cell fast lane applies only to ordinary-user Item operations. Auth,
    user management, and the public administrator Item lane remain unchanged.
    """
    try:
        with _regular_user_lock:
            kernel = _get_kernel()
            kernel.setup_rls([user_id])
            return operation(kernel)
    except KernelError as error:
        raise HTTPException(status_code=error.status, detail=error.detail) from error


@router.get("/", response_model=ItemsPublic)
def read_items(
    session: SessionDep,
    current_user: CurrentUser,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> Any:
    if current_user.is_superuser:
        return public_items.read_items(session, current_user, skip, limit)
    del session
    return _regular_call(
        current_user.id,
        lambda kernel: ItemsPublic.model_validate(kernel.list(current_user.id, skip, limit)),
    )


@router.get("/{id}", response_model=ItemPublic)
def read_item(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    if current_user.is_superuser:
        return public_items.read_item(session, current_user, id)
    del session
    return _regular_call(
        current_user.id,
        lambda kernel: ItemPublic.model_validate(kernel.read(current_user.id, id)),
    )


@router.post("/", response_model=ItemPublic)
def create_item(*, session: SessionDep, current_user: CurrentUser, item_in: ItemCreate) -> Any:
    if current_user.is_superuser:
        return public_items.create_item(
            session=session, current_user=current_user, item_in=item_in
        )
    del session
    return _regular_call(
        current_user.id,
        lambda kernel: ItemPublic.model_validate(
            kernel.create(current_user.id, item_in.title, item_in.description)
        ),
    )


@router.put("/{id}", response_model=ItemPublic)
def update_item(
    *, session: SessionDep, current_user: CurrentUser, id: uuid.UUID, item_in: ItemUpdate
) -> Any:
    if current_user.is_superuser:
        return public_items.update_item(
            session=session, current_user=current_user, id=id, item_in=item_in
        )
    del session
    return _regular_call(
        current_user.id,
        lambda kernel: ItemPublic.model_validate(
            kernel.update(
                current_user.id, id, item_in.model_dump(exclude_unset=True)
            )
        ),
    )


@router.delete("/{id}")
def delete_item(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Message:
    if current_user.is_superuser:
        return public_items.delete_item(session, current_user, id)
    del session
    return _regular_call(
        current_user.id,
        lambda kernel: Message.model_validate(kernel.delete(current_user.id, id)),
    )


def install(app: FastAPI) -> None:
    """Replace only the public item routes before the server accepts traffic."""
    global _actor, _kernel
    _actor = Actor(
        os.environ["CELL_ACTOR_CANDIDATE"], scratch=os.environ["CELL_ACTOR_SCRATCH"]
    )
    try:
        _actor.__enter__()
        _kernel = Kernel(os.environ["DATABASE_URL"], _actor)
    except Exception:
        _actor.close()
        raise
    prefix = "/api/v1/items"

    def replace_nested(routes: list[Any]) -> bool:
        for route in routes:
            included = getattr(route, "original_router", None)
            context = getattr(route, "include_context", None)
            combined_prefix = (
                f"{getattr(context, 'prefix', '')}{getattr(included, 'prefix', '')}"
                if included is not None
                else ""
            )
            if included is not None and combined_prefix == "/items":
                included.routes[:] = list(router.routes)
                return True
            if included is not None and replace_nested(included.routes):
                return True
        return False

    replaced_nested = replace_nested(app.router.routes)
    if not replaced_nested:
        app.router.routes[:] = [
            route for route in app.router.routes if not getattr(route, "path", "").startswith(prefix)
        ]
        existing = {id(route) for route in app.router.routes}
        app.include_router(router, prefix="/api/v1")
        added = [route for route in app.router.routes if id(route) not in existing]
        remaining = [route for route in app.router.routes if id(route) in existing]
        frontend_index = next(
            (
                index
                for index, route in enumerate(remaining)
                if getattr(route, "path", "") in {"/", "/{path:path}"}
            ),
            len(remaining),
        )
        app.router.routes[:] = remaining[:frontend_index] + added + remaining[frontend_index:]
    app.state.cell_gateway_route_replaced = replaced_nested

    closed = False
    def close() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        evidence: dict[str, Any] = {"actor_closed": False, "kernel_cleanup": None}
        try:
            if _actor is not None:
                _actor.close()
                evidence["actor_closed"] = True
                evidence["actor"] = _actor.observations
        finally:
            try:
                if _kernel is not None:
                    evidence["kernel_cleanup"] = _kernel.cleanup()
            except Exception as error:
                evidence["kernel_cleanup_error"] = type(error).__name__
            evidence["status"] = (
                "passed"
                if evidence["actor_closed"] and evidence["kernel_cleanup"] is not None
                else "failed"
            )
            path = os.environ.get("CELL_SHUTDOWN_EVIDENCE")
            if path:
                Path(path).write_text(__import__("json").dumps(evidence, indent=2) + "\n")
            app.state.cell_shutdown_evidence = evidence

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def cell_lifespan(application: FastAPI):
        try:
            async with original_lifespan(application):
                yield
        finally:
            close()

    app.router.lifespan_context = cell_lifespan

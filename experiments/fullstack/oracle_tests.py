"""Independent API oracles for the pinned FastAPI subject.

The execution harness supplies the subject backend on ``PYTHONPATH`` and a
dedicated migrated database; use ``run.py --mode baseline`` from the harness.

The module deliberately does not import the subject's tests or conftest.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from app.core import security
from app.core.config import settings
from app.core.db import engine
from app.main import app
from app.models import Item, User


API = settings.API_V1_STR
_ORACLE_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$MjQyZWE1MzBjYjJlZTI0Yw$"
    "YTU4NGM5ZTZmYjE2NzZlZjY0ZWY3ZGRkY2U2OWFjNjk"
)
# Trusted boundary for setup only: use the subject signer to mint credentials;
# ownership, visibility, persistence, and response assertions remain independent.


@dataclass(frozen=True)
class Scenario:
    alice_email: str
    alice_id: uuid.UUID
    bob_id: uuid.UUID
    alice_item_ids: tuple[uuid.UUID, ...]
    bob_item_ids: tuple[uuid.UUID, ...]
    alice_headers: dict[str, str]
    bob_headers: dict[str, str]


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def scenario() -> Iterator[Scenario]:
    """Create committed rows, then remove every row owned by this scenario."""

    suffix = uuid.uuid4().hex
    alice_email = f"oracle-alice-{suffix}@example.com"
    bob_email = f"oracle-bob-{suffix}@example.com"
    with Session(engine) as session:
        alice = User(
            email=alice_email,
            hashed_password=_ORACLE_PASSWORD_HASH,
            full_name="Oracle Alice",
        )
        bob = User(
            email=bob_email,
            hashed_password=_ORACLE_PASSWORD_HASH,
            full_name="Oracle Bob",
        )
        session.add(alice)
        session.add(bob)
        session.commit()
        session.refresh(alice)
        session.refresh(bob)

        base_time = datetime.now(UTC)
        alice_items = [
            Item(
                title=f"alice-{index}",
                description=f"alice-description-{index}",
                owner_id=alice.id,
                created_at=base_time + timedelta(seconds=index),
            )
            for index in range(3)
        ]
        bob_items = [
            Item(
                title=f"bob-{index}",
                description=f"bob-description-{index}",
                owner_id=bob.id,
                created_at=base_time + timedelta(seconds=index),
            )
            for index in range(2)
        ]
        session.add_all([*alice_items, *bob_items])
        session.commit()
        for item in [*alice_items, *bob_items]:
            session.refresh(item)

        alice_id = alice.id
        bob_id = bob.id
        alice_item_ids = tuple(item.id for item in alice_items)
        bob_item_ids = tuple(item.id for item in bob_items)

    expires = timedelta(minutes=5)
    yield Scenario(
        alice_email=alice_email,
        alice_id=alice_id,
        bob_id=bob_id,
        alice_item_ids=alice_item_ids,
        bob_item_ids=bob_item_ids,
        alice_headers={
            "Authorization": f"Bearer {security.create_access_token(alice_id, expires)}"
        },
        bob_headers={
            "Authorization": f"Bearer {security.create_access_token(bob_id, expires)}"
        },
    )

    with Session(engine) as session:
        for item in session.exec(
            select(Item).where(Item.owner_id.in_([alice_id, bob_id]))
        ).all():
            session.delete(item)
        for user_id in (alice_id, bob_id):
            user = session.get(User, user_id)
            if user:
                session.delete(user)
        session.commit()


def _item_count(owner_id: uuid.UUID) -> int:
    with Session(engine) as session:
        return session.exec(
            select(func.count()).select_from(Item).where(Item.owner_id == owner_id)
        ).one()


def test_REQ_AUTH_001_unauthenticated_and_wrong_password_are_rejected(
    client: TestClient, scenario: Scenario
) -> None:
    unauthenticated = client.get(f"{API}/items/")
    assert unauthenticated.status_code == 401

    bad_login = client.post(
        f"{API}/login/access-token",
        data={"username": scenario.alice_email, "password": "wrong-password"},
    )
    assert bad_login.status_code == 400
    assert bad_login.json()["detail"] == "Incorrect email or password"


def test_REQ_SCOPE_002_list_and_count_are_owner_scoped(
    client: TestClient, scenario: Scenario
) -> None:
    alice_response = client.get(f"{API}/items/", headers=scenario.alice_headers)
    bob_response = client.get(f"{API}/items/", headers=scenario.bob_headers)
    assert alice_response.status_code == 200
    assert bob_response.status_code == 200

    alice_body = alice_response.json()
    bob_body = bob_response.json()
    assert {row["id"] for row in alice_body["data"]} == {
        str(item_id) for item_id in scenario.alice_item_ids
    }
    assert {row["id"] for row in bob_body["data"]} == {
        str(item_id) for item_id in scenario.bob_item_ids
    }
    assert all(row["owner_id"] == str(scenario.alice_id) for row in alice_body["data"])
    assert all(row["owner_id"] == str(scenario.bob_id) for row in bob_body["data"])
    assert alice_body["count"] == len(alice_body["data"]) == 3
    assert bob_body["count"] == len(bob_body["data"]) == 2


def test_REQ_SCOPE_003_cross_owner_get_update_delete_do_not_interfere(
    client: TestClient, scenario: Scenario
) -> None:
    target_id = scenario.alice_item_ids[0]
    get_response = client.get(
        f"{API}/items/{target_id}", headers=scenario.bob_headers
    )
    update_response = client.put(
        f"{API}/items/{target_id}",
        headers=scenario.bob_headers,
        json={"title": "bob-must-not-write"},
    )
    delete_response = client.delete(
        f"{API}/items/{target_id}", headers=scenario.bob_headers
    )
    assert get_response.status_code == 403
    assert update_response.status_code == 403
    assert delete_response.status_code == 403

    with Session(engine) as session:
        item = session.get(Item, target_id)
        assert item is not None
        assert item.owner_id == scenario.alice_id
        assert item.title == "alice-0"

    owner_update = client.put(
        f"{API}/items/{target_id}",
        headers=scenario.alice_headers,
        json={"title": "alice-updated"},
    )
    assert owner_update.status_code == 200
    assert owner_update.json()["title"] == "alice-updated"

    owner_delete = client.delete(
        f"{API}/items/{target_id}", headers=scenario.alice_headers
    )
    assert owner_delete.status_code == 200
    assert client.get(f"{API}/items/{target_id}", headers=scenario.alice_headers).status_code == 404


def test_REQ_SCOPE_004_owner_spoof_cannot_transfer_item(
    client: TestClient, scenario: Scenario
) -> None:
    create_response = client.post(
        f"{API}/items/",
        headers=scenario.alice_headers,
        json={
            "title": "spoof-at-create",
            "description": "owner_id is not a client-controlled field",
            "owner_id": str(scenario.bob_id),
        },
    )
    assert create_response.status_code == 200
    created = create_response.json()
    assert created["owner_id"] == str(scenario.alice_id)

    update_response = client.put(
        f"{API}/items/{created['id']}",
        headers=scenario.alice_headers,
        json={"title": "spoof-at-update", "owner_id": str(scenario.bob_id)},
    )
    assert update_response.status_code == 200
    assert update_response.json()["owner_id"] == str(scenario.alice_id)
    with Session(engine) as session:
        created_item = session.get(Item, uuid.UUID(created["id"]))
        assert created_item is not None
        assert created_item.owner_id == scenario.alice_id


def test_REQ_DATA_005_user_responses_never_leak_sensitive_fields(
    client: TestClient, scenario: Scenario
) -> None:
    response = client.get(f"{API}/users/me", headers=scenario.alice_headers)
    assert response.status_code == 200
    body = response.json()
    assert "hashed_password" not in body
    assert "password" not in body
    assert _ORACLE_PASSWORD_HASH not in response.text
    assert set(body) <= {
        "email",
        "is_active",
        "is_superuser",
        "full_name",
        "id",
        "created_at",
    }


def test_REQ_PAGE_006_paging_is_complete_disjoint_and_count_stable(
    client: TestClient, scenario: Scenario
) -> None:
    full_response = client.get(
        f"{API}/items/?skip=0&limit=100", headers=scenario.alice_headers
    )
    assert full_response.status_code == 200
    expected_order = [row["id"] for row in full_response.json()["data"]]
    assert expected_order == [str(item_id) for item_id in reversed(scenario.alice_item_ids)]

    pages: list[str] = []
    for skip in range(4):
        response = client.get(
            f"{API}/items/?skip={skip}&limit=1", headers=scenario.alice_headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 3
        page_ids = [row["id"] for row in body["data"]]
        assert len(page_ids) <= 1
        pages.extend(page_ids)
    assert pages == expected_order
    assert len(pages) == len(set(pages)) == 3


def test_REQ_WRITE_007_invalid_writes_fail_without_partial_commit(
    client: TestClient, scenario: Scenario
) -> None:
    target_id = scenario.alice_item_ids[0]
    with Session(engine) as session:
        target = session.get(Item, target_id)
        assert target is not None
        original_title = target.title
    before_count = _item_count(scenario.alice_id)

    invalid_create = client.post(
        f"{API}/items/",
        headers=scenario.alice_headers,
        json={"title": "", "description": "must be rejected"},
    )
    invalid_update = client.put(
        f"{API}/items/{target_id}",
        headers=scenario.alice_headers,
        json={"title": ""},
    )
    assert invalid_create.status_code == 422
    assert invalid_update.status_code == 422

    with Session(engine) as session:
        target = session.get(Item, target_id)
        assert target is not None
        assert target.title == original_title
    assert _item_count(scenario.alice_id) == before_count

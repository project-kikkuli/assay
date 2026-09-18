"""Additional requirements prompted by blind survivors; not holdout evidence.

Setup traverses public signup and login instead of inserting users or minting
tokens. Database reads check persistence independently of response projections.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.db import engine
from app.main import app


PASSWORD = "PublicFixturePassword123!"
API = "/api/v1"


@pytest.fixture
def client():
    # HTTP 500 is a bad application outcome, not an oracle setup exception.
    with TestClient(app, raise_server_exceptions=False) as value:
        yield value


def signup(client, **extra):
    email = f"lifecycle-{uuid.uuid4().hex}@example.com"
    response = client.post(
        f"{API}/users/signup", json={"email": email, "password": PASSWORD, **extra}
    )
    assert response.status_code == 200, response.text
    return email, response.json()


def login(client, email, password=PASSWORD):
    response = client.post(
        f"{API}/login/access-token", data={"username": email, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def stored_user(email):
    with engine.connect() as connection:
        return (
            connection.execute(
                text(
                    'SELECT is_superuser, hashed_password FROM "user" WHERE email = :email'
                ),
                {"email": email},
            )
            .mappings()
            .one()
        )


@pytest.mark.parametrize("extra", [{}, {"is_superuser": True}])
def test_public_signup_cannot_create_privileged_accounts(client, extra):
    email, user = signup(client, **extra)
    assert user["is_superuser"] is False
    assert stored_user(email)["is_superuser"] is False
    headers = login(client, email)
    own_profile = client.get(f"{API}/users/me", headers=headers)
    assert own_profile.status_code == 200
    assert own_profile.json()["id"] == user["id"]
    assert client.get(f"{API}/users/", headers=headers).status_code == 403


def test_password_change_requires_current_secret_and_preserves_authentication(client):
    email, _ = signup(client)
    original = stored_user(email)["hashed_password"]
    assert original != PASSWORD
    headers = login(client, email)
    new_password = "DifferentPublicFixturePassword456!"
    refused = client.patch(
        f"{API}/users/me/password",
        headers=headers,
        json={"current_password": "wrong-password", "new_password": new_password},
    )
    assert refused.status_code == 400
    assert stored_user(email)["hashed_password"] == original
    login(client, email)
    accepted = client.patch(
        f"{API}/users/me/password",
        headers=headers,
        json={"current_password": PASSWORD, "new_password": new_password},
    )
    assert accepted.status_code == 200
    assert stored_user(email)["hashed_password"] not in {original, new_password}
    assert (
        client.post(
            f"{API}/login/access-token", data={"username": email, "password": PASSWORD}
        ).status_code
        == 400
    )
    login(client, email, new_password)


def test_partial_updates_preserve_omitted_fields_in_both_directions(client):
    email, _ = signup(client)
    headers = login(client, email)
    created = client.post(
        f"{API}/items/",
        headers=headers,
        json={"title": "original", "description": "initial"},
    )
    assert created.status_code == 200
    identity = created.json()["id"]
    for patch, expected in [
        ({"description": "changed"}, ("original", "changed")),
        ({"title": "renamed"}, ("renamed", "changed")),
    ]:
        response = client.put(f"{API}/items/{identity}", headers=headers, json=patch)
        assert response.status_code == 200, response.text
        assert (response.json()["title"], response.json()["description"]) == expected
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT title, description FROM item WHERE id = :id"),
                {"id": uuid.UUID(identity)},
            ).one()
            assert tuple(row) == expected

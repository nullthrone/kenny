"""Hostile-but-well-formed input gets a 4xx, never an unhandled exception.

- ``POST /register`` is reachable without credentials; a non-object body, or
  metadata fields of the wrong type, used to raise and surface as a 500.
- A numeric path id beyond SQLite's 64-bit INTEGER range used to raise
  ``OverflowError`` at bind time; it names no row, so it is "not found".
- ``/assets/<name with NUL>`` used to raise ``ValueError`` from ``Path.resolve``.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from kenny_server.main import build_app

_URIS = ["https://example.com/cb"]


@pytest.fixture()
def client(tmp_path):
    app = build_app(db_path=str(tmp_path / "hostile.sqlite"))
    with TestClient(app, raise_server_exceptions=False) as c:
        c.headers["Authorization"] = f"Bearer {app.state.operator_token}"
        yield c


@pytest.mark.parametrize(
    "body",
    [
        [],
        None,
        1,
        "s",
        {"redirect_uris": _URIS, "client_name": {"a": 1}},
        {"redirect_uris": _URIS, "client_name": 5},
        {"redirect_uris": _URIS, "grant_types": "x"},
        {"redirect_uris": _URIS, "grant_types": [1]},
        {"redirect_uris": _URIS, "token_endpoint_auth_method": []},
    ],
)
def test_register_rejects_malformed_metadata(client, body) -> None:
    r = client.post(
        "/register", content=json.dumps(body), headers={"content-type": "application/json"}
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_client_metadata"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/users/{}"),
        ("PATCH", "/api/users/{}"),
        ("DELETE", "/api/users/{}"),
        ("POST", "/api/users/{}/password"),
        ("PUT", "/api/users/{}/hosts"),
        ("PUT", "/api/users/{}/profile"),
        ("DELETE", "/api/users/{}/totp"),
        ("GET", "/api/users/{}/pats"),
        ("POST", "/api/users/{}/pats"),
        ("DELETE", "/api/users/1/pats/{}"),
    ],
)
def test_out_of_range_numeric_id_is_not_found(client, method, path) -> None:
    r = client.request(method, path.format(2**70), json={})
    assert r.status_code < 500


def test_asset_name_with_nul_is_404(client) -> None:
    assert client.get("/assets/%00").status_code == 404


@pytest.mark.parametrize("path", ["/api/tickets"])
def test_absurd_limit_is_not_a_500(client, path) -> None:
    assert client.get(path, params={"limit": "9" * 30}).status_code == 200


@pytest.mark.parametrize("agent_id", ["%E2%82%AC", "a%22b", "a%0d%0ab"])
@pytest.mark.parametrize("query", ["?os=linux", ""])
def test_installer_download_survives_hostile_agent_id(client, agent_id, query) -> None:
    """The agent id lands in ``Content-Disposition``; it must not break the header."""

    r = client.get(f"/api/agents/{agent_id}/installer{query}")
    assert r.status_code in (200, 503)
    disposition = r.headers.get("content-disposition", "")
    assert disposition.count('"') in (0, 2)
    assert "\r" not in disposition and "\n" not in disposition


def _create_user(client) -> int:
    r = client.post(
        "/api/users",
        json={"username": "bob", "password": "pw-123456789!Aa", "role": "user"},
    )
    assert r.status_code == 201
    return r.json()["id"]


@pytest.mark.parametrize(
    "body",
    [
        {"username": 5},
        {"username": ["x"]},
        {"email": []},
        {"email": 5},
        {"role": {}},
        {"disabled": "false"},
        {"disabled": 1},
    ],
)
def test_user_update_rejects_wrongly_typed_fields(client, body) -> None:
    uid = _create_user(client)
    r = client.patch(f"/api/users/{uid}", json=body)
    assert r.status_code == 400
    # Nothing was written: in particular a string "false" did not disable the account.
    assert client.get(f"/api/users/{uid}").json()["disabled"] is False


def test_user_update_still_accepts_well_typed_fields(client) -> None:
    uid = _create_user(client)
    r = client.patch(
        f"/api/users/{uid}", json={"username": "bobby", "email": "b@x.test", "disabled": True}
    )
    assert r.status_code == 200
    assert (r.json()["username"], r.json()["email"], r.json()["disabled"]) == (
        "bobby",
        "b@x.test",
        True,
    )


def test_user_create_rejects_non_string_email(client) -> None:
    r = client.post(
        "/api/users",
        json={"username": "z", "password": "pw-123456789!Aa", "role": "user", "email": 5},
    )
    assert r.status_code == 400


@pytest.mark.parametrize(
    "extra",
    [
        {"agent_id": {"a": 1}},
        {"agent_id": ["host"]},
        {"category": {"a": 1}},
        {"category": ["hardware"]},
        {"requester_user_id": 2**70},
        {"requester_user_id": -(2**70)},
    ],
)
def test_ticket_create_rejects_unbindable_fields(client, extra) -> None:
    r = client.post("/api/tickets", json={"title": "t", **extra})
    assert r.status_code == 400


def test_ticket_create_rejects_infinite_requester(client) -> None:
    r = client.post(
        "/api/tickets",
        content='{"title": "t", "requester_user_id": Infinity}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400


@pytest.mark.parametrize(
    "body",
    [{"title": {"a": 1}}, {"summary": [1]}, {"resolution": 5}, {"category": {"a": 1}}],
)
def test_ticket_patch_rejects_non_string_fields(client, body) -> None:
    tid = client.post("/api/tickets", json={"title": "t"}).json()["id"]
    assert client.patch(f"/api/tickets/{tid}", json=body).status_code == 400

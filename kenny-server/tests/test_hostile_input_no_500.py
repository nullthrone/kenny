"""Hostile-but-well-formed input gets a 4xx, never an unhandled exception.

- ``POST /register`` is reachable without credentials; a non-object body, or
  metadata fields of the wrong type, used to raise and surface as a 500.
- A numeric path id beyond SQLite's 64-bit INTEGER range used to raise
  ``OverflowError`` at bind time; it names no row, so it is "not found".
- ``/assets/<name with NUL>`` used to raise ``ValueError`` from ``Path.resolve``.
- A JSON body holding a lone UTF-16 surrogate parses, but its text cannot be
  encoded as UTF-8; it is refused before any handler binds it to SQLite.
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


_LONE_SURROGATE_BODIES = [
    ("POST", "/api/tickets", b'{"title": "x\\ud800", "summary": "s"}'),
    ("POST", "/api/tickets", b'{"title": "x", "summary": "\\uDFFF tail"}'),
    # The raw three-byte encoding of U+D800: json.loads decodes bytes leniently.
    ("POST", "/api/tickets", b'{"title": "x\xed\xa0\x80", "summary": "s"}'),
    ("POST", "/api/users", b'{"username": "u\\ud800", "password": "pw123456789", "role": "user"}'),
    ("POST", "/api/chat", b'{"message": "hi \\ud800", "agent_id": "dev"}'),
    ("PUT", "/api/me/theme", b'{"\\ud800": 1}'),
]


@pytest.mark.parametrize(("method", "path", "body"), _LONE_SURROGATE_BODIES)
def test_lone_surrogate_in_a_json_body_is_400(client, method, path, body) -> None:
    r = client.request(method, path, content=body, headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert "surrogate" in r.json()["error"]


def test_lone_surrogate_is_refused_before_anything_is_stored(client) -> None:
    body = b'{"title": "fine", "summary": "s\\ud800"}'
    assert client.post("/api/tickets", content=body).status_code == 400
    assert client.get("/api/tickets").json()["tickets"] == []


def test_paired_surrogates_and_plain_unicode_still_pass(client) -> None:
    """A surrogate *pair* is an ordinary character; so is text whose UTF-8
    happens to contain the byte the cheap pre-check looks for."""

    body = b'{"title": "smile \\ud83d\\ude00", "summary": "\xed\x95\x9c\xea\xb8\x80 \xf0\x9f\x98\x80"}'
    r = client.post("/api/tickets", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 201, r.text
    assert r.json()["title"] == "smile \U0001f600"
    assert r.json()["summary"] == "\ud55c\uae00 \U0001f600"


def test_a_form_post_is_not_read_as_json(client) -> None:
    """The login form is not JSON; the surrogate check leaves it to its route."""

    r = client.post(
        "/login",
        content=b"username=%5Cud800&password=x",
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code < 500

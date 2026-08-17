"""``POST /api/agents/share-link``: who may mint one, and what a link is worth.

A share link is the one credential in kenny that leaves the operator's hands.
It is handed to a relative who has no account, travels by message, and is opened
on a machine that has never talked to the server. Four properties make that
safe, and each one is asserted here against the real app rather than the store:

1. **Minting is operator-only.** ``distribution.py``'s routes used to be mounted
   without :func:`~kenny_server.webui.authz.guard`, so any authenticated
   principal — including a ``user`` scoped to one host — could mint an installer
   link for *any* agent id. Provisioning is not a thing a scoped user may do.
2. **Redemption is open, and must stay open.** The person opening the link is
   not signed in and cannot become signed in. ``/d/*`` carries its own
   credential (the nonce), so it must NOT be guarded; a 401 there is a broken
   onboarding flow, not a security win. See ADR-0053.
3. **Single use.** The second fetch of a redeemed nonce fails.
4. **An unredeemed link mints no credential.** The agent token is created at
   fetch time, never at mint time, so a link that is never opened — or that
   expires first — leaves nothing behind to revoke.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from kenny_server.distribution import SHARE_LINK_TTL_S
from kenny_server.main import build_app

BINARY_BYTES = b"MZ fake kenny-agent.exe payload \x00\x01\x02"
LINUX_BYTES = b"\x7fELF fake kenny-agent linux payload"


@pytest.fixture
def binary(tmp_path, monkeypatch):
    p = tmp_path / "kenny-agent.exe"
    p.write_bytes(BINARY_BYTES)
    monkeypatch.setenv("KENNY_AGENT_BINARY", str(p))
    monkeypatch.setenv("KENNY_PUBLIC_URL", "https://kenny.example.com")
    return p


@pytest.fixture
def linux_binary(tmp_path, monkeypatch):
    p = tmp_path / "kenny-agent"
    p.write_bytes(LINUX_BYTES)
    monkeypatch.setenv("KENNY_AGENT_BINARY_LINUX", str(p))
    monkeypatch.setenv("KENNY_PUBLIC_URL", "https://kenny.example.com")
    return p


def _app(tmp_path):
    return build_app(db_path=str(tmp_path / "sharelink.sqlite"))


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


def _nonce_of(url: str) -> str:
    return url.rsplit("/", 1)[-1]


# -- shape -------------------------------------------------------------------


def test_share_link_by_name_shape_and_24h_expiry(tmp_path, binary) -> None:
    """The body form answers the frozen contract: ``{url, expires_at, os, name}``."""

    app = _app(tmp_path)
    with TestClient(app) as c:
        before = datetime.now(timezone.utc)
        r = c.post(
            "/api/agents/share-link",
            headers=_bearer(app),
            json={"name": "study-pc", "os": "windows"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "study-pc"
        assert body["os"] == "windows"
        assert body["url"].startswith("https://kenny.example.com/d/installer/")

        expires_at = datetime.fromisoformat(body["expires_at"])
        window = (expires_at - before).total_seconds()
        # 24h out, allowing for the handful of milliseconds the call itself took.
        assert SHARE_LINK_TTL_S - 5 <= window <= SHARE_LINK_TTL_S + 5


def test_share_link_by_name_linux_carries_the_oneliner(tmp_path, linux_binary) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/api/agents/share-link",
            headers=_bearer(app),
            json={"name": "nas", "os": "linux"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["os"] == "linux"
        assert body["name"] == "nas"
        assert body["url"].startswith("https://kenny.example.com/d/install/")
        assert body["oneliner"] == f"curl -fsSL {body['url']} | sudo sh"


def test_share_link_by_name_rejects_a_bad_body(tmp_path, binary) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        h = _bearer(app)
        assert c.post("/api/agents/share-link", headers=h, json={}).status_code == 400
        assert (
            c.post("/api/agents/share-link", headers=h, json={"name": "  "}).status_code
            == 400
        )
        r = c.post(
            "/api/agents/share-link", headers=h, json={"name": "pc", "os": "plan9"}
        )
        assert r.status_code == 400


# -- who may mint ------------------------------------------------------------


def _setup_admin(c) -> None:
    r = c.post(
        "/setup",
        data={"username": "admin", "password": "pw-123456"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def _pat_for(c, username: str) -> str:
    users = {u["username"]: u for u in c.get("/api/users").json()["users"]}
    uid = users[username]["id"]
    return c.post(f"/api/users/{uid}/pats", json={"label": "t"}).json()["token"]


def test_minting_rejects_an_under_privileged_principal(tmp_path, binary) -> None:
    """A scoped ``user`` may not mint — not for a foreign host, not for their own.

    This is the gap ADR-0053 closes. Before the routes were wrapped in
    ``guard()``, the blanket auth middleware let any authenticated principal
    through, so the ``user`` below could have provisioned an enrollment path for
    a machine they cannot even read telemetry from. Host scope is not the
    control here — role is: minting an installer is provisioning, which a
    ``user`` never does, so even ``PC-KID`` (in scope) is refused.
    """

    app = _app(tmp_path)
    with TestClient(app) as c:
        _setup_admin(c)
        c.post(
            "/api/users",
            json={"username": "kid", "password": "pw-123456", "role": "user"},
        )
        c.post(
            "/api/users",
            json={"username": "op", "password": "pw-123456", "role": "operator"},
        )
        uid = {u["username"]: u["id"] for u in c.get("/api/users").json()["users"]}["kid"]
        c.put(f"/api/users/{uid}/hosts", json={"hosts": ["PC-KID"]})
        kid_pat = _pat_for(c, "kid")
        op_pat = _pat_for(c, "op")

    with TestClient(app) as c:
        kid = {"Authorization": f"Bearer {kid_pat}"}
        assert (
            c.post(
                "/api/agents/share-link", headers=kid, json={"name": "PC-KID"}
            ).status_code
            == 403
        )
        assert (
            c.post(
                "/api/agents/share-link", headers=kid, json={"name": "PC-OTHER"}
            ).status_code
            == 403
        )
        # The path-param form and the other minting routes are closed too.
        assert c.post("/api/agents/PC-KID/share-link", headers=kid).status_code == 403
        assert c.get("/api/agents/PC-KID/installer", headers=kid).status_code == 403
        assert c.post("/api/agents/PC-KID/update", headers=kid).status_code == 403
        assert c.post("/api/agent-binary/fetch", headers=kid).status_code == 403

        # An operator is the intended caller and passes.
        op = {"Authorization": f"Bearer {op_pat}"}
        assert (
            c.post(
                "/api/agents/share-link", headers=op, json={"name": "PC-KID"}
            ).status_code
            == 200
        )


def test_minting_requires_authentication_at_all(tmp_path, binary) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        assert c.post("/api/agents/share-link", json={"name": "x"}).status_code == 401


def test_path_form_survives_the_guard_and_keeps_its_own_ttl(tmp_path, binary) -> None:
    """Guarding the legacy route must not change what it does for an operator.

    The bundled dashboard still calls the path-param form and reads
    ``expires_in``, so the shorter one-hour window stays where it is: the longer
    24h window belongs to the body form, which is the one whose link travels to
    someone who is not at the keyboard. An id that has never enrolled is
    accepted, because share-linking an unknown id *is* first-time onboarding.
    """

    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/agents/never-seen-before/share-link", headers=_bearer(app))
        assert r.status_code == 200
        assert r.json()["expires_in"] == 3600


# -- redemption stays open ---------------------------------------------------


def test_redemption_needs_no_operator_credential(tmp_path, binary) -> None:
    """The whole point: an unauthenticated browser can open the link.

    If ``/d/*`` were wrapped in ``guard()`` this would 401 and onboarding would
    be impossible for the person the link was made for.
    """

    app = _app(tmp_path)
    with TestClient(app) as c:
        url = c.post(
            "/api/agents/share-link", headers=_bearer(app), json={"name": "guest-pc"}
        ).json()["url"]
        r = c.get(f"/d/installer/{_nonce_of(url)}")  # no Authorization header
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"


# -- single use --------------------------------------------------------------


def test_single_use_is_enforced_windows(tmp_path, binary) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        url = c.post(
            "/api/agents/share-link", headers=_bearer(app), json={"name": "once-pc"}
        ).json()["url"]
        nonce = _nonce_of(url)
        assert c.get(f"/d/installer/{nonce}").status_code == 200
        second = c.get(f"/d/installer/{nonce}")
        assert second.status_code == 404
        assert second.json()["error"] == "link invalid or expired"


def test_single_use_is_enforced_linux(tmp_path, linux_binary) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        url = c.post(
            "/api/agents/share-link",
            headers=_bearer(app),
            json={"name": "once-nas", "os": "linux"},
        ).json()["url"]
        nonce = _nonce_of(url)
        first = c.get(f"/d/install/{nonce}")
        assert first.status_code == 200
        assert "--enroll-token" in first.text
        assert c.get(f"/d/install/{nonce}").status_code == 404


# -- expiry ------------------------------------------------------------------


def test_expiry_is_enforced(tmp_path, binary) -> None:
    """A link past its deadline is refused, and the nonce is dropped.

    Time is moved by rewriting the entry's own deadline rather than by sleeping
    24 hours or monkeypatching ``time.time`` globally — the deadline is the thing
    under test, and reaching into it keeps the assertion about expiry alone.
    """

    app = _app(tmp_path)
    with TestClient(app) as c:
        url = c.post(
            "/api/agents/share-link", headers=_bearer(app), json={"name": "stale-pc"}
        ).json()["url"]
        nonce = _nonce_of(url)
        entry = app.state.share_links._nonces[nonce]
        assert entry.expires_at > 0
        entry.expires_at = 1.0  # 1970: long past

        assert c.get(f"/d/installer/{nonce}").status_code == 404
        # Reaped on the failed lookup, so an expired nonce cannot linger and be
        # resurrected by a clock change.
        assert nonce not in app.state.share_links._nonces


# -- an unredeemed link mints nothing ----------------------------------------


def test_unredeemed_link_mints_no_credential(tmp_path, binary) -> None:
    """Minting a link must not mint an agent token; only redeeming does.

    This is what makes the 24h window affordable. The token store is asked
    directly: after minting (and after the link expires unused) there is no
    credential for that agent id at all, so an unopened link leaves nothing to
    revoke and cannot rotate a live agent's token out from under it.
    """

    app = _app(tmp_path)
    token_store = app.state.token_store
    with TestClient(app) as c:
        url = c.post(
            "/api/agents/share-link", headers=_bearer(app), json={"name": "unused-pc"}
        ).json()["url"]
        nonce = _nonce_of(url)

        assert c.portal.call(token_store.has_token, "unused-pc") is False

        # Let it expire unused; still nothing minted.
        app.state.share_links._nonces[nonce].expires_at = 1.0
        assert c.get(f"/d/installer/{nonce}").status_code == 404
        assert c.portal.call(token_store.has_token, "unused-pc") is False


def test_redeeming_is_what_mints_the_credential(tmp_path, binary) -> None:
    """The other half of the previous test: fetching the link does mint one."""

    app = _app(tmp_path)
    token_store = app.state.token_store
    with TestClient(app) as c:
        url = c.post(
            "/api/agents/share-link", headers=_bearer(app), json={"name": "opened-pc"}
        ).json()["url"]
        assert c.portal.call(token_store.has_token, "opened-pc") is False

        r = c.get(f"/d/installer/{_nonce_of(url)}")
        assert r.status_code == 200
        assert c.portal.call(token_store.has_token, "opened-pc") is True

        # The plaintext token is baked into the ZIP, never returned to the API
        # caller who minted the link.
        cfg = json.loads(
            zipfile.ZipFile(io.BytesIO(r.content))
            .read("kenny-agent.setup.json")
            .decode()
        )
        assert cfg["agent_id"] == "opened-pc"
        assert cfg["enroll_token"]
        assert c.portal.call(token_store.verify, "opened-pc", cfg["enroll_token"]) is True

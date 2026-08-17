"""User-management and self-service account routes (ADR-0033).

Two families, both gated by the auth middleware + :mod:`authz`:

* ``/api/me*`` — any authenticated user manages *their own* account (email,
  avatar, theme, password, TOTP, personal access tokens, and browser sessions —
  list them, or sign every other one out).
* ``/api/users*`` — **superuser only**: list/create/edit/delete accounts, set
  roles, manage host scope, reset TOTP, and mint/revoke PATs for others.

The synthetic env-token principal has no backing user row; its ``/api/me`` is
read-only and account mutations return 400 (it's the legacy shared token, not an
account).
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import security
from ..auth import COOKIE_NAME, _session_ttl_secs, _set_session_cookie
from ..oauthstore import OAuthStore
from ..registry import AgentRegistry
from ..store import TelemetryStore
from ..userstore import THEMES, UserExists, UserStore
from . import _known_ids
from .authz import guard, principal_of, require_user

# Selectable profile avatars (rasterized dog-breed PNGs served from /assets).
AVATARS: tuple[str, ...] = (
    "dog-border-collie",
    "dog-labrador",
    "dog-corgi",
    "dog-dachshund",
    "dog-husky",
    "dog-pug",
)


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 - malformed/empty body
        return {}
    return data if isinstance(data, dict) else {}


def _err(detail: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": "invalid", "detail": detail}, status_code=status)


def build_user_routes(
    *,
    user_store: UserStore,
    registry: AgentRegistry,
    store: TelemetryStore,
    oauth_store: OAuthStore | None = None,
) -> list[Route]:
    """Routes for ``/api/me*`` (self) and ``/api/users*`` (superuser)."""

    async def _revoke_oauth(user_id: int) -> None:
        """Revoke a user's OAuth tokens alongside their sessions (ADR-0037).

        A credential change / disable / delete must not leave a live OAuth grant
        that keeps reaching ``/mcp`` as that account.
        """

        if oauth_store is not None:
            await oauth_store.revoke_for_user(user_id)

    # -- self-service (/api/me) ----------------------------------------------

    async def api_me(request: Request) -> JSONResponse:
        principal = principal_of(request)
        assert principal is not None  # guard ensures this
        if principal.user_id is None:
            return JSONResponse(
                {
                    "id": None,
                    "username": principal.username,
                    "role": principal.role,
                    "email": None,
                    "avatar": None,
                    "totp_enabled": False,
                    # No backing row, so no stored preference. The key is present
                    # so the response shape does not change with the identity.
                    "theme": None,
                    "hosts": [],
                    "is_shared_token": True,
                }
            )
        user = await user_store.get_user(principal.user_id)
        if user is None:
            return _err("account not found", 404)
        user["hosts"] = sorted(await user_store.get_user_hosts(principal.user_id))
        user["is_shared_token"] = False
        return JSONResponse(user)

    async def api_me_update(request: Request) -> JSONResponse:
        principal = require_user(request)
        if principal.user_id is None:
            return _err("shared-token session has no editable profile")
        body = await _body(request)
        avatar = body.get("avatar")
        if avatar is not None and avatar not in AVATARS:
            return _err("unknown avatar")
        user = await user_store.update_user(
            principal.user_id,
            email=body.get("email"),
            avatar=avatar,
        )
        return JSONResponse(user)

    async def api_me_theme(request: Request) -> JSONResponse:
        """Persist the caller's console theme (``{"theme": "light"|"dark"}``).

        A shared-token identity has no row to store a preference against, so the
        call is a **clean skip**, not an error: ``200 {"theme": ..., "stored":
        false}``. That principal's client keeps its own local choice, and the
        console never has to special-case a failure for the one identity that
        can never succeed — unlike the account-mutating ``/api/me`` handlers
        above, where a 400 is the honest answer because the caller asked to
        change an account that does not exist.
        """

        principal = require_user(request)
        body = await _body(request)
        theme = body.get("theme")
        if theme not in THEMES:
            return _err(f"theme must be one of {', '.join(sorted(THEMES))}")
        if principal.user_id is None:
            return JSONResponse({"theme": theme, "stored": False})
        await user_store.set_theme(principal.user_id, theme)
        return JSONResponse({"theme": theme, "stored": True})

    async def _rotate_own_session(
        request: Request, resp: JSONResponse, user_id: int
    ) -> JSONResponse:
        """Kill all of a user's sessions, then re-issue one for this device.

        Used after a credential change (password / 2FA) and by explicit
        session revocation, so every other session is invalidated (CWE-613)
        while the caller stays logged in on the current browser — the
        response carries a fresh session cookie.

        Every session id being killed here also owns a per-principal
        active-agent slot in the registry, keyed ``s:<session_id>``
        (ADR-0033, ``Principal.active_key``). Without clearing those too, a
        stale slot lingers in memory for a session that no longer exists in
        the database.
        """

        doomed = await user_store.list_sessions(user_id)
        await user_store.delete_user_sessions(user_id)
        await _revoke_oauth(user_id)
        for session in doomed:
            registry.clear(f"s:{session['id']}")
        sid = await user_store.create_session(
            user_id,
            ttl_secs=_session_ttl_secs(),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        _set_session_cookie(resp, COOKIE_NAME, sid)
        return resp

    async def api_me_password(request: Request) -> JSONResponse:
        principal = require_user(request)
        if principal.user_id is None:
            return _err("shared-token session has no password")
        body = await _body(request)
        current = str(body.get("current_password", ""))
        new = str(body.get("new_password", ""))
        if not new:
            return _err("new_password required")
        row = await user_store.get_by_username(principal.username)
        if row is None or not security.verify_password(current, row["password_hash"]):
            return _err("current password is incorrect", 403)
        await user_store.set_password(principal.user_id, new)
        # Invalidate every other session after a password change; keep this one.
        return await _rotate_own_session(
            request, JSONResponse({"ok": True}), principal.user_id
        )

    async def api_me_totp_setup(request: Request) -> JSONResponse:
        principal = require_user(request)
        if principal.user_id is None:
            return _err("shared-token session cannot enable 2FA")
        secret = security.generate_totp_secret()
        return JSONResponse(
            {"secret": secret, "uri": security.totp_uri(secret, principal.username)}
        )

    async def api_me_totp_enable(request: Request) -> JSONResponse:
        principal = require_user(request)
        if principal.user_id is None:
            return _err("shared-token session cannot enable 2FA")
        body = await _body(request)
        secret = str(body.get("secret", ""))
        code = str(body.get("code", ""))
        if not secret or not security.verify_totp(secret, code):
            return _err("invalid code for this secret")
        await user_store.set_totp_secret(principal.user_id, secret)
        # Enabling 2FA is a credential change too: an already-compromised
        # session is exactly what turning 2FA *on* should evict, not leave
        # valid until it expires on its own (CWE-613).
        return await _rotate_own_session(
            request,
            JSONResponse({"ok": True, "totp_enabled": True}),
            principal.user_id,
        )

    async def api_me_totp_disable(request: Request) -> JSONResponse:
        principal = require_user(request)
        if principal.user_id is None:
            return _err("shared-token session has no 2FA")
        body = await _body(request)
        row = await user_store.get_by_username(principal.username)
        if row is None or not security.verify_password(
            str(body.get("password", "")), row["password_hash"]
        ):
            return _err("password is incorrect", 403)
        await user_store.set_totp_secret(principal.user_id, None)
        # Disabling 2FA is a credential change; drop other sessions too.
        return await _rotate_own_session(
            request,
            JSONResponse({"ok": True, "totp_enabled": False}),
            principal.user_id,
        )

    async def api_me_sessions(request: Request) -> JSONResponse:
        """List the caller's own sessions — never another user's (ADR-0033).

        The raw session id (a bearer credential) is never put on the wire;
        each row is reduced to what a "where am I signed in" UI needs, plus
        ``current`` for the row that answered this very request.
        """

        principal = require_user(request)
        if principal.user_id is None:
            return JSONResponse({"sessions": []})
        sessions = await user_store.list_sessions(principal.user_id)
        return JSONResponse(
            {
                "sessions": [
                    {
                        "created_at": s["created_at"],
                        "expires_at": s["expires_at"],
                        "ip": s["ip"],
                        "user_agent": s["user_agent"],
                        "current": s["id"] == principal.session_id,
                    }
                    for s in sessions
                ]
            }
        )

    async def api_me_sessions_revoke_others(request: Request) -> JSONResponse:
        """Sign out every *other* session (and OAuth grant) for this account.

        This device stays signed in — it gets a fresh session cookie, via the
        same "kill all, re-issue one" mechanism a password/2FA change already
        uses. Personal access tokens are a separate credential (how Claude
        Desktop reaches ``/mcp``) and are deliberately out of scope: nothing
        here touches them.
        """

        principal = require_user(request)
        if principal.user_id is None:
            return _err("shared-token session has no other sessions")
        sessions = await user_store.list_sessions(principal.user_id)
        revoked = sum(1 for s in sessions if s["id"] != principal.session_id)
        return await _rotate_own_session(
            request,
            JSONResponse({"ok": True, "revoked": revoked}),
            principal.user_id,
        )

    async def api_me_pats(request: Request) -> JSONResponse:
        principal = require_user(request)
        if principal.user_id is None:
            return JSONResponse({"pats": []})
        return JSONResponse({"pats": await user_store.list_pats(principal.user_id)})

    async def api_me_pat_create(request: Request) -> JSONResponse:
        principal = require_user(request)
        if principal.user_id is None:
            return _err("shared-token session cannot mint tokens")
        body = await _body(request)
        token = await user_store.create_pat(
            principal.user_id, str(body.get("label", "")) or None
        )
        return JSONResponse({"token": token})

    async def api_me_pat_revoke(request: Request) -> JSONResponse:
        principal = require_user(request)
        if principal.user_id is None:
            return _err("shared-token session has no tokens")
        pid = int(request.path_params["pid"])
        ok = await user_store.revoke_pat(principal.user_id, pid)
        return JSONResponse({"ok": ok})

    async def api_avatars(_request: Request) -> JSONResponse:
        return JSONResponse({"avatars": list(AVATARS)})

    # -- superuser user management (/api/users) -------------------------------

    async def _user_detail(uid: int) -> dict | None:
        user = await user_store.get_user(uid)
        if user is None:
            return None
        user["hosts"] = sorted(await user_store.get_user_hosts(uid))
        user["pats"] = await user_store.list_pats(uid)
        return user

    async def api_users_list(_request: Request) -> JSONResponse:
        return JSONResponse({"users": await user_store.list_users()})

    async def api_users_directory(_request: Request) -> JSONResponse:
        """Minimal id→username/role projection, open to any operator+ account.

        Lets an operator (who cannot reach the superuser-only ``/api/users``)
        resolve another account's id to a display name — e.g. a ticket's
        requester/assignee or a trail row's actor.
        """

        return JSONResponse({"users": await user_store.list_directory()})

    async def api_user_create(request: Request) -> JSONResponse:
        body = await _body(request)
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        role = str(body.get("role", "user"))
        avatar = body.get("avatar")
        if not username or not password:
            return _err("username and password are required")
        if not security.is_valid_role(role):
            return _err("invalid role")
        if avatar is not None and avatar not in AVATARS:
            return _err("unknown avatar")
        try:
            user = await user_store.create_user(
                username, password, role, email=body.get("email"), avatar=avatar
            )
        except UserExists:
            return _err("username already taken", 409)
        hosts = body.get("hosts")
        if isinstance(hosts, list):
            await user_store.set_user_hosts(user["id"], [str(h) for h in hosts])
        return JSONResponse(await _user_detail(user["id"]), status_code=201)

    async def api_user_get(request: Request) -> JSONResponse:
        detail = await _user_detail(int(request.path_params["uid"]))
        if detail is None:
            return _err("user not found", 404)
        return JSONResponse(detail)

    async def api_user_update(request: Request) -> JSONResponse:
        uid = int(request.path_params["uid"])
        target = await user_store.get_user(uid)
        if target is None:
            return _err("user not found", 404)
        body = await _body(request)
        avatar = body.get("avatar")
        if avatar is not None and avatar not in AVATARS:
            return _err("unknown avatar")
        role = body.get("role")
        disabled = body.get("disabled")
        # Guard against removing the last enabled superuser via demotion/disable.
        loses_super = (role is not None and role != "superuser") or (disabled is True)
        if (
            target["role"] == "superuser"
            and loses_super
            and await user_store.count_superusers(exclude=uid) == 0
        ):
            return _err("cannot remove the last superuser", 409)
        try:
            user = await user_store.update_user(
                uid,
                username=body.get("username"),
                email=body.get("email"),
                role=role,
                avatar=avatar,
                disabled=disabled,
            )
        except UserExists:
            return _err("username already taken", 409)
        except ValueError as exc:
            return _err(str(exc))
        # A disabled account must lose any live OAuth grant (its sessions/PATs are
        # already gated by the enabled-user check on resolve).
        if disabled is True:
            await _revoke_oauth(uid)
        return JSONResponse(user)

    async def api_user_delete(request: Request) -> JSONResponse:
        uid = int(request.path_params["uid"])
        target = await user_store.get_user(uid)
        if target is None:
            return _err("user not found", 404)
        if (
            target["role"] == "superuser"
            and await user_store.count_superusers(exclude=uid) == 0
        ):
            return _err("cannot delete the last superuser", 409)
        await user_store.delete_user(uid)
        await _revoke_oauth(uid)
        return JSONResponse({"ok": True})

    async def api_user_password(request: Request) -> JSONResponse:
        uid = int(request.path_params["uid"])
        if await user_store.get_user(uid) is None:
            return _err("user not found", 404)
        body = await _body(request)
        new = str(body.get("new_password", ""))
        if not new:
            return _err("new_password required")
        await user_store.set_password(uid, new)
        await user_store.delete_user_sessions(uid)
        await _revoke_oauth(uid)
        return JSONResponse({"ok": True})

    async def api_user_hosts(request: Request) -> JSONResponse:
        uid = int(request.path_params["uid"])
        if await user_store.get_user(uid) is None:
            return _err("user not found", 404)
        body = await _body(request)
        hosts = body.get("hosts")
        if not isinstance(hosts, list):
            return _err("hosts must be a list")
        await user_store.set_user_hosts(uid, [str(h) for h in hosts])
        known = await _known_ids(registry, store)
        return JSONResponse(
            {"hosts": sorted(await user_store.get_user_hosts(uid)), "available": known}
        )

    async def api_user_totp_reset(request: Request) -> JSONResponse:
        uid = int(request.path_params["uid"])
        if await user_store.get_user(uid) is None:
            return _err("user not found", 404)
        await user_store.set_totp_secret(uid, None)
        return JSONResponse({"ok": True, "totp_enabled": False})

    async def api_user_pats(request: Request) -> JSONResponse:
        uid = int(request.path_params["uid"])
        if await user_store.get_user(uid) is None:
            return _err("user not found", 404)
        return JSONResponse({"pats": await user_store.list_pats(uid)})

    async def api_user_pat_create(request: Request) -> JSONResponse:
        uid = int(request.path_params["uid"])
        if await user_store.get_user(uid) is None:
            return _err("user not found", 404)
        body = await _body(request)
        token = await user_store.create_pat(uid, str(body.get("label", "")) or None)
        return JSONResponse({"token": token})

    async def api_user_pat_revoke(request: Request) -> JSONResponse:
        uid = int(request.path_params["uid"])
        pid = int(request.path_params["pid"])
        ok = await user_store.revoke_pat(uid, pid)
        return JSONResponse({"ok": ok})

    su = {"min_role": "superuser"}
    return [
        Route("/api/avatars", guard(api_avatars)),
        Route("/api/me", guard(api_me)),
        Route("/api/me", guard(api_me_update), methods=["PATCH"]),
        Route("/api/me/theme", guard(api_me_theme), methods=["PUT"]),
        Route("/api/me/password", guard(api_me_password), methods=["POST"]),
        Route("/api/me/sessions", guard(api_me_sessions, min_role="user")),
        Route(
            "/api/me/sessions/revoke-others",
            guard(api_me_sessions_revoke_others, min_role="user"),
            methods=["POST"],
        ),
        Route("/api/me/totp", guard(api_me_totp_setup), methods=["POST"]),
        Route("/api/me/totp", guard(api_me_totp_enable), methods=["PUT"]),
        Route("/api/me/totp", guard(api_me_totp_disable), methods=["DELETE"]),
        Route("/api/me/pats", guard(api_me_pats)),
        Route("/api/me/pats", guard(api_me_pat_create), methods=["POST"]),
        Route("/api/me/pats/{pid}", guard(api_me_pat_revoke), methods=["DELETE"]),
        Route("/api/users", guard(api_users_list, **su)),
        Route("/api/users/directory", guard(api_users_directory, min_role="operator")),
        Route("/api/users", guard(api_user_create, **su), methods=["POST"]),
        Route("/api/users/{uid}", guard(api_user_get, **su)),
        Route("/api/users/{uid}", guard(api_user_update, **su), methods=["PATCH"]),
        Route("/api/users/{uid}", guard(api_user_delete, **su), methods=["DELETE"]),
        Route(
            "/api/users/{uid}/password",
            guard(api_user_password, **su),
            methods=["POST"],
        ),
        Route("/api/users/{uid}/hosts", guard(api_user_hosts, **su), methods=["PUT"]),
        Route(
            "/api/users/{uid}/totp", guard(api_user_totp_reset, **su), methods=["DELETE"]
        ),
        Route("/api/users/{uid}/pats", guard(api_user_pats, **su)),
        Route(
            "/api/users/{uid}/pats", guard(api_user_pat_create, **su), methods=["POST"]
        ),
        Route(
            "/api/users/{uid}/pats/{pid}",
            guard(api_user_pat_revoke, **su),
            methods=["DELETE"],
        ),
    ]

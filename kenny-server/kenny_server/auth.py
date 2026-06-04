"""Operator authentication for the MCP endpoint, dashboard API, and web UI.

A single shared **operator bearer token** (``KENNY_OPERATOR_TOKEN``) gates
everything an operator can reach: the MCP Streamable-HTTP endpoint (``/mcp``),
the dashboard JSON API (``/api``), and the web UI (``/``). Agents authenticate
separately with their own per-agent token on ``/agent/ws`` (see ``registry.py``);
that WebSocket path is intentionally **not** gated here.

This is a deliberately simple single-token scheme for a family-scale deployment
(see ADR-0008). Harden later (per-operator identities, Cloudflare Access/Tailscale).
"""

from __future__ import annotations

import hmac
import logging
import os
from http.cookies import SimpleCookie

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

logger = logging.getLogger("kenny.auth")

COOKIE_NAME = "kenny_op"
_DEV_OPERATOR_TOKEN = "dev-operator-token"


def load_operator_token() -> str:
    """Operator token from ``KENNY_OPERATOR_TOKEN``, or an insecure dev fallback."""

    token = os.environ.get("KENNY_OPERATOR_TOKEN", "").strip()
    if token:
        return token
    logger.warning(
        "KENNY_OPERATOR_TOKEN is not set; using an INSECURE dev operator token. "
        "Set KENNY_OPERATOR_TOKEN (and serve over wss/https) before any non-local use."
    )
    return _DEV_OPERATOR_TOKEN


def _token_valid(provided: str | None, expected: str) -> bool:
    return bool(provided) and hmac.compare_digest(provided, expected)


def _bearer_from_headers(scope: dict) -> str | None:
    for key, value in scope.get("headers", []):
        if key == b"authorization":
            raw = value.decode("latin-1")
            if raw.lower().startswith("bearer "):
                return raw[7:].strip()
    return None


def _cookie_token(scope: dict, cookie_name: str) -> str | None:
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            jar: SimpleCookie = SimpleCookie()
            jar.load(value.decode("latin-1"))
            morsel = jar.get(cookie_name)
            if morsel is not None:
                return morsel.value
    return None


def _credential(scope: dict, cookie_name: str) -> str | None:
    return _bearer_from_headers(scope) or _cookie_token(scope, cookie_name)


def _is_public(path: str) -> bool:
    """Paths reachable without an operator token."""

    return path in ("/login", "/logout")


def _is_api_or_mcp(path: str) -> bool:
    return path.startswith("/api") or path.startswith("/mcp")


class OperatorAuthMiddleware:
    """Pure-ASGI gate.

    Websocket (``/agent/ws``) and lifespan scopes pass through untouched — agents
    authenticate with their own token. For HTTP requests, a valid operator bearer
    (header) or cookie is required; unauthenticated API/MCP requests get ``401``
    and UI requests are redirected to ``/login``. On success the request passes
    through unbuffered, so MCP streaming/SSE is preserved.
    """

    def __init__(self, app, *, token: str, cookie_name: str = COOKIE_NAME) -> None:
        self.app = app
        self.token = token
        self.cookie_name = cookie_name

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if _is_public(path) or _token_valid(
            _credential(scope, self.cookie_name), self.token
        ):
            await self.app(scope, receive, send)
            return

        if _is_api_or_mcp(path):
            response: Response = JSONResponse(
                {"error": "unauthorized", "detail": "operator token required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        else:
            response = RedirectResponse(url="/login", status_code=302)
        await response(scope, receive, send)


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>kenny — sign in</title>
<style>
  body {{ margin:0; height:100vh; display:flex; align-items:center; justify-content:center;
    font:14px/1.5 system-ui, sans-serif; background:#0f1419; color:#e6edf3; }}
  form {{ background:#1b2330; border:1px solid #2a3441; border-radius:12px; padding:28px;
    width:320px; }}
  h1 {{ font-size:18px; margin:0 0 16px; }}
  input {{ width:100%; padding:9px 10px; border-radius:8px; border:1px solid #2a3441;
    background:#0b0f14; color:#e6edf3; font-size:14px; box-sizing:border-box; }}
  button {{ margin-top:14px; width:100%; background:#2563eb; color:#fff; border:0;
    padding:10px; border-radius:8px; font-size:14px; cursor:pointer; }}
  button:hover {{ background:#1d4ed8; }}
  .err {{ color:#c0392b; margin:10px 0 0; font-size:13px; }}
  .muted {{ color:#8b98a5; font-size:12px; margin-top:12px; }}
</style></head>
<body>
  <form method="post" action="/login">
    <h1>kenny — operator sign in</h1>
    <input type="password" name="token" placeholder="operator token" autofocus />
    <button type="submit">Sign in</button>
    {msg}
    <div class="muted">Token is set via KENNY_OPERATOR_TOKEN on the server.</div>
  </form>
</body></html>"""


def build_auth_routes(token: str, *, cookie_name: str = COOKIE_NAME) -> list[Route]:
    """Login/logout routes (public; the middleware exempts them)."""

    async def login(request: Request) -> Response:
        if request.method == "POST":
            form = await request.form()
            provided = str(form.get("token", ""))
            if _token_valid(provided, token):
                resp = RedirectResponse(url="/", status_code=303)
                # Cookie carries the shared token; HttpOnly so page JS can't read it.
                # `secure` is left off so it works over local http; set it behind TLS.
                resp.set_cookie(
                    cookie_name, token, httponly=True, samesite="lax", path="/"
                )
                return resp
            return HTMLResponse(
                _LOGIN_HTML.format(msg='<p class="err">Invalid token.</p>'),
                status_code=401,
            )
        return HTMLResponse(_LOGIN_HTML.format(msg=""))

    async def logout(_request: Request) -> Response:
        resp = RedirectResponse(url="/login", status_code=303)
        resp.delete_cookie(cookie_name, path="/")
        return resp

    return [
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout),
    ]

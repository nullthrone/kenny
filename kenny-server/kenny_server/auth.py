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
    """Primary operator token (``KENNY_OPERATOR_TOKEN``) or insecure dev fallback.

    Kept for callers that want a single canonical token (e.g. the login cookie
    value and tests). Additional accepted tokens live in
    :func:`load_operator_tokens`.
    """

    token = os.environ.get("KENNY_OPERATOR_TOKEN", "").strip()
    if token:
        return token
    if os.environ.get("KENNY_OPERATOR_TOKENS", "").strip():
        # Only the comma-separated set is configured; use its first member.
        return load_operator_tokens()[0]
    logger.warning(
        "KENNY_OPERATOR_TOKEN is not set; using an INSECURE dev operator token. "
        "Set KENNY_OPERATOR_TOKEN (and serve over wss/https) before any non-local use."
    )
    return _DEV_OPERATOR_TOKEN


def load_operator_tokens() -> list[str]:
    """All accepted operator tokens.

    Combines ``KENNY_OPERATOR_TOKEN`` (single) with the optional
    ``KENNY_OPERATOR_TOKENS`` comma-separated list. Falls back to the insecure
    dev token (with a warning) when neither is set.
    """

    tokens: list[str] = []
    single = os.environ.get("KENNY_OPERATOR_TOKEN", "").strip()
    if single:
        tokens.append(single)
    raw = os.environ.get("KENNY_OPERATOR_TOKENS", "").strip()
    for part in raw.split(","):
        part = part.strip()
        if part and part not in tokens:
            tokens.append(part)
    if tokens:
        return tokens
    logger.warning(
        "No operator token configured (KENNY_OPERATOR_TOKEN/KENNY_OPERATOR_TOKENS); "
        "using an INSECURE dev operator token. Set one (and serve over wss/https) "
        "before any non-local use."
    )
    return [_DEV_OPERATOR_TOKEN]


def _token_valid(provided: str | None, expected: str | list[str]) -> bool:
    """Constant-time check of ``provided`` against one or many accepted tokens.

    Always compares against every candidate (no short-circuit) so timing does
    not leak which token, if any, matched.
    """

    if not provided:
        return False
    candidates = [expected] if isinstance(expected, str) else expected
    matched = False
    for candidate in candidates:
        if hmac.compare_digest(provided, candidate):
            matched = True
    return matched


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
    """Paths reachable without an operator token.

    ``/d/*`` are nonce-gated agent-distribution downloads (the nonce in the URL is the
    credential), so a target user / the agent self-updater can fetch without a login.
    ``/assets/*`` are non-sensitive brand assets (logo, favicon) needed by the login
    page itself, so they are served without a token. ``/api/agents/<id>/enroll`` is
    gated by the agent's own one-time enrollment token (verified in the handler), so
    it bypasses the operator gate like ``/agent/ws`` does (ADR-0023).
    """

    return (
        path in ("/login", "/logout")
        or path.startswith("/d/")
        or path.startswith("/assets/")
        or _is_enroll(path)
    )


def _is_enroll(path: str) -> bool:
    """True for ``/api/agents/<id>/enroll`` (agent-token authed, not operator)."""

    return (
        path.startswith("/api/agents/")
        and path.endswith("/enroll")
        and path.count("/") == 4
    )


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

    def __init__(
        self,
        app,
        *,
        token: str | list[str],
        cookie_name: str = COOKIE_NAME,
    ) -> None:
        self.app = app
        # Accept a single token or a list; both flow through `_token_valid`.
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
<link rel="icon" href="/assets/kenny-favicon.png" />
<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;600&display=swap" rel="stylesheet" />
<style>
  /* Warm border-collie palette (see kenny design system). Flat, hairline
     borders, amber accent. Inline hex (no shared token file on this page). */
  :root {{ --bg:#1A1917; --surface:#23211E; --sunken:#141311; --border:#34312C;
    --fg:#ECE6DA; --muted:#A89F8E; --faint:#756B5C; --amber:#E8A33D; --amber-deep:#C9852A;
    --crit:#DD7A62; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    font-family:'Hanken Grotesk', ui-sans-serif, system-ui, 'Segoe UI', sans-serif;
    font-size:15px; line-height:1.55; background:var(--bg); color:var(--fg);
    -webkit-font-smoothing:antialiased; }}
  form {{ background:var(--surface); border:1px solid var(--border); border-radius:10px;
    padding:28px; width:340px; }}
  .brand {{ display:flex; align-items:center; gap:12px; margin-bottom:20px; }}
  .brand img {{ width:40px; height:40px; border-radius:50%; display:block; }}
  .brand .name {{ font-size:19px; font-weight:600; letter-spacing:-0.01em; line-height:1; }}
  .brand .sub {{ font-size:11px; color:var(--muted); letter-spacing:.04em;
    text-transform:uppercase; margin-top:3px; }}
  label {{ font-size:11px; color:var(--muted); letter-spacing:.04em; text-transform:uppercase;
    display:block; margin-bottom:6px; }}
  input {{ width:100%; padding:9px 11px; border-radius:5px; border:1px solid var(--border);
    background:var(--sunken); color:var(--fg); font-size:14px; font-family:inherit;
    transition:border-color .16s cubic-bezier(.2,0,0,1), box-shadow .16s cubic-bezier(.2,0,0,1); }}
  input::placeholder {{ color:var(--faint); }}
  input:focus {{ outline:none; border-color:var(--amber); box-shadow:0 0 0 2px rgba(232,163,61,.12); }}
  button {{ margin-top:16px; width:100%; background:var(--amber); color:#1A1917; border:0;
    padding:10px; border-radius:5px; font-size:14px; font-weight:600; font-family:inherit;
    cursor:pointer; transition:background .16s cubic-bezier(.2,0,0,1); }}
  button:hover {{ background:#F0B65C; }}
  button:active {{ background:var(--amber-deep); }}
  .err {{ color:var(--crit); margin:12px 0 0; font-size:13px; }}
  .muted {{ color:var(--muted); font-size:12px; margin-top:14px; }}
</style></head>
<body>
  <form method="post" action="/login">
    <div class="brand">
      <img src="/assets/kenny-mark-64.png" alt="kenny" width="40" height="40" />
      <div><div class="name">kenny</div><div class="sub">operator sign in</div></div>
    </div>
    <label for="token">operator token</label>
    <input id="token" type="password" name="token" placeholder="operator token" autofocus />
    <button type="submit">Sign in</button>
    {msg}
    <div class="muted">Token is set via KENNY_OPERATOR_TOKEN on the server.</div>
  </form>
</body></html>"""


def _tls_enabled() -> bool:
    """Whether the deployment is behind TLS (set the ``secure`` cookie flag)."""

    return os.environ.get("KENNY_TLS", "").strip() in ("1", "true", "True", "yes")


def build_auth_routes(
    token: str | list[str], *, cookie_name: str = COOKIE_NAME
) -> list[Route]:
    """Login/logout routes (public; the middleware exempts them)."""

    # The cookie carries one canonical token (the first accepted one); any
    # configured token is accepted at login.
    cookie_value = token[0] if isinstance(token, list) else token
    secure_cookie = _tls_enabled()

    async def login(request: Request) -> Response:
        if request.method == "POST":
            form = await request.form()
            provided = str(form.get("token", ""))
            if _token_valid(provided, token):
                resp = RedirectResponse(url="/", status_code=303)
                # Cookie carries the shared token; HttpOnly so page JS can't read it.
                # `secure` is set behind TLS (KENNY_TLS=1) so it isn't sent over http.
                resp.set_cookie(
                    cookie_name,
                    cookie_value,
                    httponly=True,
                    samesite="lax",
                    secure=secure_cookie,
                    path="/",
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

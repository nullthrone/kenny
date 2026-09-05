"""Operator authentication for the MCP endpoint, dashboard API, and web UI.

Multi-user (ADR-0033): every HTTP request is resolved to a :class:`Principal`
(a user id, role, and host scope) and stashed on ``scope["kenny_principal"]`` so
the API routes and MCP tools can enforce role/scope. Credentials resolve in this
order:

1. ``Authorization: Bearer <pat>`` — a per-user **personal access token** (how
   Claude reaches ``/mcp``).
2. A session cookie carrying an opaque **session id** (how the browser logs in).
3. **Back-compat:** the legacy shared ``KENNY_OPERATOR_TOKEN`` /
   ``KENNY_OPERATOR_TOKENS`` — accepted as a synthetic *superuser* so an existing
   single-token install (and Claude's existing config) keeps working across the
   upgrade with no manual steps. Deprecated; see ADR-0033.

Agents authenticate separately with their own per-agent token on ``/agent/ws``;
that WebSocket path is intentionally **not** gated here.
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import security
from .urls import mcp_resource_url, public_base_url
from .userstore import _DEFAULT_SESSION_TTL_SECS

if TYPE_CHECKING:
    from .oauthstore import OAuthStore
    from .registry import AgentRegistry
    from .userstore import UserStore

logger = logging.getLogger("kenny.auth")

COOKIE_NAME = "kenny_op"
_DEV_OPERATOR_TOKEN = "dev-operator-token"


# -- principal ----------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """The authenticated caller for one request.

    ``hosts`` is only meaningful for the ``user`` role (the set of agents that
    user may see/operate on); operators and superusers are unrestricted. The
    synthetic env-token principal (``is_env_token``) is a superuser with no user
    row, kept for back-compat during the single-token → accounts migration.
    """

    user_id: int | None
    username: str
    role: str
    hosts: frozenset[str] = field(default_factory=frozenset)
    email: str | None = None
    avatar: str | None = None
    session_id: str | None = None
    pat_id: str | None = None
    oauth_token_id: str | None = None
    oauth_client_id: str | None = None
    # POSSIBLY DEAD: set on the env-token principal but never read by any
    # caller — kept rather than removed since it is the one marker that lets
    # future code distinguish the back-compat superuser from a real account.
    is_env_token: bool = False

    @property
    def scoped(self) -> bool:
        """Whether this principal is limited to an explicit host set."""

        return self.role == "user"

    @property
    def active_key(self) -> str | None:
        """Per-caller key for the registry's active-agent slot (ADR-0033)."""

        if self.session_id:
            return f"s:{self.session_id}"
        if self.pat_id:
            return f"p:{self.pat_id}"
        if self.oauth_token_id:
            return f"o:{self.oauth_token_id}"
        return None

    def at_least(self, minimum: str) -> bool:
        return security.role_at_least(self.role, minimum)

    def may_see(self, agent_id: str) -> bool:
        """Whether this principal may see/target ``agent_id``."""

        return (not self.scoped) or (agent_id in self.hosts)


def _env_principal() -> Principal:
    """The back-compat superuser resolved from the shared operator token."""

    return Principal(
        user_id=None,
        username="operator",
        role="superuser",
        is_env_token=True,
    )


def load_operator_token() -> str:
    """Primary operator token (``KENNY_OPERATOR_TOKEN``) or insecure dev fallback.

    Kept for callers that want a single canonical token (e.g. tests). Additional
    accepted tokens live in :func:`load_operator_tokens`.
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


def _is_public(path: str) -> bool:
    """Paths reachable without any authentication.

    ``/setup`` bootstraps the first account (its handler is a no-op once a user
    exists). ``/d/*`` are nonce-gated agent-distribution downloads. ``/assets/*``
    are non-sensitive brand assets (logo, favicon, avatars) needed by the login
    page. ``/api/agents/<id>/enroll`` is gated by the agent's own one-time
    enrollment token (verified in the handler), so it bypasses the operator gate
    like ``/agent/ws`` does (ADR-0022).

    The OAuth endpoints (``/.well-known/oauth-*``, ``/authorize``, ``/token``,
    ``/register``, ``/revoke``) are the handshake by which a client *obtains* a
    credential, so they must be reachable without one; ``/authorize`` resolves the
    session cookie itself and redirects to ``/login`` when signed out (ADR-0037).
    """

    return (
        path in ("/login", "/logout", "/setup")
        or path in _OAUTH_PUBLIC_PATHS
        or path.startswith("/.well-known/oauth-")
        or path.startswith("/d/")
        or path.startswith("/assets/")
        or _is_enroll(path)
    )


_OAUTH_PUBLIC_PATHS = frozenset(
    {"/authorize", "/authorize/consent", "/token", "/register", "/revoke"}
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
    """Pure-ASGI gate that also resolves the request's :class:`Principal`.

    Websocket (``/agent/ws``) and lifespan scopes pass through untouched — agents
    authenticate with their own token. For HTTP requests it resolves a principal
    (PAT, session, or legacy shared token) and attaches it to
    ``scope["kenny_principal"]``; unauthenticated API/MCP requests get ``401`` and
    UI requests are redirected to ``/login``. On success the request passes
    through unbuffered, so MCP streaming/SSE is preserved.
    """

    def __init__(
        self,
        app,
        *,
        token: str | list[str],
        user_store: "UserStore | None" = None,
        oauth_store: "OAuthStore | None" = None,
        cookie_name: str = COOKIE_NAME,
    ) -> None:
        self.app = app
        # Accept a single token or a list; both flow through `_token_valid`.
        self.token = token
        self.user_store = user_store
        self.oauth_store = oauth_store
        self.cookie_name = cookie_name

    async def _principal_from_row(
        self,
        row,
        *,
        session_id: str | None = None,
        pat_token: str | None = None,
        oauth_token: str | None = None,
        oauth_client_id: str | None = None,
    ) -> Principal:
        role = row["role"]
        hosts: frozenset[str] = frozenset()
        if role == "user" and self.user_store is not None:
            hosts = frozenset(await self.user_store.get_user_hosts(row["id"]))
        return Principal(
            user_id=row["id"],
            username=row["username"],
            role=role,
            hosts=hosts,
            email=row["email"],
            avatar=row["avatar"],
            session_id=session_id,
            pat_id=security.sha256_hex(pat_token) if pat_token else None,
            oauth_token_id=security.sha256_hex(oauth_token) if oauth_token else None,
            oauth_client_id=oauth_client_id,
        )

    async def _resolve_oauth_bearer(self, bearer: str) -> Principal | None:
        """Resolve an OAuth access token to its account principal, or ``None``.

        The token must be known, unexpired, unrevoked *and* audience-bound to this
        server's MCP resource URL (RFC 8707): kenny must only accept tokens issued
        specifically for it. The account's role/host scope becomes the principal's,
        so the same RBAC as PATs applies.
        """

        if self.oauth_store is None or self.user_store is None:
            return None
        row = await self.oauth_store.resolve_access_token(bearer)
        if row is None:
            return None
        expected = mcp_resource_url()
        if row["resource"] != expected:
            # A known, unexpired token bound to a different audience than this
            # server currently advertises — almost always a KENNY_PUBLIC_URL
            # mismatch (scheme/host/trailing slash). Log it so the operator can see
            # why an authorized client still fails to connect, instead of a silent
            # 401 (the token value itself is never logged).
            logger.warning(
                "oauth: rejecting token bound to resource %r; this server expects %r "
                "(check KENNY_PUBLIC_URL)",
                row["resource"],
                expected,
            )
            return None
        account = await self.user_store.get_enabled_row(row["user_id"])
        if account is None:
            return None
        return await self._principal_from_row(
            account, oauth_token=bearer, oauth_client_id=row["client_id"]
        )

    async def _resolve_principal(self, scope: dict) -> Principal | None:
        bearer = _bearer_from_headers(scope)
        if bearer:
            if self.user_store is not None:
                row = await self.user_store.resolve_pat(bearer)
                if row is not None:
                    return await self._principal_from_row(row, pat_token=bearer)
            oauth_principal = await self._resolve_oauth_bearer(bearer)
            if oauth_principal is not None:
                return oauth_principal
            if _token_valid(bearer, self.token):
                return _env_principal()
            return None
        cookie = _cookie_token(scope, self.cookie_name)
        if cookie:
            if self.user_store is not None:
                row = await self.user_store.resolve_session(cookie)
                if row is not None:
                    return await self._principal_from_row(row, session_id=cookie)
            # Legacy cookie that carried the shared token (pre-ADR-0033).
            if _token_valid(cookie, self.token):
                return _env_principal()
        return None

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        principal = await self._resolve_principal(scope)
        if principal is not None:
            scope["kenny_principal"] = principal
            await self.app(scope, receive, send)
            return

        if _is_public(path):
            await self.app(scope, receive, send)
            return

        if _is_api_or_mcp(path):
            # RFC 9728: point MCP clients at the protected-resource metadata so
            # they can discover the authorization server and start the OAuth flow
            # (ADR-0037). Plain API callers keep the bare Bearer challenge.
            if path.startswith("/mcp"):
                prm = f'{public_base_url()}/.well-known/oauth-protected-resource'
                www_auth = f'Bearer resource_metadata="{prm}"'
            else:
                www_auth = "Bearer"
            response: Response = JSONResponse(
                {"error": "unauthorized", "detail": "authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": www_auth},
            )
        else:
            response = RedirectResponse(url="/login", status_code=302)
        await response(scope, receive, send)


# -- login / setup pages ------------------------------------------------------

_PAGE_STYLE = """
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link rel="stylesheet"
  href="https://fonts.googleapis.com/css2?family=Jost:wght@400;500;600&family=Public+Sans:wght@400;600&family=JetBrains+Mono:wght@400;500&display=swap" />
<style>
  /* Nullthrone: ink on warm paper, one brass accent, square corners, 1px
     hairlines. Inline hex — this page is served before the app's token files
     are reachable. The webfonts are the same three the console loads; every
     stack keeps a system fallback so an air-gapped deployment still renders
     a correct, if plainer, page rather than failing. */
  :root {{ --ink:#141317; --ink-900:#1E1C21; --paper:#F4F2EC; --paper-white:#FFFFFF;
    --rule:#C8C4BA; --label:#4A4750; --muted:#928F96; --faint:#6E6B72;
    --brass:#C09A4B; --crit:#8C2F2E; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center; padding:48px 24px;
    font-family:'Public Sans', ui-sans-serif, system-ui, 'Segoe UI', sans-serif;
    font-size:15px; line-height:1.55; background:var(--ink); color:var(--paper);
    -webkit-font-smoothing:antialiased; }}
  .shell {{ width:min(400px, 100%); text-align:center; }}
  .brand svg {{ color:var(--brass); margin-bottom:20px; }}
  .brand .name {{ font-family:'Jost', ui-sans-serif, system-ui, sans-serif; font-weight:500;
    font-size:26px; letter-spacing:0.14em; color:var(--paper); }}
  .brand .sub {{ font-family:'Jost', ui-sans-serif, system-ui, sans-serif; font-size:10px;
    letter-spacing:0.22em; color:var(--muted); margin-bottom:36px; }}
  form {{ background:var(--paper); padding:28px; text-align:left; }}
  label {{ font-family:'Jost', ui-sans-serif, system-ui, sans-serif; font-size:10px;
    letter-spacing:0.14em; text-transform:uppercase; color:var(--label);
    display:block; margin-bottom:6px; margin-top:16px; }}
  label:first-of-type {{ margin-top:0; }}
  input {{ width:100%; padding:12px; border:1px solid var(--rule); border-radius:0;
    background:var(--paper-white); color:var(--ink); font-size:13px;
    font-family:'JetBrains Mono', ui-monospace, monospace;
    transition:border-color 120ms cubic-bezier(.2,0,0,1); }}
  input::placeholder {{ color:var(--faint); }}
  input:focus {{ outline:none; border-color:var(--ink);
    box-shadow:0 0 0 2px var(--paper), 0 0 0 4px #A87E2F; }}
  button {{ margin-top:22px; width:100%; background:var(--ink); color:var(--paper); border:0;
    border-radius:0; padding:14px; min-height:48px;
    font-family:'Jost', ui-sans-serif, system-ui, sans-serif; font-size:11px;
    letter-spacing:0.14em; text-transform:uppercase; cursor:pointer;
    transition:background 120ms cubic-bezier(.2,0,0,1); }}
  button:hover {{ background:#2A282E; }}
  button:active {{ background:var(--ink-900); }}
  .err {{ color:var(--crit); margin:14px 0 0; font-size:13px; }}
  .muted {{ color:var(--label); font-size:12px; margin-top:14px; }}
  .foot {{ font-family:'JetBrains Mono', ui-monospace, monospace; font-size:10px;
    color:var(--faint); margin-top:20px; }}
  @media (prefers-reduced-motion:reduce) {{ input, button {{ transition:none; }} }}
</style>
"""

# The stepped-arch monogram, filled with ``currentColor`` so the brass comes from
# the wrapper. Same mark the console's sidebar carries, so the sign-in page and
# the app it opens read as one object.
_MARK_SVG = (
    '<svg viewBox="0 0 274 266" width="52" height="50" aria-hidden="true">'
    '<g fill="currentColor" fill-rule="nonzero">'
    '<path d="M81,0 H193 V17 H81 Z"></path>'
    '<path fill-rule="evenodd" d="M61,21 H213 V266 H61 Z '
    'M92,97 A45,45 0 0 1 182,97 V199 A45,45 0 0 1 92,199 Z"></path>'
    '<path d="M39,62 L56,50 V266 H39 Z"></path>'
    '<path transform="translate(274,0) scale(-1,1)" d="M39,62 L56,50 V266 H39 Z"></path>'
    '<path d="M17,106 L34,94 V266 H17 Z"></path>'
    '<path transform="translate(274,0) scale(-1,1)" d="M17,106 L34,94 V266 H17 Z"></path>'
    '<path d="M0,257 H56 V266 H0 Z"></path>'
    '<path transform="translate(274,0) scale(-1,1)" d="M0,257 H56 V266 H0 Z"></path>'
    "</g></svg>"
)

_LOGIN_HTML = (
    """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>kenny — sign in</title>
<link rel="icon" href="/assets/kenny-favicon.png" />
"""
    + _PAGE_STYLE
    + """</head>
<body>
 <div class="shell">
  <div class="brand">"""
    + _MARK_SVG
    + """
    <div class="name">KENNY</div><div class="sub">FLEET CONSOLE</div>
  </div>
  <form method="post" action="/login">
    <label for="username">Username</label>
    <input id="username" type="text" name="username" placeholder="username" autofocus
      autocomplete="username" />
    <label for="password">Password</label>
    <input id="password" type="password" name="password" placeholder="password"
      autocomplete="current-password" />
    <label for="totp">2FA code <span style="text-transform:none">(if enabled)</span></label>
    <input id="totp" type="text" name="totp" placeholder="123456" inputmode="numeric"
      autocomplete="one-time-code" />
    {next_field}
    <button type="submit">Sign in</button>
    {msg}
  </form>
  <div class="foot">Sessions expire {ttl} after sign-in — not renewed by use.</div>
 </div>
</body></html>"""
)


def _safe_next(raw: str | None) -> str:
    """Sanitize a ``next`` redirect target to a safe in-app path.

    Only a relative path that begins the OAuth authorize flow is allowed;
    absolute URLs and protocol-relative ``//host`` values are rejected so the
    login form can never be turned into an open redirect.
    """

    if not raw:
        return ""
    if raw.startswith("/authorize") and not raw.startswith("//"):
        return raw
    return ""


def _ttl_human(secs: int) -> str:
    """Render a TTL in whatever whole unit fits it cleanly, e.g. ``"7 days"``.

    ``KENNY_SESSION_TTL_SECS`` is free-form seconds, so the footer copy has to
    cope with a value that is not a whole number of days (an operator could set
    it to an hour for testing) without printing something like "0 days".
    """

    for unit_secs, name in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if secs % unit_secs == 0:
            n = secs // unit_secs
            return f"{n} {name}" + ("" if n == 1 else "s")
    return f"{secs} seconds"


def _login_page(msg: str = "", next_val: str = "") -> str:
    """Render the login page, carrying a validated ``next`` target through POST."""

    from html import escape

    field = (
        f'<input type="hidden" name="next" value="{escape(next_val)}" />'
        if next_val
        else ""
    )
    return _LOGIN_HTML.format(
        msg=msg, next_field=field, ttl=_ttl_human(_session_ttl_secs())
    )

_SETUP_HTML = (
    """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>kenny — first-run setup</title>
<link rel="icon" href="/assets/kenny-favicon.png" />
"""
    + _PAGE_STYLE
    + """</head>
<body>
 <div class="shell">
  <div class="brand">"""
    + _MARK_SVG
    + """
    <div class="name">KENNY</div><div class="sub">CREATE ADMINISTRATOR</div>
  </div>
  <form method="post" action="/setup">
    <p class="muted" style="margin-top:0">
      This is the first account on this server. It becomes the superuser.
    </p>
    <label for="username">Username</label>
    <input id="username" type="text" name="username" placeholder="username" autofocus
      autocomplete="username" />
    <label for="password">Password</label>
    <input id="password" type="password" name="password" placeholder="password"
      autocomplete="new-password" />
    <label for="email">Email <span style="text-transform:none">(optional)</span></label>
    <input id="email" type="email" name="email" placeholder="you@example.com"
      autocomplete="email" />
    <button type="submit">Create &amp; sign in</button>
    {msg}
  </form>
 </div>
</body></html>"""
)


def _tls_enabled() -> bool:
    """Whether the deployment is behind TLS (set the ``secure`` cookie flag)."""

    return os.environ.get("KENNY_TLS", "").strip() in ("1", "true", "True", "yes")


def _session_ttl_secs() -> int:
    raw = os.environ.get("KENNY_SESSION_TTL_SECS", "").strip()
    if not raw:
        return _DEFAULT_SESSION_TTL_SECS
    try:
        return max(60, int(raw))
    except ValueError:
        return _DEFAULT_SESSION_TTL_SECS


class LoginRateLimiter:
    """In-memory per-IP login throttle (dev-grade, like ``ShareLinks``/``CallLog``).

    ``/login`` is unauthenticated by design, so without a limiter passwords can be
    brute-forced online (CWE-307). After ``max_attempts`` consecutive failures
    from one client IP, that IP is locked out for ``lockout_secs``; a successful
    login clears its counter. Bounds are read from the environment at construction
    so a deployment can tune them.
    """

    def __init__(
        self,
        *,
        max_attempts: int | None = None,
        lockout_secs: float | None = None,
        clock=time.monotonic,
    ) -> None:
        self.max_attempts = (
            max_attempts
            if max_attempts is not None
            else int(os.environ.get("KENNY_LOGIN_MAX_ATTEMPTS", "5"))
        )
        self.lockout_secs = (
            lockout_secs
            if lockout_secs is not None
            else float(os.environ.get("KENNY_LOGIN_LOCKOUT_SECS", "60"))
        )
        self._clock = clock
        self._fails: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}

    def retry_after(self, ip: str) -> float | None:
        """Seconds the IP must wait, or ``None`` if it may attempt now."""

        until = self._locked_until.get(ip)
        if until is None:
            return None
        remaining = until - self._clock()
        if remaining <= 0:
            self._locked_until.pop(ip, None)
            self._fails.pop(ip, None)
            return None
        return remaining

    def record_failure(self, ip: str) -> None:
        self._fails[ip] = self._fails.get(ip, 0) + 1
        if self._fails[ip] >= self.max_attempts:
            self._locked_until[ip] = self._clock() + self.lockout_secs

    def reset(self, ip: str) -> None:
        self._fails.pop(ip, None)
        self._locked_until.pop(ip, None)


def _set_session_cookie(resp: Response, cookie_name: str, sid: str) -> None:
    resp.set_cookie(
        cookie_name,
        sid,
        httponly=True,
        samesite="lax",
        secure=_tls_enabled(),
        path="/",
        max_age=_session_ttl_secs(),
    )


def build_auth_routes(
    token: str | list[str],
    *,
    user_store: "UserStore | None" = None,
    cookie_name: str = COOKIE_NAME,
    registry: "AgentRegistry | None" = None,
) -> list[Route]:
    """Login / setup / logout routes (public; the middleware exempts them).

    ``registry`` is optional so a test can build these routes alone; when it is
    supplied, logging out also releases the caller's active-agent slot.
    """

    limiter = LoginRateLimiter()

    async def login(request: Request) -> Response:
        if request.method == "POST":
            ip = request.client.host if request.client else "unknown"
            form = await request.form()
            username = str(form.get("username", "")).strip()
            password = str(form.get("password", ""))
            totp = str(form.get("totp", "")).strip()
            # Carry a validated OAuth ``next`` target through the POST so a signed-out
            # /authorize request resumes after login (ADR-0037).
            next_val = _safe_next(str(form.get("next", "")))

            # Throttle per (client IP, username): behind a shared NAT (a whole
            # family on one public IP) one member's failures must not lock out the
            # others (CWE-307). The real client IP requires uvicorn proxy-header
            # trust in production (see main.run / KENNY_FORWARDED_ALLOW_IPS).
            limiter_key = f"{ip}|{username}"
            retry = limiter.retry_after(limiter_key)
            if retry is not None:
                resp = HTMLResponse(
                    _login_page(
                        msg='<p class="err">Too many attempts. Try again later.</p>',
                        next_val=next_val,
                    ),
                    status_code=429,
                )
                resp.headers["Retry-After"] = str(int(retry) + 1)
                return resp

            row = None
            if user_store is not None:
                row = await user_store.verify_login(username, password)
            if row is not None:
                secret = row["totp_secret"]
                if secret and not security.verify_totp(secret, totp):
                    limiter.record_failure(limiter_key)
                    logger.warning("failed 2FA for %s from %s", username, ip)
                    return HTMLResponse(
                        _login_page(
                            msg='<p class="err">Invalid 2FA code.</p>',
                            next_val=next_val,
                        ),
                        status_code=401,
                    )
                limiter.reset(limiter_key)
                sid = await user_store.create_session(
                    row["id"],
                    ttl_secs=_session_ttl_secs(),
                    ip=ip,
                    user_agent=request.headers.get("user-agent"),
                )
                resp = RedirectResponse(url=next_val or "/", status_code=303)
                _set_session_cookie(resp, cookie_name, sid)
                return resp
            limiter.record_failure(limiter_key)
            logger.warning("failed login for %r from %s", username, ip)
            return HTMLResponse(
                _login_page(
                    msg='<p class="err">Invalid credentials.</p>', next_val=next_val
                ),
                status_code=401,
            )

        # GET: first-run installs have no accounts yet → send to setup.
        if user_store is not None and await user_store.count_users() == 0:
            return RedirectResponse(url="/setup", status_code=302)
        return HTMLResponse(_login_page(next_val=_safe_next(request.query_params.get("next"))))

    async def setup(request: Request) -> Response:
        # Only usable to bootstrap the very first (superuser) account.
        if user_store is None:
            return RedirectResponse(url="/login", status_code=302)
        if await user_store.count_users() > 0:
            if request.method == "POST":
                return JSONResponse(
                    {"error": "already_configured"}, status_code=409
                )
            return RedirectResponse(url="/login", status_code=302)

        if request.method == "POST":
            form = await request.form()
            username = str(form.get("username", "")).strip()
            password = str(form.get("password", ""))
            email = str(form.get("email", "")).strip() or None
            if not username or not password:
                return HTMLResponse(
                    _SETUP_HTML.format(
                        msg='<p class="err">Username and password are required.</p>'
                    ),
                    status_code=400,
                )
            user = await user_store.create_user(
                username, password, "superuser", email=email
            )
            sid = await user_store.create_session(
                user["id"],
                ttl_secs=_session_ttl_secs(),
                ip=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
            resp = RedirectResponse(url="/", status_code=303)
            _set_session_cookie(resp, cookie_name, sid)
            logger.info("first-run setup created superuser %r", username)
            return resp

        return HTMLResponse(_SETUP_HTML.format(msg=""))

    async def logout(request: Request) -> Response:
        if user_store is not None:
            sid = request.cookies.get(cookie_name)
            if sid:
                await user_store.delete_session(sid)
                # Release the per-caller active-agent selection keyed on this
                # session (``Principal.active_key``, ADR-0033). Without it the
                # registry keeps a slot for a session that no longer exists —
                # the same leak the credential-rotation paths close, and what
                # ``AgentRegistry.clear``'s own docstring means by "on logout".
                if registry is not None:
                    registry.clear(f"s:{sid}")
        resp = RedirectResponse(url="/login", status_code=303)
        resp.delete_cookie(cookie_name, path="/")
        return resp

    return [
        Route("/login", login, methods=["GET", "POST"]),
        Route("/setup", setup, methods=["GET", "POST"]),
        Route("/logout", logout),
    ]

"""Co-hosted OAuth 2.1 Authorization Server for the MCP connector (ADR-0041).

kenny is its own OAuth 2.1 Authorization Server *and* Resource Server so a client
like Claude Desktop can connect to ``/mcp`` through the standard "add custom
connector" OAuth handshake instead of a hand-pasted personal access token. This
module builds the AS-facing routes; the RS-facing token validation lives in
:class:`~kenny_server.auth.OperatorAuthMiddleware`.

The flow implemented here is OAuth 2.1 with the MCP profile
(``modelcontextprotocol.io`` authorization spec):

* **Discovery** — RFC 9728 Protected Resource Metadata and RFC 8414 Authorization
  Server Metadata under ``/.well-known/``.
* **Registration** — RFC 7591 Dynamic Client Registration at ``/register`` (public
  PKCE clients; loopback or https redirect URIs only).
* **Authorization** — ``/authorize`` reuses the existing session-cookie login
  (redirecting to ``/login?next=`` when signed out) and shows a minimal consent
  screen; ``/authorize/consent`` mints a single-use, PKCE-bound authorization code.
* **Token** — ``/token`` exchanges the code (PKCE ``S256`` verified) for an opaque
  access token + rotating refresh token, both audience-bound to the MCP resource
  URL (RFC 8707). ``/revoke`` is RFC 7009.

Identity stays in-process (ADR-0037): every token binds to an existing kenny
account, and the account's role/host scope is what the resolved principal carries.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .auth import COOKIE_NAME, _PAGE_STYLE
from .urls import mcp_resource_url, public_base_url

logger = logging.getLogger("kenny.oauth")

#: Nominal scope advertised/accepted; kenny derives authority from the account,
#: not from OAuth scopes, so this is informational.
DEFAULT_SCOPE = "kenny:mcp"

_DEFAULT_ACCESS_TTL_SECS = 3600
_DEFAULT_REFRESH_TTL_SECS = 30 * 24 * 3600


# -- small helpers ------------------------------------------------------------


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _access_ttl() -> int:
    return _int_env("KENNY_OAUTH_ACCESS_TTL_SECS", _DEFAULT_ACCESS_TTL_SECS)


def _refresh_ttl() -> int:
    return _int_env("KENNY_OAUTH_REFRESH_TTL_SECS", _DEFAULT_REFRESH_TTL_SECS)


def pkce_s256_challenge(verifier: str) -> str:
    """The ``S256`` code challenge for a PKCE ``code_verifier`` (RFC 7636)."""

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _acceptable_resources() -> set[str]:
    """Resource-indicator values accepted at ``/authorize`` and ``/token``.

    Tokens are always bound to the canonical :func:`mcp_resource_url`; we accept a
    few equivalent spellings a client might send (the base origin, trailing slash)
    for interoperability per RFC 8707 guidance.
    """

    resource = mcp_resource_url()
    base = public_base_url()
    return {resource, resource + "/", base, base + "/"}


def _resource_ok(resource: str | None) -> bool:
    # Missing resource is tolerated for robustness (bound to canonical anyway);
    # a present one must be an accepted spelling of this server's MCP endpoint.
    return not resource or resource in _acceptable_resources()


def _valid_redirect_uri(uri: str) -> bool:
    """True for an https URI or an http loopback URI (OAuth 2.1 §1.5)."""

    try:
        parts = urlparse(uri)
    except ValueError:
        return False
    if parts.scheme == "https":
        return bool(parts.netloc)
    if parts.scheme == "http":
        host = (parts.hostname or "").lower()
        return host in ("localhost", "127.0.0.1", "::1")
    return False


def _redirect_with(uri: str, **params: str | None) -> str:
    """Append query params to a redirect URI, preserving existing ones."""

    parts = urlparse(uri)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({k: v for k, v in params.items() if v is not None})
    return urlunparse(parts._replace(query=urlencode(query)))


def _error_page(message: str, status: int = 400) -> HTMLResponse:
    """An HTML error shown when we must NOT redirect (bad client/redirect_uri)."""

    body = (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\" />"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />"
        "<title>kenny — authorization error</title>"
        "<link rel=\"icon\" href=\"/assets/kenny-favicon.png\" />"
        + _PAGE_STYLE
        + "</head><body><form onsubmit=\"return false\">"
        "<div class=\"brand\"><img src=\"/assets/kenny-mark-64.png\" alt=\"kenny\" "
        "width=\"40\" height=\"40\" /><div><div class=\"name\">kenny</div>"
        "<div class=\"sub\">authorization error</div></div></div>"
        f"<p class=\"err\">{message}</p>"
        "<p class=\"muted\">Close this window and try connecting again.</p>"
        "</form></body></html>"
    )
    # _PAGE_STYLE carries doubled braces for str.format; render them literally.
    return HTMLResponse(body.replace("{{", "{").replace("}}", "}"), status_code=status)


_CONSENT_HTML = (
    "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\" />"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />"
    "<title>kenny — authorize</title>"
    "<link rel=\"icon\" href=\"/assets/kenny-favicon.png\" />"
    + _PAGE_STYLE
    + """</head>
<body>
  <form method="post" action="/authorize/consent">
    <div class="brand">
      <img src="/assets/kenny-mark-64.png" alt="kenny" width="40" height="40" />
      <div><div class="name">kenny</div><div class="sub">authorize connection</div></div>
    </div>
    <p class="muted" style="margin-top:0">
      <strong>{client_name}</strong> wants to connect to kenny and act as
      <strong>{username}</strong> ({role}). It will be able to use the same tools
      you can.
    </p>
    {fields}
    <button type="submit" name="action" value="allow">Allow</button>
    <button type="submit" name="action" value="deny"
      style="margin-top:10px;background:var(--sunken);color:var(--fg);border:1px solid var(--border)">
      Deny
    </button>
  </form>
</body></html>"""
)


def _hidden(name: str, value: str) -> str:
    from html import escape

    return f'<input type="hidden" name="{escape(name)}" value="{escape(value)}" />'


# -- route builder ------------------------------------------------------------


def build_oauth_routes(*, oauth_store, user_store) -> list[Route]:
    """OAuth AS routes (all public; the operator middleware exempts them).

    ``oauth_store`` persists clients/codes/tokens; ``user_store`` resolves the
    session cookie to the consenting account at ``/authorize``.
    """

    # Per-process secret for the consent CSRF token. A consent page lives for only
    # a few seconds, so an in-memory secret (reset on restart) is sufficient — a
    # restart mid-consent simply asks the user to approve again.
    consent_secret = secrets.token_bytes(32)

    def _consent_token(session_id: str, client_id: str, redirect_uri: str) -> str:
        msg = f"{session_id}\n{client_id}\n{redirect_uri}".encode("utf-8")
        return hmac.new(consent_secret, msg, hashlib.sha256).hexdigest()

    def _metadata_headers() -> dict:
        # Discovery documents are safe to cache briefly and read cross-origin.
        return {"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"}

    async def protected_resource_metadata(_request: Request) -> JSONResponse:
        base = public_base_url()
        return JSONResponse(
            {
                "resource": mcp_resource_url(),
                "authorization_servers": [base],
                "bearer_methods_supported": ["header"],
                "scopes_supported": [DEFAULT_SCOPE],
                "resource_name": "kenny MCP",
            },
            headers=_metadata_headers(),
        )

    async def authorization_server_metadata(_request: Request) -> JSONResponse:
        base = public_base_url()
        return JSONResponse(
            {
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "registration_endpoint": f"{base}/register",
                "revocation_endpoint": f"{base}/revoke",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": [DEFAULT_SCOPE],
            },
            headers=_metadata_headers(),
        )

    async def register(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "invalid_client_metadata", "error_description": "body must be JSON"},
                status_code=400,
            )
        redirect_uris = body.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return JSONResponse(
                {"error": "invalid_redirect_uri", "error_description": "redirect_uris required"},
                status_code=400,
            )
        for uri in redirect_uris:
            if not isinstance(uri, str) or not _valid_redirect_uri(uri):
                return JSONResponse(
                    {
                        "error": "invalid_redirect_uri",
                        "error_description": f"redirect URI not allowed: {uri!r}",
                    },
                    status_code=400,
                )
        client = await oauth_store.register_client(
            redirect_uris,
            client_name=body.get("client_name"),
            token_endpoint_auth_method=body.get("token_endpoint_auth_method", "none"),
            grant_types=body.get("grant_types"),
        )
        return JSONResponse(client, status_code=201)

    async def authorize(request: Request) -> Response:
        q = request.query_params
        client_id = q.get("client_id", "")
        redirect_uri = q.get("redirect_uri", "")

        client = await oauth_store.get_client(client_id)
        if client is None:
            return _error_page("Unknown or unregistered client.")
        if redirect_uri not in oauth_store.client_redirect_uris(client):
            return _error_page("The redirect URI does not match this client's registration.")

        # From here redirect_uri is trusted; parameter errors go back to it.
        if q.get("response_type") != "code":
            return RedirectResponse(
                _redirect_with(
                    redirect_uri, error="unsupported_response_type", state=q.get("state")
                ),
                status_code=302,
            )
        challenge = q.get("code_challenge", "")
        if not challenge or q.get("code_challenge_method") != "S256":
            return RedirectResponse(
                _redirect_with(
                    redirect_uri,
                    error="invalid_request",
                    error_description="PKCE S256 required",
                    state=q.get("state"),
                ),
                status_code=302,
            )
        if not _resource_ok(q.get("resource")):
            return RedirectResponse(
                _redirect_with(
                    redirect_uri, error="invalid_target", state=q.get("state")
                ),
                status_code=302,
            )

        # Reuse the existing browser session; if signed out, come back after login.
        sid = request.cookies.get(COOKIE_NAME)
        row = await user_store.resolve_session(sid) if sid else None
        if row is None:
            nxt = request.url.path
            if request.url.query:
                nxt += "?" + request.url.query
            return RedirectResponse(f"/login?next={quote(nxt, safe='')}", status_code=302)

        fields = "".join(
            _hidden(name, q.get(name, ""))
            for name in (
                "client_id",
                "redirect_uri",
                "code_challenge",
                "code_challenge_method",
                "state",
                "scope",
                "resource",
            )
        )
        fields += _hidden("csrf", _consent_token(sid, client_id, redirect_uri))
        page = _CONSENT_HTML.format(
            client_name=_safe(client["client_name"] or "An MCP client"),
            username=_safe(row["username"]),
            role=_safe(row["role"]),
            fields=fields,
        )
        return HTMLResponse(page)

    async def consent(request: Request) -> Response:
        form = await request.form()
        client_id = str(form.get("client_id", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        state = form.get("state")
        state = str(state) if state is not None else None

        sid = request.cookies.get(COOKIE_NAME)
        row = await user_store.resolve_session(sid) if sid else None
        if row is None:
            return RedirectResponse("/login", status_code=302)

        client = await oauth_store.get_client(client_id)
        if client is None or redirect_uri not in oauth_store.client_redirect_uris(client):
            return _error_page("The authorization request is no longer valid.")

        expected_csrf = _consent_token(sid, client_id, redirect_uri)
        if not hmac.compare_digest(str(form.get("csrf", "")), expected_csrf):
            return _error_page("This authorization form has expired. Please try again.")

        if str(form.get("action")) != "allow":
            return RedirectResponse(
                _redirect_with(redirect_uri, error="access_denied", state=state),
                status_code=302,
            )

        code = await oauth_store.create_auth_code(
            client_id=client_id,
            user_id=row["id"],
            redirect_uri=redirect_uri,
            code_challenge=str(form.get("code_challenge", "")),
            resource=mcp_resource_url(),
            scope=(str(form.get("scope")) if form.get("scope") else None),
        )
        logger.info("oauth: issued auth code for user %r client %s", row["username"], client_id)
        return RedirectResponse(
            _redirect_with(redirect_uri, code=code, state=state), status_code=302
        )

    def _token_error(error: str, description: str | None = None, status: int = 400) -> JSONResponse:
        body = {"error": error}
        if description:
            body["error_description"] = description
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    async def token(request: Request) -> JSONResponse:
        form = await request.form()
        grant_type = str(form.get("grant_type", ""))

        if grant_type == "authorization_code":
            code = str(form.get("code", ""))
            redirect_uri = str(form.get("redirect_uri", ""))
            verifier = str(form.get("code_verifier", ""))
            if not code or not verifier:
                return _token_error("invalid_request", "code and code_verifier required")
            row = await oauth_store.consume_auth_code(code)
            if row is None:
                return _token_error("invalid_grant", "code invalid, expired, or already used")
            if redirect_uri != row["redirect_uri"]:
                return _token_error("invalid_grant", "redirect_uri mismatch")
            if not hmac.compare_digest(pkce_s256_challenge(verifier), row["code_challenge"]):
                return _token_error("invalid_grant", "PKCE verification failed")
            access, refresh, _fam = await oauth_store.issue_token_pair(
                client_id=row["client_id"],
                user_id=row["user_id"],
                resource=row["resource"],
                scope=row["scope"],
                access_ttl_secs=_access_ttl(),
                refresh_ttl_secs=_refresh_ttl(),
            )
            return _token_response(access, refresh, row["scope"])

        if grant_type == "refresh_token":
            refresh_in = str(form.get("refresh_token", ""))
            if not refresh_in:
                return _token_error("invalid_request", "refresh_token required")
            result = await oauth_store.rotate_refresh_token(
                refresh_in,
                access_ttl_secs=_access_ttl(),
                refresh_ttl_secs=_refresh_ttl(),
            )
            if result is None:
                return _token_error("invalid_grant", "refresh token invalid, expired, or reused")
            old_row, access, refresh = result
            return _token_response(access, refresh, old_row["scope"])

        return _token_error("unsupported_grant_type", f"unsupported grant_type {grant_type!r}")

    async def revoke(request: Request) -> Response:
        form = await request.form()
        tok = str(form.get("token", ""))
        # RFC 7009: always answer 200, even for unknown tokens.
        if tok:
            await oauth_store.revoke_token(tok)
        return Response(status_code=200, headers={"Cache-Control": "no-store"})

    return [
        Route(
            "/.well-known/oauth-protected-resource",
            protected_resource_metadata,
        ),
        Route(
            "/.well-known/oauth-protected-resource/mcp",
            protected_resource_metadata,
        ),
        Route(
            "/.well-known/oauth-authorization-server",
            authorization_server_metadata,
        ),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize),
        Route("/authorize/consent", consent, methods=["POST"]),
        Route("/token", token, methods=["POST"]),
        Route("/revoke", revoke, methods=["POST"]),
    ]


def _token_response(access: str, refresh: str, scope: str | None) -> JSONResponse:
    body = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": _access_ttl(),
        "refresh_token": refresh,
    }
    if scope:
        body["scope"] = scope
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


def _safe(value: str) -> str:
    """HTML-escape a value for interpolation into the consent template."""

    from html import escape

    return escape(str(value))

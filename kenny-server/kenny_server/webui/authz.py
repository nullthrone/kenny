"""Role/scope enforcement for the dashboard API (ADR-0033).

The auth middleware resolves each request to a :class:`~kenny_server.auth.Principal`
on ``scope["kenny_principal"]``. These helpers read it and enforce the role
hierarchy (``superuser > operator > user``) and per-user host scope:

* :func:`guard` wraps a route handler with a minimum-role check, and optionally a
  host-scope check on a path parameter.
* :func:`require_user` / :func:`require_host` raise :class:`Forbidden` (a typed
  401/403) inside handlers that need finer control.
* :func:`visible_ids` filters a fleet list down to what a ``user`` may see.
* :func:`may_see_audit_args` / :func:`strip_audit_args` keep the arguments of audited
  tool calls from principals below ``operator``.
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable, Iterable

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..auth import Principal


class Forbidden(Exception):
    """A role/scope denial carrying the HTTP status and detail to return."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail

    @property
    def response(self) -> JSONResponse:
        error = "unauthorized" if self.status_code == 401 else "forbidden"
        return JSONResponse(
            {"error": error, "detail": self.detail}, status_code=self.status_code
        )


def principal_of(request: Request) -> Principal | None:
    return request.scope.get("kenny_principal")


def require_user(request: Request, min_role: str = "user") -> Principal:
    """Return the caller's principal or raise :class:`Forbidden`."""

    principal = principal_of(request)
    if principal is None:
        raise Forbidden(401, "authentication required")
    if not principal.at_least(min_role):
        raise Forbidden(403, f"requires {min_role} role")
    return principal


def require_host(principal: Principal, agent_id: str) -> None:
    """Raise if a scoped (``user``) principal may not see ``agent_id``."""

    if not principal.may_see(agent_id):
        raise Forbidden(403, "host not in your scope")


def visible_ids(principal: Principal, ids: Iterable[str]) -> list[str]:
    """Filter ``ids`` to those the principal may see (all, unless scoped)."""

    if not principal.scoped:
        return list(ids)
    return [i for i in ids if i in principal.hosts]


def may_see_audit_args(principal: Principal | None) -> bool:
    """Whether ``principal`` may read the arguments recorded on audit entries.

    Only ``operator`` and above: the audit trail is readable by a ``user`` for the
    hosts in their scope, but what an operator ran there is not. A missing
    principal sees nothing.
    """

    return principal is not None and principal.at_least("operator")


def strip_audit_args(principal: Principal | None, entry: dict[str, Any]) -> dict[str, Any]:
    """``entry`` without its ``args`` unless ``principal`` may read them.

    Works on both audit shapes: a ``CallLog.list`` entry (``args`` at the top) and an
    ``EventStore`` row (``args`` inside ``fields``). The actor and run id stay. The
    input is not mutated.
    """

    if may_see_audit_args(principal):
        return entry
    out = dict(entry)
    out.pop("args", None)
    fields = out.get("fields")
    if isinstance(fields, dict):
        out["fields"] = {k: v for k, v in fields.items() if k != "args"}
    return out


def strip_audit_rows(
    principal: Principal | None, rows: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """``EventStore`` rows with ``args`` removed from the ``audit`` ones.

    Other kinds (log, alert) pass through untouched: their ``fields`` are not audit
    arguments.
    """

    return [
        strip_audit_args(principal, r) if r.get("kind") == "audit" else r for r in rows
    ]


def guard(
    handler: Callable,
    *,
    min_role: str = "user",
    host_param: str | None = None,
) -> Callable:
    """Wrap ``handler`` with a role check (and optional host-scope check).

    ``host_param`` names a path parameter (e.g. ``"id"``) whose value must be in
    the caller's host scope when the caller is a ``user``.
    """

    @wraps(handler)
    async def wrapped(request: Request):
        try:
            principal = require_user(request, min_role)
            if host_param is not None:
                require_host(principal, request.path_params[host_param])
        except Forbidden as exc:
            return exc.response
        return await handler(request)

    return wrapped

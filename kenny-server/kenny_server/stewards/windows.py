"""What the scheduler and the event trigger share: the maintenance window and the host.

Both ask the same two questions before a steward touches a machine. *Is a
maintenance window open, and when does this occurrence of it end?* ::

    occurrence_end(window, now)

and *can this machine be run on now?* (online, an OS that serves every
capability the steward names, and, for a steward that asks for it, nobody signed
in): :func:`machine_unready` and :func:`not_idle`, composed with the spec's own
preconditions by :func:`unready`. One implementation of each, so a scheduled run
and an event-started one cannot disagree about either.

The window is a recurring ``{days, start, end[, tz]}`` mapping parsed by
:mod:`kenny_server.webfilter`'s own ``make_window`` and ``schedule_state``, not a
second parser. A window nobody can interpret never means "always".
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from ..tools import CAPABILITY_TOOLS, supports_tool
from ..webfilter import CATEGORY_KEYS, make_window, schedule_state
from .spec import StewardSpec

logger = logging.getLogger("kenny.stewards.windows")

__all__ = [
    "declared_hosts",
    "host_supports",
    "idle_required",
    "machine_unready",
    "not_idle",
    "occurrence_end",
    "unready",
]

#: The OS that can serve a tool, where :func:`kenny_server.tools.supports_tool`
#: does not say (it lists the tools the *agent binary* refuses by name; a
#: ``winget_*`` call on Linux is refused by the steward at run time, which is too
#: late for a scheduled run). Entries here are redundant once ``tools`` scopes
#: the tool itself.
_TOOL_OS: dict[str, frozenset[str]] = {
    "winget_list": frozenset({"windows"}),
    "winget_update": frozenset({"windows"}),
}

StateOf = Callable[..., dict[str, Any]]


# -- the window ----------------------------------------------------------------


def occurrence_end(
    window: Any,
    now: datetime,
    *,
    label: str = "steward",
    state_of: StateOf | None = None,
) -> datetime | None:
    """The end of the window occurrence open at ``now``, or ``None``.

    The end identifies the occurrence for as long as it is open. ``None`` for no
    window, an unreadable one, one that is closed, or one whose end cannot be
    told. ``label`` names the steward in the log. ``state_of`` replaces
    :func:`~kenny_server.webfilter.schedule_state` (the seam a caller's own test
    patches).
    """

    if not isinstance(window, Mapping):
        return None
    try:
        parsed = make_window(
            label,
            days=window.get("days"),
            start=window.get("start"),
            end=window.get("end"),
            # Windows are shared with the web filter, which wants a category;
            # none is used here, the window is only a time span.
            categories=CATEGORY_KEYS[:1],
            tz=window.get("tz") or window.get("timezone"),
        )
    except (ValueError, TypeError) as exc:
        logger.warning("steward %s: its maintenance window is unreadable (%s)", label, exc)
        return None
    state = (state_of or schedule_state)({}, [parsed], at=now)
    if not state["active_windows"] or not state["next_change_at"]:
        return None
    try:
        end = datetime.fromisoformat(str(state["next_change_at"]))
    except ValueError:
        return None
    return end if end.tzinfo is not None else end.replace(tzinfo=timezone.utc)


# -- the host ------------------------------------------------------------------


def host_supports(spec: StewardSpec, os_name: str | None) -> bool:
    """Whether a host running ``os_name`` can serve every capability ``spec`` names."""

    name = (os_name or "").lower()
    for tool in spec.tools:
        if tool not in CAPABILITY_TOOLS:
            continue
        if not supports_tool(tool, name):
            return False
        scoped = _TOOL_OS.get(tool)
        if scoped is not None and name not in scoped:
            return False
    return True


def declared_hosts(params: Mapping[str, Any]) -> list[str]:
    """The hosts a steward may run on: sorted, unique, and empty unless named."""

    raw = params.get("hosts")
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return []
    return sorted({h.strip() for h in raw if isinstance(h, str) and h.strip()})


def idle_required(params: Mapping[str, Any]) -> bool:
    """Whether to check the host is unattended: yes, unless the parameter is ``false``.

    The parameter is a JSON boolean (``runner.validate_params``); anything else
    — absent, a list, a string a parameter written before that rule left
    behind — means check, the safe direction.
    """

    return params.get("require_idle") is not False


def machine_unready(spec: StewardSpec, executor: Any, host: str) -> str | None:
    """Why ``host`` cannot be run on at all right now (offline, wrong OS), or ``None``."""

    agent = executor.registry.get(host)
    if agent is None or not agent.online:
        return "the machine is offline"
    if not host_supports(spec, getattr(agent, "os", None)):
        return f"this steward does not run on {getattr(agent, 'os', 'this')} machines"
    return None


async def not_idle(executor: Any, spec: StewardSpec, host: str) -> str | None:
    """``None`` only when the host itself says nobody is signed in.

    Asked as ``steward:<id>``, by server code, not by the model.
    """

    try:
        status = await executor.run_capability(
            "remotehelp_status", {}, agent_id=host, actor=f"steward:{spec.id}"
        )
    except Exception as exc:  # noqa: BLE001 - a check that fails is "do not disturb"
        return f"could not tell whether anyone is using the machine ({type(exc).__name__})"
    if isinstance(status, Mapping) and status.get("interactive_session") is False:
        return None
    if isinstance(status, Mapping) and status.get("interactive_session") is True:
        return "someone is signed in at the machine"
    return "could not tell whether anyone is using the machine"


async def unready(
    runner: Any, executor: Any, spec: StewardSpec, host: str | None, params: Mapping[str, Any]
) -> str | None:
    """Why ``host`` must not be run on now, or ``None``.

    Online and OS first, then the spec's preconditions (the server's own
    records, :meth:`StewardRunner.precondition_refusal`), then the idle check
    (a call to the host). A server-only run (``host`` ``None``) has no machine
    to be ready; the preconditions are still asked.
    """

    if host is None:
        return await runner.precondition_refusal(spec, None, params)
    reason = machine_unready(spec, executor, host)
    if reason is not None:
        return reason
    refused = await runner.precondition_refusal(spec, host, params)
    if refused is not None:
        return refused
    if "require_idle" in spec.params and idle_required(params):
        return await not_idle(executor, spec, host)
    return None

"""Three-tier classification of every kenny tool — the single source of truth.

**The governing principle: the tier is a property of the tool; the gate is a
property of the calling surface.** This module says what a tool *is* (does it
change the world, and how consequential is that change). It never says what a
surface must *do* about it. A tier is not permission to skip a confirmation:
each surface — the dashboard chat, the MCP endpoint, anything added later —
decides for itself which tiers it holds, denies, or lets through.

That separation exists so moving a tool from ``normal_change`` to
``standard_change`` can never silently remove the dashboard's confirmation
dialog. The dashboard holds *both* change tiers; a re-tiering is a statement
about the tool's blast radius, not an approval shortcut.

The tiers:

* ``read_only`` — observes; changes nothing on the host or on the server.
* ``standard_change`` — a change that is routine, reversible and low-blast-radius
  (flush a DNS cache, open a remote-help session, push the block list already
  configured). "Standard" in the ITSM sense: pre-authorised *as a category*, not
  pre-approved for any particular caller.
* ``normal_change`` — everything else that changes state: arbitrary code
  execution, package install/uninstall, who may sign in to a family PC, changing
  what the server itself enforces.

Unknown tools **fail closed** — :func:`classify` returns ``normal_change`` for a
name it has never heard of, so a tool added to a catalog without being classified
here is treated as the most consequential thing it could be, not the least.

**Dependency discipline:** stdlib only, and zero imports from other kenny
modules (same rule as ``policy.py``). ``tools.py`` and ``chat.py`` are free to
import this module; if it imported them back, the catalogs could not use it.
Exhaustiveness against those catalogs is therefore asserted in
``tests/test_tool_classes.py``, not enforced by an import.
"""

from __future__ import annotations

READ_ONLY = "read_only"
STANDARD_CHANGE = "standard_change"
NORMAL_CHANGE = "normal_change"

# Every tool name in the repo: the server-only tools of ``chat.SERVER_TOOLS``,
# every key of ``tools.CAPABILITY_TOOLS``, and the MCP-only server tools
# registered in ``tools.py``. Kept as a literal map (not derived) so this module
# stays dependency-free; ``test_catalog_exhaustive`` fails if a catalog grows a
# name that is missing here.
TOOL_CLASSES: dict[str, str] = {
    # -- server-only tools (registry/store reads) --------------------------
    "list_agents": READ_ONLY,
    "select_agent": READ_ONLY,
    "fleet_overview": READ_ONLY,
    "agent_health": READ_ONLY,
    "agent_snapshot": READ_ONLY,
    # -- shells: arbitrary code on the host --------------------------------
    "powershell_exec": NORMAL_CHANGE,
    "shell_exec": NORMAL_CHANGE,
    # -- filesystem (read paths only; there is no write tool) --------------
    "fs_list": READ_ONLY,
    "fs_search": READ_ONLY,
    "fs_read": READ_ONLY,
    "fs_disk_usage": READ_ONLY,
    # -- packages ----------------------------------------------------------
    "winget_list": READ_ONLY,
    "winget_install": NORMAL_CHANGE,
    "winget_uninstall": NORMAL_CHANGE,
    # Updating an already-installed package is the routine, reversible half of
    # the package family; installing or removing software is not.
    "winget_update": STANDARD_CHANGE,
    # -- diagnostics -------------------------------------------------------
    "diag_processes": READ_ONLY,
    "diag_services": READ_ONLY,
    "diag_eventlog": READ_ONLY,
    "diag_autostart": READ_ONLY,
    # -- network -----------------------------------------------------------
    "net_config": READ_ONLY,
    "net_dns_flush": STANDARD_CHANGE,
    "net_adapter_reset": STANDARD_CHANGE,
    # -- screen / remote help ----------------------------------------------
    "screen_capture": READ_ONLY,
    "remotehelp_status": READ_ONLY,
    # Opening/closing Quick Assist on the user's desktop is visible and
    # trivially undone, but it is mutating on the agent (control.rs::is_mutating)
    # and must never be auto-invoked (ADR-0021).
    "remotehelp_start": STANDARD_CHANGE,
    "remotehelp_stop": STANDARD_CHANGE,
    # -- telemetry / agent lifecycle ---------------------------------------
    "telemetry_collect": READ_ONLY,
    "agent_update": NORMAL_CHANGE,
    # -- parental controls (ADR-0024) --------------------------------------
    "webfilter_status": READ_ONLY,
    "webfilter_apply": NORMAL_CHANGE,
    "webfilter_clear": NORMAL_CHANGE,
    "webfilter_get": READ_ONLY,
    # webfilter_set changes *what* is enforced; webfilter_push only ships the
    # already-configured list to the host, which is the routine half.
    "webfilter_set": NORMAL_CHANGE,
    "webfilter_push": STANDARD_CHANGE,
    "web_activity_query": READ_ONLY,
    # -- reliability alarm suppression (ADR-0041) --------------------------
    "reliability_suppression_list": READ_ONLY,
    "reliability_suppression_add": NORMAL_CHANGE,
    "reliability_suppression_remove": NORMAL_CHANGE,
    # -- auto-ticket rules (ticket_rules.py) --------------------------------
    "ticket_rule_list": READ_ONLY,
    "ticket_rule_set": NORMAL_CHANGE,
    "ticket_rule_remove": NORMAL_CHANGE,
    # -- account governance (ADR-0042) -------------------------------------
    # Deciding who may sign in to a family PC — and being able to lock the
    # household out by getting it wrong — is never routine.
    "account_set_enabled": NORMAL_CHANGE,
    "account_set_admin": NORMAL_CHANGE,
    "account_set_logon_rights": NORMAL_CHANGE,
    "account_create": NORMAL_CHANGE,
    "account_delete": NORMAL_CHANGE,
    "account_session_action": NORMAL_CHANGE,
    "password_policy_set": NORMAL_CHANGE,
    # -- unprompted triage (kenny_server/triage.py) -------------------------
    # Records the verdict of an unprompted investigation and, when the server's
    # own preconditions hold, resolves the ticket. STANDARD_CHANGE, not
    # NORMAL_CHANGE, and the distinction is load-bearing: a normal change holds
    # for an operator, and an unprompted triage session has no operator to
    # answer the hold -- the ticket would simply stall on
    # ``blocked_on="operator"``. It earns the "routine, reversible,
    # low-blast-radius" reading on its own terms: it moves a ticket to
    # ``resolved``, never to ``closed``, and ``resolved`` carries a reopen
    # window (``tickets.auto_close_resolved``) plus a ``resolved -> in_progress``
    # transition any requester or operator may make.
    "ticket_triage_verdict": STANDARD_CHANGE,
}

#: Every tool that only observes. Derived, never hand-listed, so a tool added to
#: :data:`TOOL_CLASSES` is in or out of this set by its own tier and cannot be
#: forgotten here. This is what bounds an unprompted triage session: it is
#: handed exactly these names plus its own verdict tool, so a change-tier call
#: is not merely refused at the gate -- it is never in the schemas to attempt.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    name for name, tier in TOOL_CLASSES.items() if tier == READ_ONLY
)

# Tools whose *invocation* touches someone's privacy or needs the person at the
# keyboard to know: looking at their screen, opening a remote-help session on
# their desktop, reading their files, listing the sites they visited. Orthogonal
# to the tier — ``screen_capture`` is read-only and still sensitive.
SENSITIVE_TOOLS: frozenset[str] = frozenset(
    {
        "screen_capture",
        "remotehelp_start",
        "fs_read",
        "web_activity_query",
    }
)

# Tools whose *results* must never be echoed verbatim to an external chat
# surface: they carry screen pixels, file contents, event-log text or browsing
# history off the host. A surface that renders tool output outside the operator
# dashboard has to summarise or suppress these, never paste them.
REDACTED_OUTPUT: frozenset[str] = frozenset(
    {
        "screen_capture",
        "fs_read",
        "fs_search",
        "web_activity_query",
        "diag_eventlog",
    }
)

# Named tool allowlists. A profile only ever *narrows* what a caller may reach —
# it grants nothing on its own, and it is not a gate: a tool inside a profile is
# still subject to whatever confirmation its surface applies to its tier.
#
# ``self-service-basic`` is deliberately free of any ``normal_change`` tool: the
# household member fixing their own PC can look, collect telemetry, flush DNS and
# open a remote-help session, and nothing else.
_SELF_SERVICE_BASIC: frozenset[str] = frozenset(
    {
        "list_agents",
        "select_agent",
        "agent_health",
        "agent_snapshot",
        "telemetry_collect",
        "diag_processes",
        "diag_services",
        "net_config",
        "winget_list",
        "fs_disk_usage",
        "webfilter_status",
        "remotehelp_status",
        "remotehelp_start",
        "remotehelp_stop",
        "net_dns_flush",
    }
)

# The household's technical member: full diagnostics, files, screen and package
# management — but no shell, no agent update, no account governance, no changes
# to what the server itself enforces, and no browsing history.
_POWER_USER: frozenset[str] = _SELF_SERVICE_BASIC | frozenset(
    {
        "fleet_overview",
        "diag_eventlog",
        "diag_autostart",
        "fs_list",
        "fs_search",
        "fs_read",
        "screen_capture",
        "net_adapter_reset",
        "winget_install",
        "winget_uninstall",
        "winget_update",
        "webfilter_get",
        "reliability_suppression_list",
    }
)

PROFILES: dict[str, frozenset[str]] = {
    "self-service-basic": _SELF_SERVICE_BASIC,
    "power-user": _POWER_USER,
    "operator": frozenset(TOOL_CLASSES),
}


def classify(tool: str) -> str:
    """Return the tier of ``tool``; an unknown name fails closed.

    A name absent from :data:`TOOL_CLASSES` is reported as
    :data:`NORMAL_CHANGE` — the most consequential tier — so a tool that reaches
    a catalog without being classified here is never treated as harmless.
    """

    return TOOL_CLASSES.get(tool, NORMAL_CHANGE)


def is_state_changing(tool: str) -> bool:
    """True if ``tool`` changes state (either change tier), false if read-only."""

    return classify(tool) != READ_ONLY


def profile_allows(profile: str | None, tool: str) -> bool:
    """May a caller on ``profile`` reach ``tool``?

    ``None`` means "no profile in force" and allows everything — the current
    behaviour of every existing surface. An *unknown* profile name allows
    nothing (fail closed): a typo must not silently widen access.
    """

    if profile is None:
        return True
    return tool in PROFILES.get(profile, frozenset())

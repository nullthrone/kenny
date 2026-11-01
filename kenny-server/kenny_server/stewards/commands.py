"""A person starts a steward now: command runs (ADR-0073 rule 3).

A command run is a :meth:`~kenny_server.stewards.runner.StewardRunner.run_generic`
run that a superuser started from the dashboard. The person who clicks is the
consent in time, so the run carries no ``act_until``; everything else that
bounds an unattended run bounds it too — the steward's tools and constraints,
its budget, the global caps, and, for a ``normal_change``, a standing
authorization that the gate consumes as for any other run. Its trigger is
``command:<username>`` (:data:`COMMAND_TRIGGER_PREFIX`), and the circuit breaker
does not read it (``runner.BREAKER_TRIGGER_PREFIXES``): a person's run neither
counts toward a streak nor ends one.

:func:`act_refusal` is the one list of what refuses a run that may act, in this
order, the first failure winning (the caller's credential, check 1, is
:func:`caller_refusal`):

2. a steward that runs on a ticket's own surface (triage) — 400;
3. its command trigger off in the configuration in force — 400;
4. the global steward switch off, or no AI to run it — 409;
5. the steward not in ``act`` — 409;
6. the effective hash the person was shown missing (400) or no longer the
   live one (409);
7. the target: a host-bound steward takes one known machine — among the
   steward's ``hosts`` when its spec declares that parameter, an empty list
   admitting none — that is online (409 if not) and runs an OS that serves its
   tools; a server-only steward takes none; then the spec's own preconditions
   (409);
8. a manual run of this steward on this target already in flight — a preview
   or a command run, at most one of either — and
   :data:`COMMANDS_PER_DAY_SETTING` command runs of this steward in the last
   24 hours (both 409).

The readiness view (:func:`readiness`) asks the same list with the hash
supplied, so what the dashboard offers is what the route accepts.

A manual run shares the runner's preview bookkeeping on purpose: the
one-in-flight set (``_preview_targets``), the admission lock that makes the
daily count and that set atomic with opening the row, and the task set the
server cancels on shutdown (``cancel_previews``) and tests wait on
(``wait_previews``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..tool_classes import NORMAL_CHANGE, READ_ONLY, TOOL_CLASSES, classify
from ..tools import CAPABILITY_TOOLS
from .policy import HOST_ARG, host_bound, run_target_problem
from .runner import StewardRunner, surface_refusal
from .spec import StewardSpec, resolve
from .store import StewardRun
from .windows import declared_hosts, host_supports

__all__ = [
    "COMMANDS_PER_DAY_SETTING",
    "COMMAND_TRIGGER_PREFIX",
    "COMPUTED_AT_RUN_START",
    "CommandRefused",
    "act_refusal",
    "caller_refusal",
    "command_actions",
    "readiness",
    "start_command",
]

logger = logging.getLogger("kenny.stewards.commands")

#: The ``trigger`` of a command run is this followed by the username of the
#: person who started it.
COMMAND_TRIGGER_PREFIX = "command:"

#: How many command runs of one steward may start in any 24 hours.
COMMANDS_PER_DAY_SETTING = "KENNY_STEWARDS_COMMANDS_PER_DAY"

#: What the readiness view shows for a constraint whose values server code
#: computes when the run starts (ADR-0072 rule 6): they do not exist yet.
COMPUTED_AT_RUN_START = "computed at run start"

#: Longest username kept in a command run's ``trigger``.
_MAX_USERNAME = 64

#: The parameter naming the machines a host-bound steward may work on.
_HOSTS_PARAM = "hosts"


class CommandRefused(ValueError):
    """A command run that must not start; ``status`` is the HTTP status, ``code`` the reason."""

    def __init__(
        self, message: str, *, status: int, code: str, run_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        #: The ``skipped`` row a global cap left on the record, if one did.
        self.run_id = run_id


@dataclass(frozen=True)
class Hosts:
    """What the server knows about machines: every id it has seen, and the live registry."""

    known: frozenset[str]
    #: ``registry.get(host_id) -> Agent | None`` (``online``, ``os``).
    registry: Any


def caller_refusal(*, person: bool, role: str | None) -> CommandRefused | None:
    """Check 1: only a superuser signed in to the dashboard starts a run that may act."""

    if person and role == "superuser":
        return None
    return CommandRefused(
        "starting a run that may act takes a superuser signed in to the dashboard; "
        "API tokens, OAuth tokens, the shared operator token and operators cannot "
        "(an operator may start a preview)",
        status=403,
        code="not_a_superuser_at_a_browser",
    )


async def _target_problem(
    runner: StewardRunner, spec: StewardSpec, host_id: str | None, hosts: Hosts
) -> CommandRefused | None:
    """Check 7: whether ``host_id`` is a machine this command run may work on now."""

    problem = run_target_problem(spec, host_id, hosts.known)
    if problem is not None:
        return CommandRefused(problem, status=400, code="bad_target")
    params = await runner.get_params(spec.id)
    if host_bound(spec) and host_id is not None:
        agent = hosts.registry.get(host_id) if hosts.registry is not None else None
        if agent is None or not getattr(agent, "online", False):
            return CommandRefused(
                f"{host_id} is offline; start the run when it is back",
                status=409,
                code="host_offline",
            )
        os_name = getattr(agent, "os", None)
        if not host_supports(spec, os_name):
            return CommandRefused(
                f"{spec.id} does not run on {os_name or 'this'} machines",
                status=400,
                code="os_unsupported",
            )
        if _HOSTS_PARAM in spec.params:
            named = declared_hosts(params)
            if not named:
                return CommandRefused(
                    f"{spec.id} names no machine in its hosts; an empty list is never all",
                    status=400,
                    code="no_hosts",
                )
            if host_id not in named:
                return CommandRefused(
                    f"{host_id} is not among {spec.id}'s hosts",
                    status=400,
                    code="host_not_named",
                )
    refused = await runner.precondition_refusal(spec, host_id, params)
    if refused is not None:
        return CommandRefused(refused, status=409, code="precondition")
    return None


async def _manual_run_problem(
    runner: StewardRunner, spec: StewardSpec, host_id: str | None
) -> CommandRefused | None:
    """Check 8: one manual run per steward and target at a time, and the daily limit."""

    if (spec.id, host_id) in runner._preview_targets:
        where = f" on {host_id}" if host_id else ""
        return CommandRefused(
            f"a run of {spec.id}{where} started by a person is already running; wait for it",
            status=409,
            code="manual_run_running",
        )
    limit = max(1, int(runner.settings.get(COMMANDS_PER_DAY_SETTING)))
    since = (runner.now() - timedelta(hours=24)).isoformat()
    started = await runner.store.count_runs_since(
        spec.id, prefix=COMMAND_TRIGGER_PREFIX, since_iso=since
    )
    if started >= limit:
        return CommandRefused(
            f"{spec.id} was started by command {started} times in the last 24 hours; "
            f"the limit is {limit}",
            status=409,
            code="command_limit",
        )
    return None


async def act_refusal(
    runner: StewardRunner,
    spec: StewardSpec,
    *,
    host_id: str | None,
    effective_hash: Any,
    hosts: Hosts,
    check_hash: bool = True,
) -> CommandRefused | None:
    """Why a command run of ``spec`` that may act must not start now (checks 2-8), or ``None``.

    ``check_hash=False`` skips check 6, for the readiness view that supplies
    the hash itself.
    """

    why = surface_refusal(spec)
    if why is not None:
        return CommandRefused(why, status=400, code="not_commandable")
    if not (await runner.trigger_config(spec)).get("command"):
        return CommandRefused(
            f"command trigger off: {spec.id} is not configured to be started by a person",
            status=400,
            code="command_off",
        )
    if not runner.enabled():
        return CommandRefused("stewards are switched off", status=409, code="stewards_off")
    if not runner._ai_ready():
        return CommandRefused(
            "AI is switched off or not configured", status=409, code="ai_unavailable"
        )
    mode = await runner.mode_of(spec.id)
    if mode != "act":
        return CommandRefused(
            f"steward is in {mode}; use preview or promote",
            status=409,
            code="not_in_act",
        )
    if check_hash:
        if not isinstance(effective_hash, str) or not effective_hash:
            return CommandRefused(
                "effective_hash is required: the steward's effective_hash as it was shown",
                status=400,
                code="hash_missing",
            )
        live = await runner.live_hash(spec.id)
        if effective_hash != live:
            return CommandRefused(
                f"steward {spec.id} changed since it was shown (effective hash "
                f"{effective_hash[:12]} is now {str(live)[:12]}); review it again",
                status=409,
                code="hash_stale",
            )
    refused = await _target_problem(runner, spec, host_id, hosts)
    if refused is not None:
        return refused
    return await _manual_run_problem(runner, spec, host_id)


def _brief(spec: StewardSpec, host_id: str | None, params: Mapping[str, Any]) -> str:
    """The one message that starts a command run: facts, not limits (the gate holds those)."""

    where = f' on the machine "{host_id}"' if host_id else " on the server; no machine is involved"
    lines = [
        f"Run of the {spec.title.lower()}{where}, started now by a person from the dashboard. "
        "Work as you normally would; the server decides which changes may run."
    ]
    if "packages" in spec.params:
        raw = params.get("packages")
        allowed = (
            sorted({p for p in raw if isinstance(p, str) and p})
            if isinstance(raw, (list, tuple, set, frozenset))
            else []
        )
        if allowed:
            lines.append("Package ids you may update: " + ", ".join(allowed) + ".")
        else:
            lines.append("No package is allowed to be updated; update nothing.")
    return "\n".join(lines)


async def start_command(
    runner: StewardRunner,
    spec: StewardSpec,
    *,
    host_id: str | None,
    username: str,
    effective_hash: Any,
    hosts: Hosts,
) -> StewardRun:
    """Start one command run of ``spec`` in the background; return its row once admitted.

    Checks 2-8 are asked again under the runner's manual-run admission lock,
    held until the row exists, so two requests cannot both pass the daily limit
    or the one-in-flight rule. Raises :class:`CommandRefused` for a run that must
    not start, including one a global cap skipped (its ``skipped`` row is on
    the record, and named by ``run_id`` on the exception) and one that could not
    start at all (a switch went off in between); :class:`RuntimeError` when no
    executor, model or client is configured. Who may call this (check 1) is the
    caller's to enforce.
    """

    who = username.strip()[:_MAX_USERNAME] or "unknown"
    key = (spec.id, host_id)
    async with runner._preview_admission:
        refused = await act_refusal(
            runner, spec, host_id=host_id, effective_hash=effective_hash, hosts=hosts
        )
        if refused is not None:
            raise refused
        trigger = f"{COMMAND_TRIGGER_PREFIX}{who}"
        brief = _brief(spec, host_id, await runner.get_params(spec.id))
        loop = asyncio.get_running_loop()
        admitted: asyncio.Future[StewardRun | None] = loop.create_future()

        def on_admitted(run: StewardRun) -> None:
            if not admitted.done():
                admitted.set_result(run)

        async def go() -> None:
            try:
                await runner.run_generic(
                    spec,
                    host_id=host_id,
                    trigger=trigger,
                    brief=brief,
                    on_admitted=on_admitted,
                    initiated_by=who,
                )
            except Exception as exc:  # noqa: BLE001 - reported to the caller if still waiting
                logger.exception("steward %s command run failed to start", spec.id)
                if not admitted.done():
                    admitted.set_exception(exc)
            finally:
                runner._preview_targets.discard(key)
                if not admitted.done():
                    admitted.set_result(None)

        runner._preview_targets.add(key)
        task = asyncio.create_task(go(), name=f"steward-command-{spec.id}")
        runner._previews.add(task)
        task.add_done_callback(runner._previews.discard)
        run = await admitted
    if run is None:
        raise CommandRefused(
            "the steward could not start (stewards or AI are switched off, or it left act)",
            status=409,
            code="not_started",
        )
    if run.status == "skipped":
        raise CommandRefused(
            run.error or "a global cap refused the run", status=409, code="over_cap", run_id=run.id
        )
    await runner._record_event(
        spec.id,
        f"steward {spec.id}: command run started by {who} on {host_id or 'the server'}",
        {"actor": who, "run_id": run.id, "host_id": host_id, "trigger": trigger},
    )
    return run


# -- what a person sees before starting one ---------------------------------------


def _tool_target(tool: str, host_id: str | None) -> str | None:
    """Where ``tool`` acts in a run on ``host_id``: the host, or ``None`` for the server."""

    return host_id if tool in CAPABILITY_TOOLS or tool in HOST_ARG else None


async def _tools(
    runner: StewardRunner, spec: StewardSpec, host_id: str | None, live_hash: str | None
) -> list[dict[str, Any]]:
    """Each change-tier tool of ``spec``: its tier, the values a run may pass, its authorization."""

    resolved = resolve(spec, await runner.get_params(spec.id))
    out: list[dict[str, Any]] = []
    for tool in sorted(spec.tools):
        if tool == spec.verdict_tool or classify(tool) == READ_ONLY:
            continue
        values: dict[str, Any] = {}
        for c in sorted(resolved.constraints_for(tool), key=lambda c: c.arg):
            if c.evidence is not None:
                values[c.arg] = COMPUTED_AT_RUN_START
                continue
            admitted = sorted(c.allowed)
            prior = values.get(c.arg)
            if isinstance(prior, list):
                # Every constraint on an argument must hold: the values are their intersection.
                admitted = sorted(set(prior) & set(admitted))
            values[c.arg] = admitted
        tier = TOOL_CLASSES.get(tool)
        authorization: dict[str, Any] | None = None
        if tier == NORMAL_CHANGE and runner.authorizations is not None and live_hash:
            authorization = await runner.authorizations.remaining(
                spec.id, live_hash, tool, _tool_target(tool, host_id), runner.now()
            )
        out.append(
            {
                "tool": tool,
                "tier": tier,
                "values": values,
                "needs_authorization": tier == NORMAL_CHANGE,
                "authorization": authorization,
            }
        )
    return out


async def _preview_check(
    runner: StewardRunner, spec: StewardSpec, host_id: str | None, hosts: Hosts
) -> str | None:
    refused = await runner.preview_refusal(spec, host_id)
    if refused is not None:
        return str(refused)
    return run_target_problem(spec, host_id, hosts.known)


async def readiness(
    runner: StewardRunner,
    spec: StewardSpec,
    *,
    host_id: str | None,
    hosts: Hosts,
    person: bool,
    role: str | None,
) -> dict[str, Any]:
    """What a caller may start of ``spec`` on ``host_id`` now, and with what.

    ``run.allowed`` only when every check a command run that may act passes
    for this caller except the hash, which this view supplies
    (``effective_hash``) for the caller to send back.
    """

    live = await runner.live_hash(spec.id)
    preview_reason = await _preview_check(runner, spec, host_id, hosts)
    refused = caller_refusal(person=person, role=role) or await act_refusal(
        runner, spec, host_id=host_id, effective_hash=None, hosts=hosts, check_hash=False
    )
    return {
        "steward_id": spec.id,
        "host_id": host_id,
        "effective_hash": live,
        "mode": await runner.mode_of(spec.id),
        "preview": {"allowed": preview_reason is None, "reason": preview_reason},
        "run": {
            "allowed": refused is None,
            "reason": None if refused is None else str(refused),
            "code": None if refused is None else refused.code,
        },
        "tools": await _tools(runner, spec, host_id, live),
    }


async def command_actions(
    runner: StewardRunner,
    *,
    host_id: str | None,
    hosts: Hosts,
    person: bool,
    role: str | None,
    catalog: Iterable[StewardSpec] | None = None,
) -> list[dict[str, Any]]:
    """The stewards a person could start on ``host_id``: command trigger on, eligible for it.

    Eligible: not a ticket-surface steward, not ``off``, and ``host_id`` a
    target it takes — a known machine among its ``hosts`` (when it declares
    them) for a host-bound steward, no machine (``host_id`` ``None``) for a
    server-only one.
    """

    out: list[dict[str, Any]] = []
    for spec in catalog if catalog is not None else runner.catalog.values():
        if surface_refusal(spec) is not None:
            continue
        if not (await runner.trigger_config(spec)).get("command"):
            continue
        mode = await runner.mode_of(spec.id)
        if mode == "off":
            continue
        if run_target_problem(spec, host_id, hosts.known) is not None:
            continue
        if host_bound(spec) and _HOSTS_PARAM in spec.params:
            if host_id not in declared_hosts(await runner.get_params(spec.id)):
                continue
        preview_reason = await _preview_check(runner, spec, host_id, hosts)
        refused = caller_refusal(person=person, role=role) or await act_refusal(
            runner, spec, host_id=host_id, effective_hash=None, hosts=hosts, check_hash=False
        )
        out.append(
            {
                "steward_id": spec.id,
                "title": spec.title,
                "summary": spec.description,
                "mode": mode,
                "preview_allowed": preview_reason is None,
                "run_allowed": refused is None,
            }
        )
    return out

"""The one place a specialized agent's run is started (ADR-0071).

Every run passes the same doors in the same order, and each door is here rather
than in the agent so no agent can forget one:

1. **An agent's effect never starts another agent** (rule 6). A ticket whose
   origin is :data:`~kenny_server.ticketstore.AGENT_ORIGIN` starts nothing.
2. **The global switch** (``KENNY_AGENTS_ENABLED``) and **the agent's mode**.
   ``off`` either way means the run never existed: nothing is recorded.
3. **The global caps**, concurrent runs (``KENNY_AGENTS_MAX_CONCURRENT``) and
   tokens over the last 24 hours (``KENNY_AGENTS_DAILY_TOKENS``), checked
   before the model is called. A run a cap refuses *is* recorded, as
   ``skipped`` with the reason, because "kenny would have looked but was over
   budget" is something a person needs to be able to see. The concurrency cap
   bounds :meth:`AgentRunner.run_generic` runs only, counted in memory from
   admission to the run's ``finally`` so a run that fails, is cancelled or
   cannot close its row never keeps its slot. Triage is exempt from it: every
   new ticket is investigated, however many arrive together; the token cap
   applies to it like to every run.
4. **The run row** (``agent_runs``): opened ``running`` before the first model
   call, closed in a ``finally`` (retried once) with how it ended, its verdict,
   what it did and proposed, and what it cost.

Two ways in. :meth:`AgentRunner.on_ticket_created` starts the ``triage`` agent,
which keeps running on its ticket's own surface (``triage.TriageService``);
:meth:`AgentRunner.run_generic` drives any other agent through
:class:`~kenny_server.agents.policy.AgentPolicy` and the ordinary tool loop.

Triage's mode is not stored here. It is derived from the settings that already
switch it (``KENNY_TRIAGE_ENABLED`` and the AI availability behind it -> ``off``;
``KENNY_TRIAGE_RESOLVE`` off -> ``shadow``, on -> ``act``), so there is one
source of truth and nothing to migrate; :meth:`AgentRunner.set_mode` writes those
settings for it. Every other agent's mode is its ``agent_settings`` row, or the
spec's default while nobody has chosen one.

A run's mode is read when it starts and again before every change it makes:
the runner hands each run a live predicate (the global switch on and the
agent's mode ``act``) — the gate's ``still_acting`` for a generic run,
``TriageService.still_acting`` for triage's verdict — so demoting an agent or
switching every agent off reaches a run already in flight.

**``act`` binds to the effective hash** (ADR-0072 rule 6): the spec hash
combined with the agent's parameters
(:func:`~kenny_server.agents.spec.effective_hash`). Choosing ``act`` stores the
hash it was chosen at; an agent is in ``act`` only while that stored hash
equals the live one, computed from the catalog entry and the stored parameters
on every read. The first read that finds them apart drops the agent to
``shadow`` for good (a code rollback does not promote it again), records who
and why on the event log, and voids every authorization bound to another hash.
A parameter edit (:meth:`AgentRunner.set_params`) does the same at once. A
generic run freezes its parameters and effective hash at start, and its
``still_acting`` also turns false once the live hash moves away from the run's.

Triage is outside that binding: its mode is its settings, and it takes no
parameters.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..ticketstore import AGENT_ORIGIN, Ticket
from ..tool_classes import READ_ONLY, classify
from ..toolloop import ToolExecutor, UsageMeter, drive_events
from ..tools import redact_audit_args
from ..webfilter import DAY_KEYS, format_hhmm, parse_recurrence
from .authorizations import Authorization, AuthorizationStore
from .catalog import CATALOG, check_dispatchable
from .policy import AgentPolicy, AgentSession, Authorizer
from .spec import MODES, AgentSpec, effective_hash, resolve, validate
from .store import AgentRun, AgentStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..triage import TriageService

__all__ = [
    "DAILY_TOKENS_SETTING",
    "ENABLED_SETTING",
    "MAX_CONCURRENT_SETTING",
    "RUN_RETENTION_DAYS",
    "TICKET_CREATED_TRIGGER",
    "TRIAGE_AGENT_ID",
    "AgentRunner",
    "caused_by_agent",
]

logger = logging.getLogger("kenny.agents")

#: The catalog id of the agent that runs on a newly created ticket.
TRIAGE_AGENT_ID = "triage"

#: The ``trigger`` a ticket-creation run is recorded with.
TICKET_CREATED_TRIGGER = "event:ticket_created"

#: The global switch, and the two caps every run is checked against.
ENABLED_SETTING = "KENNY_AGENTS_ENABLED"
MAX_CONCURRENT_SETTING = "KENNY_AGENTS_MAX_CONCURRENT"
DAILY_TOKENS_SETTING = "KENNY_AGENTS_DAILY_TOKENS"

#: The settings triage's mode is derived from and written to.
_TRIAGE_ENABLED = "KENNY_TRIAGE_ENABLED"
_TRIAGE_RESOLVE = "KENNY_TRIAGE_RESOLVE"

#: How long a finished run stays on the record.
RUN_RETENTION_DAYS = 90

#: Ceiling on the free text a run row carries (summary, error).
_MAX_TEXT = 2000

#: What a run that left without finishing (cancelled, interrupted) is closed with.
_INTERRUPTED = "the run was interrupted before it finished"

#: The actor recorded when the server, not a person, drops an agent to shadow.
_SYSTEM_ACTOR = "system"

#: The parameter a ``schedule`` trigger reads its maintenance window from.
WINDOW_PARAM = "window"

#: Ceiling on one list parameter's length and on one value's length.
_MAX_PARAM_VALUES = 200
_MAX_PARAM_VALUE_LEN = 200


def validate_params(spec: AgentSpec, params: Any) -> dict[str, Any]:
    """``params`` checked against what ``spec`` declares, normalised; else :class:`ValueError`.

    Keys must be among ``spec.params``. ``window`` is a mapping
    ``{days, start, end[, tz]}`` validated by the web filter's own parser
    (:func:`~kenny_server.webfilter.parse_recurrence`) and stored normalised
    (``days`` as weekday keys, times as ``HH:MM``, the zone filled in). Every
    other parameter is a list of non-empty strings, deduplicated and sorted, so
    the same set always yields the same effective hash. An empty list is kept:
    it admits nothing.
    """

    if not isinstance(params, Mapping):
        raise ValueError("params must be an object")
    unknown = sorted(str(k) for k in params if k not in spec.params)
    if unknown:
        raise ValueError(f"agent {spec.id} takes no parameter(s) {', '.join(unknown)}")
    out: dict[str, Any] = {}
    for name, value in params.items():
        if name == WINDOW_PARAM:
            if not isinstance(value, Mapping):
                raise ValueError("window must be an object with days, start and end")
            extra = sorted(str(k) for k in value if k not in ("days", "start", "end", "tz"))
            if extra:
                raise ValueError(f"window takes no field(s) {', '.join(extra)}")
            days, start_min, end_min, zone = parse_recurrence(
                days=value.get("days"),
                start=value.get("start"),
                end=value.get("end"),
                tz=value.get("tz"),
            )
            out[name] = {
                "days": [DAY_KEYS[d] for d in days],
                "start": format_hhmm(start_min),
                "end": format_hhmm(end_min),
                "tz": zone,
            }
            continue
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{name} must be a list of values")
        if len(value) > _MAX_PARAM_VALUES:
            raise ValueError(f"{name} takes at most {_MAX_PARAM_VALUES} values")
        if any(not isinstance(v, str) or not v.strip() for v in value):
            raise ValueError(f"every value of {name} must be a non-empty string")
        if any(len(v) > _MAX_PARAM_VALUE_LEN for v in value):
            raise ValueError(f"a value of {name} is longer than {_MAX_PARAM_VALUE_LEN}")
        out[name] = sorted({v.strip() for v in value})
    return out


def _declared(spec: AgentSpec, params: Mapping[str, Any]) -> dict[str, Any]:
    """The stored parameters ``spec`` still declares; stray keys left behind."""

    return {name: params[name] for name in spec.params if name in params}


def _clip(value: Any, limit: int = _MAX_TEXT) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def caused_by_agent(ticket: Ticket) -> bool:
    """Whether ``ticket`` was opened by an agent's own effect (ADR-0071 rule 6).

    Such a ticket never starts an agent: otherwise a change that breaks a
    service raises an alert, opens a ticket and starts the next run.
    """

    return ticket.origin == AGENT_ORIGIN


def _redacted(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{**entry, "args": redact_audit_args(entry.get("args") or {})} for entry in entries]


def _with_outcomes(
    actions: Sequence[Mapping[str, Any]], results: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Each allowed change, with whether it then actually ran (``ok``/``code``).

    The gate records an action when it allows a call; the loop's ``tool_result``
    for that call follows it. Paired in order, by tool and arguments.
    """

    out = [dict(a) for a in actions]
    for result in results:
        tool, args = result.get("tool"), result.get("args")
        candidates = [a for a in out if "ok" not in a and a.get("tool") == tool]
        match = next((a for a in candidates if a.get("args") == args), None)
        if match is None and candidates:
            match = candidates[0]
        if match is None:
            continue
        match["ok"] = bool(result.get("ok"))
        error = result.get("error")
        if isinstance(error, Mapping) and error.get("code"):
            match["code"] = str(error["code"])
    return out


class AgentRunner:
    """Starts, bounds and records every run of every specialized agent.

    ``ai_access`` is the :class:`~kenny_server.ai.AiAccess` the rest of the
    server asks whether a model may be called; ``triage`` is the service the
    ``triage`` agent runs through (``None`` while no assistant could be built).
    ``catalog`` defaults to the shipped one; tests pass their own.
    """

    def __init__(
        self,
        *,
        store: AgentStore,
        settings: Any,
        ai_access: Any = None,
        triage: TriageService | None = None,
        catalog: Mapping[str, AgentSpec] = CATALOG,
        event_store: Any = None,
        now: Callable[[], datetime] | None = None,
        authorizations: AuthorizationStore | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.ai_access = ai_access
        self._triage: TriageService | None = None
        self.triage = triage
        self.catalog = catalog
        #: Where a mode change is recorded with who made it.
        self.event_store = event_store
        self._now = now or (lambda: datetime.now(timezone.utc))
        # Held across "count what is running" and "open the row", so two runs
        # starting together cannot both see room under the concurrency cap.
        self._admission = asyncio.Lock()
        #: Generic runs admitted and not yet past their ``finally``. In memory,
        #: not ``agent_runs``: a row a failed close left ``running`` must not
        #: hold a slot until the next restart.
        self._in_flight = 0
        #: The standing authorizations (ADR-0072) a run's ``normal_change`` is
        #: matched against. Without a store no ``normal_change`` ever runs.
        self.authorizations = authorizations
        #: What :meth:`run_generic` uses when its caller passes none
        #: (:meth:`configure`).
        self._executor: ToolExecutor | None = None
        self._client_factory: Callable[[], Any] | None = None
        self._model: str | Callable[[], str] | None = None

    def now(self) -> datetime:
        """The runner's clock (injected in tests); what expiry and budgets are judged by."""

        return self._now()

    def configure(
        self,
        *,
        executor: ToolExecutor | None = None,
        client_factory: Callable[[], Any] | None = None,
        model: str | Callable[[], str] | None = None,
    ) -> None:
        """Set what a run started without explicit ``client``/``model``/``executor`` uses.

        ``model`` may be a callable, read at each run start, so a live setting
        (``KENNY_CHAT_MODEL``) reaches the next run. Arguments left ``None``
        keep what was configured before.
        """

        if executor is not None:
            self._executor = executor
        if client_factory is not None:
            self._client_factory = client_factory
        if model is not None:
            self._model = model

    @property
    def triage(self) -> TriageService | None:
        """The service the ``triage`` agent runs through, wired to this runner's mode."""

        return self._triage

    @triage.setter
    def triage(self, service: TriageService | None) -> None:
        self._triage = service
        if service is not None:
            service.still_acting = self._triage_still_acting

    def _triage_still_acting(self) -> bool:
        """Whether a triage verdict may still resolve: switch on, triage in ``act``."""

        return self.enabled() and self._triage_mode() == "act"

    # -- lifecycle -----------------------------------------------------------

    async def startup(self) -> None:
        """Account for runs a dead process left open; drop runs past retention."""

        interrupted = await self.store.fail_interrupted()
        if interrupted:
            logger.warning("%d agent run(s) were interrupted by a restart", interrupted)
        cutoff = self._now() - timedelta(days=RUN_RETENTION_DAYS)
        await self.store.prune(cutoff.isoformat())
        # A new release can change what an agent is (its spec, a tool's tier).
        # Unbind what was granted against the old one before anything runs,
        # whatever mode the agent is in, so a later rollback revives nothing.
        for spec in self.catalog.values():
            live = await self.live_hash(spec.id)
            if live is not None:
                await self._void_others(spec.id, live, _SYSTEM_ACTOR)
            await self._mode_for(spec)

    # -- modes ---------------------------------------------------------------

    def enabled(self) -> bool:
        """The global switch: off means no agent starts."""

        return bool(self.settings.get(ENABLED_SETTING))

    def spec(self, agent_id: str) -> AgentSpec:
        """The catalog spec for ``agent_id``; :class:`KeyError` if there is none."""

        spec = self.catalog.get(agent_id)
        if spec is None:
            raise KeyError(agent_id)
        return spec

    def _triage_mode(self) -> str:
        if self.ai_access is not None:
            on = bool(self.ai_access.enabled("triage"))
        else:
            on = bool(self.settings.get(_TRIAGE_ENABLED))
        if not on:
            return "off"
        return "act" if self.settings.get(_TRIAGE_RESOLVE) else "shadow"

    async def live_hash(self, agent_id: str) -> str | None:
        """What ``agent_id`` is right now: its catalog spec and stored parameters.

        ``None`` for an id the catalog no longer has.
        """

        spec = self.catalog.get(agent_id)
        if spec is None:
            return None
        params = {} if spec.id == TRIAGE_AGENT_ID else await self.store.get_params(spec.id)
        return effective_hash(spec, params)

    async def _mode_for(self, spec: AgentSpec) -> str:
        if spec.id == TRIAGE_AGENT_ID:
            return self._triage_mode()
        stored = await self.store.get_mode(spec.id)
        mode = stored if stored in MODES else spec.default_mode
        if mode != "act":
            return mode
        live = await self.live_hash(spec.id)
        if live is not None and await self.store.get_act_hash(spec.id) == live:
            return "act"
        # ``act`` was chosen for something this agent no longer is. Exactly one
        # caller wins the conditional demotion and records it.
        if await self.store.demote_unbound(spec.id, live or "", actor=_SYSTEM_ACTOR):
            logger.warning(
                "agent %s: its effective hash changed since act was chosen; dropped to shadow",
                spec.id,
            )
            await self._record_event(
                spec.id,
                f"agent {spec.id}: mode set to shadow by {_SYSTEM_ACTOR}: "
                "its effective hash changed since act was chosen",
                {"mode": "shadow", "actor": _SYSTEM_ACTOR, "effective_hash": live},
                level="warning",
            )
            if live is not None:
                await self._void_others(spec.id, live, _SYSTEM_ACTOR)
        return "shadow"

    async def _void_others(self, agent_id: str, live: str, actor: str) -> int:
        """Void ``agent_id``'s authorizations bound to any hash but ``live``; record it."""

        if self.authorizations is None:
            return 0
        voided = await self.authorizations.void_other_hashes(agent_id, live, actor)
        if voided:
            await self._record_event(
                agent_id,
                f"agent {agent_id}: {voided} authorization(s) voided by {actor}: "
                "bound to what the agent no longer is",
                {"voided": voided, "actor": actor, "effective_hash": live},
                level="warning",
            )
        return voided

    async def mode_of(self, agent_id: str) -> str:
        """The mode ``agent_id`` runs in now; :class:`KeyError` for an unknown id."""

        return await self._mode_for(self.spec(agent_id))

    async def set_mode(self, agent_id: str, mode: str, *, actor: str) -> str:
        """Choose ``agent_id``'s mode; returns the mode now in force.

        For ``triage`` this writes its settings through the same
        :meth:`~kenny_server.config.Settings.set` the dashboard's settings page
        uses, so their apply-hooks rebind it at once; ``KENNY_TRIAGE_RESOLVE``
        is written before ``KENNY_TRIAGE_ENABLED`` so a ticket created between
        the two writes never runs in the mode being left, and ``off`` writes
        ``KENNY_TRIAGE_RESOLVE`` off as well. The mode in force can
        differ from ``mode``: triage chosen ``shadow`` stays ``off`` while no AI
        key or gateway is configured.

        Raises :class:`KeyError` for an unknown agent, :class:`ValueError` for
        an unknown mode.
        """

        spec = self.spec(agent_id)
        if mode not in MODES:
            raise ValueError(f"unknown agent mode {mode!r}; expected one of {', '.join(MODES)}")
        if spec.id == TRIAGE_AGENT_ID:
            # Off clears resolve too, so a run already in flight cannot resolve
            # at its verdict, and turning triage back on starts it in shadow.
            await self.settings.set(_TRIAGE_RESOLVE, "1" if mode == "act" else "0")
            await self.settings.set(_TRIAGE_ENABLED, "0" if mode == "off" else "1")
        else:
            # ``act`` is chosen for what the agent is now; it binds to that.
            bound = await self.live_hash(spec.id) if mode == "act" else None
            await self.store.set_mode(spec.id, mode, actor=actor, act_hash=bound)
        await self._record_mode_change(spec.id, mode, actor)
        return await self._mode_for(spec)

    async def _record_mode_change(self, agent_id: str, mode: str, actor: str) -> None:
        """Put who chose which mode on the event log.

        For every agent alike: triage's settings rows carry no author, and an
        ``agent_settings`` row keeps only the latest choice.
        """

        await self._record_event(
            agent_id,
            f"agent {agent_id}: mode set to {mode} by {actor}",
            {"mode": mode, "actor": actor},
        )

    async def _record_event(
        self, agent_id: str, message: str, fields: Mapping[str, Any], *, level: str = "info"
    ) -> None:
        """One row on the event log about ``agent_id``; losing it never undoes the change."""

        if self.event_store is None:
            logger.info(message)
            return
        try:
            await self.event_store.insert_log(
                source="server",
                at=self._now().isoformat(),
                level=level,
                target=logger.name,
                message=message,
                fields={"agent": agent_id, **fields},
            )
        except Exception:  # noqa: BLE001 - the change is made; losing its record must not undo it
            logger.warning("failed to record a change of agent %s", agent_id, exc_info=True)

    # -- parameters ----------------------------------------------------------

    async def get_params(self, agent_id: str) -> dict[str, Any]:
        """``agent_id``'s parameters as its spec declares them; :class:`KeyError` if unknown."""

        spec = self.spec(agent_id)
        if spec.id == TRIAGE_AGENT_ID:
            return {}
        return _declared(spec, await self.store.get_params(spec.id))

    async def set_params(
        self, agent_id: str, params: Mapping[str, Any], *, actor: str
    ) -> dict[str, Any]:
        """Replace ``agent_id``'s parameters; returns ``{params, effective_hash, mode, voided}``.

        A change of the effective hash drops an agent in ``act`` to ``shadow``
        (in the same write as the parameters) and voids every authorization
        bound to another hash — first, so no attempt can be spent against what
        the agent used to be (ADR-0072 rule 6). Writing the parameters it
        already has changes nothing. Raises :class:`KeyError` for an unknown
        agent and :class:`ValueError` for parameters its spec does not take.
        """

        spec = self.spec(agent_id)
        clean = validate_params(spec, params)
        before_hash = await self.live_hash(spec.id)
        after_hash = effective_hash(spec, clean)
        changed = before_hash != after_hash
        voided = await self._void_others(spec.id, after_hash, actor) if changed else 0
        before_mode = await self.store.set_params(spec.id, clean, actor=actor, demote=changed)
        if changed:
            await self._record_event(
                spec.id,
                f"agent {spec.id}: parameters changed by {actor}",
                {"actor": actor, "params": clean, "effective_hash": after_hash},
            )
            if before_mode == "act":
                await self._record_event(
                    spec.id,
                    f"agent {spec.id}: mode set to shadow by {actor}: its parameters changed",
                    {"mode": "shadow", "actor": actor, "effective_hash": after_hash},
                )
        return {
            "params": clean,
            "effective_hash": after_hash,
            "mode": await self._mode_for(spec),
            "voided": voided,
        }

    # -- standing authorizations ---------------------------------------------

    def _authorizations(self) -> AuthorizationStore:
        if self.authorizations is None:
            raise RuntimeError("standing authorizations are not configured")
        return self.authorizations

    async def grant(
        self,
        agent_id: str,
        *,
        tool: str,
        scope: Any,
        max_attempts_per_day: Any,
        expires_at: Any,
        actor: str,
        note: str = "",
    ) -> Authorization:
        """Grant ``agent_id`` a standing authorization bound to its *current* effective hash.

        Raises :class:`KeyError` for an unknown agent and
        :class:`~kenny_server.agents.authorizations.AuthorizationError` for a
        grant the store refuses. Who may call this is the API's to enforce.
        """

        spec = self.spec(agent_id)
        live = await self.live_hash(spec.id)
        assert live is not None  # self.spec() found it in the catalog
        return await self._authorizations().grant(
            spec=spec,
            effective_hash=live,
            tool=tool,
            scope=scope,
            max_attempts_per_day=max_attempts_per_day,
            expires_at=expires_at,
            granted_by=actor,
            note=note,
            now=self._now(),
        )

    # -- what the API shows --------------------------------------------------

    async def overview(self) -> list[dict[str, Any]]:
        """Every catalog agent: its public spec, its mode and its latest run."""

        out: list[dict[str, Any]] = []
        for spec in self.catalog.values():
            latest = await self.store.list_runs(agent_id=spec.id, limit=1)
            mode = await self._mode_for(spec)
            live = await self.live_hash(spec.id)
            if spec.id == TRIAGE_AGENT_ID:
                # Triage's ``act`` is its settings, not a binding to a hash.
                bound: bool | None = None
            else:
                bound = mode == "act" and await self.store.get_act_hash(spec.id) == live
            out.append(
                {
                    **spec.to_public(),
                    "mode": mode,
                    "params": await self.get_params(spec.id),
                    "effective_hash": live,
                    "act_bound": bound,
                    "latest_run": latest[0].to_public() if latest else None,
                }
            )
        return out

    # -- admission -----------------------------------------------------------

    async def _cap_reason(self, *, counted: bool) -> str | None:
        """Why a run may not start now under the global caps, or ``None``.

        ``counted`` runs are bounded by the concurrency cap; every run by the
        token cap.
        """

        if counted:
            limit = max(1, int(self.settings.get(MAX_CONCURRENT_SETTING)))
            if self._in_flight >= limit:
                return f"{self._in_flight} agent run(s) already in flight; the limit is {limit}"
        cap = int(self.settings.get(DAILY_TOKENS_SETTING))
        if cap > 0:
            since = self._now() - timedelta(hours=24)
            spent = await self.store.tokens_since(since.isoformat())
            if spent >= cap:
                return f"agent runs spent {spent} tokens in the last 24 hours; the cap is {cap}"
        return None

    async def _admit(
        self,
        spec: AgentSpec,
        mode: str,
        *,
        trigger: str,
        subject: str | None,
        host_id: str | None,
        ticket_id: str | None = None,
        counted: bool,
        params: Mapping[str, Any] | None = None,
        run_hash: str | None = None,
    ) -> tuple[AgentRun, bool]:
        """Open the run row; ``(row, False)`` when a cap refused it (row ``skipped``).

        An admitted ``counted`` run holds a concurrency slot; the caller must
        :meth:`_release` it in a ``finally``.
        """

        async with self._admission:
            reason = await self._cap_reason(counted=counted)
            run = await self.store.start_run(
                agent_id=spec.id,
                spec_hash=spec.spec_hash,
                trigger=trigger,
                mode=mode,
                subject=subject,
                host_id=host_id,
                ticket_id=ticket_id,
                params=params,
                effective_hash=run_hash,
            )
            if reason is None:
                if counted:
                    self._in_flight += 1
                return run, True
            logger.info("agent %s run not started: %s", spec.id, reason)
            return await self.store.finish_run(run.id, status="skipped", error=reason), False

    def _release(self) -> None:
        """Give back the concurrency slot an admitted ``counted`` run held."""

        self._in_flight = max(0, self._in_flight - 1)

    async def _close(self, run_id: str, **outcome: Any) -> AgentRun | None:
        """Close the run row; one retry, then log and give up (``None``).

        Whatever happens here the run's slot is already free: the slot is held
        in memory, and a row left ``running`` is accounted for at the next
        :meth:`startup`.
        """

        for attempt in (1, 2):
            try:
                return await self.store.finish_run(run_id, **outcome)
            except Exception:  # noqa: BLE001 - the run is over either way
                if attempt == 2:
                    logger.exception("agent run %s could not be closed; left running", run_id)
                else:
                    logger.warning("agent run %s failed to close; retrying", run_id, exc_info=True)
        return None

    # -- triage: a new ticket ------------------------------------------------

    async def on_ticket_created(self, ticket: Ticket) -> None:
        """Start the ``triage`` agent on ``ticket``, best-effort.

        Registered with :meth:`~kenny_server.tickets.TicketService.set_triage`.
        Never raises: the ticket already exists and is the operator's to see,
        so a failure here costs the analysis and nothing else (ADR-0027's
        bargain, as ``TriageService.run`` strikes it).
        """

        try:
            await self._on_ticket_created(ticket)
        except Exception:  # noqa: BLE001 - a failed run must not cost the ticket
            logger.exception("agent run for ticket %s failed to start or finish", ticket.id)

    async def _on_ticket_created(self, ticket: Ticket) -> None:
        if caused_by_agent(ticket):
            logger.debug("ticket %s was opened by an agent; no agent starts on it", ticket.id)
            return
        if self.triage is None or TRIAGE_AGENT_ID not in self.catalog or not self.enabled():
            return
        spec = self.catalog[TRIAGE_AGENT_ID]
        mode = await self._mode_for(spec)
        if mode == "off":
            return
        if ticket.agent_id is None:
            # Nowhere to look, so no investigation and no run to record.
            return
        run, admitted = await self._admit(
            spec,
            mode,
            trigger=TICKET_CREATED_TRIGGER,
            subject=f"ticket:{ticket.id}",
            host_id=ticket.agent_id,
            ticket_id=ticket.id,
            counted=False,
        )
        if not admitted:
            return
        meter = UsageMeter()
        status, error, verdict = "failed", _INTERRUPTED, None
        try:
            verdict = await self.triage.investigate(
                ticket, run_id=run.id, usage=meter, resolve=mode == "act"
            )
            status, error = "completed", None
        except Exception as exc:  # noqa: BLE001 - recorded on the run, never raised
            logger.exception("triage run %s on ticket %s failed", run.id, ticket.id)
            status, error = "failed", _clip(exc) or type(exc).__name__
        finally:
            await self._close(
                run.id, status=status, verdict=verdict, usage=meter.to_dict(), error=error
            )

    # -- any other agent -----------------------------------------------------

    async def run_generic(
        self,
        spec: AgentSpec,
        *,
        host_id: str | None,
        trigger: str,
        brief: str,
        client: Any = None,
        model: str | None = None,
        executor: ToolExecutor | None = None,
        authorizer: Authorizer | None = None,
    ) -> AgentRun | None:
        """Run ``spec`` once on ``host_id`` through the agent gate.

        ``client``, ``model`` and ``executor`` default to what :meth:`configure`
        set; :class:`RuntimeError` if neither supplies one. ``authorizer``
        defaults to spending an attempt of a standing authorization bound to
        this run's effective hash (:meth:`AuthorizationStore.consume`); without
        an authorization store no ``normal_change`` runs.

        The run's parameters are read once at start, resolved into the spec its
        gate enforces (:func:`~kenny_server.agents.spec.resolve`), and stored on
        the run row with the effective hash they make; a parameter edit during
        the run does not reach its constraints, it ends its ``act``.

        Returns the finished run row — ``completed``, ``failed`` or a cap's
        ``skipped`` — or ``None`` when the run was never started because the
        global switch, the AI master switch or the agent's mode is off, or when
        its row could not be closed (logged). Never raises for a failure inside
        the run; it is recorded as ``failed``.

        Raises :class:`ValueError` (or :class:`~kenny_server.agents.spec.SpecError`)
        for a spec that must not run here: an invalid one, one that runs on a
        ticket's surface (that is :meth:`on_ticket_created`'s), one whose id is
        not in the catalog, or one that differs from its catalog entry's
        ``spec_hash``. Only a catalog spec runs: its mode is a superuser's
        choice, and a spec from anywhere else has no such choice behind it.
        """

        check_dispatchable(validate(spec))
        if spec.trigger.kind == "event" and spec.trigger.event == "ticket_created":
            raise ValueError(f"agent {spec.id} runs on a ticket's own surface, not run_generic")
        known = self.catalog.get(spec.id)
        if known is None:
            raise ValueError(f"agent {spec.id} is not in the catalog")
        if known.spec_hash != spec.spec_hash:
            raise ValueError(f"agent {spec.id}: spec differs from the catalog entry")
        executor = executor if executor is not None else self._executor
        if executor is None:
            raise RuntimeError("run_generic needs an executor; call configure() first")
        if model is None:
            model = self._model() if callable(self._model) else self._model
        if not model:
            raise RuntimeError("run_generic needs a model; call configure() first")
        if client is None and self._client_factory is None:
            raise RuntimeError("run_generic needs a client; call configure() first")
        if not self.enabled() or not self._ai_ready():
            return None
        # Parameters before the mode: if they change in between, the mode read
        # sees the newer hash and ``still_acting`` catches the difference.
        params = _declared(spec, await self.store.get_params(spec.id))
        run_hash = effective_hash(spec, params)
        mode = await self._mode_for(spec)
        if mode == "off":
            return None
        run, admitted = await self._admit(
            spec,
            mode,
            trigger=trigger,
            subject=f"host:{host_id}" if host_id else None,
            host_id=host_id,
            counted=True,
            params=params,
            run_hash=run_hash,
        )
        if not admitted:
            return run

        meter = UsageMeter()
        session: AgentSession | None = None
        status, error, summary, verdict = "failed", _INTERRUPTED, None, None
        change_results: list[dict[str, Any]] = []
        finished: AgentRun | None = None

        async def still_acting() -> bool:
            if not self.enabled() or await self._mode_for(spec) != "act":
                return False
            # Bound to what the agent was when this run started.
            return await self.live_hash(spec.id) == run_hash

        async def consume(
            _session: AgentSession, tool: str, _args: dict[str, Any], target: str | None
        ) -> Authorization | None:
            store = self.authorizations
            if store is None or await self.live_hash(spec.id) != run_hash:
                return None
            return await store.consume(
                agent_id=spec.id,
                effective_hash=run_hash,
                tool=tool,
                host_id=target,
                run_id=run.id,
                now=self._now(),
            )

        try:
            if client is None:
                assert self._client_factory is not None  # checked above
                client = self._client_factory()
            session = AgentSession(
                id=run.id, spec=resolve(spec, params), mode=mode, agent_id=host_id
            )
            session.usage = meter
            session.messages.append({"role": "user", "content": brief})
            policy = AgentPolicy(
                session,
                authorizer=authorizer if authorizer is not None else consume,
                still_acting=still_acting,
            )
            async for event in drive_events(
                session,
                executor,
                client=client,
                model=model,
                policy=policy,
                max_iterations=spec.budget.max_iterations,
            ):
                kind = event.get("type")
                if kind == "tool_result":
                    tool = str(event.get("tool") or "")
                    if tool == spec.verdict_tool:
                        said = (event.get("args") or {}).get("verdict")
                        if event.get("ok") and isinstance(said, str) and said.strip():
                            verdict = said.strip()[:200]
                    elif classify(tool) != READ_ONLY:
                        change_results.append(event)
                elif kind == "done":
                    summary = _clip(event.get("assistant_text"))
            status, error = "completed", None
        except Exception as exc:  # noqa: BLE001 - recorded on the run, never raised
            logger.exception("agent %s run %s failed", spec.id, run.id)
            status, error = "failed", _clip(exc) or type(exc).__name__
        finally:
            self._release()
            actions = session.actions if session is not None else []
            recommendations = session.recommendations if session is not None else []
            finished = await self._close(
                run.id,
                status=status,
                verdict=verdict,
                summary=summary,
                usage=meter.to_dict(),
                error=error,
                actions=_redacted(_with_outcomes(actions, change_results)),
                recommendations=_redacted(recommendations),
            )
        return finished

    def _ai_ready(self) -> bool:
        if self.ai_access is None:
            return True
        return bool(self.ai_access.master_on() and self.ai_access.available())

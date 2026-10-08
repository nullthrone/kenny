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
   budget" is something a person needs to be able to see.
4. **The run row** (``agent_runs``): opened ``running`` before the first model
   call, closed with how it ended, its verdict, what it did and proposed, and
   what it cost.

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
from .catalog import CATALOG, check_dispatchable
from .policy import AgentPolicy, AgentSession, Authorizer
from .spec import MODES, AgentSpec, validate
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
    ) -> None:
        self.store = store
        self.settings = settings
        self.ai_access = ai_access
        self.triage = triage
        self.catalog = catalog
        #: Where a mode change is recorded with who made it.
        self.event_store = event_store
        self._now = now or (lambda: datetime.now(timezone.utc))
        # Held across "count what is running" and "open the row", so two runs
        # starting together cannot both see room under the concurrency cap.
        self._admission = asyncio.Lock()

    # -- lifecycle -----------------------------------------------------------

    async def startup(self) -> None:
        """Account for runs a dead process left open; drop runs past retention."""

        interrupted = await self.store.fail_interrupted()
        if interrupted:
            logger.warning("%d agent run(s) were interrupted by a restart", interrupted)
        cutoff = self._now() - timedelta(days=RUN_RETENTION_DAYS)
        await self.store.prune(cutoff.isoformat())

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

    async def _mode_for(self, spec: AgentSpec) -> str:
        if spec.id == TRIAGE_AGENT_ID:
            return self._triage_mode()
        stored = await self.store.get_mode(spec.id)
        return stored if stored in MODES else spec.default_mode

    async def mode_of(self, agent_id: str) -> str:
        """The mode ``agent_id`` runs in now; :class:`KeyError` for an unknown id."""

        return await self._mode_for(self.spec(agent_id))

    async def set_mode(self, agent_id: str, mode: str, *, actor: str) -> str:
        """Choose ``agent_id``'s mode; returns the mode now in force.

        For ``triage`` this writes its settings through the same
        :meth:`~kenny_server.config.Settings.set` the dashboard's settings page
        uses, so their apply-hooks rebind it at once; ``KENNY_TRIAGE_RESOLVE``
        is written before ``KENNY_TRIAGE_ENABLED`` so a ticket created between
        the two writes never runs in the mode being left. The mode in force can
        differ from ``mode``: triage chosen ``shadow`` stays ``off`` while no AI
        key or gateway is configured.

        Raises :class:`KeyError` for an unknown agent, :class:`ValueError` for
        an unknown mode.
        """

        spec = self.spec(agent_id)
        if mode not in MODES:
            raise ValueError(f"unknown agent mode {mode!r}; expected one of {', '.join(MODES)}")
        if spec.id == TRIAGE_AGENT_ID:
            if mode != "off":
                await self.settings.set(_TRIAGE_RESOLVE, "1" if mode == "act" else "0")
            await self.settings.set(_TRIAGE_ENABLED, "0" if mode == "off" else "1")
        else:
            await self.store.set_mode(spec.id, mode, actor=actor)
        await self._record_mode_change(spec.id, mode, actor)
        return await self._mode_for(spec)

    async def _record_mode_change(self, agent_id: str, mode: str, actor: str) -> None:
        """Put who chose which mode on the event log.

        For every agent alike: triage's settings rows carry no author, and an
        ``agent_settings`` row keeps only the latest choice.
        """

        message = f"agent {agent_id}: mode set to {mode} by {actor}"
        if self.event_store is None:
            logger.info(message)
            return
        try:
            await self.event_store.insert_log(
                source="server",
                at=self._now().isoformat(),
                level="info",
                target=logger.name,
                message=message,
                fields={"agent": agent_id, "mode": mode, "actor": actor},
            )
        except Exception:  # noqa: BLE001 - the mode is set; losing its record must not undo it
            logger.warning("failed to record the mode change of agent %s", agent_id, exc_info=True)

    # -- what the API shows --------------------------------------------------

    async def overview(self) -> list[dict[str, Any]]:
        """Every catalog agent: its public spec, its mode and its latest run."""

        out: list[dict[str, Any]] = []
        for spec in self.catalog.values():
            latest = await self.store.list_runs(agent_id=spec.id, limit=1)
            out.append(
                {
                    **spec.to_public(),
                    "mode": await self._mode_for(spec),
                    "latest_run": latest[0].to_public() if latest else None,
                }
            )
        return out

    # -- admission -----------------------------------------------------------

    async def _cap_reason(self) -> str | None:
        """Why a run may not start now under the global caps, or ``None``."""

        limit = max(1, int(self.settings.get(MAX_CONCURRENT_SETTING)))
        running = await self.store.count_running()
        if running >= limit:
            return f"{running} agent run(s) already in flight; the limit is {limit}"
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
    ) -> tuple[AgentRun, bool]:
        """Open the run row; ``(row, False)`` when a cap refused it (row ``skipped``)."""

        async with self._admission:
            reason = await self._cap_reason()
            run = await self.store.start_run(
                agent_id=spec.id,
                spec_hash=spec.spec_hash,
                trigger=trigger,
                mode=mode,
                subject=subject,
                host_id=host_id,
                ticket_id=ticket_id,
            )
            if reason is None:
                return run, True
            logger.info("agent %s run not started: %s", spec.id, reason)
            return await self.store.finish_run(run.id, status="skipped", error=reason), False

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
        )
        if not admitted:
            return
        meter = UsageMeter()
        status, error, verdict = "completed", None, None
        try:
            verdict = await self.triage.investigate(
                ticket, run_id=run.id, usage=meter, resolve=mode == "act"
            )
        except Exception as exc:  # noqa: BLE001 - recorded on the run, never raised
            logger.exception("triage run %s on ticket %s failed", run.id, ticket.id)
            status, error = "failed", _clip(exc) or type(exc).__name__
        await self.store.finish_run(
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
        client: Any,
        model: str,
        executor: ToolExecutor,
        authorizer: Authorizer | None = None,
    ) -> AgentRun | None:
        """Run ``spec`` once on ``host_id`` through the agent gate.

        Returns the finished run row — ``completed``, ``failed`` or a cap's
        ``skipped`` — or ``None`` when the run was never started because the
        global switch, the AI master switch or the agent's mode is off. Never
        raises for a failure inside the run; it is recorded as ``failed``.

        Raises :class:`ValueError` (or :class:`~kenny_server.agents.spec.SpecError`)
        for a spec that must not run here: an invalid one, one that runs on a
        ticket's surface (that is :meth:`on_ticket_created`'s), or one whose id
        is in the catalog with a different ``spec_hash``.
        """

        check_dispatchable(validate(spec))
        if spec.trigger.kind == "event" and spec.trigger.event == "ticket_created":
            raise ValueError(f"agent {spec.id} runs on a ticket's own surface, not run_generic")
        known = self.catalog.get(spec.id)
        if known is not None and known.spec_hash != spec.spec_hash:
            raise ValueError(f"agent {spec.id}: spec differs from the catalog entry")
        if not self.enabled() or not self._ai_ready():
            return None
        mode = await self._mode_for(spec)
        if mode == "off":
            return None
        run, admitted = await self._admit(
            spec,
            mode,
            trigger=trigger,
            subject=f"host:{host_id}" if host_id else None,
            host_id=host_id,
        )
        if not admitted:
            return run

        session = AgentSession(id=run.id, spec=spec, mode=mode, agent_id=host_id)
        session.usage = UsageMeter()
        session.messages.append({"role": "user", "content": brief})
        status, error, summary, verdict = "completed", None, None, None
        change_results: list[dict[str, Any]] = []
        try:
            policy = AgentPolicy(session, authorizer=authorizer)
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
        except Exception as exc:  # noqa: BLE001 - recorded on the run, never raised
            logger.exception("agent %s run %s failed", spec.id, run.id)
            status, error = "failed", _clip(exc) or type(exc).__name__
        return await self.store.finish_run(
            run.id,
            status=status,
            verdict=verdict,
            summary=summary,
            usage=session.usage.to_dict(),
            error=error,
            actions=_redacted(_with_outcomes(session.actions, change_results)),
            recommendations=_redacted(session.recommendations),
        )

    def _ai_ready(self) -> bool:
        if self.ai_access is None:
            return True
        return bool(self.ai_access.master_on() and self.ai_access.available())

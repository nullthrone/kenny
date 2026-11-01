"""Starts the stewards an alert may start (ADR-0073 rules 3 and 4).

:class:`EventTrigger` is the alert engine's hook (``AlertEngine.set_alert_hook``).
It is called for every ``alert`` and ``change`` notification, never for a
recovery, a digest or a test, and it never blocks the alert loop: it only
classifies the notification into event names and hands the work to a task.

**Event names.** A ``health`` alert is one event per section it names,
``alert:health:<section>``; ``offline``, ``disk_forecast``, ``hardware_forecast``
and ``change`` are ``alert:<type>`` (the closed set of
:data:`~kenny_server.stewards.spec.EVENT_RE`). A steward is started only if its
*live* trigger configuration (:meth:`StewardRunner.trigger_config`) enables the
event; the spec's baseline decides nothing here.

**Per steward, host and event, in this order.** The global switch, the AI switch
and the steward's mode; the host (a steward that takes ``hosts`` runs only on a
host it names, one that names none runs on none; online, an OS that serves its
tools; a steward naming host tools without a ``hosts`` parameter runs nowhere);
one run of a steward on a host at a time, whatever event started it; the quiet
period after a steward's change (ADR-0071 rule 6: recorded as a ``skipped`` run,
as on a new ticket); the event's cooldown; the spec's preconditions and the idle
check (a ``skipped`` run with the reason); and the window.

**The quiet period** is asked of the machine the alert is about, whichever
machine the run itself is frozen to (none, for a server-only steward). It counts
a steward run that changed that machine within the period or is still in flight
in ``act`` on it, and any server-only run that changed something within the
period: a change to the server's own records can raise an alert on any machine
(:meth:`StewardRunner.quiet_period_refusal`).

**The window bounds the work (ADR-0073 rule 3).** A steward with a ``window``
parameter acts only inside an open occurrence of it (``act_until`` is its end);
outside it, or with the window unset, the run happens as ``shadow_only`` and every
change is a recommendation. A steward without a window parameter just runs.

**What the model is told.** The brief carries structured facts only: the event
name, the machine and the severity of each section. Never the notification's
title or body, which are free text built from machine data.

A run's trigger is ``event:<name>``; it counts toward the global caps (the runner
admits it as a counted run) and toward the circuit breaker, which is asked after
every run exactly as the scheduler does.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from ..notify import Notification
from .runner import BREAKER_THRESHOLD, StewardRunner, interrupted, stops_canary
from .policy import host_bound
from .spec import (
    ALERT_EVENT_TYPES,
    StewardSpec,
    is_event_name,
    resolve,
    runs_on_ticket,
)
from .store import StewardRun
from .windows import declared_hosts, machine_unready, occurrence_end, unready

logger = logging.getLogger("kenny.stewards.events")

__all__ = ["EVENT_TRIGGER_PREFIX", "EVENT_KINDS", "EventTrigger", "events_of"]

#: The ``trigger`` of an event-started run is this plus the event's name.
EVENT_TRIGGER_PREFIX = "event:"

#: The notification kinds that can start a steward. A recovery, a digest and a
#: test never do.
EVENT_KINDS: frozenset[str] = frozenset({"alert", "change"})

_SECTION_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_SEVERITIES = frozenset({"warn", "crit"})


def events_of(note: Notification) -> dict[str, dict[str, str]]:
    """The events ``note`` raises, each with the section severities it is about.

    ``{}`` for a kind that never starts a steward and for an ``event_type`` that
    is not one. A section name that is not an event name's section pattern is
    dropped rather than carried into a name or a brief.
    """

    if note.kind not in EVENT_KINDS:
        return {}
    if note.event_type == "health":
        out: dict[str, dict[str, str]] = {}
        for section, severity in note.sections.items():
            name = f"alert:health:{section}"
            if isinstance(section, str) and _SECTION_RE.fullmatch(section) and is_event_name(name):
                out[name] = {section: severity if severity in _SEVERITIES else ""}
        return out
    if note.event_type in ALERT_EVENT_TYPES:
        facts = {
            s: (v if v in _SEVERITIES else "")
            for s, v in note.sections.items()
            if isinstance(s, str) and _SECTION_RE.fullmatch(s)
        }
        return {f"alert:{note.event_type}": facts}
    return {}


class EventTrigger:
    """The alert hook that starts event-triggered stewards.

    ``params_of(steward_id)`` is the steward's stored parameters, as for the
    scheduler. ``executor`` supplies the registry (online, OS) and the idle
    check. ``client`` and ``model`` are handed to the runner only if given and
    only if its ``run_generic`` takes them.
    """

    def __init__(
        self,
        runner: StewardRunner,
        catalog: Mapping[str, StewardSpec],
        params_of: Callable[[str], Any],
        executor: Any,
        *,
        now: Callable[[], datetime] | None = None,
        client: Any = None,
        model: str | None = None,
        breaker_threshold: int = BREAKER_THRESHOLD,
    ) -> None:
        self.runner = runner
        self.catalog = catalog
        self.params_of = params_of
        self.executor = executor
        self._now = now or runner.now
        self._client = client
        self._model = model
        self._breaker_threshold = max(1, int(breaker_threshold))
        self._tasks: set[asyncio.Task[None]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        #: ``(steward id, host id)`` of every run from its checks to its end: one
        #: steward never runs twice at once on a host, whichever events start it.
        self._in_flight: set[tuple[str, str | None]] = set()
        accepted = inspect.signature(runner.run_generic).parameters
        self._takes_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in accepted.values())
        self._accepted = frozenset(accepted)

    # -- the hook ----------------------------------------------------------------

    def bind(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Run every start on ``loop`` (default: the running one), whichever loop calls the hook.

        The stores belong to the server's loop. A caller on another loop (a
        script driving the alert engine with ``asyncio.run``) hands the work
        over instead of touching them from its own, which it would close under
        a half-finished query.
        """

        self._loop = loop or asyncio.get_running_loop()

    def on_alert(self, note: Notification) -> None:
        """Classify ``note`` and, if it raises an event, handle it in a task.

        Returns at once and never raises: the alert loop that called it goes on.
        """

        try:
            events = events_of(note)
            if not events:
                return
            running = asyncio.get_running_loop()
            home = self._loop
            if home is None or home is running or home.is_closed():
                self._spawn(note, events)
            else:
                home.call_soon_threadsafe(self._spawn, note, events)
        except Exception:  # noqa: BLE001 - the alert has been delivered; this is best-effort
            logger.exception("classifying an alert for the stewards failed")

    def _spawn(self, note: Notification, events: dict[str, dict[str, str]]) -> None:
        task = asyncio.get_running_loop().create_task(self._handle_safely(note, events))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _mine(self) -> list[asyncio.Task[None]]:
        """The tasks running on the current loop; a task of a loop that has gone is forgotten.

        A hook called from a short-lived loop (a script driving the engine with
        ``asyncio.run``) leaves a task nothing can await any more.
        """

        loop = asyncio.get_running_loop()
        gone = [t for t in self._tasks if t.get_loop() is not loop and t.get_loop().is_closed()]
        self._tasks.difference_update(gone)
        return [t for t in self._tasks if t.get_loop() is loop]

    async def wait(self) -> None:
        """Wait for the runs the hook has started (shutdown, tests)."""

        while tasks := self._mine():
            await asyncio.gather(*tasks, return_exceptions=True)

    async def cancel(self) -> None:
        """Cancel what is still going; the runner records an interrupted run."""

        tasks = self._mine()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- one notification ----------------------------------------------------------

    async def _handle_safely(self, note: Notification, events: dict[str, dict[str, str]]) -> None:
        try:
            await self.handle(note, events)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one alert's fault must not be anyone's problem
            logger.exception("starting stewards for an alert failed")

    async def handle(
        self, note: Notification, events: dict[str, dict[str, str]] | None = None
    ) -> list[StewardRun]:
        """Start every steward ``note`` is an enabled event for; the runs started, in order.

        One after another, in steward and event order: the runner's caps are a
        safeguard, not how one alert is paced.
        """

        events = events_of(note) if events is None else events
        started: list[StewardRun] = []
        if not events:
            return started
        for spec in sorted(self.catalog.values(), key=lambda s: s.id):
            if runs_on_ticket(spec):
                continue
            try:
                enabled = {
                    e["name"]: e["cooldown_hours"]
                    for e in (await self.runner.trigger_config(spec))["events"]
                }
                for name in sorted(events):
                    if name not in enabled:
                        continue
                    run = await self._for_event(
                        spec, note.agent_id, name, events[name], timedelta(hours=enabled[name])
                    )
                    if run is not None:
                        started.append(run)
            except Exception:  # noqa: BLE001 - one steward's fault must not skip the others
                logger.exception("event start of steward %s failed", spec.id)
        return started

    async def _for_event(
        self,
        spec: StewardSpec,
        agent_id: str | None,
        event: str,
        facts: Mapping[str, str],
        cooldown: timedelta,
    ) -> StewardRun | None:
        runner = self.runner
        if not runner.enabled() or not runner.ai_ready():
            return None
        mode = await runner.mode_of(spec.id)
        if mode == "off":
            return None
        params = await self.params_of(spec.id)
        # A steward that takes ``hosts`` runs on a host it names; one that takes
        # none touches no host and runs on none.
        host: str | None
        if "hosts" in spec.params:
            if agent_id is None or agent_id not in declared_hosts(params):
                return None
            host = agent_id
            unreachable = machine_unready(spec, self.executor, host)
            if unreachable is not None:
                logger.debug("steward %s: no run on %s for %s: %s", spec.id, host, event, unreachable)
                return None
        elif host_bound(spec):
            # A spec like this is refused at load (``catalog.check_dispatchable``);
            # without ``hosts`` it would have no machine it may be run on.
            logger.warning(
                "steward %s names host tools but takes no hosts parameter; no event run", spec.id
            )
            return None
        else:
            host = None

        key = (spec.id, host)
        if key in self._in_flight:
            logger.debug("steward %s on %s is already running; %s starts nothing", spec.id, host, event)
            return None
        self._in_flight.add(key)
        try:
            return await self._checked_run(
                spec, host, event, facts, cooldown, mode, params, origin=agent_id
            )
        finally:
            self._in_flight.discard(key)

    async def _checked_run(
        self,
        spec: StewardSpec,
        host: str | None,
        event: str,
        facts: Mapping[str, str],
        cooldown: timedelta,
        mode: str,
        params: Mapping[str, Any],
        *,
        origin: str | None,
    ) -> StewardRun | None:
        runner = self.runner
        trigger = f"{EVENT_TRIGGER_PREFIX}{event}"
        # The machine the alert is about, and every server-only change.
        reason = await runner.quiet_period_refusal(origin, server_changes=True)
        if reason is not None:
            logger.info("steward %s: no run for %s on %s: %s", spec.id, event, host, reason)
            await self._record_skip(spec, mode, trigger, host, f"{event}: {reason}")
            return None
        last = await runner.store.latest_run_started(spec.id, host, trigger)
        if last is not None and self._now() - last < cooldown:
            logger.debug("steward %s: %s on %s is cooling down", spec.id, event, host)
            return None
        refused = await unready(runner, self.executor, spec, host, params)
        if refused is not None:
            logger.info("steward %s: no run for %s on %s: %s", spec.id, event, host, refused)
            await self._record_skip(spec, mode, trigger, host, f"{event}: {refused}")
            return None

        # ADR-0073 rule 3: acts only inside its window, otherwise recommends.
        act_until: datetime | None = None
        shadow_only = False
        if "window" in spec.params:
            act_until = occurrence_end(params.get("window"), self._now(), label=spec.id)
            shadow_only = act_until is None

        run = await runner.run_generic(
            resolve(spec, params),
            **self._kwargs(host, trigger, _brief(spec, host, event, facts), act_until, shadow_only),
        )
        if run is not None and stops_canary(run) and not interrupted(run):
            await runner.maybe_trip(spec.id, threshold=self._breaker_threshold)
        return run

    def _kwargs(
        self,
        host: str | None,
        trigger: str,
        brief: str,
        act_until: datetime | None,
        shadow_only: bool,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"host_id": host, "trigger": trigger, "brief": brief}
        for name, value in (
            ("client", self._client),
            ("model", self._model),
            ("executor", self.executor),
            ("act_until", act_until),
        ):
            if value is not None and (self._takes_any or name in self._accepted):
                kwargs[name] = value
        if shadow_only:
            # Not filtered by signature: a runner that cannot hold a run to
            # shadow must fail the call, never act.
            kwargs["shadow_only"] = True
        return kwargs

    async def _record_skip(
        self, spec: StewardSpec, mode: str, trigger: str, host: str | None, reason: str
    ) -> None:
        """Leave a ``skipped`` run so a person can see an event passed, and why."""

        store = self.runner.store
        run = await store.start_run(
            steward_id=spec.id,
            spec_hash=spec.spec_hash,
            trigger=trigger,
            mode=mode,
            subject=f"host:{host}" if host else None,
            host_id=host,
        )
        await store.finish_run(run.id, status="skipped", error=reason)


def _brief(spec: StewardSpec, host: str | None, event: str, facts: Mapping[str, str]) -> str:
    """The one message that starts an event run: structured facts, never alert text."""

    where = f'on the machine "{host}"' if host else "on the server; no machine is involved"
    lines = [f"{spec.title} run started by the event {event}, {where}."]
    known = [f"{section} ({severity})" for section, severity in sorted(facts.items()) if severity]
    if known:
        lines.append("Sections the event is about: " + ", ".join(known) + ".")
    return "\n".join(lines)

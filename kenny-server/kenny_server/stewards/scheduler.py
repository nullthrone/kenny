"""Starts the stewards whose trigger is a schedule (ADR-0071, ADR-0072).

One pass looks at every catalog steward whose *live* trigger configuration
(:meth:`StewardRunner.trigger_config`) has the schedule on and, for each
host the install named, decides whether a run is due *now*. Everything here is
deterministic server code; the model is only ever started, never asked.

**When a run is due.** The steward's ``window`` parameter is a recurring
maintenance window (weekday/time/zone, parsed by :mod:`kenny_server.webfilter`'s
own ``make_window`` and ``schedule_state``, not a second parser). Outside it
nothing starts. Inside it, each host in the steward's ``hosts`` parameter runs
**once per window occurrence**; an empty or missing ``hosts`` is no host, never
"all". "Already ran" is read off ``steward_runs``: a run is recorded with the
trigger ``schedule:<end of the occurrence>``, so the answer survives a restart
without a table of its own. A run that failed also counts as having run — a
broken steward retries at the next occurrence, not every pass. Everything the
scheduler reads back — this, the canary stop, the breaker's streak and the
interval — comes from the steward's scheduled runs alone, selected by trigger in
the store, so no number of preview runs can hide one.

A steward that takes no ``hosts`` parameter touches no host: it runs once per
occurrence with no host at all, and none of the host preconditions below
apply. The configuration's ``schedule.min_interval_days`` skips every occurrence that ends
closer than that to the last occurrence the steward ran in (read off the same
triggers), which is how a weekly window carries a monthly steward.

**The window bounds the work, not only the start.** Before each host the
occurrence is computed again; once it is no longer the one the pass started
in, the pass stops. A run already started is handed the end of its occurrence
(``act_until``), and its gate refuses every change requested after it, so a
run that outlasts its window can report but not act.

**Canary order.** Hosts run one after another in sorted order. The pass for an
steward stops at the first run that failed, or that *changed something* (an
action the gate allowed that did not fail) and did not end ``acted`` — an
``actionable``, ``inconclusive`` or missing verdict leaves the change
unconfirmed. The stop outlasts the pass: while any run of this occurrence
stopped the canary, the steward starts nothing else until the next occurrence,
so the next pass cannot walk past a host that went wrong.

**Circuit breaker.** After :data:`BREAKER_THRESHOLD` consecutive runs of one
steward in ``act`` that stopped the canary, it is moved to ``shadow`` as
``system:circuit-breaker`` and a warning goes on the event log. A run stops the
canary when it ended ``failed``, a change it made errored, or it changed
something it did not confirm. Refusals that are an outcome rather than a fault
— the steward's ``disabled`` kill switch, its ``blocked`` guard, a ``paused``
game session — never count, and a run that was interrupted (a graceful
shutdown or a restart, :data:`~kenny_server.stewards.store.INTERRUPTED_ERROR`)
is neutral: it neither counts nor breaks a streak. The breaker, and the run
readers above, are the runner's (:meth:`StewardRunner.maybe_trip`): event-started
runs count toward it as well, so it is decided in one place.

**Preconditions, before any run.** A host that is offline, or whose OS cannot
serve a tool the steward names, is skipped. A precondition the spec declares
(:meth:`StewardRunner.precondition_refusal`) is asked next, for a server-only
run too. A steward whose ``require_idle``
parameter is not ``false`` asks the host itself — ``remotehelp_status``, called
here as ``steward:<id>``, not by the model — whether somebody is signed in, and
skips the host unless the answer is a clear *no*: a failed check is "do not
disturb". A skip is recorded once per occurrence as a ``skipped`` run with the
reason and is retried at the next pass; it is not a failure.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..webfilter import schedule_state
from .runner import (
    BENIGN_REFUSALS,
    BREAKER_ACTOR,
    BREAKER_THRESHOLD,
    StewardRunner,
    halts,
    interrupted,
    made_change,
    run_failed,
    stops_canary,
)
from .spec import StewardSpec, resolve
from .store import StewardRun
from .windows import (
    _TOOL_OS,  # noqa: F401 - kept importable from here; tests join it to the tool list
    declared_hosts,
    host_supports,
    occurrence_end,
    unready,
)

logger = logging.getLogger("kenny.stewards.scheduler")

__all__ = [
    "BENIGN_REFUSALS",
    "BREAKER_ACTOR",
    "BREAKER_THRESHOLD",
    "INTERVAL_SETTING",
    "MIN_INTERVAL_S",
    "SCHEDULE_TRIGGER_PREFIX",
    "StewardScheduler",
    "Outcome",
    "halts",
    "host_supports",
    "interrupted",
    "made_change",
    "run_failed",
    "stops_canary",
]

#: The live setting a pass's cadence is read from (``config.py``).
INTERVAL_SETTING = "KENNY_STEWARDS_SCHEDULE_INTERVAL_SECS"

#: The shortest cadence a loop honours, whatever the setting says.
MIN_INTERVAL_S = 60

#: The ``trigger`` of a scheduled run is this plus the end of the window
#: occurrence it belongs to (UTC, ISO-8601): one occurrence, one trigger.
SCHEDULE_TRIGGER_PREFIX = "schedule:"

#: Run statuses that mean "this host has had its run for this occurrence".
_DONE_STATUSES: frozenset[str] = frozenset({"running", "completed", "failed"})

@dataclass(frozen=True)
class Outcome:
    """What one pass did for one host of one steward."""

    steward_id: str
    #: ``None`` for a run of a steward that touches no host.
    host_id: str | None
    #: ``ran`` | ``skipped`` | ``halted`` | ``tripped``
    kind: str
    detail: str = ""
    run: StewardRun | None = None


def _packages(params: Mapping[str, Any]) -> list[str]:
    raw = params.get("packages")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return []
    return sorted({p for p in raw if isinstance(p, str) and p})


class StewardScheduler:
    """Starts the scheduled stewards; one :meth:`pass_once` per cadence tick.

    ``params_of(steward_id)`` is the steward's stored parameters (superuser-edited,
    ADR-0072). ``executor`` is used to ask a host whether it is idle and for the
    registry that says whether it is online and which OS it runs. ``client`` and
    ``model`` are handed to the runner only if given, and only if its
    ``run_generic`` takes them.
    """

    def __init__(
        self,
        runner: StewardRunner,
        catalog: Mapping[str, StewardSpec],
        params_of: Callable[[str], Awaitable[Mapping[str, Any]]],
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
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._client = client
        self._model = model
        self._breaker_threshold = max(1, int(breaker_threshold))
        # One pass at a time: a pass can outlast the cadence (a package update
        # runs for minutes) and two must not both decide a host is due.
        self._pass_lock = asyncio.Lock()
        accepted = inspect.signature(runner.run_generic).parameters
        self._takes_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in accepted.values())
        self._accepted = frozenset(accepted)

    # -- the loop ------------------------------------------------------------

    async def run(
        self, interval_s: Callable[[], float | int], initial_delay_s: float = 10.0
    ) -> None:
        """Run a pass every ``interval_s()`` seconds, forever.

        The cadence is read again after every pass, so changing the setting
        retimes the running loop, and is never shorter than
        :data:`MIN_INTERVAL_S`. A pass that raises is logged and the loop goes
        on (as ``AlertEngine.run`` does).
        """

        await asyncio.sleep(initial_delay_s)
        while True:
            try:
                await self.pass_once()
            except Exception:  # noqa: BLE001 - never let the loop die
                logger.exception("steward schedule pass failed")
            try:
                interval = float(interval_s())
            except Exception:  # noqa: BLE001 - a bad setting must not stop the loop
                interval = float(MIN_INTERVAL_S)
            await asyncio.sleep(max(float(MIN_INTERVAL_S), interval))

    # -- one pass --------------------------------------------------------------

    async def pass_once(self) -> list[Outcome]:
        """Start whatever is due now; return what was done, in order."""

        async with self._pass_lock:
            if not self.runner.enabled():
                return []
            outcomes: list[Outcome] = []
            for spec in sorted(self.catalog.values(), key=lambda s: s.id):
                try:
                    outcomes += await self._pass_steward(spec)
                except Exception:  # noqa: BLE001 - one steward's fault must not skip the others
                    logger.exception("schedule pass for steward %s failed", spec.id)
            return outcomes

    def _occurrence(self, spec: StewardSpec, raw: Any, at: datetime) -> datetime | None:
        """The end of the window occurrence open at ``at``, or ``None``
        (:func:`~kenny_server.stewards.windows.occurrence_end`)."""

        # ``schedule_state`` passed from this module's namespace: the seam the
        # scheduler's own tests patch.
        return occurrence_end(raw, at, label=spec.id, state_of=schedule_state)

    async def _still_open(self, spec: StewardSpec, end: datetime) -> bool:
        """Whether the occurrence ending at ``end`` is still the one open now.

        Read from the stored window again, so a window edited during the pass
        counts as well as the clock.
        """

        params = await self.params_of(spec.id)
        return self._occurrence(spec, params.get("window"), self._now()) == end

    async def _pass_steward(self, spec: StewardSpec) -> list[Outcome]:
        # The live configuration, not the spec's baseline: a schedule switched
        # off runs nothing here, not even in shadow.
        schedule = (await self.runner.trigger_config(spec))["schedule"]
        if schedule is None:
            return []
        min_interval_days = schedule.get("min_interval_days")
        mode = await self.runner.mode_of(spec.id)
        if mode == "off":
            return []
        params = await self.params_of(spec.id)
        end = self._occurrence(spec, params.get("window"), self._now())
        if end is None:
            return []
        trigger = _trigger(end)
        # A steward without a ``hosts`` parameter touches no host: it runs once
        # per occurrence, on none (``None`` stands for "the server" below).
        hosts: list[str | None] = [None] if _server_only(spec) else list(declared_hosts(params))
        if not hosts:
            return []
        history = await self._history(spec)
        if _too_soon(min_interval_days, history, end):
            return []
        mine = [r for r in history if r.trigger == trigger]
        if any(stops_canary(r) for r in mine):
            return [Outcome(spec.id, "", "halted", "an earlier run of this window stopped the canary")]
        ran = {r.host_id for r in mine if r.status in _DONE_STATUSES}
        skipped = {(r.host_id, r.error) for r in mine if r.status == "skipped"}

        outcomes: list[Outcome] = []
        for host in hosts:
            if host in ran:
                continue
            if not await self._still_open(spec, end):
                # Hosts run one after another; the ones left wait for the next
                # occurrence rather than start outside this one.
                outcomes.append(Outcome(spec.id, host, "halted", "the maintenance window closed"))
                break
            reason = await self._unready(spec, host, params)
            if reason is not None:
                if (host, reason) not in skipped:
                    await self._record_skip(spec, mode, trigger, host, reason)
                    skipped.add((host, reason))
                outcomes.append(Outcome(spec.id, host, "skipped", reason))
                continue
            run = await self._run(spec, host, trigger, params, end)
            if run is None:
                # The global switch, the AI switch or the mode went off meanwhile.
                break
            if run.status == "skipped":
                # A global cap refused it (and has recorded why); it would
                # refuse the next host too.
                outcomes.append(Outcome(spec.id, host, "skipped", run.error or "", run))
                break
            outcomes.append(Outcome(spec.id, host, "ran", run.status, run))
            tripped = (
                stops_canary(run) and not interrupted(run) and await self._maybe_trip(spec)
            )
            if tripped:
                outcomes.append(Outcome(spec.id, host, "tripped", "back to shadow", run))
            if stops_canary(run):
                break
        return outcomes

    async def _history(self, spec: StewardSpec) -> list[StewardRun]:
        """Every scheduled run of ``spec`` on the record, newest first.

        Selected by trigger in the store, never a window of the latest runs of
        every kind: a person can start previews at will, and none of them may
        push out the runs that say a host already ran, that the canary stopped,
        that the breaker's streak stands, or when the steward last ran.
        """

        return await self.runner.store.scheduled_runs(spec.id, prefix=SCHEDULE_TRIGGER_PREFIX)

    # -- preconditions ---------------------------------------------------------

    async def _unready(
        self, spec: StewardSpec, host: str | None, params: Mapping[str, Any]
    ) -> str | None:
        """Why ``host`` must not be run on now, or ``None`` (:func:`~kenny_server.stewards.windows.unready`)."""

        return await unready(self.runner, self.executor, spec, host, params)

    async def _record_skip(
        self, spec: StewardSpec, mode: str, trigger: str, host: str, reason: str
    ) -> None:
        """Leave a ``skipped`` run so a person can see the host was passed over, and why."""

        store = self.runner.store
        run = await store.start_run(
            steward_id=spec.id,
            spec_hash=spec.spec_hash,
            trigger=trigger,
            mode=mode,
            subject=f"host:{host}",
            host_id=host,
        )
        await store.finish_run(run.id, status="skipped", error=reason)

    # -- a run -----------------------------------------------------------------

    async def _run(
        self,
        spec: StewardSpec,
        host: str | None,
        trigger: str,
        params: Mapping[str, Any],
        end: datetime,
    ) -> StewardRun | None:
        kwargs: dict[str, Any] = {
            "host_id": host,
            "trigger": trigger,
            "brief": _brief(spec, host, params),
        }
        for name, value in (
            ("client", self._client),
            ("model", self._model),
            ("executor", self.executor),
            # No change of this run may start after its window has closed.
            ("act_until", end),
        ):
            if value is not None and (self._takes_any or name in self._accepted):
                kwargs[name] = value
        # Resolved here as well as in the runner: what the run's gate enforces
        # is this spec with the allowlist filled in from the stored parameters.
        return await self.runner.run_generic(resolve(spec, params), **kwargs)

    async def _maybe_trip(self, spec: StewardSpec) -> bool:
        """Move ``spec`` to ``shadow`` after N consecutive canary stops while in ``act``.

        The runner's breaker (:meth:`StewardRunner.maybe_trip`), with this
        scheduler's threshold.
        """

        return await self.runner.maybe_trip(spec.id, threshold=self._breaker_threshold)


def _trigger(end: datetime) -> str:
    """The ``trigger`` of every scheduled run of the occurrence ending at ``end``."""

    return f"{SCHEDULE_TRIGGER_PREFIX}{end.astimezone(timezone.utc).isoformat()}"


def _server_only(spec: StewardSpec) -> bool:
    """Whether ``spec`` touches no host: it takes no ``hosts`` parameter.

    ``catalog.check_dispatchable`` refuses such a scheduled spec that names a
    capability, so there is nothing it could run on a host.
    """

    return "hosts" not in spec.params


def _occurrence_end(trigger: str) -> datetime | None:
    """The end of the occurrence a scheduled run's ``trigger`` names, or ``None``."""

    if not trigger.startswith(SCHEDULE_TRIGGER_PREFIX):
        return None
    try:
        end = datetime.fromisoformat(trigger[len(SCHEDULE_TRIGGER_PREFIX):])
    except ValueError:
        return None
    return end if end.tzinfo is not None else end.replace(tzinfo=timezone.utc)


def _too_soon(days: int | None, history: list[StewardRun], end: datetime) -> bool:
    """Whether the occurrence ending at ``end`` is closer than ``days`` to the last.

    ``days`` is the live configuration's ``schedule.min_interval_days``.

    Measured between occurrence ends, read off the triggers of the steward's runs
    that ran (a ``skipped`` run did not), so a weekly window with a 28-day
    interval runs in exactly every fourth occurrence, and the answer survives a
    restart. An occurrence is never too soon after itself: its own hosts finish.
    """

    if days is None:
        return False
    for run in history:
        if run.status not in _DONE_STATUSES:
            continue
        last = _occurrence_end(run.trigger)
        if last is not None and last != end and end - last < timedelta(days=days):
            return True
    return False


def _brief(spec: StewardSpec, host: str | None, params: Mapping[str, Any]) -> str:
    """The one message that starts a scheduled run.

    Facts the run needs to be efficient, not limits: the gate enforces those.
    """

    if host is None:
        lines = [f"Scheduled {spec.title.lower()} run on the server; no machine is involved."]
    else:
        lines = [f"Scheduled {spec.title.lower()} run on the machine \"{host}\"."]
    if "packages" in spec.params:
        packages = _packages(params)
        if packages:
            lines.append("Package ids you may update: " + ", ".join(packages) + ".")
        else:
            lines.append("No package is allowed to be updated; update nothing.")
    return "\n".join(lines)

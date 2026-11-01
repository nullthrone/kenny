"""Ticket callbacks: a person arms a steward to work a ticket when its machine is back (ADR-0075).

A machine that is rarely online cannot be helped by a command run (ADR-0073
rule 3): the person who could click is seldom there when the machine is. A
*ticket callback* is that click given ahead. A superuser at the dashboard arms
one steward on one ticket; the next time the ticket's machine pushes telemetry,
the server starts a command run of that steward on it, as that person, through
:func:`~kenny_server.stewards.commands.start_command` -- the same gate, budget,
caps and authorizations as a click, and nothing a click could not do.

What it adds is bounded here, and only here:

* **What may be armed.** A ticket whose subject a server predicate can check:
  an alert ticket whose dedup key names health sections of the ticket's own
  machine (:func:`checkable_sections`). A ticket a person wrote has no such
  predicate, so no callback can claim to have fixed it.
* **Who, and against what.** A superuser signed in to the dashboard, naming the
  effective hash they were shown. Every check of a command run that does not
  depend on the machine being there is asked when arming
  (``act_refusal(deferred=True)``); the rest when the run starts.
* **How long.** :data:`CALLBACK_TTL` (14 days) and :data:`MAX_ATTEMPTS` runs that
  reached the machine. A run a cap skipped, or one a restart interrupted, spends
  no attempt; a refusal that waiting can lift (the machine busy, the daily
  command limit) leaves it armed. A refusal waiting cannot lift (the steward
  left ``act``, its hash moved, its command trigger went off) ends it: the
  consent was given to what the steward was.
* **When.** On a telemetry push from the machine, never on the connection
  itself: a run starts on fresh data.
* **What counts as fixed.** Never the model's verdict. After a run the server
  collects telemetry from the machine and re-evaluates it through the alert
  engine (:meth:`~kenny_server.alerting.AlertEngine.evaluate_agent_now`), so the
  ordinary recovery path closes the ticket. If the machine is gone before that,
  the next push it sends is the evidence. The ticket's sections all ``ok`` on a
  snapshot received after the run ended is success; anything else is a failed
  attempt.
* **What a person hears.** Each step is a note on the ticket, built from
  records (state, counts, the run's id), never from text a model wrote. A
  callback that ends without fixing the ticket -- attempts spent, expired, its
  steward changed -- is announced on the alert channels as a ``push``
  (:meth:`~kenny_server.alerting.AlertEngine.announce`), which opens no ticket
  and starts no steward.

A ticket that is resolved, closed or cancelled ends its callback (``cancelled``);
a callback is never re-armed by anything but a person.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from .. import alert_subject
from ..notify import Notification, public_link_base
from ..store import DEFAULT_DB_PATH, _configure_connection, write_lock
from .commands import CommandRefused, Hosts, act_refusal, caller_refusal, start_command
from .runner import StewardRunner, surface_refusal
from .spec import StewardSpec
from .store import INTERRUPTED_ERROR

__all__ = [
    "CALLBACK_TTL",
    "CALLBACK_STATES",
    "LIVE_STATES",
    "MAX_ATTEMPTS",
    "TRANSIENT_REFUSALS",
    "CallbackError",
    "CallbackStore",
    "TicketCallback",
    "TicketCallbacks",
    "checkable_sections",
]

logger = logging.getLogger("kenny.stewards.callbacks")

#: How long an armed callback waits for its machine.
CALLBACK_TTL = timedelta(days=14)

#: How many runs that reached the machine a callback may spend.
MAX_ATTEMPTS = 3

#: ``armed`` waits for a push; ``running`` has a run in flight; ``verifying``
#: waits for telemetry newer than the run; the rest are final.
CALLBACK_STATES: tuple[str, ...] = (
    "armed",
    "running",
    "verifying",
    "succeeded",
    "failed",
    "expired",
    "cancelled",
)
LIVE_STATES: tuple[str, ...] = ("armed", "running", "verifying")

#: Command-run refusals that waiting can lift: the callback stays armed and
#: spends no attempt. Every other refusal ends it.
TRANSIENT_REFUSALS: frozenset[str] = frozenset(
    {
        "host_offline",
        "host_not_idle",
        "precondition",
        "manual_run_running",
        "command_limit",
        "over_cap",
        "stewards_off",
        "ai_unavailable",
        "not_started",
    }
)

#: Ticket states that end a callback.
_TICKET_CLOSED = frozenset({"resolved", "closed", "cancelled"})

_INCIDENT = frozenset({"warn", "crit"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticket_callbacks (
    id              TEXT PRIMARY KEY,
    ticket_id       TEXT NOT NULL,
    steward_id      TEXT NOT NULL,
    host_id         TEXT NOT NULL,
    effective_hash  TEXT NOT NULL,
    sections        TEXT NOT NULL,
    armed_by        TEXT NOT NULL,
    armed_at        TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    max_attempts    INTEGER NOT NULL CHECK (max_attempts >= 1),
    attempts        INTEGER NOT NULL DEFAULT 0,
    state           TEXT NOT NULL,
    last_run_id     TEXT,
    run_finished_at TEXT,
    last_error      TEXT NOT NULL DEFAULT '',
    ended_at        TEXT,
    ended_by        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ticket_callbacks_live
    ON ticket_callbacks (ticket_id) WHERE state IN ('armed', 'running', 'verifying');
CREATE INDEX IF NOT EXISTS idx_ticket_callbacks_host
    ON ticket_callbacks (host_id, state);
"""

_COLUMNS = (
    "id, ticket_id, steward_id, host_id, effective_hash, sections, armed_by, armed_at, "
    "expires_at, max_attempts, attempts, state, last_run_id, run_finished_at, last_error, "
    "ended_at, ended_by"
)

_MAX_ERROR = 500


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def checkable_sections(ticket: Any) -> list[str] | None:
    """The health sections a server predicate can check for ``ticket``, or ``None``.

    Only an alert ticket about health sections of its own machine has one: its
    dedup key names the sections (:mod:`~kenny_server.alert_subject`), and the
    alert engine's verdict on them is the predicate.
    """

    parsed = alert_subject.parse(getattr(ticket, "dedup_key", "") or "")
    if parsed is None:
        return None
    agent_id, space, subjects = parsed
    if space != "state" or not agent_id or agent_id != getattr(ticket, "agent_id", None):
        return None
    # ``offline`` and the forecasts are subjects with no section to recover.
    if not subjects or any(s in ("offline", "disk_forecast", "hardware_forecast") for s in subjects):
        return None
    return sorted(subjects)


class CallbackError(ValueError):
    """A request about a callback this module refuses; ``status`` is the HTTP status."""

    def __init__(self, message: str, *, status: int, code: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class TicketCallback:
    """One row of ``ticket_callbacks``."""

    id: str
    ticket_id: str
    steward_id: str
    host_id: str
    effective_hash: str
    sections: list[str]
    armed_by: str
    armed_at: str
    expires_at: str
    max_attempts: int
    attempts: int
    state: str
    last_run_id: str | None
    run_finished_at: str | None
    last_error: str
    ended_at: str | None
    ended_by: str | None

    @classmethod
    def _from_row(cls, row: aiosqlite.Row) -> TicketCallback:
        values = {key: row[key] for key in row.keys()}
        values["sections"] = [s for s in str(values["sections"]).split(",") if s]
        return cls(**values)

    @property
    def live(self) -> bool:
        return self.state in LIVE_STATES

    def to_public(self) -> dict[str, Any]:
        out = asdict(self)
        out["live"] = self.live
        return out


class CallbackStore:
    """``ticket_callbacks`` on the shared DB file; every state change is conditional."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("CallbackStore is not connected; call connect() first")
        return self._db

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        await _configure_connection(self._db)
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def recover(self) -> int:
        """At start-up: a callback left ``running`` lost its run with the process.

        It goes back to ``armed`` and spends no attempt, as an interrupted run
        never does.
        """

        async with write_lock():
            cur = await self._conn.execute(
                "UPDATE ticket_callbacks SET state = 'armed', last_error = ? "
                "WHERE state = 'running'",
                (INTERRUPTED_ERROR,),
            )
            await self._conn.commit()
        return cur.rowcount or 0

    async def insert(
        self,
        *,
        ticket_id: str,
        steward_id: str,
        host_id: str,
        effective_hash: str,
        sections: Iterable[str],
        armed_by: str,
        now: datetime,
        ttl: timedelta = CALLBACK_TTL,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> TicketCallback:
        callback_id = uuid.uuid4().hex
        async with write_lock():
            try:
                await self._conn.execute(
                    f"INSERT INTO ticket_callbacks ({_COLUMNS}) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'armed', NULL, NULL, '', NULL, NULL)",
                    (
                        callback_id,
                        ticket_id,
                        steward_id,
                        host_id,
                        effective_hash,
                        ",".join(sorted(set(sections))),
                        armed_by,
                        _iso(now),
                        _iso(now + ttl),
                        max_attempts,
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                # The failed INSERT opened a transaction; leaving it open would
                # hold the file's write lock against every other connection.
                await self._conn.rollback()
                raise CallbackError(
                    "this ticket already has a callback armed; withdraw it first",
                    status=409,
                    code="already_armed",
                ) from exc
            await self._conn.commit()
        found = await self.get(callback_id)
        assert found is not None
        return found

    async def get(self, callback_id: str) -> TicketCallback | None:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM ticket_callbacks WHERE id = ?", (callback_id,)
        ) as cur:
            row = await cur.fetchone()
        return TicketCallback._from_row(row) if row else None

    async def for_ticket(self, ticket_id: str) -> list[TicketCallback]:
        """Every callback of ``ticket_id``, newest first."""

        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM ticket_callbacks WHERE ticket_id = ? "
            "ORDER BY armed_at DESC, rowid DESC",
            (ticket_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [TicketCallback._from_row(r) for r in rows]

    async def live(self, host_id: str | None = None) -> list[TicketCallback]:
        """Every live callback, or those on ``host_id``, oldest first."""

        marks = ", ".join("?" for _ in LIVE_STATES)
        sql = f"SELECT {_COLUMNS} FROM ticket_callbacks WHERE state IN ({marks})"
        params: list[Any] = list(LIVE_STATES)
        if host_id is not None:
            sql += " AND host_id = ?"
            params.append(host_id)
        async with self._conn.execute(sql + " ORDER BY armed_at, rowid", params) as cur:
            rows = await cur.fetchall()
        return [TicketCallback._from_row(r) for r in rows]

    async def move(
        self,
        callback_id: str,
        from_states: Iterable[str],
        to_state: str,
        *,
        now: datetime,
        by: str | None = None,
        **fields: Any,
    ) -> TicketCallback | None:
        """Move one callback from one of ``from_states`` to ``to_state``.

        Returns the row as it is afterwards, or ``None`` when it was in no such
        state -- somebody else moved it first, and this caller does nothing.
        ``attempts_plus`` adds to the attempt count; ``last_run_id``,
        ``run_finished_at`` and ``last_error`` are written when given.
        """

        if to_state not in CALLBACK_STATES:
            raise ValueError(f"unknown callback state {to_state!r}")
        sets = ["state = ?"]
        params: list[Any] = [to_state]
        plus = int(fields.pop("attempts_plus", 0) or 0)
        if plus:
            sets.append("attempts = attempts + ?")
            params.append(plus)
        for column in ("last_run_id", "run_finished_at"):
            if column in fields:
                sets.append(f"{column} = ?")
                params.append(fields.pop(column))
        if "last_error" in fields:
            sets.append("last_error = ?")
            params.append(str(fields.pop("last_error") or "")[:_MAX_ERROR])
        if fields:
            raise TypeError(f"unknown fields {sorted(fields)}")
        if to_state not in LIVE_STATES:
            sets += ["ended_at = ?", "ended_by = ?"]
            params += [_iso(now), by or "system"]
        froms = list(from_states)
        marks = ", ".join("?" for _ in froms)
        async with write_lock():
            cur = await self._conn.execute(
                f"UPDATE ticket_callbacks SET {', '.join(sets)} "
                f"WHERE id = ? AND state IN ({marks})",
                (*params, callback_id, *froms),
            )
            await self._conn.commit()
        if not cur.rowcount:
            return None
        return await self.get(callback_id)


#: ``refresh(host_id) -> bool``: collect telemetry from the machine now and store it.
Refresh = Callable[[str], Awaitable[bool]]
#: ``reevaluate(host_id)``: one alert-engine pass over the machine.
Reevaluate = Callable[[str], Awaitable[Any]]
#: ``latest(host_id) -> (received_at, {section: status}) | None`` from the store.
Latest = Callable[[str], Awaitable["tuple[str, dict[str, str]] | None"]]
#: ``announce(note)``: deliver a notification on the alert channels.
Announce = Callable[[Notification], Awaitable[None]]


class TicketCallbacks:
    """Arms, fires, verifies and ends ticket callbacks. One per server."""

    def __init__(
        self,
        *,
        store: CallbackStore,
        runner: StewardRunner,
        tickets: Any,
        hosts: Callable[[], Awaitable[Hosts]],
        refresh: Refresh,
        reevaluate: Reevaluate,
        latest: Latest,
        announce: Announce | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self.tickets = tickets
        self.hosts = hosts
        self.refresh = refresh
        self.reevaluate = reevaluate
        self.latest = latest
        self.announce = announce
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._tasks: set[asyncio.Task[Any]] = set()
        # One callback is worked by one task at a time, whatever woke it.
        self._busy: set[str] = set()

    # -- what a person sees and does ------------------------------------------

    async def _ticket(self, ticket_id: str) -> Any:
        from ..tickets import TicketNotFoundError

        try:
            return await self.tickets.get(ticket_id)
        except TicketNotFoundError as exc:
            raise CallbackError("ticket not found", status=404, code="no_ticket") from exc

    async def _candidates(
        self, ticket: Any, sections: list[str] | None
    ) -> list[dict[str, Any]]:
        """The stewards a callback on ``ticket`` could name, and whether each may be armed now."""

        host_id = getattr(ticket, "agent_id", None)
        if sections is None or not host_id:
            return []
        hosts = await self.hosts()
        out: list[dict[str, Any]] = []
        for spec in self.runner.catalog.values():
            if surface_refusal(spec) is not None:
                continue
            if not (await self.runner.trigger_config(spec)).get("command"):
                continue
            mode = await self.runner.mode_of(spec.id)
            if mode == "off":
                continue
            refused = await act_refusal(
                self.runner,
                spec,
                host_id=host_id,
                effective_hash=None,
                hosts=hosts,
                check_hash=False,
                deferred=True,
            )
            if refused is not None and refused.code in ("bad_target", "no_hosts_param",
                                                        "no_hosts", "host_not_named",
                                                        "os_unsupported"):
                # Not a steward for this machine at all: not offered.
                continue
            out.append(
                {
                    "steward_id": spec.id,
                    "title": spec.title,
                    "summary": spec.description,
                    "mode": mode,
                    "effective_hash": await self.runner.live_hash(spec.id),
                    "allowed": refused is None,
                    "reason": None if refused is None else str(refused),
                    "code": None if refused is None else refused.code,
                }
            )
        return out

    async def view(self, ticket_id: str, *, person: bool, role: str | None) -> dict[str, Any]:
        """The ticket's callback (live, else the latest) and what could be armed on it."""

        ticket = await self._ticket(ticket_id)
        sections = checkable_sections(ticket)
        history = await self.store.for_ticket(ticket_id)
        current = history[0] if history else None
        reason: str | None = None
        if sections is None:
            reason = (
                "only an alert ticket about health findings of its machine can be "
                "handed to a steward: nothing else has a check the server can run"
            )
        elif ticket.state in _TICKET_CLOSED:
            reason = f"the ticket is {ticket.state}"
        elif current is not None and current.live:
            reason = "a callback is already armed on this ticket"
        caller = caller_refusal(person=person, role=role)
        if reason is None and caller is not None:
            reason = str(caller)
        return {
            "ticket_id": ticket.id,
            "host_id": ticket.agent_id,
            "sections": sections or [],
            "callback": current.to_public() if current is not None else None,
            "can_arm": reason is None,
            "reason": reason,
            "ttl_days": CALLBACK_TTL.days,
            "max_attempts": MAX_ATTEMPTS,
            "stewards": await self._candidates(ticket, sections),
        }

    async def arm(
        self,
        ticket_id: str,
        *,
        steward_id: str,
        effective_hash: Any,
        username: str,
        actor: str,
        person: bool,
        role: str | None,
    ) -> TicketCallback:
        """Arm ``steward_id`` on ``ticket_id`` for its machine's next push.

        ``username`` is who the run will be started as (the audit trail's
        name); ``actor`` is how the ticket's trail names the person.
        """

        refused = caller_refusal(person=person, role=role)
        if refused is not None:
            raise CallbackError(str(refused), status=refused.status, code=refused.code)
        ticket = await self._ticket(ticket_id)
        if ticket.state in _TICKET_CLOSED:
            raise CallbackError(
                f"the ticket is {ticket.state}", status=409, code="ticket_closed"
            )
        sections = checkable_sections(ticket)
        if sections is None:
            raise CallbackError(
                "only an alert ticket about health findings of its machine can be handed "
                "to a steward: nothing else has a check the server can run",
                status=400,
                code="not_checkable",
            )
        spec = self.runner.catalog.get(steward_id)
        if spec is None:
            raise CallbackError(f"unknown steward {steward_id}", status=404, code="no_steward")
        refused = await act_refusal(
            self.runner,
            spec,
            host_id=ticket.agent_id,
            effective_hash=effective_hash,
            hosts=await self.hosts(),
            deferred=True,
        )
        if refused is not None:
            raise CallbackError(str(refused), status=refused.status, code=refused.code)
        who = username.strip()[:64] or "unknown"
        callback = await self.store.insert(
            ticket_id=ticket.id,
            steward_id=spec.id,
            host_id=ticket.agent_id,
            effective_hash=str(effective_hash),
            sections=sections,
            armed_by=who,
            now=self.now(),
        )
        await self._note(
            callback,
            f"{who} handed this ticket to the steward {spec.title} ({spec.id}): it runs on "
            f"{callback.host_id} at the machine's next telemetry push, at most "
            f"{callback.max_attempts} times, until {callback.expires_at[:16].replace('T', ' ')} UTC. "
            f"It counts as fixed only when {', '.join(callback.sections)} "
            "read healthy after a run.",
            actor=actor,
        )
        hosts = await self.hosts()
        agent = hosts.registry.get(callback.host_id) if hosts.registry is not None else None
        if agent is not None and getattr(agent, "online", False):
            # Already there: start on the telemetry it already sent rather than
            # waiting up to a push interval.
            self._spawn(self.fire(callback.host_id))
        return callback

    async def disarm(self, ticket_id: str, *, username: str, actor: str) -> TicketCallback:
        """Withdraw the ticket's live callback. A run already in flight is not recalled."""

        await self._ticket(ticket_id)
        history = await self.store.for_ticket(ticket_id)
        live = next((c for c in history if c.live), None)
        if live is None:
            raise CallbackError("no callback is armed on this ticket", status=404, code="none_armed")
        who = username.strip()[:64] or "unknown"
        moved = await self.store.move(
            live.id, LIVE_STATES, "cancelled", now=self.now(), by=who,
            last_error=f"withdrawn by {who}",
        )
        if moved is None:
            raise CallbackError("the callback ended in the meantime", status=409, code="ended")
        tail = " A run already started keeps running." if live.state == "running" else ""
        await self._note(moved, f"{who} withdrew the callback to {moved.steward_id}.{tail}",
                         actor=actor)
        return moved

    # -- what the server does -----------------------------------------------

    def on_telemetry(self, agent_id: str, _snapshot: Any = None) -> None:
        """``tunnel.after_insert``: a push from ``agent_id`` landed. Never blocks the tunnel."""

        self._spawn(self.fire(agent_id))

    def _spawn(self, work: Awaitable[Any]) -> None:
        try:
            task = asyncio.ensure_future(work)
        except RuntimeError:  # no running loop (a synchronous test hook)
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait(self) -> None:
        """Until every task this has started is done (tests, shutdown)."""

        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def cancel(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def fire(self, host_id: str) -> None:
        """Work every live callback on ``host_id``: verify what ran, start what is armed."""

        try:
            callbacks = await self.store.live(host_id)
        except Exception:  # noqa: BLE001 - a push must never fail on this
            logger.exception("reading the callbacks of %s failed", host_id)
            return
        for callback in callbacks:
            if callback.id in self._busy:
                continue
            self._busy.add(callback.id)
            try:
                if callback.state == "verifying":
                    await self._verify(callback)
                elif callback.state == "armed":
                    await self._start(callback)
            except Exception:  # noqa: BLE001 - one callback must not stop the rest
                logger.exception("ticket callback %s failed", callback.id)
            finally:
                self._busy.discard(callback.id)

    async def sweep(self) -> int:
        """End callbacks whose ticket closed or whose time ran out; returns how many."""

        ended = 0
        for callback in await self.store.live():
            if callback.id in self._busy or callback.state == "running":
                continue
            if await self._ended_by_time_or_ticket(callback):
                ended += 1
        return ended

    async def run(self, interval_s: Callable[[], float] | float = 3600.0) -> None:
        """The sweep loop; a machine that never returns still has its callback end."""

        while True:
            delay = interval_s() if callable(interval_s) else interval_s
            await asyncio.sleep(max(60.0, float(delay)))
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 - the loop outlives one bad pass
                logger.exception("ticket callback sweep failed")

    async def _ended_by_time_or_ticket(self, callback: TicketCallback) -> bool:
        """End ``callback`` if its ticket closed or it expired; ``True`` if it did."""

        from ..tickets import TicketNotFoundError

        now = self.now()
        try:
            ticket = await self.tickets.get(callback.ticket_id)
        except TicketNotFoundError:
            ticket = None
        if ticket is None or ticket.state in _TICKET_CLOSED:
            state = ticket.state if ticket is not None else "gone"
            moved = await self.store.move(
                callback.id, ("armed", "verifying"), "cancelled", now=now,
                last_error=f"the ticket is {state}",
            )
            if moved is not None and ticket is not None:
                await self._note(moved, f"Callback to {moved.steward_id} ended: the ticket "
                                        f"was {state} before it was fixed by the steward.")
            return moved is not None
        expires = _parse(callback.expires_at)
        if expires is not None and now >= expires:
            moved = await self.store.move(
                callback.id, ("armed", "verifying"), "expired", now=now,
                last_error="expired before the ticket was fixed",
            )
            if moved is not None:
                await self._give_up(
                    moved,
                    ticket,
                    f"the callback expired after {CALLBACK_TTL.days} days "
                    + (
                        f"and {moved.attempts} run(s) without the condition clearing"
                        if moved.attempts
                        else f"without {moved.host_id} sending telemetry it could act on"
                    ),
                )
            return moved is not None
        return False

    def _facts(self, callback: TicketCallback, ticket: Any) -> list[str]:
        """Structured facts for the brief: the ticket and its sections, nothing a machine wrote."""

        return [
            f"This run was armed on ticket #{ticket.number} about the health "
            f"section(s) {', '.join(callback.sections)} of this machine, and started "
            "when the machine came back. The run counts as having fixed the ticket only "
            "if those sections read healthy afterwards.",
        ]

    async def _start(self, callback: TicketCallback) -> None:
        if await self._ended_by_time_or_ticket(callback):
            return
        ticket = await self.tickets.get(callback.ticket_id)
        spec = self.runner.catalog.get(callback.steward_id)
        if spec is None:
            moved = await self.store.move(
                callback.id, ("armed",), "failed", now=self.now(),
                last_error=f"steward {callback.steward_id} no longer exists",
            )
            if moved is not None:
                await self._give_up(moved, ticket, moved.last_error)
            return
        if await self.store.move(callback.id, ("armed",), "running", now=self.now()) is None:
            return
        try:
            run = await start_command(
                self.runner,
                spec,
                host_id=callback.host_id,
                username=callback.armed_by,
                effective_hash=callback.effective_hash,
                hosts=await self.hosts(),
                facts=self._facts(callback, ticket),
                on_finished=lambda run_id: self._finished(callback.id, run_id),
            )
        except CommandRefused as exc:
            await self._refused(callback, spec, ticket, exc)
            return
        except RuntimeError as exc:  # no executor / model / client configured
            await self.store.move(
                callback.id, ("running",), "armed", now=self.now(), last_error=str(exc)
            )
            return
        except Exception as exc:  # noqa: BLE001 - e.g. a spec that no longer validates
            logger.exception("ticket callback %s: the steward could not start", callback.id)
            moved = await self.store.move(
                callback.id, ("running",), "failed", now=self.now(), last_error=str(exc)
            )
            if moved is not None:
                await self._give_up(
                    moved, ticket, f"the steward {spec.id} could not be started: {exc}"
                )
            return
        await self._note(
            callback,
            f"{callback.host_id} is back: started the steward {spec.title} ({spec.id}) "
            f"for {callback.armed_by}, attempt {callback.attempts + 1} of "
            f"{callback.max_attempts} (run {run.id}).",
        )

    async def _refused(
        self, callback: TicketCallback, spec: StewardSpec, ticket: Any, exc: CommandRefused
    ) -> None:
        if exc.code in TRANSIENT_REFUSALS:
            await self.store.move(
                callback.id, ("running",), "armed", now=self.now(), last_error=str(exc)
            )
            return
        moved = await self.store.move(
            callback.id, ("running",), "failed", now=self.now(), last_error=str(exc)
        )
        if moved is not None:
            await self._give_up(moved, ticket, f"the steward {spec.id} could not be started: {exc}")

    async def _finished(self, callback_id: str, run_id: str | None) -> None:
        """A run of the callback ended. No row means the starter's refusal handles it."""

        if run_id is None:
            return
        callback = await self.store.get(callback_id)
        if callback is None or callback.state != "running":
            return
        run = await self.runner.store.get_run(run_id)
        if run is None or run.status == "skipped" or run.error == INTERRUPTED_ERROR:
            # Never reached the machine as an attempt: wait for the next push.
            await self.store.move(
                callback_id, ("running",), "armed", now=self.now(), last_run_id=run_id,
                last_error=(run.error if run is not None else "") or "the run did not happen",
            )
            return
        moved = await self.store.move(
            callback_id,
            ("running",),
            "verifying",
            now=self.now(),
            attempts_plus=1,
            last_run_id=run_id,
            run_finished_at=run.finished_at or _iso(self.now()),
            last_error=run.error or "",
        )
        if moved is None:
            return
        owned = callback_id not in self._busy
        self._busy.add(callback_id)
        try:
            await self._refresh(moved.host_id)
            await self._verify(moved)
        finally:
            if owned:
                self._busy.discard(callback_id)

    async def _refresh(self, host_id: str) -> bool:
        try:
            return bool(await self.refresh(host_id))
        except Exception:  # noqa: BLE001 - the next push is the evidence instead
            logger.info("collecting telemetry from %s after a callback run failed", host_id,
                        exc_info=True)
            return False

    async def _verify(self, callback: TicketCallback) -> None:
        """Decide a run's outcome from telemetry received after it ended.

        Success is the ticket's sections reading healthy, never the ticket's
        state: a person closing it while they still fail did not have a steward
        fix it. A callback past its time or on a closed ticket is still judged
        here, on the evidence of a run it was allowed to make; the sweep ends
        one that never gets that evidence.
        """

        latest = await self.latest(callback.host_id)
        finished = _parse(callback.run_finished_at)
        received = _parse(latest[0]) if latest is not None else None
        if latest is None or finished is None or received is None or received < finished:
            # Nothing newer than the run yet: the next push decides.
            return
        try:
            # The ordinary recovery path closes the alert ticket (and edits the
            # pushed channel message) when the condition is gone.
            await self.reevaluate(callback.host_id)
        except Exception:  # noqa: BLE001 - the verdict below does not depend on it
            logger.exception("re-evaluating %s after a callback run failed", callback.host_id)
        ticket = await self.tickets.get(callback.ticket_id)
        statuses = latest[1]
        still = [s for s in callback.sections if statuses.get(s, "ok") in _INCIDENT]
        run_ref = f"run {callback.last_run_id}"
        if not still:
            moved = await self.store.move(
                callback.id, ("verifying",), "succeeded", now=self.now(), last_error=""
            )
            if moved is None:
                return
            if ticket.state not in _TICKET_CLOSED:
                await self.tickets.transition(
                    ticket.id,
                    "resolved",
                    actor="system",
                    reason=f"{', '.join(moved.sections)} healthy after the steward "
                    f"{moved.steward_id}'s {run_ref}",
                    resolved_by=f"steward:{moved.steward_id}",
                )
            await self._note(
                moved,
                f"Fixed by the steward {moved.steward_id} ({run_ref}, attempt {moved.attempts} "
                f"of {moved.max_attempts}): {', '.join(moved.sections)} read healthy on "
                f"telemetry {callback.host_id} sent after the run. The ticket is resolved.",
            )
            return
        if ticket.state in _TICKET_CLOSED:
            moved = await self.store.move(
                callback.id, ("verifying",), "cancelled", now=self.now(),
                last_error=f"the ticket is {ticket.state}",
            )
            if moved is not None:
                await self._note(
                    moved,
                    f"Callback to {moved.steward_id} ended: the ticket was {ticket.state} "
                    f"while {', '.join(still)} still read unhealthy after {run_ref}.",
                )
            return
        expires = _parse(callback.expires_at)
        spent = callback.attempts >= callback.max_attempts
        if spent or (expires is not None and self.now() >= expires):
            state = "failed" if spent else "expired"
            moved = await self.store.move(
                callback.id, ("verifying",), state, now=self.now(),
                last_error=f"{', '.join(still)} still unhealthy after {callback.attempts} run(s)",
            )
            if moved is not None:
                await self._give_up(
                    moved,
                    ticket,
                    f"{', '.join(still)} still read unhealthy after {moved.attempts} run(s) "
                    f"(last: {run_ref})" + ("" if spent else "; the callback has expired"),
                )
            return
        moved = await self.store.move(
            callback.id, ("verifying",), "armed", now=self.now(),
            last_error=f"{', '.join(still)} still unhealthy after {run_ref}",
        )
        if moved is not None:
            await self._note(
                moved,
                f"Attempt {moved.attempts} of {moved.max_attempts} by the steward "
                f"{moved.steward_id} ({run_ref}) did not clear {', '.join(still)}; it runs "
                f"again at {moved.host_id}'s next telemetry push.",
            )

    # -- telling people ---------------------------------------------------------

    async def _note(self, callback: TicketCallback, summary: str, *, actor: str | None = None) -> None:
        try:
            await self.tickets.append_event(
                callback.ticket_id,
                kind="note",
                actor=actor or f"steward:{callback.steward_id}",
                summary=summary,
                fields={"callback_id": callback.id, "callback_state": callback.state,
                        "run_id": callback.last_run_id},
            )
        except Exception:  # noqa: BLE001 - the row is the record; the note is best-effort
            logger.exception("noting callback %s on its ticket failed", callback.id)

    async def _give_up(self, callback: TicketCallback, ticket: Any, why: str) -> None:
        """A callback ended without fixing its ticket: say so on the ticket and the channels."""

        await self._note(
            callback,
            f"The steward {callback.steward_id} did not fix this ticket: {why}. "
            "A person needs to look at it.",
        )
        if self.announce is None:
            return
        number = getattr(ticket, "number", None)
        label = f"ticket #{number}" if number is not None else "a ticket"
        base = public_link_base()
        link = f"{base}/#/inbox/ticket/{callback.ticket_id}" if base else ""
        body = f"{why}.\nArmed by {callback.armed_by} for {', '.join(callback.sections)}."
        note = Notification(
            title=f"{callback.host_id}: steward {callback.steward_id} could not fix {label}",
            body=body,
            priority="default",
            tags=["wrench"],
            agent_id=callback.host_id,
            kind="steward",
            event_type="ticket_callback",
            route="push",
            markdown=f"{body}\n[Open {label}]({link})" if link else "",
        )
        try:
            await self.announce(note)
        except Exception:  # noqa: BLE001 - delivery is best-effort
            logger.exception("announcing callback %s failed", callback.id)

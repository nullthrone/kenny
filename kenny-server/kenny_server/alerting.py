"""Server-side alert evaluation loop (ADR-0027).

Periodically re-evaluates every known agent's latest snapshot with the
authoritative health rules and notifies the operator on *transitions* only:
ok->warn, ok->crit, warn->crit (escalation), warn/crit->ok (recovery), a host
going missing, inventory changes and disk forecasts. Thresholds stay
exclusively in ``health_rules.py``; this module only compares the evaluated
status against the persisted last-known state (``AlertStateStore``) and
applies flap suppression:

* a per-scope cooldown (default 1 h) bounds a flapping section to at most one
  alert plus one recovery per cooldown window,
* escalations to ``crit`` bypass the cooldown,
* a section listed in ``health_rules.CONFIRM_BEFORE_ALARM`` must still report
  the same incident on a **newer** ``collected_at`` before it notifies, so a
  finding that appears and vanishes inside one push interval never reaches
  anyone,
* a recovery is only notified when the degraded episode itself was notified.

An escalation that cannot be sent yet -- because its cooldown has not passed,
or because it is still awaiting confirmation -- is held as a candidate in a
``pending:section:<name>`` scope rather than emitted-or-dropped. It used to be
dropped: ``status`` advanced to the new value while nothing was sent, the next
pass saw no change, and the warning was lost for the whole episode instead of
being delayed. A candidate is cleared when it is delivered, or when the
section resolves or improves before it was ever worth sending.

Every notification carries a ``route`` (ADR-0067) that decides who it
interrupts, independently of whether it opens a ticket:

* ``push`` -- sent to the human channels now: a crit finding (with any warn
  lines of the same pass as context), an inventory change on the operator's
  allowlist (``KENNY_ALERT_CHANGE_PUSH``), and the recovery of a pushed episode;
* ``daily`` -- held for one daily summary (``maybe_send_daily``): warn-only
  findings, a missing host, a disk forecast;
* ``record`` -- history and ticket queue only: every other change, the recovery
  of an episode that was never pushed, and any recovery an operator's own write
  caused.

A pushed alert is tracked on channels that can edit (Discord), and its
recovery rewrites that message instead of posting a new one.

Offline detection is push-based: an agent is offline when its newest snapshot
is older than ``offline_after_s`` (default three missed 900 s push intervals)
and the in-memory registry has no live connection. Health evaluation is
skipped for offline agents so stale snapshots cannot flap. Being offline
notifies no one -- a switched-off PC is not an incident. A host silent for
``KENNY_ALERT_MISSING_AFTER_DAYS`` is: its agent is broken or gone.

Every emitted notification is also persisted to the events table
(``kind='alert'``) as the audit trail and the weekly digest's input.

*Which* channels it is delivered on is asked fresh at every dispatch through a
``notifier_provider`` (ADR-0054), not fixed at construction, so an operator who
adds or clears a channel in the dashboard sees it apply to the next alert
without a restart. Zero channels stays a legitimate state — the loop still
evaluates and records, it just pushes nothing.

An optional ``open_ticket`` callable may be injected to turn a notification
into a ticket, and a matching ``close_ticket`` to resolve that ticket again
when the condition recovers — without the second half the surface only ever
accumulated, since a recovery is never itself ticketed and nothing else reads
health. Both are opt-in (a server without the ticket surface simply passes
nothing) and best-effort: delivery happens first and a failing ticket call is
logged, never raised — alerting must not become less reliable by gaining a
side effect (ADR-0027). *Which* notifications actually open a ticket is
operator policy, decided by an optional ``ticket_rules`` mirror
(:class:`kenny_server.ticket_rules.TicketRuleList`) consulted through the same
``ticket_rules.decide`` function whether or not any rule is configured -- with
no rules the outcome is byte-for-byte the old hardcoded rule (a genuine alert
opens a ticket, a recovery/change/digest does not), so the default cannot
drift from the ruled case. A recovery or the digest can never open a ticket,
no matter what any rule says (see ``ticket_rules.NEVER_TICKETED_KINDS``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from . import ticket_rules as ticket_rules_module
from .diffs import CHANGE_KINDS, SPECS, diff_snapshots
from .health_rules import CONFIRM_BEFORE_ALARM, evaluate_snapshot
from .notify import Notification, Notifier, doc_sections, resolve_doc
from .registry import AgentRegistry
from .store import AlertStateStore, EventStore, TelemetryStore
from .trends import DISK_FULL_ALERT_DAYS, disk_forecast

logger = logging.getLogger("kenny.alerting")

# ``posture`` (ADR-0058) ranks with ``ok``: a posture section never escalates
# and never recovers, so its transitions only ever update state -- which is
# what gives a posture finding its age.
_ORDER = {"ok": 0, "posture": 0, "warn": 1, "crit": 2}
_INCIDENT = ("warn", "crit")
_TITLE_MAX = 96

DEFAULT_COOLDOWN_S = 3600
# Three missed 900 s telemetry pushes (docs/protocol.md § Telemetry).
DEFAULT_OFFLINE_AFTER_S = 2700

DEFAULT_MISSING_AFTER_DAYS = 7
DEFAULT_DAILY_HOUR = 8
# Additions of persistence mechanisms and any account drift: rare by
# construction, and each one is worth an interruption. Updaters rewrite
# autostart/task commands and processes open ephemeral ports all day, which is
# why ``changed`` kinds and ``listening_ports`` are not in the default.
DEFAULT_CHANGE_PUSH = "local_accounts,autostart:added,scheduled_tasks:added,browser_extensions:added"

_PRUNE_EVERY = timedelta(hours=24)
_MAX_DAILY_LINES = 25

_DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# A zero-argument callable returning the channels to deliver on right now. The
# composition root passes ``notify.NotifierProvider`` (settings-backed, so an
# operator change applies to the next dispatch, ADR-0054); a fixed list passed
# as ``notifiers=`` is wrapped into one of these internally.
NotifierSource = Callable[[], Sequence[Notifier]]


class _Prunable(Protocol):
    async def prune(self, *, retention_days: int | None = None) -> int: ...


def parse_change_allowlist(raw: str) -> frozenset[tuple[str, str]]:
    """Parse ``KENNY_ALERT_CHANGE_PUSH`` into ``(section, kind)`` pairs.

    An entry is ``section`` (every kind) or ``section:kind``. Entries naming a
    section ``diffs.SPECS`` does not diff, or a kind outside
    ``diffs.CHANGE_KINDS``, can never match anything; they are logged and
    dropped rather than silently kept.
    """

    pairs: set[tuple[str, str]] = set()
    for entry in str(raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        section, _, kind = entry.partition(":")
        section, kind = section.strip(), kind.strip()
        if section not in SPECS or (kind and kind not in CHANGE_KINDS):
            logger.warning("ignoring change-push entry %r: unknown section or kind", entry)
            continue
        if kind:
            pairs.add((section, kind))
        else:
            pairs.update((section, k) for k in CHANGE_KINDS)
    return frozenset(pairs)


def _age(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return f"{max(0, int(delta.total_seconds() // 60))}m"
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


class AlertEngine:
    """Evaluates transitions and fans notifications out to the channels."""

    def __init__(
        self,
        *,
        store: TelemetryStore,
        alert_state: AlertStateStore,
        event_store: EventStore,
        registry: AgentRegistry,
        notifiers: Sequence[Notifier] | None = None,
        notifier_provider: NotifierSource | None = None,
        settings: Any = None,
        cooldown_s: int = DEFAULT_COOLDOWN_S,
        offline_after_s: int = DEFAULT_OFFLINE_AFTER_S,
        prunables: list[tuple[_Prunable, str | None]] | None = None,
        digest_enabled: bool = True,
        digest_day: str = "mon",
        digest_hour: int = 8,
        missing_after_days: int = DEFAULT_MISSING_AFTER_DAYS,
        daily_hour: int = DEFAULT_DAILY_HOUR,
        change_push: str = DEFAULT_CHANGE_PUSH,
        open_ticket: Callable[[Notification], Awaitable[str | None]] | None = None,
        close_ticket: Callable[[Notification], Awaitable[str | None]] | None = None,
        ticket_rules: Any = None,
    ) -> None:
        self._store = store
        self._alert_state = alert_state
        self._event_store = event_store
        self._registry = registry
        # Channels are obtained per dispatch, never captured at boot (ADR-0054):
        # ``notifier_provider`` is asked again for every notification, so adding,
        # changing or clearing a channel from the dashboard applies immediately.
        # ``notifiers=`` remains for direct construction (tests, a server with a
        # deliberately fixed set) and is wrapped in a constant provider; passing
        # both would make it ambiguous which one actually delivers, so it is
        # refused loudly rather than resolved silently.
        if notifiers is not None and notifier_provider is not None:
            raise ValueError("pass either notifiers= or notifier_provider=, not both")
        fixed: tuple[Notifier, ...] = tuple(notifiers or ())
        self._notifier_source: NotifierSource = notifier_provider or (lambda: fixed)
        # When ``settings`` is provided the alerting knobs are read live from it
        # (DB > env > default) on every pass, so an operator change from the
        # dashboard takes effect without a restart. The scalar kwargs remain as
        # fallbacks for direct construction in tests.
        self._settings = settings
        self._cooldown_s = cooldown_s
        self._offline_after_s = offline_after_s
        self._digest_enabled_fb = digest_enabled
        self._digest_day_fb = digest_day
        self._digest_hour_fb = digest_hour
        self._missing_after_days = missing_after_days
        self._daily_hour_fb = daily_hour
        self._change_push_fb = change_push
        # Each entry is (store, settings_key). ``settings_key`` is None for a
        # store with no operator-facing retention setting yet (ADR-0051) --
        # those keep pruning on their own hardcoded default. A key present
        # here must also carry a live-reread ``@property`` below or a spec
        # lookup in ``_maybe_prune``; see ``KENNY_TELEMETRY_RETENTION_DAYS``.
        self._prunables = prunables or []
        self._last_prune: datetime | None = None
        # Last resolved value per settings key, so a *decrease* (operator
        # tightens retention in the dashboard) can force an immediate prune
        # pass instead of waiting up to _PRUNE_EVERY -- see _maybe_prune.
        self._last_retention: dict[str, int] = {}
        # Injected by the composition root when the ticket surface exists; see
        # ``_dispatch``. None means alerts never open tickets, which is the
        # behaviour of every server that does not wire one.
        self._open_ticket = open_ticket
        self._close_ticket = close_ticket
        # Operator-authored auto-ticket rules (ticket_rules.py), consulted in
        # ``_dispatch``. None mirrors an empty rule set -- ``ticket_rules.decide``
        # is called either way, so "no mirror wired" and "mirror with zero rules"
        # produce the identical decision.
        self._ticket_rules = ticket_rules

    # -- live config accessors -------------------------------------------------

    @property
    def _notifiers(self) -> Sequence[Notifier]:
        """The channels to deliver on right now — resolved, never remembered.

        Zero channels is a legitimate state: the loop keeps evaluating and
        recording history, it just pushes nothing. A provider that raises is
        treated the same way, because a broken channel lookup must not be able
        to stop the evaluation pass.
        """

        try:
            return self._notifier_source()
        except Exception:  # noqa: BLE001 - delivery is best-effort (ADR-0027)
            logger.exception("resolving the alert delivery channels failed")
            return ()

    def _cfg(self, key: str, fallback: Any) -> Any:
        return self._settings.get(key) if self._settings is not None else fallback

    @property
    def _cooldown(self) -> timedelta:
        return timedelta(seconds=self._cfg("KENNY_ALERT_COOLDOWN_SECS", self._cooldown_s))

    @property
    def _offline_after(self) -> timedelta:
        return timedelta(
            seconds=self._cfg("KENNY_ALERT_OFFLINE_AFTER_SECS", self._offline_after_s)
        )

    @property
    def _missing_after(self) -> timedelta:
        return timedelta(
            days=self._cfg("KENNY_ALERT_MISSING_AFTER_DAYS", self._missing_after_days)
        )

    @property
    def _change_push(self) -> frozenset[tuple[str, str]]:
        return parse_change_allowlist(self._cfg("KENNY_ALERT_CHANGE_PUSH", self._change_push_fb))

    # -- one evaluation pass -------------------------------------------------

    async def evaluate_once(self, now: datetime | None = None) -> list[Notification]:
        """Evaluate every known agent once; returns the notifications dispatched.

        Every one of them is recorded and put to the ticket decision; which
        channels it reached depends on its ``route``.
        """

        now = now or datetime.now(timezone.utc)
        sent: list[Notification] = []
        for agent_id in await self._store.known_agents():
            try:
                sent.extend(await self._evaluate_agent(agent_id, now))
            except Exception:  # noqa: BLE001 - one bad agent must not stop the rest
                logger.exception("alert evaluation failed for %s", agent_id)
        return sent

    async def evaluate_agent_now(self, agent_id: str = "") -> list[Notification]:
        """Re-evaluate one host (or the whole fleet) immediately.

        The hook an operator write needs. Adding a suppression rule changes
        what the *next* read scores, but nothing was re-reading until the
        loop's next pass -- so a muted pattern's alert stayed live and the
        ticket it opened stayed open, which is the single most common reason
        the module looked like it ignored suppression. Running a pass here
        produces the ordinary recovery transition, and the recovery closes the
        ticket through the same path every other section uses.

        Best-effort and never raised: an operator's rule write must not fail
        because re-evaluation did.
        """

        now = datetime.now(timezone.utc)
        agents = [agent_id] if agent_id else await self._store.known_agents()
        sent: list[Notification] = []
        for aid in agents:
            try:
                sent.extend(await self._evaluate_agent(aid, now, operator_initiated=True))
            except Exception:  # noqa: BLE001 - an operator write must still succeed
                logger.exception("re-evaluation after an operator change failed for %s", aid)
        return sent

    async def _evaluate_agent(
        self, agent_id: str, now: datetime, *, operator_initiated: bool = False
    ) -> list[Notification]:
        latest = await self._store.latest(agent_id)
        if latest is None:
            return []
        state = await self._alert_state.get_all(agent_id)
        out: list[Notification] = []

        missing_note, is_offline = await self._offline_transition(agent_id, latest, state, now)
        if missing_note is not None:
            out.append(missing_note)
        if not is_offline:
            out.extend(
                await self._health_transitions(
                    agent_id, latest, state, now, operator_initiated=operator_initiated
                )
            )
            out.extend(await self._change_notifications(agent_id, latest, state, now))
        for note in out:
            await self._dispatch(note, now)
        return out

    # -- offline detection ----------------------------------------------------

    async def _offline_transition(
        self,
        agent_id: str,
        latest: dict[str, Any],
        state: dict[str, dict[str, Any]],
        now: datetime,
    ) -> tuple[Notification | None, bool]:
        """Track offline silently; notify only when a host goes missing.

        ``offline`` gates health evaluation and nothing else: a family PC that
        is switched off is not an incident, and announcing it (plus its return)
        was most of the channel's noise. ``missing`` -- no telemetry for days --
        is: the agent is broken or gone, and nobody would otherwise notice.
        """

        received = _parse_ts(latest.get("received_at"))
        agent = self._registry.get(agent_id)
        connected = agent.online if agent is not None else False
        silent_for = now - received if received is not None else None
        is_offline = not connected and silent_for is not None and silent_for > self._offline_after
        is_missing = not connected and silent_for is not None and silent_for > self._missing_after

        row = state.get("offline")
        prev = row["status"] if row else "online"
        new = "offline" if is_offline else "online"
        legacy_note: Notification | None = None
        if new != prev:
            if new == "online" and self._episode_was_notified(row):
                # An offline episode announced before offline stopped notifying
                # still has its ticket open; close it without telling anyone.
                # No new episode can take this branch: going offline never
                # stamps ``last_notified_at`` any more.
                legacy_note = Notification(
                    title=f"{agent_id} is back online",
                    body="Telemetry is flowing again.",
                    priority="low",
                    tags=["white_check_mark"],
                    agent_id=agent_id,
                    kind="recovery",
                    event_type="offline",
                    route="record",
                )
            await self._alert_state.upsert(
                agent_id,
                "offline",
                status=new,
                since=now.isoformat(),
                last_notified_at=(row or {}).get("last_notified_at"),
            )

        missing_row = state.get("missing")
        was_missing = bool(missing_row and missing_row["status"] == "missing")
        if is_missing == was_missing:
            return legacy_note, is_offline
        note: Notification | None = None
        if is_missing:
            days = silent_for.total_seconds() / 86400 if silent_for else 0.0
            note = Notification(
                title=f"{agent_id} has not reported for {days:.0f} days",
                body=(
                    f"No telemetry since {latest.get('received_at')}; "
                    "the agent may be broken or uninstalled."
                ),
                priority="default",
                tags=["electric_plug"],
                agent_id=agent_id,
                kind="alert",
                event_type="offline",
                route="daily",
            )
        elif self._episode_was_notified(missing_row):
            # Closes the ticket the missing alert opened; interrupts no one.
            note = Notification(
                title=f"{agent_id} is reporting again",
                body="Telemetry is flowing again.",
                priority="low",
                tags=["white_check_mark"],
                agent_id=agent_id,
                kind="recovery",
                event_type="offline",
                route="record",
            )
        await self._alert_state.upsert(
            agent_id,
            "missing",
            status="missing" if is_missing else "present",
            since=now.isoformat(),
            last_notified_at=now.isoformat() if note else (missing_row or {}).get("last_notified_at"),
        )
        return note, is_offline

    # -- health transitions ----------------------------------------------------

    async def _health_transitions(
        self,
        agent_id: str,
        latest: dict[str, Any],
        state: dict[str, dict[str, Any]],
        now: datetime,
        *,
        operator_initiated: bool = False,
    ) -> list[Notification]:
        agent_os = getattr(self._registry.get(agent_id), "os", "windows")
        evaluation = evaluate_snapshot(latest["snapshot"], agent_os=agent_os, now=now)
        alert_lines: list[str] = []
        recovery_lines: list[str] = []
        alert_worst = "ok"
        # Which sections actually escalated/recovered in this pass, for the
        # ticket-rule matcher (ticket_rules.py) -- only sections whose lines made it
        # into the notification body are subjects, so a rule fires exactly on
        # what the operator would read.
        alert_sections: dict[str, str] = {}
        recovery_sections: dict[str, str] = {}
        alert_items: list[dict[str, str]] = []
        recovery_items: list[dict[str, str]] = []
        # Whether any recovered section belonged to a pushed episode -- only
        # then is its recovery news to the human channels (ADR-0067).
        recovery_pushed = False

        headline = ""
        collected_at = str(latest.get("collected_at") or "")
        for name, section in evaluation["sections"].items():
            scope = f"section:{name}"
            pending_scope = f"pending:{scope}"
            row = state.get(scope)
            pending = state.get(pending_scope)
            old = row["status"] if row else "ok"
            new = section["status"]
            # The body carries the finding, not the transition: what is wrong
            # and since when is what a reader acts on; "ok -> crit" is
            # bookkeeping the Log page already keeps.
            reason = section.get("reason") or section.get("summary") or ""
            notified = False
            escalated = new in _INCIDENT and _ORDER.get(new, 0) > _ORDER.get(old, 0)

            # A candidate is an escalation that has not been sent yet. It is
            # recorded rather than emitted-or-dropped, because a cooldown used
            # to swallow one outright: `status` advanced to the new value, the
            # next pass saw no change, and the warn was lost for the whole
            # episode instead of being delayed.
            if escalated:
                if not pending or pending["status"] != new:
                    await self._alert_state.upsert(
                        agent_id, pending_scope, status=new, since=collected_at or now.isoformat()
                    )
                    pending = {"status": new, "since": collected_at or now.isoformat()}
            elif pending and (new not in _INCIDENT or _ORDER.get(new, 0) < _ORDER.get(pending["status"], 0)):
                # It resolved or improved before it was ever worth sending.
                await self._alert_state.remove(agent_id, pending_scope)
                pending = None

            if pending and pending["status"] == new:
                # Escalations to crit still bypass the rate limit; what they no
                # longer bypass is confirmation. A section in
                # CONFIRM_BEFORE_ALARM must still say the same thing on a
                # *newer* collection -- one that appears and vanishes inside a
                # single push interval never reaches anyone.
                confirmed = (
                    name not in CONFIRM_BEFORE_ALARM
                    or not collected_at
                    or not pending.get("since")
                    or collected_at > pending["since"]
                )
                if confirmed and (new == "crit" or self._cooldown_passed(row, now)):
                    alert_lines.append(f"[{new.upper()}] {name}: {reason}".rstrip(": "))
                    alert_sections[name] = new
                    alert_items.append({"section": name, "severity": new, "text": reason})
                    if new == "crit" and alert_worst != "crit":
                        alert_worst = "crit"
                        headline = f"{name}: {reason}" if reason else name
                    elif alert_worst == "ok":
                        alert_worst = "warn"
                        headline = f"{name}: {reason}" if reason else name
                    notified = True
                    await self._alert_state.remove(agent_id, pending_scope)
            elif new == old:
                continue
            elif new == "ok" and self._episode_was_notified(row):
                recovery_lines.append(f"[RESOLVED] {name}: {reason}".rstrip(": "))
                recovery_sections[name] = old
                recovery_items.append({"section": name, "severity": "resolved", "text": reason})
                notified = True

            if new not in _INCIDENT and f"pushed:{scope}" in state:
                # The pushed episode is over, whether or not its end is news.
                recovery_pushed = recovery_pushed or name in recovery_sections
                await self._alert_state.remove(agent_id, f"pushed:{scope}")

            if new == old and not notified:
                continue
            # crit -> warn improvements, and every transition into or out of
            # posture, update state silently (ADR-0058). `since` only moves
            # when the status does: it is the age of the current finding, which
            # the host page and the ticket both read.
            await self._alert_state.upsert(
                agent_id,
                scope,
                status=new,
                since=now.isoformat() if new != old else (row or {}).get("since") or now.isoformat(),
                last_notified_at=now.isoformat() if notified else (row or {}).get("last_notified_at"),
            )

        # Track the roll-up too (read by the digest; no separate notification —
        # the per-section lines above already carry the story).
        overall_row = state.get("overall")
        overall = evaluation["overall"]
        if overall != (overall_row["status"] if overall_row else "ok"):
            await self._alert_state.upsert(
                agent_id,
                "overall",
                status=overall,
                since=now.isoformat(),
                last_notified_at=(overall_row or {}).get("last_notified_at"),
            )

        out: list[Notification] = []
        if alert_lines:
            title = f"{agent_id}: {headline}"
            if len(title) > _TITLE_MAX:
                title = title[: _TITLE_MAX - 1] + "…"
            # One notification per pass keeps one ticket per pass. A crit pushes
            # and carries the pass's warn lines along as context; warn alone is
            # work for the inbox and the daily summary, not an interruption.
            pushed = alert_worst == "crit"
            out.append(
                Notification(
                    title=title,
                    body="\n".join(alert_lines),
                    priority="high" if pushed else "default",
                    tags=["rotating_light" if pushed else "warning"],
                    agent_id=agent_id,
                    kind="alert",
                    event_type="health",
                    sections=alert_sections,
                    route="push" if pushed else "daily",
                    items=alert_items,
                )
            )
            if pushed:
                for name in alert_sections:
                    await self._alert_state.upsert(
                        agent_id, f"pushed:section:{name}", status="pushed", since=now.isoformat()
                    )
        if recovery_lines:
            out.append(
                Notification(
                    title=f"{agent_id} recovered",
                    body="\n".join(recovery_lines),
                    priority="low",
                    tags=["white_check_mark"],
                    agent_id=agent_id,
                    kind="recovery",
                    event_type="health",
                    sections=recovery_sections,
                    # An operator's own write (a suppression) caused this one:
                    # telling them is noise. Tracked messages are still edited.
                    route="push" if recovery_pushed and not operator_initiated else "record",
                    items=recovery_items,
                )
            )
        return out

    # -- inventory changes & forecasts (diffs.py / trends.py) --------------------

    async def _change_notifications(
        self,
        agent_id: str,
        latest: dict[str, Any],
        state: dict[str, dict[str, Any]],
        now: datetime,
    ) -> list[Notification]:
        """Diff consecutive snapshots and check the disk forecast.

        Gated on a persisted cursor (scope ``change:_cursor``) holding the last
        processed ``collected_at``, so the work runs once per new snapshot (not
        per evaluation tick) and a restart never re-notifies an old diff.
        """

        cursor_row = state.get("change:_cursor")
        cursor = cursor_row["status"] if cursor_row else None
        latest_at = str(latest.get("collected_at"))
        if cursor == latest_at:
            return []
        await self._alert_state.upsert(
            agent_id,
            "change:_cursor",
            status=latest_at,
            since=now.isoformat(),
            last_notified_at=(cursor_row or {}).get("last_notified_at"),
        )
        out: list[Notification] = []
        # Only diff when this is genuinely the next snapshot after a processed
        # one; on the very first sighting just set the cursor.
        if cursor is not None:
            history = await self._store.history(agent_id, limit=2)
            if len(history) == 2:
                changes = diff_snapshots(history[1]["snapshot"], latest["snapshot"])
                out.extend(await self._notify_changes(agent_id, changes, state, now))
        forecast_note = await self._forecast_alert(agent_id, state, now)
        if forecast_note is not None:
            out.append(forecast_note)
        return out

    async def _notify_changes(
        self,
        agent_id: str,
        changes: list[dict[str, Any]],
        state: dict[str, dict[str, Any]],
        now: datetime,
    ) -> list[Notification]:
        """Split one snapshot's diff into what pushes and what is only recorded.

        A change on the operator's allowlist pushes and is **not** held back by
        the section cooldown: dropping a new autostart entry because another
        one appeared within the hour is a security miss, and an allowlisted
        entry that flaps is a signal in itself. Everything else is recorded,
        under the cooldown, for the digest and the Log page.
        """

        allow = self._change_push
        by_section: dict[str, list[dict[str, Any]]] = {}
        for change in changes:
            by_section.setdefault(change["section"], []).append(change)

        push_lines: list[str] = []
        push_items: list[dict[str, str]] = []
        record_lines: list[str] = []
        push_sections: dict[str, str] = {}
        record_sections: dict[str, str] = {}
        for section, section_changes in sorted(by_section.items()):
            scope = f"change:{section}"
            cooled = self._cooldown_passed(state.get(scope), now)
            emitted = False
            for c in section_changes:
                detail = f" ({c['detail']})" if c.get("detail") else ""
                line = f"{section}: {c['kind']} {c['key']}{detail}"
                if (section, c["kind"]) in allow:
                    push_lines.append(line)
                    push_items.append(
                        {"section": section, "severity": "warn", "text": f"{c['kind']} {c['key']}{detail}"}
                    )
                    # ``change`` has no severity axis of its own, so each
                    # subject carries "" -- the severity-wildcard slot
                    # ticket_rules.decide expects from such a producer.
                    push_sections[section] = ""
                    emitted = True
                elif cooled:
                    record_lines.append(line)
                    record_sections[section] = ""
                    emitted = True
            if emitted:
                await self._alert_state.upsert(
                    agent_id,
                    scope,
                    status="changed",
                    since=now.isoformat(),
                    last_notified_at=now.isoformat(),
                )

        out: list[Notification] = []
        if push_lines:
            out.append(
                Notification(
                    title=f"{agent_id}: {len(push_lines)} change(s) detected",
                    body="\n".join(push_lines),
                    priority="high" if "local_accounts" in push_sections else "default",
                    tags=["mag"],
                    agent_id=agent_id,
                    kind="change",
                    event_type="change",
                    sections=push_sections,
                    route="push",
                    items=push_items,
                )
            )
        if record_lines:
            out.append(
                Notification(
                    title=f"{agent_id}: {len(record_lines)} change(s) recorded",
                    body="\n".join(record_lines),
                    priority="high" if "local_accounts" in record_sections else "default",
                    tags=["mag"],
                    agent_id=agent_id,
                    kind="change",
                    event_type="change",
                    sections=record_sections,
                    route="record",
                )
            )
        return out

    async def _forecast_alert(
        self,
        agent_id: str,
        state: dict[str, dict[str, Any]],
        now: datetime,
    ) -> Notification | None:
        since = (now - timedelta(days=30)).date().isoformat()
        daily = await self._store.daily_latest(agent_id, since)
        filling = [
            f
            for f in disk_forecast(daily)
            if f["days_until_full"] is not None and f["days_until_full"] < DISK_FULL_ALERT_DAYS
        ]
        scope = "section:disk_forecast"
        row = state.get(scope)
        if not filling:
            if row and row["status"] != "ok":
                await self._alert_state.upsert(
                    agent_id,
                    scope,
                    status="ok",
                    since=now.isoformat(),
                    last_notified_at=row.get("last_notified_at"),
                )
            return None
        if row and row["status"] == "warn":
            # Once per episode: a forecast that stays true is not news again
            # tomorrow. It stays visible in the daily summary's open count, on
            # the host page and in the digest.
            return None
        await self._alert_state.upsert(
            agent_id, scope, status="warn", since=now.isoformat(), last_notified_at=now.isoformat()
        )
        lines = [
            f"{f['mount']}: ~{f['days_until_full']:.0f}d until full "
            f"({f['current_percent']:.0f}% now, +{f['slope_percent_per_day']:.2f}%/day)"
            for f in filling
        ]
        return Notification(
            title=f"{agent_id}: disk filling up",
            body="\n".join(lines),
            priority="default",
            tags=["chart_with_upwards_trend"],
            agent_id=agent_id,
            kind="alert",
            event_type="disk_forecast",
            route="daily",
            # The forecast is a statement about the ``disk`` section, so it
            # names it: that is what puts it on the same ticket subject as an
            # acute disk finding instead of a second ticket for one filling
            # volume, and what lets an auto-ticket rule target it at all
            # (``ticket_rules.decide`` derives its subject from ``sections``).
            # ``warn`` matches the severity the priority already implied, so no
            # existing ``open_crit`` rule starts firing because of this.
            sections={"disk": "warn"},
        )

    # -- helpers ----------------------------------------------------------------

    def _cooldown_passed(self, row: dict[str, Any] | None, now: datetime) -> bool:
        last = _parse_ts((row or {}).get("last_notified_at"))
        return last is None or now - last > self._cooldown

    @staticmethod
    def _episode_was_notified(row: dict[str, Any] | None) -> bool:
        """True when a notification went out during the current degraded episode."""

        if not row:
            return False
        last = _parse_ts(row.get("last_notified_at"))
        since = _parse_ts(row.get("since"))
        return last is not None and (since is None or last >= since)

    async def _dispatch(self, note: Notification, now: datetime) -> None:
        if note.kind in ("recovery", "digest"):
            level = "info"
        else:
            level = "crit" if note.priority in ("high", "urgent") else "warn"
        event_id = await self._event_store.insert_alert(
            agent_id=note.agent_id,
            message=f"{note.title}\n{note.body}",
            level=level,
            # ``title`` verbatim so a reader never has to re-split ``message``;
            # ``event_type`` so it can say which producer raised this.
            fields={
                "kind": note.kind,
                "priority": note.priority,
                "title": note.title,
                "event_type": note.event_type,
                "route": note.route,
            },
            at=now.isoformat(),
        )
        # Each channel is isolated: ``Notifier.send`` swallows its own transport
        # errors (ADR-0027), and this guard covers everything else a channel
        # could throw — so one misconfigured or misbehaving channel can never
        # cost the others their delivery.
        for notifier in self._notifiers:
            try:
                await self._deliver(notifier, note, now)
            except Exception:  # noqa: BLE001 - one dead channel must not stop the rest
                logger.exception(
                    "delivery via %s failed", getattr(notifier, "name", type(notifier).__name__)
                )
        # Which notifications become a ticket is operator policy (ticket_rules.py),
        # decided by ``ticket_rules.decide`` -- called the same way whether or
        # not a mirror is wired, so "no rules configured" and "no mirror at
        # all" can never diverge. Runs after delivery and inside this swallow,
        # so neither the rule lookup nor the ticket surface can ever make a
        # notification late or lost.
        if self._open_ticket is not None:
            try:
                rules_map = self._ticket_rules.mapping() if self._ticket_rules is not None else {}
                decision = ticket_rules_module.decide(
                    rules_map,
                    kind=note.kind,
                    agent_id=note.agent_id or "",
                    event_type=note.event_type,
                    priority=note.priority,
                    sections=note.sections,
                )
                if decision.open:
                    ticket_id = await self._open_ticket(note)
                    # The alert row is already written; this hangs it on the
                    # ticket it opened (or, for a recurrence, on the open
                    # ticket that absorbed it) so the ticket can show what it
                    # is about without re-deriving anything.
                    if ticket_id:
                        await self._event_store.link_alert_to_ticket(event_id, ticket_id)
            except Exception:  # noqa: BLE001 - alerting stays best-effort
                logger.exception("the ticket decision for %r failed", note.title)
        # A recovery closes what the alert opened. It is not routed through
        # ``ticket_rules`` -- those decide whether work gets *created*, and a
        # recovery is explicitly never ticketed by them. Nothing was deciding
        # whether work gets closed, which is why an automatically opened
        # ticket could only ever be closed by a person, even after the
        # operator suppressed the pattern that caused it.
        if self._close_ticket is not None and note.kind == "recovery":
            try:
                await self._close_ticket(note)
            except Exception:  # noqa: BLE001 - alerting stays best-effort
                logger.exception("resolving the ticket for %r failed", note.title)

    async def _deliver(self, notifier: Notifier, note: Notification, now: datetime) -> None:
        """Hand ``note`` to one channel according to its route (ADR-0067).

        * A machine channel (``receives_all_routes``) gets everything.
        * A channel that can edit (``edit``) never gets a new message for a
          recovery: the message the alert became is rewritten instead, whatever
          the recovery's route -- an edit is not a ping, and a resolved alert
          left red would be a false statement in the channel.
        * Otherwise a human channel gets ``push`` only. A pushed health alert
          on a channel that reports message ids (``send_tracked``) is
          remembered so its recovery can find it.
        """

        if getattr(notifier, "receives_all_routes", False):
            await notifier.send(note)
            return
        edit = getattr(notifier, "edit", None)
        if note.kind == "recovery" and edit is not None:
            await self._resolve_messages(notifier, note, now)
            return
        if note.route != "push":
            return
        send_tracked = getattr(notifier, "send_tracked", None)
        if send_tracked is not None and note.kind == "alert" and note.items and note.agent_id:
            _error, message_id, doc = await send_tracked(note, at=now)
            if message_id:
                await self._alert_state.save_message(
                    notifier.name, message_id, note.agent_id, doc, at=now.isoformat()
                )
            return
        await notifier.send(note)

    async def _resolve_messages(self, notifier: Any, note: Notification, now: datetime) -> None:
        """Mark the recovered sections resolved on every open message naming them."""

        if not note.agent_id or not note.sections:
            return
        for msg in await self._alert_state.open_messages(notifier.name, note.agent_id):
            doc = msg["doc"]
            if not resolve_doc(doc, set(note.sections) & doc_sections(doc), at=now):
                continue
            error = await notifier.edit(msg["message_id"], doc)
            if error is not None and error.startswith("HTTP 404"):
                # Deleted by hand, or posted through a webhook since replaced
                # (ADR-0054): there is nothing left to edit.
                await self._alert_state.drop_message(notifier.name, msg["message_id"])
                continue
            if error is None:
                await self._alert_state.update_message(
                    notifier.name, msg["message_id"], doc, resolved_at=doc.get("resolved_at")
                )

    # -- loop ---------------------------------------------------------------------

    async def run(self, interval_s: int, initial_delay_s: float = 10.0) -> None:
        """Evaluate forever; also runs daily retention pruning (ADR-0007)."""

        await asyncio.sleep(initial_delay_s)
        while True:
            try:
                await self.evaluate_once()
                await self.maybe_send_digest()
                await self.maybe_send_daily()
                await self._maybe_prune()
            except Exception:  # noqa: BLE001 - never let the loop die
                logger.exception("alert evaluation pass failed")
            # Re-read the cadence each pass so a dashboard change retimes the
            # running loop. A runtime 0/negative keeps the loop alive at the
            # startup interval (disabling entirely stays a restart decision).
            interval = self._cfg("KENNY_ALERT_INTERVAL_SECS", interval_s)
            await asyncio.sleep(interval if interval and interval > 0 else interval_s)

    # -- weekly digest (ADR-0027) -------------------------------------------------

    async def maybe_send_digest(self, now: datetime | None = None) -> bool:
        """Send the weekly digest when the scheduled slot has passed; True if sent.

        The last-sent timestamp is persisted (``alert_state`` scope ``digest``),
        so a restart never double-sends. On the very first run the current time
        becomes the baseline without sending — the first digest arrives at the
        next scheduled slot instead of on install.
        """

        digest_enabled = self._cfg("KENNY_DIGEST_ENABLED", self._digest_enabled_fb)
        if not digest_enabled or not self._notifiers:
            return False
        digest_day = _DAY_INDEX.get(
            str(self._cfg("KENNY_DIGEST_DAY", self._digest_day_fb)).strip().lower()[:3], 0
        )
        digest_hour = max(0, min(23, int(self._cfg("KENNY_DIGEST_HOUR", self._digest_hour_fb))))
        now = now or datetime.now(timezone.utc)
        row = await self._alert_state.get("", "digest")
        if row is None:
            await self._alert_state.upsert(
                "", "digest", status=now.isoformat(), since=now.isoformat(), last_notified_at=None
            )
            return False
        last_sent = _parse_ts(row["status"]) or now
        days_back = (now.weekday() - digest_day) % 7
        slot = (now - timedelta(days=days_back)).replace(
            hour=digest_hour, minute=0, second=0, microsecond=0
        )
        if slot > now:
            slot -= timedelta(days=7)
        if last_sent >= slot:
            return False
        from .digest import build_digest

        title, body = await build_digest(
            self._store, self._event_store, self._registry, now=now
        )
        await self._dispatch(
            Notification(
                title=title,
                body=body,
                priority="low",
                tags=["newspaper"],
                agent_id=None,
                kind="digest",
                # "digest" is not a member of ticket_rules.EVENT_TYPES -- kind
                # alone already guarantees NEVER_TICKETED_KINDS catches it, this
                # label just keeps every producer identifiable in the webhook
                # payload and in the seam tests.
                event_type="digest",
            ),
            now,
        )
        await self._alert_state.upsert(
            "", "digest", status=now.isoformat(), since=now.isoformat(), last_notified_at=now.isoformat()
        )
        return True

    # -- daily summary (ADR-0067) ------------------------------------------------

    async def maybe_send_daily(self, now: datetime | None = None) -> bool:
        """Send the daily summary when its slot has passed; True if sent.

        What waited for it: warn findings, missing hosts and disk forecasts
        routed ``daily`` since the last summary and **still open** now -- one
        that came and went in between is not worth reading. Derived from
        ``alert_state`` rather than a queue, so a restart neither loses nor
        repeats anything. Nothing is sent when nothing is new, and the slot the
        weekly digest occupies is skipped so that morning brings one message.
        State follows the digest's pattern (scope ``daily``; the first run only
        records a baseline).
        """

        if not self._notifiers:
            return False
        now = now or datetime.now(timezone.utc)
        hour = max(0, min(23, int(self._cfg("KENNY_ALERT_DAILY_HOUR", self._daily_hour_fb))))
        row = await self._alert_state.get("", "daily")
        if row is None:
            await self._alert_state.upsert(
                "", "daily", status=now.isoformat(), since=now.isoformat(), last_notified_at=None
            )
            return False
        last_sent = _parse_ts(row["status"]) or now
        slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if slot > now:
            slot -= timedelta(days=1)
        if last_sent >= slot:
            return False

        if self._is_digest_slot(slot):
            findings: list[tuple[str, str, str]] = []
            older = 0
        else:
            findings, older = await self._daily_findings(last_sent, now)
        # The window always advances: what was new before this slot is not
        # carried into the next summary, only into the open count.
        await self._alert_state.upsert(
            "", "daily", status=now.isoformat(), since=now.isoformat(),
            last_notified_at=now.isoformat() if findings else row.get("last_notified_at"),
        )
        if not findings:
            return False

        lines = [f"{agent} · {section}: {text}" for agent, section, text in findings[:_MAX_DAILY_LINES]]
        if len(findings) > _MAX_DAILY_LINES:
            lines.append(f"… and {len(findings) - _MAX_DAILY_LINES} more")
        if older:
            lines.append(f"{older} older finding(s) still open.")
        base = os.environ.get("KENNY_PUBLIC_URL", "").strip().rstrip("/")
        if base:
            lines.append(f"{base}/#/inbox")
        await self._dispatch(
            Notification(
                title=f"kenny daily: {len(findings)} new finding(s)",
                body="\n".join(lines),
                priority="default",
                tags=["clipboard"],
                agent_id=None,
                # ``digest`` is in ticket_rules.NEVER_TICKETED_KINDS: the findings
                # it lists already have their tickets.
                kind="digest",
                event_type="daily",
                route="push",
            ),
            now,
        )
        return True

    def _is_digest_slot(self, slot: datetime) -> bool:
        if not self._cfg("KENNY_DIGEST_ENABLED", self._digest_enabled_fb):
            return False
        day = _DAY_INDEX.get(str(self._cfg("KENNY_DIGEST_DAY", self._digest_day_fb)).strip().lower()[:3], 0)
        hour = int(self._cfg("KENNY_DIGEST_HOUR", self._digest_hour_fb))
        return slot.weekday() == day and slot.hour == hour

    async def _daily_findings(
        self, since: datetime, now: datetime
    ) -> tuple[list[tuple[str, str, str]], int]:
        """``([(agent, section, text), ...], older_open_count)`` for the summary."""

        by_agent: dict[str, dict[str, dict[str, Any]]] = {}
        for r in await self._alert_state.all_rows():
            if r["agent_id"]:
                by_agent.setdefault(r["agent_id"], {})[r["scope"]] = r

        findings: list[tuple[str, str, str]] = []
        older = 0
        for agent_id, rows in sorted(by_agent.items()):
            new: dict[str, dict[str, Any]] = {}
            for scope, r in rows.items():
                is_open = (scope.startswith("section:") and r["status"] in _INCIDENT) or (
                    scope == "missing" and r["status"] == "missing"
                )
                if not is_open:
                    continue
                notified = _parse_ts(r.get("last_notified_at"))
                if notified is not None and notified > since and f"pushed:{scope}" not in rows:
                    new[scope.removeprefix("section:")] = r
                else:
                    older += 1
            if new:
                findings.extend(await self._describe(agent_id, new, now))
        return findings, older

    async def _describe(
        self, agent_id: str, new: dict[str, dict[str, Any]], now: datetime
    ) -> list[tuple[str, str, str]]:
        """Current reason text for each new finding, one line per section.

        A disk forecast is a statement about ``disk`` and shares its line.
        """

        latest = await self._store.latest(agent_id)
        texts: dict[str, list[str]] = {}
        if latest is not None and "missing" in new:
            texts["missing"] = [f"no telemetry since {latest.get('received_at')}"]
        sections = [n for n in new if n not in ("missing", "disk_forecast")]
        if latest is not None and sections:
            agent_os = getattr(self._registry.get(agent_id), "os", "windows")
            evaluation = evaluate_snapshot(latest["snapshot"], agent_os=agent_os, now=now)
            for name in sections:
                sec = evaluation["sections"].get(name) or {}
                texts[name] = [str(sec.get("reason") or sec.get("summary") or new[name]["status"])]
        if "disk_forecast" in new:
            since = (now - timedelta(days=30)).date().isoformat()
            forecast = [
                f"{f['mount']} full in ~{f['days_until_full']:.0f}d"
                for f in disk_forecast(await self._store.daily_latest(agent_id, since))
                if f["days_until_full"] is not None and f["days_until_full"] < DISK_FULL_ALERT_DAYS
            ]
            texts.setdefault("disk", []).extend(forecast or ["filling up"])
        out = []
        for name, parts in texts.items():
            since_ts = _parse_ts((new.get(name) or new.get("disk_forecast") or {}).get("since"))
            age = f" (for {_age(now - since_ts)})" if since_ts else ""
            out.append((agent_id, name, "; ".join(parts) + age))
        return out

    async def _maybe_prune(self, now: datetime | None = None) -> None:
        """Run each prunable store's retention sweep, at most every _PRUNE_EVERY --
        except a settings-backed retention key that just *decreased* forces an
        immediate pass, so tightening it from the dashboard (ADR-0051) is
        visible within one alert cycle (~60s) instead of up to a day later.
        Loosening a key never forces a pass -- there is nothing extra to delete.
        """

        now = now or datetime.now(timezone.utc)
        due = self._last_prune is None or now - self._last_prune >= _PRUNE_EVERY
        forced = False
        for _store, key in self._prunables:
            if key is None:
                continue
            days = self._cfg(key, None)
            if days is None:
                continue
            prev = self._last_retention.get(key)
            if prev is not None and days < prev:
                forced = True
            self._last_retention[key] = days
        if not due and not forced:
            return
        self._last_prune = now
        for store, key in self._prunables:
            days = self._cfg(key, None) if key is not None else None
            try:
                if days is None:
                    await store.prune()
                else:
                    await store.prune(retention_days=days)
            except Exception:  # noqa: BLE001
                logger.exception("periodic prune failed for %r", store)

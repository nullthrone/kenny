"""Server-side alert evaluation loop (ADR-0029).

Periodically re-evaluates every known agent's latest snapshot with the
authoritative health rules and notifies the operator on *transitions* only:
ok->warn, ok->crit, warn->crit (escalation), warn/crit->ok (recovery) and
online<->offline. Thresholds stay exclusively in ``health_rules.py``; this
module only compares the evaluated status against the persisted last-known
state (``AlertStateStore``) and applies flap suppression:

* a per-scope cooldown (default 1 h) bounds a flapping section to at most one
  alert plus one recovery per cooldown window,
* escalations to ``crit`` always fire,
* a recovery is only notified when the degraded episode itself was notified.

Offline detection is push-based: an agent is offline when its newest snapshot
is older than ``offline_after_s`` (default three missed 900 s push intervals)
and the in-memory registry has no live connection. Health evaluation is
skipped for offline agents so stale snapshots cannot flap.

Every emitted notification is also persisted to the events table
(``kind='alert'``) as the audit trail and the weekly digest's input.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .diffs import diff_snapshots
from .health_rules import evaluate_snapshot
from .notify import Notification, Notifier
from .registry import AgentRegistry
from .store import AlertStateStore, EventStore, TelemetryStore
from .trends import DISK_FULL_ALERT_DAYS, disk_forecast

logger = logging.getLogger("kenny.alerting")

_ORDER = {"ok": 0, "warn": 1, "crit": 2}

DEFAULT_COOLDOWN_S = 3600
# Three missed 900 s telemetry pushes (docs/protocol.md § Telemetry).
DEFAULT_OFFLINE_AFTER_S = 2700

_PRUNE_EVERY = timedelta(hours=24)
# Forecast alerts re-fire at most daily; the underlying condition moves slowly.
_FORECAST_COOLDOWN = timedelta(hours=24)

_DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class _Prunable(Protocol):
    async def prune(self) -> int: ...


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
        notifiers: list[Notifier],
        settings: Any = None,
        cooldown_s: int = DEFAULT_COOLDOWN_S,
        offline_after_s: int = DEFAULT_OFFLINE_AFTER_S,
        prunables: list[_Prunable] | None = None,
        digest_enabled: bool = True,
        digest_day: str = "mon",
        digest_hour: int = 8,
    ) -> None:
        self._store = store
        self._alert_state = alert_state
        self._event_store = event_store
        self._registry = registry
        self._notifiers = notifiers
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
        self._prunables = prunables or []
        self._last_prune: datetime | None = None

    # -- live config accessors -------------------------------------------------

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

    # -- one evaluation pass -------------------------------------------------

    async def evaluate_once(self, now: datetime | None = None) -> list[Notification]:
        """Evaluate every known agent once; returns the notifications sent."""

        now = now or datetime.now(timezone.utc)
        sent: list[Notification] = []
        for agent_id in await self._store.known_agents():
            try:
                sent.extend(await self._evaluate_agent(agent_id, now))
            except Exception:  # noqa: BLE001 - one bad agent must not stop the rest
                logger.exception("alert evaluation failed for %s", agent_id)
        return sent

    async def _evaluate_agent(self, agent_id: str, now: datetime) -> list[Notification]:
        latest = await self._store.latest(agent_id)
        if latest is None:
            return []
        state = await self._alert_state.get_all(agent_id)
        out: list[Notification] = []

        offline_note, is_offline = await self._offline_transition(agent_id, latest, state, now)
        if offline_note is not None:
            out.append(offline_note)
        if not is_offline:
            out.extend(await self._health_transitions(agent_id, latest, state, now))
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
        received = _parse_ts(latest.get("received_at"))
        agent = self._registry.get(agent_id)
        connected = agent.online if agent is not None else False
        is_offline = (
            not connected
            and received is not None
            and now - received > self._offline_after
        )

        row = state.get("offline")
        prev = row["status"] if row else "online"
        new = "offline" if is_offline else "online"
        if new == prev:
            return None, is_offline

        note: Notification | None = None
        if new == "offline":
            if self._cooldown_passed(row, now):
                age_h = (now - received).total_seconds() / 3600 if received else 0.0
                note = Notification(
                    title=f"{agent_id} is offline",
                    body=f"No telemetry for {age_h:.1f}h (last push {latest.get('received_at')}).",
                    priority="high",
                    tags=["electric_plug"],
                    agent_id=agent_id,
                    kind="alert",
                )
        elif self._episode_was_notified(row):
            note = Notification(
                title=f"{agent_id} is back online",
                body="Telemetry is flowing again.",
                priority="default",
                tags=["white_check_mark"],
                agent_id=agent_id,
                kind="recovery",
            )
        await self._alert_state.upsert(
            agent_id,
            "offline",
            status=new,
            since=now.isoformat(),
            last_notified_at=now.isoformat() if note else (row or {}).get("last_notified_at"),
        )
        return note, is_offline

    # -- health transitions ----------------------------------------------------

    async def _health_transitions(
        self,
        agent_id: str,
        latest: dict[str, Any],
        state: dict[str, dict[str, Any]],
        now: datetime,
    ) -> list[Notification]:
        evaluation = evaluate_snapshot(latest["snapshot"], now=now)
        alert_lines: list[str] = []
        recovery_lines: list[str] = []
        alert_worst = "ok"

        for name, section in evaluation["sections"].items():
            scope = f"section:{name}"
            row = state.get(scope)
            old = row["status"] if row else "ok"
            new = section["status"]
            if new == old:
                continue
            reason = section.get("reason") or section.get("summary") or ""
            line = f"{name}: {old} -> {new}" + (f" ({reason})" if reason else "")
            notified = False
            if _ORDER.get(new, 0) > _ORDER.get(old, 0):
                # Escalations to crit always fire; warn respects the cooldown.
                if new == "crit" or self._cooldown_passed(row, now):
                    alert_lines.append(line)
                    alert_worst = "crit" if new == "crit" else alert_worst
                    if alert_worst == "ok":
                        alert_worst = "warn"
                    notified = True
            elif new == "ok" and self._episode_was_notified(row):
                recovery_lines.append(line)
                notified = True
            # crit -> warn improvements update state silently.
            await self._alert_state.upsert(
                agent_id,
                scope,
                status=new,
                since=now.isoformat(),
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
            out.append(
                Notification(
                    title=f"{agent_id} health: {overall}",
                    body="\n".join(alert_lines),
                    priority="high" if alert_worst == "crit" else "default",
                    tags=["rotating_light" if alert_worst == "crit" else "warning"],
                    agent_id=agent_id,
                    kind="alert",
                )
            )
        if recovery_lines:
            out.append(
                Notification(
                    title=f"{agent_id} recovered",
                    body="\n".join(recovery_lines),
                    priority="default",
                    tags=["white_check_mark"],
                    agent_id=agent_id,
                    kind="recovery",
                )
            )
        return out

    # -- inventory changes & forecasts (ADR-0030) --------------------------------

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
                note = await self._notify_changes(agent_id, changes, state, now)
                if note is not None:
                    out.append(note)
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
    ) -> Notification | None:
        by_section: dict[str, list[dict[str, Any]]] = {}
        for change in changes:
            by_section.setdefault(change["section"], []).append(change)

        lines: list[str] = []
        priority = "default"
        for section, section_changes in sorted(by_section.items()):
            scope = f"change:{section}"
            row = state.get(scope)
            if not self._cooldown_passed(row, now):
                continue
            for c in section_changes:
                detail = f" ({c['detail']})" if c.get("detail") else ""
                lines.append(f"{section}: {c['kind']} {c['key']}{detail}")
            if section == "local_accounts":
                priority = "high"
            await self._alert_state.upsert(
                agent_id,
                scope,
                status="changed",
                since=now.isoformat(),
                last_notified_at=now.isoformat(),
            )
        if not lines:
            return None
        return Notification(
            title=f"{agent_id}: {len(lines)} change(s) detected",
            body="\n".join(lines),
            priority=priority,
            tags=["mag"],
            agent_id=agent_id,
            kind="change",
        )

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
        last = _parse_ts((row or {}).get("last_notified_at"))
        if last is not None and now - last < _FORECAST_COOLDOWN:
            return None
        await self._alert_state.upsert(
            agent_id,
            scope,
            status="warn",
            since=(row or {}).get("since") if row and row["status"] == "warn" else now.isoformat(),
            last_notified_at=now.isoformat(),
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
        await self._event_store.insert_alert(
            agent_id=note.agent_id,
            message=f"{note.title}\n{note.body}",
            level=level,
            fields={"kind": note.kind, "priority": note.priority},
            at=now.isoformat(),
        )
        for notifier in self._notifiers:
            await notifier.send(note)  # best-effort; send() never raises

    # -- loop ---------------------------------------------------------------------

    async def run(self, interval_s: int, initial_delay_s: float = 10.0) -> None:
        """Evaluate forever; also runs daily retention pruning (ADR-0007)."""

        await asyncio.sleep(initial_delay_s)
        while True:
            try:
                await self.evaluate_once()
                await self.maybe_send_digest()
                await self._maybe_prune()
            except Exception:  # noqa: BLE001 - never let the loop die
                logger.exception("alert evaluation pass failed")
            # Re-read the cadence each pass so a dashboard change retimes the
            # running loop. A runtime 0/negative keeps the loop alive at the
            # startup interval (disabling entirely stays a restart decision).
            interval = self._cfg("KENNY_ALERT_INTERVAL_SECS", interval_s)
            await asyncio.sleep(interval if interval and interval > 0 else interval_s)

    # -- weekly digest (ADR-0029) -------------------------------------------------

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
            ),
            now,
        )
        await self._alert_state.upsert(
            "", "digest", status=now.isoformat(), since=now.isoformat(), last_notified_at=now.isoformat()
        )
        return True

    async def _maybe_prune(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if self._last_prune is not None and now - self._last_prune < _PRUNE_EVERY:
            return
        self._last_prune = now
        for store in self._prunables:
            try:
                await store.prune()
            except Exception:  # noqa: BLE001
                logger.exception("periodic prune failed for %r", store)

"""A ``live`` setting takes effect without a restart, at the object that uses it.

ADR-0032 makes ``lifecycle`` the honesty mechanism of the settings surface: the
dashboard labels a setting ``live`` only if its consumer actually picks a change
up. Many consumers hold their value in an attribute set at construction, which
``build_app`` does before the operator's overrides are loaded, so the label and
the behaviour are two places that must agree. These tests join them through the
real composition root:

* an override stored before boot reaches the consumer on the next start, and
* a write through ``PUT /api/settings/{key}`` reaches it at once;
* a ``restart`` setting whose stored value differs from the running one says so;
* the backup loop and prune follow their settings, 0 = paused included.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any, Callable

import pytest
from starlette.testclient import TestClient

from kenny_server import backup as backup_mod
from kenny_server.backup import BackupManager
from kenny_server.config import CATALOG
from kenny_server.main import _backup_loop, build_app
from kenny_server.store import BackupTargetStore, TelemetryStore


class _FakeAnthropic:
    """Stands in for the Anthropic client so no real key is used."""


_ENV = (
    "KENNY_DISCORD_BOT_TOKEN",
    "KENNY_DISCORD_ENABLED",
    "KENNY_DISCORD_GUILD_IDS",
    "KENNY_DISCORD_MODEL",
    "KENNY_CHAT_MODEL",
    "KENNY_TRIAGE_ENABLED",
    "KENNY_TRIAGE_RESOLVE",
    "KENNY_TRIAGE_MAX_ITERATIONS",
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)
    # A bot token builds the Discord service (never started without ENABLED);
    # a key wires triage. Neither is used for real here.
    monkeypatch.setenv("KENNY_DISCORD_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")


def _bearer(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.operator_token}"}


# key -> (value written, how to read what the running consumer uses)
BOUND: dict[str, tuple[Any, Callable[[Any], Any]]] = {
    "KENNY_TICKET_APPROVAL_TTL_SECS": (
        1234,
        lambda s: (s.tickets.approval_ttl_secs, s.ticket_assistant.approval_ttl_secs),
    ),
    "KENNY_TICKET_AUTOCLOSE_SECS": (1111, lambda s: s.tickets.autoclose_secs),
    "KENNY_TICKET_STALL_NUDGE_SECS": (2222, lambda s: s.tickets.stall_nudge_secs),
    "KENNY_TICKET_STALL_GIVEUP_SECS": (3333, lambda s: s.tickets.stall_giveup_secs),
    "KENNY_TICKET_ABANDON_SECS": (4444, lambda s: s.tickets.abandon_secs),
    "KENNY_TICKET_RETENTION_DAYS": (9, lambda s: s.ticket_store.run_retention_days),
    "KENNY_CHAT_MODEL": ("claude-test-chat", lambda s: s.ticket_assistant.model),
    "KENNY_DISCORD_MAX_TURNS_PER_TICKET": (
        7,
        lambda s: s.ticket_assistant.max_turns_per_ticket,
    ),
    "KENNY_TRIAGE_ENABLED": (False, lambda s: s.tickets._triage is not None),
    "KENNY_TRIAGE_RESOLVE": (True, lambda s: s.triage.resolve_enabled),
    "KENNY_TRIAGE_MAX_ITERATIONS": (3, lambda s: s.triage.max_iterations),
    "KENNY_DISCORD_GUILD_IDS": (
        "g1, g2",
        lambda s: (
            s.discord_service.guild_ids,
            s.discord_service.gateway.guild_allowlist,
        ),
    ),
    "KENNY_DISCORD_SUPPORT_CHANNEL_ID": ("c-support", lambda s: s.discord_service.support_channel_id),
    "KENNY_DISCORD_OPERATOR_CHANNEL_ID": ("c-ops", lambda s: s.discord_service.operator_channel_id),
    "KENNY_DISCORD_PRIVATE_THREADS": (False, lambda s: s.discord_service.private_threads),
    "KENNY_DISCORD_RATE_LIMIT_PER_USER_HOUR": (5, lambda s: s.discord_service.rate_limit_per_hour),
    "KENNY_DISCORD_MODEL": ("claude-test-discord", lambda s: s.discord_service.model),
}

EXPECTED: dict[str, Any] = {
    "KENNY_TICKET_APPROVAL_TTL_SECS": (1234, 1234),
    "KENNY_TRIAGE_ENABLED": False,
    "KENNY_DISCORD_GUILD_IDS": (frozenset({"g1", "g2"}), frozenset({"g1", "g2"})),
}

# Live settings in the same groups whose consumer reads ``Settings`` on every
# use instead of holding the value: nothing to bind.
READ_PER_USE = {
    "KENNY_DISCORD_WEBHOOK_URL",  # notify.NotifierProvider, per dispatch
    "KENNY_TICKET_SWEEP_INTERVAL_SECS",  # tickets.ticket_sweep_loop, per pass
}


def _expected(key: str) -> Any:
    return EXPECTED.get(key, BOUND[key][0])


def test_every_live_ticket_discord_and_chat_setting_is_bound_or_read_per_use() -> None:
    """A new ``live`` key in these groups must join ``BOUND`` or ``READ_PER_USE``.

    Otherwise it is a label the running server does not honour.
    """

    live = {
        key
        for key, spec in CATALOG.items()
        if spec.lifecycle == "live"
        and (spec.group == "Discord & Tickets" or key == "KENNY_CHAT_MODEL")
    }
    assert live == set(BOUND) | READ_PER_USE


def _build(db_path: str) -> Any:
    return build_app(db_path=db_path, client_factory=_FakeAnthropic)


def test_a_dashboard_write_reaches_the_running_consumer(tmp_path) -> None:
    app = _build(str(tmp_path / "live.sqlite"))
    with TestClient(app) as c:
        for key, (value, read) in BOUND.items():
            r = c.put(f"/api/settings/{key}", headers=_bearer(app), json={"value": value})
            assert r.status_code == 200, (key, r.text)
            assert read(app.state) == _expected(key), key


def test_an_override_stored_before_boot_is_in_force_after_it(tmp_path) -> None:
    db_path = str(tmp_path / "boot.sqlite")
    first = _build(db_path)
    with TestClient(first) as c:
        for key, (value, _read) in BOUND.items():
            assert c.put(
                f"/api/settings/{key}", headers=_bearer(first), json={"value": value}
            ).status_code == 200

    second = _build(db_path)
    with TestClient(second):
        for key, (_value, read) in BOUND.items():
            assert read(second.state) == _expected(key), key


def test_resetting_a_bound_setting_restores_the_default(tmp_path) -> None:
    app = _build(str(tmp_path / "reset.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        c.put("/api/settings/KENNY_TRIAGE_ENABLED", headers=h, json={"value": False})
        assert app.state.tickets._triage is None
        c.delete("/api/settings/KENNY_TRIAGE_ENABLED", headers=h)
        assert app.state.tickets._triage is not None


def test_a_restart_setting_reports_a_pending_change_until_it_is_undone(tmp_path) -> None:
    app = _build(str(tmp_path / "restart.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        assert CATALOG["KENNY_DISCORD_ENABLED"].lifecycle == "restart"
        r = c.put("/api/settings/KENNY_DISCORD_ENABLED", headers=h, json={"value": True})
        assert r.json()["pending_restart"] is True
        flat = {
            s["key"]: s
            for g in c.get("/api/settings", headers=h).json()["groups"]
            for s in g["settings"]
        }
        assert flat["KENNY_DISCORD_ENABLED"]["pending_restart"] is True
        # a live setting never waits for a restart
        assert flat["KENNY_TRIAGE_ENABLED"]["pending_restart"] is False
        r = c.delete("/api/settings/KENNY_DISCORD_ENABLED", headers=h)
        assert r.json()["pending_restart"] is False


def test_a_stored_restart_setting_is_in_force_and_not_pending_after_the_restart(tmp_path) -> None:
    db_path = str(tmp_path / "restarted.sqlite")
    first = _build(db_path)
    with TestClient(first) as c:
        c.put("/api/settings/KENNY_DISCORD_ENABLED", headers=_bearer(first), json={"value": True})
    second = _build(db_path)
    with TestClient(second) as c:
        assert c.get("/api/settings", headers=_bearer(second)).status_code == 200
        assert second.state.settings.pending_restart("KENNY_DISCORD_ENABLED") is False


# -- backup ----------------------------------------------------------------------


async def test_prune_keeps_as_many_backups_as_the_retention_setting_says(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stamps = (f"20260101T0000{n:02d}Z" for n in itertools.count())
    monkeypatch.setattr(backup_mod, "_utc_basic_now", lambda: next(stamps))
    db_path = str(tmp_path / "kenny.sqlite")
    telemetry = TelemetryStore(db_path=db_path)
    await telemetry.connect()
    await telemetry.close()
    target_store = BackupTargetStore(db_path)
    await target_store.connect()
    retention = {"value": 2}
    try:
        mgr = BackupManager(db_path, target_store, retention=lambda: retention["value"])
        for _ in range(3):
            await mgr.create("manual")
        assert len(await mgr.list()) == 2
        # read again on every run, not captured at construction
        retention["value"] = 1
        await mgr.create("manual")
        assert len(await mgr.list()) == 1
    finally:
        await target_store.close()


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str) -> Any:
        return self.values[key]


class _CountingBackups:
    def __init__(self) -> None:
        self.created = 0

    async def create(self, trigger: str) -> dict[str, Any]:
        self.created += 1
        return {}


async def test_the_backup_loop_pauses_at_zero_and_resumes_without_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kenny_server.main as main_mod

    monkeypatch.setattr(main_mod, "_BACKUP_PAUSED_POLL_SECS", 0.01)
    settings = _Settings({"KENNY_BACKUP_INTERVAL_SECS": 0})
    backups = _CountingBackups()
    task = asyncio.create_task(_backup_loop(backups, settings, 0))  # type: ignore[arg-type]
    try:
        await asyncio.sleep(0.05)
        assert backups.created == 0
        settings.values["KENNY_BACKUP_INTERVAL_SECS"] = 3600
        for _ in range(100):
            if backups.created:
                break
            await asyncio.sleep(0.01)
        assert backups.created == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

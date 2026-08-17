"""Runtime settings resolver, store, and live-read wiring (config.py)."""

from __future__ import annotations

import pytest

from kenny_server import notify
from kenny_server.alerting import AlertEngine
from kenny_server.config import CATALOG, SettingNotWritable, Settings
from kenny_server.store import SettingsStore


class _MemStore:
    """In-memory stand-in for SettingsStore (resolver unit tests)."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.data = dict(initial or {})

    async def all(self) -> dict[str, str]:
        return dict(self.data)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value

    async def delete(self, key: str) -> bool:
        existed = key in self.data
        self.data.pop(key, None)
        return existed


def _settings(env=None, initial=None) -> Settings:
    # apply_hooks disabled so resolver tests never touch global logging state.
    return Settings(_MemStore(initial), env=env or {}, apply_hooks={})


# -- precedence: DB > env > default -------------------------------------------


async def test_default_when_unset() -> None:
    s = _settings()
    value, source = s.effective("KENNY_ALERT_COOLDOWN_SECS")
    assert value == 3600 and source == "default"


async def test_env_overrides_default() -> None:
    s = _settings(env={"KENNY_ALERT_COOLDOWN_SECS": "120"})
    value, source = s.effective("KENNY_ALERT_COOLDOWN_SECS")
    assert value == 120 and source == "env"


async def test_empty_env_is_ignored() -> None:
    # An exported-but-empty var must not shadow the coded default.
    s = _settings(env={"KENNY_ALERT_COOLDOWN_SECS": ""})
    assert s.effective("KENNY_ALERT_COOLDOWN_SECS") == (3600, "default")


async def test_db_overrides_env() -> None:
    s = _settings(env={"KENNY_ALERT_COOLDOWN_SECS": "120"})
    await s.load()
    await s.set("KENNY_ALERT_COOLDOWN_SECS", "999")
    assert s.effective("KENNY_ALERT_COOLDOWN_SECS") == (999, "db")


async def test_reset_falls_back_to_env_then_default() -> None:
    s = _settings(env={"KENNY_ALERT_COOLDOWN_SECS": "120"})
    await s.set("KENNY_ALERT_COOLDOWN_SECS", "999")
    await s.reset("KENNY_ALERT_COOLDOWN_SECS")
    assert s.effective("KENNY_ALERT_COOLDOWN_SECS") == (120, "env")  # env still there
    s2 = _settings()
    await s2.set("KENNY_DIGEST_HOUR", "5")
    await s2.reset("KENNY_DIGEST_HOUR")
    assert s2.effective("KENNY_DIGEST_HOUR") == (8, "default")


async def test_get_after_set_is_synchronous() -> None:
    s = _settings()
    await s.set("KENNY_CHAT_MODEL", "claude-opus-4-8")
    # No reload / re-query: the in-memory map is authoritative immediately.
    assert s.get("KENNY_CHAT_MODEL") == "claude-opus-4-8"


# -- typed parsing -------------------------------------------------------------


async def test_typed_parsing() -> None:
    s = _settings(env={
        "KENNY_DIGEST_ENABLED": "0",
        "KENNY_DIGEST_HOUR": "9",
        "KENNY_ALERT_INITIAL_DELAY": "2.5",
        "KENNY_CHAT_MODEL": "m",
    })
    assert s.get("KENNY_DIGEST_ENABLED") is False
    assert s.get("KENNY_DIGEST_HOUR") == 9
    assert s.get("KENNY_ALERT_INITIAL_DELAY") == 2.5
    assert s.get("KENNY_CHAT_MODEL") == "m"


async def test_invalid_env_value_falls_back_to_default() -> None:
    # A garbage env value must not raise on the read path; it falls back.
    s = _settings(env={"KENNY_DIGEST_HOUR": "not-a-number"})
    assert s.get("KENNY_DIGEST_HOUR") == 8


# -- validation on write -------------------------------------------------------


async def test_validate_rejects_bad_values() -> None:
    s = _settings()
    with pytest.raises(ValueError):
        await s.set("KENNY_DIGEST_HOUR", "99")  # > max 23
    with pytest.raises(ValueError):
        await s.set("KENNY_DIGEST_HOUR", "abc")  # not an int
    with pytest.raises(ValueError):
        await s.set("KENNY_DIGEST_DAY", "funday")  # not a choice
    # nothing persisted
    assert s._store.data == {}


async def test_env_only_write_rejected() -> None:
    s = _settings()
    with pytest.raises(SettingNotWritable):
        await s.set("KENNY_OPERATOR_TOKEN", "hunter2")
    with pytest.raises(SettingNotWritable):
        await s.reset("KENNY_HOST")


# -- describe (API serialisation) ---------------------------------------------


async def test_describe_masks_secrets_and_groups() -> None:
    s = _settings(env={"KENNY_OPERATOR_TOKEN": "s3cret"})
    groups = s.describe()
    names = [g["name"] for g in groups]
    assert "Alerting & Digest" in names and "Operator & Agent Auth" in names
    flat = {row["key"]: row for g in groups for row in g["settings"]}
    tok = flat["KENNY_OPERATOR_TOKEN"]
    assert tok["value"] is None and tok["is_set"] is True and tok["sensitive"] is True
    # a non-secret live setting exposes its value + source
    cd = flat["KENNY_ALERT_COOLDOWN_SECS"]
    assert cd["value"] == 3600 and cd["source"] == "default" and cd["lifecycle"] == "live"


def test_catalog_groups_are_declared() -> None:
    from kenny_server.config import GROUP_ORDER
    for spec in CATALOG.values():
        assert spec.group in GROUP_ORDER, f"{spec.key} in undeclared group {spec.group}"


def test_describe_carries_a_slug_per_group() -> None:
    # The dashboard sidebar routes on this slug (#/settings/{slug}); a group
    # rename that silently changes it would break every bookmark and the
    # discord-settings screenshot target, so the exact mapping is pinned here.
    s = _settings()
    slugs = {g["name"]: g["slug"] for g in s.describe()}
    assert slugs == {
        "Alerting & Digest": "alerting-digest",
        "Web filter": "web-filter",
        "Chat & AI": "chat-ai",
        "Logging": "logging",
        "Network & Process": "network-process",
        "Operator & Agent Auth": "operator-agent-auth",
        "Telemetry limits": "telemetry-limits",
        "Agent distribution": "agent-distribution",
        "Backup": "backup",
        "Updates": "updates",
        "Discord & Tickets": "discord-tickets",
    }
    assert len(slugs) == len(set(slugs.values())), "group slugs must be unique"


def test_group_slug_is_stable_and_unique() -> None:
    from kenny_server.config import GROUP_ORDER, group_slug

    slugs = [group_slug(g) for g in GROUP_ORDER]
    assert len(slugs) == len(set(slugs))
    assert all(slug and " " not in slug for slug in slugs)


# -- catalog gaps: env vars the server reads but the old catalog didn't list --


def test_alert_push_channels_are_writable_and_sensitive() -> None:
    # notify.NotifierProvider resolves these through Settings on every dispatch
    # (ADR-0054), so they are genuinely live -- the catalog may advertise them
    # as editable. Sensitive is the orthogonal axis and must not move with it:
    # an ntfy topic URL and a webhook URL are bearer-equivalent, so the value
    # is stored but never serialised back out.
    for key in notify.CHANNEL_KEYS:
        spec = CATALOG[key]
        assert spec.lifecycle == "live", key
        assert spec.writable is True, key
        assert spec.sensitive is True, key
        # A masked row's editor starts blank only for type "secret"; a
        # sensitive "str" would prefill the console's draft with the literal
        # mask text ("set"/"not set") and save that as the channel URL.
        assert spec.type == "secret", key
    for key in ("KENNY_NTFY_URL", "KENNY_NTFY_TOKEN", "KENNY_WEBHOOK_URL"):
        assert CATALOG[key].group == "Alerting & Digest"


def test_channel_keys_match_the_catalog() -> None:
    """The provider's key list and the catalog cannot drift apart.

    ``notify.CHANNEL_KEYS`` decides what is *read* per dispatch; the catalog
    decides what is *writable* in Admin. A key in one and not the other is
    either a dead control or a channel nobody can configure.
    """

    assert set(notify.CHANNEL_KEYS) <= set(CATALOG)
    assert len(set(notify.CHANNEL_KEYS)) == len(notify.CHANNEL_KEYS)


async def test_a_channel_secret_is_never_serialised_back(tmp_path) -> None:
    """Writable does not mean readable: describe() still reports set/not set."""

    s = _settings()
    await s.set("KENNY_WEBHOOK_URL", "https://hook.example/very-secret")
    flat = {row["key"]: row for g in s.describe() for row in g["settings"]}
    row = flat["KENNY_WEBHOOK_URL"]
    assert row["editable"] is True and row["source"] == "db"
    assert row["value"] is None and row["is_set"] is True
    assert row["default"] is None
    assert "very-secret" not in str(s.describe())
    assert s.describe_one("KENNY_WEBHOOK_URL")["value"] is None


def test_oauth_ttls_are_in_catalog_with_matching_defaults() -> None:
    # oauth.py's _access_ttl()/_refresh_ttl() fall back to module constants
    # that never flow through Settings; the catalog's coded default must match
    # them exactly or the read-only row on the page would lie about what the
    # server actually uses.
    from kenny_server.oauth import _DEFAULT_ACCESS_TTL_SECS, _DEFAULT_REFRESH_TTL_SECS

    access = CATALOG["KENNY_OAUTH_ACCESS_TTL_SECS"]
    refresh = CATALOG["KENNY_OAUTH_REFRESH_TTL_SECS"]
    assert access.group == refresh.group == "Operator & Agent Auth"
    assert access.lifecycle == refresh.lifecycle == "env_only"
    assert access.parse(access.default_raw) == _DEFAULT_ACCESS_TTL_SECS
    assert refresh.parse(refresh.default_raw) == _DEFAULT_REFRESH_TTL_SECS


def test_sqlite_busy_timeout_is_env_only_and_matches_coded_default() -> None:
    # store._BUSY_TIMEOUT_MS is read once from os.environ at import time
    # (ADR-0051) -- it cannot be changed live, and the catalog's coded default
    # must match it or the read-only row on the settings page would lie.
    from kenny_server.store import _BUSY_TIMEOUT_MS

    spec = CATALOG["KENNY_SQLITE_BUSY_TIMEOUT_MS"]
    assert spec.lifecycle == "env_only"
    assert spec.parse(spec.default_raw) == _BUSY_TIMEOUT_MS


def test_telemetry_retention_is_live_and_matches_coded_default() -> None:
    # store.TELEMETRY_RETENTION_DAYS is TelemetryStore's fallback when no live
    # setting value is resolved; the catalog default must match it or an
    # operator who never touches the setting would be shown the wrong
    # effective value. Deliberately its own constant, not store.RETENTION_DAYS
    # (which EventStore/WebFilterStore still default to) -- so this assertion
    # cannot be satisfied by a change that silently moves their retention too.
    from kenny_server.store import TELEMETRY_RETENTION_DAYS

    spec = CATALOG["KENNY_TELEMETRY_RETENTION_DAYS"]
    assert spec.lifecycle == "live"
    assert spec.min == 1
    assert spec.parse(spec.default_raw) == TELEMETRY_RETENTION_DAYS


# -- SettingsStore persistence -------------------------------------------------


async def test_settings_store_roundtrip(tmp_path) -> None:
    db = str(tmp_path / "settings.sqlite")
    store = SettingsStore(db)
    await store.connect()
    try:
        await store.set("KENNY_CHAT_MODEL", "claude-opus-4-8")
        await store.set("KENNY_CHAT_MODEL", "claude-sonnet-5")  # upsert
        await store.set("KENNY_LOG_LEVEL", "DEBUG")
        assert await store.all() == {
            "KENNY_CHAT_MODEL": "claude-sonnet-5",
            "KENNY_LOG_LEVEL": "DEBUG",
        }
        assert await store.delete("KENNY_LOG_LEVEL") is True
        assert await store.delete("KENNY_LOG_LEVEL") is False
    finally:
        await store.close()

    # survives close + reopen
    store2 = SettingsStore(db)
    await store2.connect()
    try:
        assert await store2.all() == {"KENNY_CHAT_MODEL": "claude-sonnet-5"}
    finally:
        await store2.close()


async def test_settings_load_restores_overrides(tmp_path) -> None:
    db = str(tmp_path / "load.sqlite")
    store = SettingsStore(db)
    await store.connect()
    try:
        await store.set("KENNY_ALERT_COOLDOWN_SECS", "42")
        settings = Settings(store, apply_hooks={})
        await settings.load()
        assert settings.effective("KENNY_ALERT_COOLDOWN_SECS") == (42, "db")
    finally:
        await store.close()


# -- AlertEngine reads settings live ------------------------------------------


async def test_alert_engine_reads_cooldown_live() -> None:
    from datetime import datetime, timedelta, timezone

    settings = _settings()
    engine = AlertEngine(
        store=None, alert_state=None, event_store=None, registry=None,
        notifiers=[], settings=settings,
    )
    assert engine._cooldown == timedelta(seconds=3600)
    await settings.set("KENNY_ALERT_COOLDOWN_SECS", "10")
    # No reconstruction: the property reflects the new value immediately.
    assert engine._cooldown == timedelta(seconds=10)

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    recent = {"last_notified_at": (now - timedelta(seconds=30)).isoformat()}
    # 30s since last notify: still suppressed under the old 3600s window...
    settings._overrides["KENNY_ALERT_COOLDOWN_SECS"] = "3600"
    assert engine._cooldown_passed(recent, now) is False
    # ...but a live drop to 5s lets it through.
    settings._overrides["KENNY_ALERT_COOLDOWN_SECS"] = "5"
    assert engine._cooldown_passed(recent, now) is True

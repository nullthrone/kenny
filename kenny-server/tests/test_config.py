"""Runtime settings resolver, store, and live-read wiring (config.py)."""

from __future__ import annotations

import pytest

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


def test_alert_push_channels_are_in_catalog() -> None:
    # notify.load_notifiers() reads these three directly from os.environ, not
    # through Settings, so they must stay env_only — but they still belong in
    # the catalog or the Settings page silently omits real configuration.
    for key in ("KENNY_NTFY_URL", "KENNY_NTFY_TOKEN", "KENNY_WEBHOOK_URL"):
        spec = CATALOG[key]
        assert spec.group == "Alerting & Digest"
        assert spec.lifecycle == "env_only"
        assert spec.sensitive is True
        assert spec.writable is False


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

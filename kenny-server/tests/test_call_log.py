"""Tests for :class:`kenny_server.tools.CallLog`.

Covers the in-memory fallback and, most importantly, that a transient
``EventStore`` write failure (e.g. sqlite "database is locked" under
concurrent agent pushes) is swallowed rather than propagated — a failing
audit-log write must never fail the tool call that already succeeded.
"""

from __future__ import annotations

import hashlib

import pytest

from kenny_server.store import EventStore
from kenny_server.tools import CAPABILITY_TOOLS, CallLog, redact_audit_args


@pytest.fixture
async def events(tmp_path) -> EventStore:
    es = EventStore(db_path=str(tmp_path / "events.sqlite"))
    await es.connect()
    yield es
    await es.close()


async def test_record_without_event_store_uses_memory() -> None:
    log = CallLog()
    await log.record("example-pc", "screen_capture", {}, ok=True)
    entries = await log.list()
    assert len(entries) == 1
    assert entries[0]["tool"] == "screen_capture"
    assert entries[0]["ok"] is True


async def test_record_persists_to_event_store(events: EventStore) -> None:
    log = CallLog(event_store=events)
    await log.record("example-pc", "screen_capture", {}, ok=True)
    entries = await log.list()
    assert len(entries) == 1
    assert entries[0]["tool"] == "screen_capture"


async def test_record_swallows_event_store_failure(events: EventStore, monkeypatch) -> None:
    log = CallLog(event_store=events)

    async def boom(**kwargs):
        raise Exception("database is locked")

    monkeypatch.setattr(events, "insert_audit", boom)

    # Must not raise: the tool call this logs already succeeded/failed on its
    # own terms, and a broken audit write is not grounds to break the caller.
    await log.record("example-pc", "screen_capture", {}, ok=True)


# -- audit identity: actor / run_id / redacted args ---------------------------


async def test_actor_run_id_and_args_are_persisted_and_listed(events: EventStore) -> None:
    log = CallLog(event_store=events)
    await log.record("pc1", "fs_list", {"path": "C:\\"}, ok=True, actor="alice", run_id="run-7")
    [entry] = await log.list()
    assert entry["actor"] == "alice"
    assert entry["run_id"] == "run-7"
    assert entry["args"] == {"path": "C:\\"}
    assert entry["tool"] == "fs_list" and entry["ok"] is True


async def test_in_memory_fallback_carries_identity_and_redacts() -> None:
    log = CallLog()
    await log.record(
        "pc1",
        "account_create",
        {"name": "kid", "password": "hunter2"},
        ok=True,
        actor="op",
        run_id="r1",
    )
    [entry] = await log.list()
    assert entry["actor"] == "op" and entry["run_id"] == "r1"
    assert entry["args"] == {"name": "kid", "password": "[redacted]"}


async def test_identity_defaults_to_none(events: EventStore) -> None:
    log = CallLog(event_store=events)
    await log.record("pc1", "fs_list", {}, ok=True)
    [entry] = await log.list()
    assert entry["actor"] is None and entry["run_id"] is None
    assert entry["args"] == {}


async def test_secrets_never_reach_the_database(events: EventStore) -> None:
    log = CallLog(event_store=events)
    await log.record("pc1", "account_create", {"name": "kid", "password": "hunter2"}, ok=True)
    rows = await events.query(kind="audit")
    assert "hunter2" not in repr(rows)


def test_redaction_matches_keys_case_insensitively_and_recurses() -> None:
    args = {
        "Password": "a",
        "new_passwd": "b",
        "ClientSecret": "c",
        "auth_token": "d",
        "API_KEY": "e",
        "apikey": "f",
        "credentials": "g",
        "name": "keep",
        "nested": {"inner": {"Token": "h", "ok": 1}},
        "items": [{"secret": "i", "n": 2}, "plain", ["x", {"password": "j"}]],
    }
    out = redact_audit_args(args)
    for key in ("Password", "new_passwd", "ClientSecret", "auth_token", "API_KEY", "apikey"):
        assert out[key] == "[redacted]"
    assert out["credentials"] == "[redacted]"
    assert out["name"] == "keep"
    assert out["nested"] == {"inner": {"Token": "[redacted]", "ok": 1}}
    assert out["items"] == [
        {"secret": "[redacted]", "n": 2},
        "plain",
        ["x", {"password": "[redacted]"}],
    ]
    # The caller's dict is never mutated.
    assert args["Password"] == "a" and args["nested"]["inner"]["Token"] == "h"


def test_redaction_clips_long_strings_at_every_depth() -> None:
    out = redact_audit_args({"label": "x" * 900, "n": {"l": ["y" * 501, "z" * 500]}})
    assert out["label"] == "x" * 500
    assert out["n"]["l"] == ["y" * 500, "z" * 500]


@pytest.mark.parametrize("key", ["script", "command", "content", "body", "code", "Script", "scriptBody"])
def test_free_text_bodies_are_fingerprinted_not_stored(key: str) -> None:
    value = "net user bob Hunter2 /add \u00e9"
    out = redact_audit_args({key: value, "timeout_s": 30})
    assert out[key] == {
        "redacted": "body",
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "length": len(value),
    }
    assert out["timeout_s"] == 30
    assert "Hunter2" not in repr(out)


def test_long_bodies_are_hashed_in_full_not_clipped() -> None:
    value = "x" * 9000
    out = redact_audit_args({"content": value})
    assert out["content"]["length"] == 9000
    assert out["content"]["sha256"] == hashlib.sha256(value.encode()).hexdigest()


def test_non_text_values_under_body_keys_pass_through() -> None:
    assert redact_audit_args({"code": 7, "body": None}) == {"code": 7, "body": None}


@pytest.mark.parametrize(
    "key",
    ["pass", "Pass", "pwd", "pin", "private_key", "privateKey", "authorization",
     "passphrase", "cookie", "Set-Cookie", "session", "ssh_pass", "user.pin"],
)
def test_widened_secret_keys_are_redacted(key: str) -> None:
    assert redact_audit_args({key: "s3cret"}) == {key: "[redacted]"}


@pytest.mark.parametrize(
    "key",
    ["path", "id", "name", "version", "display_name", "sessions", "pinned", "bypass",
     "compass", "passed", "pinger", "action", "keyboard"],
)
def test_innocent_keys_are_not_redacted(key: str) -> None:
    assert redact_audit_args({key: "v"}) == {key: "v"}


def test_every_capability_tool_arg_key_is_classified() -> None:
    """Pin which tool argument keys are hidden: a new key must be a conscious choice."""

    keys = {k.rstrip("?") for spec in CAPABILITY_TOOLS.values() for k in spec}
    redacted = {
        k for k in keys if redact_audit_args({k: "v"}) != {k: "v"}
    }
    assert redacted == {"script", "command", "password"}
    assert redact_audit_args({"password": "v"}) == {"password": "[redacted]"}
    for k in ("script", "command"):
        assert redact_audit_args({k: "v"})[k]["redacted"] == "body"


def test_lists_are_capped_with_a_marker() -> None:
    out = redact_audit_args({"domains": [f"d{i}.example" for i in range(120)]})
    assert len(out["domains"]) == 51
    assert out["domains"][:50] == [f"d{i}.example" for i in range(50)]
    assert out["domains"][-1] == "\u2026 (70 more)"
    exact = redact_audit_args({"domains": list(range(50))})
    assert exact["domains"] == list(range(50))


def test_list_items_are_redacted_and_clipped_before_capping() -> None:
    out = redact_audit_args([{"password": "p"}] * 3 + ["y" * 600])
    assert out == [{"password": "[redacted]"}] * 3 + ["y" * 500]


def test_pathological_nesting_does_not_raise() -> None:
    deep: dict = {}
    cur = deep
    for _ in range(5000):
        cur["a"] = {}
        cur = cur["a"]
    redact_audit_args(deep)


async def test_legacy_rows_without_fields_list_as_none(events: EventStore) -> None:
    await events.insert_audit(agent_id="pc1", tool="fs_list", ok=True)  # no fields
    [entry] = await CallLog(event_store=events).list()
    assert entry["actor"] is None and entry["run_id"] is None and entry["args"] is None
    assert entry["tool"] == "fs_list"

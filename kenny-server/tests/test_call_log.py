"""Tests for :class:`kenny_server.tools.CallLog`.

Covers the in-memory fallback and, most importantly, that a transient
``EventStore`` write failure (e.g. sqlite "database is locked" under
concurrent agent pushes) is swallowed rather than propagated — a failing
audit-log write must never fail the tool call that already succeeded.
"""

from __future__ import annotations

import pytest

from kenny_server.store import EventStore
from kenny_server.tools import CallLog, redact_audit_args


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
    out = redact_audit_args({"script": "x" * 900, "n": {"l": ["y" * 501, "z" * 500]}})
    assert out["script"] == "x" * 500
    assert out["n"]["l"] == ["y" * 500, "z" * 500]


async def test_legacy_rows_without_fields_list_as_none(events: EventStore) -> None:
    await events.insert_audit(agent_id="pc1", tool="fs_list", ok=True)  # no fields
    [entry] = await CallLog(event_store=events).list()
    assert entry["actor"] is None and entry["run_id"] is None and entry["args"] is None
    assert entry["tool"] == "fs_list"

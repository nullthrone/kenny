"""What a ticket's alerts are, and whether what they reported still holds."""

from __future__ import annotations

import pytest

from kenny_server.registry import AgentRegistry
from kenny_server.store import AlertStateStore, EventStore, TelemetryStore
from kenny_server.ticket_alerts import TicketAlertReader
from kenny_server.ticketstore import TicketStore

CRIT = {
    "disk": {
        "status": "crit",
        "summary": "C: 97% full",
        "volumes": [{"mount": "C:", "percent_used": 97.0}],
    }
}
RECOVERED = {
    "disk": {
        "status": "ok",
        "summary": "C: 41% full",
        "volumes": [{"mount": "C:", "percent_used": 41.0}],
    }
}


@pytest.fixture
async def kit(tmp_path):
    db = str(tmp_path / "kenny.sqlite")
    store, events, state, tickets = (
        TelemetryStore(db),
        EventStore(db),
        AlertStateStore(db),
        TicketStore(db),
    )
    for s in (store, events, state, tickets):
        await s.connect()
    reader = TicketAlertReader(
        event_store=events, store=store, registry=AgentRegistry(), alert_state=state
    )
    yield store, events, tickets, reader
    for s in (store, events, state, tickets):
        await s.close()


async def _ticket(tickets: TicketStore, *, dedup_key: str, agent_id: str | None = "pc1"):
    return await tickets.create(
        title="pc1: disk is full",
        origin="alert" if dedup_key else "dashboard",
        priority="high",
        category="alert",
        summary="",
        agent_id=agent_id,
        dedup_key=dedup_key,
    )


async def test_a_still_failing_subject_reads_as_crit(kit) -> None:
    store, events, tickets, reader = kit
    await store.insert("pc1", "2026-09-01T08:00:00Z", CRIT)
    ticket = await _ticket(tickets, dedup_key="alert|pc1|state|disk")

    out = await reader.for_ticket(ticket)
    assert [(f["name"], f["status"]) for f in out["findings"]] == [("disk", "crit")]
    assert out["collected_at"] == "2026-09-01T08:00:00Z"


async def test_a_recovered_subject_reads_as_ok_while_the_ticket_stays_open(kit) -> None:
    """The trail says what was reported; only the current evaluation says
    whether the condition is still there. A ticket nobody has closed yet can
    perfectly well be about a problem that went away."""

    store, events, tickets, reader = kit
    await store.insert("pc1", "2026-09-01T08:00:00Z", CRIT)
    await store.insert("pc1", "2026-09-02T08:00:00Z", RECOVERED)
    ticket = await _ticket(tickets, dedup_key="alert|pc1|state|disk")

    out = await reader.for_ticket(ticket)
    assert [(f["name"], f["status"]) for f in out["findings"]] == [("disk", "ok")]


async def test_a_subject_that_is_not_a_section_reads_as_unknown(kit) -> None:
    """``offline`` names the host, not a section of it — so there is no verdict
    to give. Reported rather than dropped: a reader deciding whether to close
    needs to see that this subject has no live state."""

    store, events, tickets, reader = kit
    await store.insert("pc1", "2026-09-01T08:00:00Z", CRIT)
    ticket = await _ticket(tickets, dedup_key="alert|pc1|state|offline")

    out = await reader.for_ticket(ticket)
    assert [(f["name"], f["status"]) for f in out["findings"]] == [("offline", "unknown")]


async def test_a_section_whose_collector_stopped_reporting_reads_as_unknown(kit) -> None:
    store, events, tickets, reader = kit
    await store.insert("pc1", "2026-09-01T08:00:00Z", RECOVERED)  # no `memory` section
    ticket = await _ticket(tickets, dedup_key="alert|pc1|state|memory")

    out = await reader.for_ticket(ticket)
    assert [(f["name"], f["status"]) for f in out["findings"]] == [("memory", "unknown")]


async def test_every_subject_of_a_multi_section_alert_is_reported(kit) -> None:
    store, events, tickets, reader = kit
    await store.insert("pc1", "2026-09-01T08:00:00Z", CRIT)
    ticket = await _ticket(tickets, dedup_key="alert|pc1|state|disk+memory")

    out = await reader.for_ticket(ticket)
    assert [f["name"] for f in out["findings"]] == ["disk", "memory"]


async def test_a_human_opened_ticket_has_no_subjects_and_no_alerts(kit) -> None:
    store, events, tickets, reader = kit
    await store.insert("pc1", "2026-09-01T08:00:00Z", CRIT)
    ticket = await _ticket(tickets, dedup_key="")

    out = await reader.for_ticket(ticket)
    assert out["findings"] == []
    assert out["alerts"] == []


async def test_a_ticket_whose_host_has_no_snapshot_still_names_its_subjects(kit) -> None:
    """A host that has never pushed, or whose snapshot fell out of retention,
    is a normal state and must not raise. Its subjects read ``unknown`` — the
    same answer as a collector that stopped reporting, because it is the same
    situation: the ticket names a section and we have no verdict for it.

    A ticket with no host at all has nothing to evaluate, so it reports none."""

    store, events, tickets, reader = kit
    unseen = await _ticket(tickets, dedup_key="alert|pc9|state|disk", agent_id="pc9")
    out = await reader.for_ticket(unseen)
    assert [(f["name"], f["status"]) for f in out["findings"]] == [("disk", "unknown")]
    assert out["collected_at"] == ""

    hostless = await _ticket(tickets, dedup_key="alert||state|disk", agent_id=None)
    assert (await reader.for_ticket(hostless))["findings"] == []


async def test_the_alert_history_is_chronological_and_carries_its_title(kit) -> None:
    store, events, tickets, reader = kit
    await store.insert("pc1", "2026-09-01T08:00:00Z", CRIT)
    ticket = await _ticket(tickets, dedup_key="alert|pc1|state|disk")
    for at, title in [
        ("2026-09-02T08:00:00Z", "second"),
        ("2026-09-01T08:00:00Z", "first"),
    ]:
        eid = await events.insert_alert(
            agent_id="pc1",
            message=f"{title}\nbody",
            level="crit",
            fields={"title": title, "priority": "high", "event_type": "health"},
            at=at,
        )
        await events.link_alert_to_ticket(eid, ticket.id)

    out = await reader.for_ticket(ticket)
    assert [a["title"] for a in out["alerts"]] == ["first", "second"]
    assert out["alerts"][0]["body"] == "body"


async def test_another_tickets_alerts_are_not_mixed_in(kit) -> None:
    store, events, tickets, reader = kit
    await store.insert("pc1", "2026-09-01T08:00:00Z", CRIT)
    mine = await _ticket(tickets, dedup_key="alert|pc1|state|disk")
    theirs = await _ticket(tickets, dedup_key="alert|pc1|state|memory")
    eid = await events.insert_alert(agent_id="pc1", message="theirs\nbody", level="warn")
    await events.link_alert_to_ticket(eid, theirs.id)

    assert (await reader.for_ticket(mine))["alerts"] == []
    assert [a["title"] for a in (await reader.for_ticket(theirs))["alerts"]] == ["theirs"]

"""The presented timeline: what it condenses, what it drops, and what it must not touch.

Four seams are asserted here, and the first two are the ones that make the
projection safe to change later:

1. **Completeness.** Every trail row is either inside some entry's
   ``source_event_ids`` or matched by a *named* rule in ``DROP_RULES``. A row
   that is neither has been lost, and "lost" is exactly the failure mode a
   tidier view invites.
2. **Prose coverage.** Every tool in ``tool_classes.TOOL_CLASSES`` has a phrase,
   so adding a tool to the catalog cannot drop a bare ``powershell_exec`` into
   the middle of an English sentence.
3. **One verdict, one entry** — the regression for a verdict that used to be
   rendered three times over.
4. **The resolve gate still sees what the view hides.** ``triage.may_resolve``
   reads the trail, not the projection; a read-only call that is condensed out
   of sight must still license a closing verdict (ADR-0056).
"""

from __future__ import annotations

import pytest

from kenny_server import ticket_timeline as tl
from kenny_server.ticket_assistant import EXCLUDED_TOOLS
from kenny_server.ticketstore import TicketEvent, TicketStore
from kenny_server.tool_classes import READ_ONLY, TOOL_CLASSES
from kenny_server.toolloop import SURFACE_ONLY_TOOLS
from kenny_server.triage import may_resolve


def ev(i: int, kind: str, actor: str = "assistant", **kw) -> TicketEvent:
    return TicketEvent(
        id=i, ticket_id="t1", at=f"2026-09-06T18:32:{i:02d}Z", kind=kind, actor=actor, **kw
    )


def tool_call(i: int, tool: str, *, actor="assistant", ok=True, args=None, **kw) -> TicketEvent:
    fields = {"args": dict(args or {})}
    fields.update(kw.pop("fields", {}))
    return ev(
        i,
        "tool_call",
        actor,
        tool=tool,
        tool_class=TOOL_CLASSES[tool],
        ok=ok,
        summary=f"{tool} {'succeeded' if ok else 'failed: timeout'}",
        fields=fields,
        **kw,
    )


#: An alert-opened ticket kenny investigated unprompted, then an operator
#: worked: the full vocabulary in one trail, shaped like a real one.
def full_trail() -> list[TicketEvent]:
    return [
        ev(1, "state", "system", to_state="new", summary="opened from an alert"),
        ev(2, "note", "triage", summary="looking into this before anyone is asked to"),
        ev(3, "state", "system", from_state="new", to_state="in_progress", summary="work started"),
        tool_call(4, "agent_health", actor="triage", args={"id": "linus-pc"}),
        tool_call(5, "diag_eventlog", actor="triage", args={"log": "System", "count": 50}),
        tool_call(6, "fs_disk_usage", actor="triage"),
        tool_call(7, "diag_eventlog", actor="triage", args={"log": "Setup", "count": 50}),
        tool_call(8, "diag_eventlog", actor="triage", args={"log": "Application", "count": 30}),
        ev(
            9,
            "tool_call",
            tool="ticket_triage_verdict",
            tool_class="standard_change",
            summary="ticket_triage_verdict authorized autonomously as a standard change",
            fields={"args": {"verdict": "actionable"}},
        ),
        ev(
            10,
            "note",
            "triage",
            summary="triage verdict: actionable - the disk is full",
            fields={
                "verdict": "actionable",
                "finding": "Windows Update is failing because C:\\ is full.",
                "evidence": "fs_disk_usage confirms 97% used.",
                "resolvable": False,
            },
        ),
        ev(
            11,
            "tool_call",
            tool="ticket_triage_verdict",
            tool_class="standard_change",
            ok=True,
            summary="ticket_triage_verdict succeeded",
            fields={"args": {"verdict": "actionable"}},
        ),
        ev(12, "block", "system", summary="waiting for a reply", fields={"to_blocked_on": "user"}),
        ev(13, "note", "system", summary="stall reminder sent (blocked on user)"),
        ev(
            14,
            "message",
            "operator:3",
            summary="message from the operator",
            fields={"text": "Can you free some space?", "surface": "dashboard"},
        ),
        ev(
            15,
            "handoff",
            summary="discarded attempt to target other-pc",
            fields={"applied": False, "attempted_agent_id": "other-pc"},
        ),
        ev(
            16,
            "approval",
            "assistant",
            tool="winget_uninstall",
            tool_class="normal_change",
            summary="operator_approval requested for winget_uninstall",
            fields={"args": {"id": "Foo"}, "approval_id": "a1"},
        ),
        ev(
            17,
            "approval",
            "operator:3",
            tool="winget_uninstall",
            tool_class="normal_change",
            ok=True,
            summary="operator_approval approved for winget_uninstall",
            fields={"approval_id": "a1"},
        ),
        tool_call(18, "winget_uninstall", args={"id": "Foo"}),
        ev(
            19,
            "tool_call",
            tool="net_dns_flush",
            tool_class="standard_change",
            summary="net_dns_flush authorized autonomously as a standard change",
            fields={"args": {}},
        ),
        tool_call(20, "net_dns_flush"),
        tool_call(21, "diag_services", ok=False, fields={"error": {"code": "timeout"}}),
        ev(
            22,
            "error",
            summary="screen_capture was refused: needs_consent",
            tool="screen_capture",
            ok=False,
            fields={"error": {"code": "needs_consent", "message": ""}},
        ),
        ev(
            23,
            "message",
            "assistant",
            summary="reply",
            fields={"text": "I freed **12 GB**.", "surface": "dashboard"},
        ),
        ev(
            28,
            "note",
            "assistant",
            summary="Drive C: had **12 GB** of update leftovers; they are gone.",
            fields={"turn_summary": True},
        ),
        ev(24, "note", "operator:3", summary="Watched it overnight; stable."),
        ev(25, "assign", "operator:3", summary="claimed by thomas"),
        ev(26, "state", "operator:3", from_state="in_progress", to_state="resolved"),
        ev(27, "note", "system", summary="an earlier diag_services call was never completed"),
    ]


# -- seam 1: nothing is lost, nothing is invented --------------------------


def test_every_trail_row_is_presented_or_explicitly_dropped():
    trail = full_trail()
    entries = tl.project(trail)

    presented: set[int] = set()
    for entry in entries:
        assert entry.source_event_ids, f"{entry.kind} entry stands for no trail row"
        presented |= set(entry.source_event_ids)

    ids = {e.id for e in trail}
    assert presented <= ids, "the projection referenced a trail row that does not exist"

    by_id = {e.id: e for e in trail}
    for missing in sorted(ids - presented):
        reason = tl._drop_reason(by_id[missing])
        assert reason is not None, (
            f"event {missing} ({by_id[missing].kind}: {by_id[missing].summary!r}) "
            "is neither presented nor covered by a named drop rule"
        )

    # And no row is presented twice: a reader must never see one fact as two.
    seen: set[int] = set()
    for entry in entries:
        for source in entry.source_event_ids:
            assert source not in seen, f"trail row {source} is presented more than once"
            seen.add(source)


def test_every_drop_rule_is_exercised_by_the_reference_trail():
    """A rule nothing matches is a rule nobody is testing."""

    trail = full_trail()
    fired = {tl._drop_reason(e) for e in trail} - {None}
    assert fired == {name for name, _ in tl.DROP_RULES}


def test_presented_kinds_are_the_declared_ones():
    for entry in tl.project(full_trail()):
        assert entry.kind in tl.PRESENTED_KINDS
        assert entry.body in (tl.MARKDOWN, tl.VERBATIM, tl.STATUS)


# -- seam 2: the prose tables track the tool catalog ------------------------


#: The tools that can never put a ``tool_call`` row on a ticket's trail, and so
#: need no phrase. Derived from the two sets that make it true, never listed by
#: hand: a name taken back out of either immediately owes a phrase again.
_NO_TICKET_ROW: frozenset[str] = SURFACE_ONLY_TOOLS | EXCLUDED_TOOLS


@pytest.mark.parametrize(
    "tool,tier",
    sorted((t, c) for t, c in TOOL_CLASSES.items() if t not in _NO_TICKET_ROW),
)
def test_every_tool_has_a_phrase(tool: str, tier: str):
    """Every tool a ticket-bound turn can call composes into a sentence.

    Exempt is everything no such turn can reach. A surface-only tool is not a
    call on a machine at all — it is how kenny speaks to the ticket, and the
    projection drops its rows under a named rule (`surface_only_tool`) rather
    than describing them. An excluded tool is withheld from the ticket surface
    outright (`ticket_assistant.EXCLUDED_TOOLS`): the copilot's `ticket_draft`
    and `ticket_find`, and `select_agent`, which no ticket may call because its
    target is frozen. A phrase for a row that cannot exist would describe
    nothing; a missing one for a row that can would print a raw tool name.

    The verdict tool and `select_agent` keep phrases from when that was not yet
    true; an unused phrase is harmless.
    """

    table = tl.OBSERVED_PHRASES if tier == READ_ONLY else tl.CHANGED_PHRASES
    assert tool in table, (
        f"{tool} has no phrase in ticket_timeline; a timeline sentence would "
        "otherwise read out a raw tool name"
    )


# -- the condensation itself -----------------------------------------------


def test_read_only_calls_condense_into_one_factual_sentence():
    entries = tl.project(full_trail())
    activity = [e for e in entries if e.kind == tl.ACTIVITY]
    assert len(activity) == 1
    only = activity[0]
    assert only.actor == "triage"
    assert only.source_event_ids == (4, 5, 6, 7, 8)
    # The three event-log reads are one phrase naming all three logs, not three
    # identical ones: which logs were read is the whole informational content.
    assert only.text == (
        "I looked at the agent's health, the event log (System, Setup, Application) "
        "and disk usage."
    )


def test_an_autonomous_change_says_nobody_was_asked():
    entries = tl.project(full_trail())
    dns = [e for e in entries if "DNS" in e.text]
    assert len(dns) == 1
    assert dns[0].kind == tl.ACTION
    assert dns[0].text == "I flushed the DNS cache without being asked — that is a routine change."
    # Gate row and result row are one entry, not two.
    assert dns[0].source_event_ids == (19, 20)


def test_an_approved_change_does_not_claim_autonomy():
    entries = tl.project(full_trail())
    removal = [e for e in entries if e.kind == tl.ACTION and "removed a package" in e.text]
    assert len(removal) == 1
    assert removal[0].text == "I removed a package."
    assert "without being asked" not in removal[0].text


def test_failures_are_kept_because_they_explain_gaps():
    problems = [e for e in tl.project(full_trail()) if e.kind == tl.PROBLEM]
    texts = [p.text for p in problems]
    assert "I could not read the system services (the host returned `timeout`)." in texts
    assert "screen_capture was refused: needs_consent" in texts


def test_a_message_kenny_does_not_keep_says_so_instead_of_showing_its_label():
    """A Discord-origin message carries a summary, never the family's wording.

    Rendering that summary as the message reads as though somebody typed
    "opening message". The gap is named instead (ADR-0046).
    """

    trail = [
        ev(1, "message", "user:7", summary="opening message", fields={"surface": "discord"}),
    ]
    entry = tl.project(trail)[0]
    assert entry.kind == tl.LIFECYCLE
    assert "does not keep" in entry.text
    assert "opening message" not in entry.text


def test_an_automatic_start_is_bookkeeping_but_a_persons_is_a_decision():
    """`new -> in_progress` by nobody is machinery; by a person it is a choice."""

    auto = ev(1, "state", "system", from_state="new", to_state="in_progress", summary="work started")
    assert tl._drop_reason(auto) == "automatic_start"
    # The same move with a different reason string is the same machinery.
    assert tl._drop_reason(ev(2, "state", "system", from_state="new", to_state="in_progress",
                              summary="started on creation")) == "automatic_start"
    human = ev(3, "state", "operator:3", from_state="new", to_state="in_progress")
    assert tl._drop_reason(human) is None
    assert tl.project([human])[0].text == "started work on this ticket"


def test_a_persons_words_are_verbatim_and_kennys_are_markdown():
    by_source = {e.source_event_ids[0]: e for e in tl.project(full_trail())}
    assert by_source[14].kind == tl.MESSAGE
    assert by_source[14].body == tl.VERBATIM
    assert by_source[14].text == "Can you free some space?"
    # Kenny's own prose is markdown where it is shown — and what is shown of a
    # turn is the summary it wrote for the ticket, not the reply it gave in the
    # conversation (see the drop rule test below).
    assert by_source[28].body == tl.MARKDOWN
    assert by_source[28].text == "Drive C: had **12 GB** of update leftovers; they are gone."
    # An operator's note is their own typing too, and is never parsed as markup.
    assert by_source[24].body == tl.VERBATIM


def test_the_ticket_shows_what_a_turn_came_to_not_what_was_said():
    """The conversation belongs to the surface it happened on; the ticket keeps
    the record. Nothing is lost — the reply is still in the trail, which the
    audit tab renders verbatim."""

    reply = ev(
        1, "message", "assistant", summary="reply",
        fields={"text": "I freed **12 GB**.", "surface": "dashboard"},
    )
    assert tl._drop_reason(reply) == "assistant_prose"
    # A Discord-driven reply is kenny's too, and goes the same way.
    assert tl._drop_reason(
        ev(2, "message", "assistant", summary="reply",
           fields={"text": "done", "surface": "discord"})
    ) == "assistant_prose"
    # A person's message is not kenny's to drop.
    assert tl._drop_reason(
        ev(3, "message", "user:7", summary="msg", fields={"text": "hi"})
    ) is None


def test_lifecycle_reads_as_a_decision_somebody_made():
    by_source = {e.source_event_ids[0]: e for e in tl.project(full_trail())}
    assert by_source[26].kind == tl.LIFECYCLE
    assert by_source[26].text == "marked this ticket resolved"
    assert by_source[17].kind == tl.LIFECYCLE
    # The gate's own vocabulary ("operator_approval approved for …") says what
    # kind of gate it was; a reader wants to know who let what happen.
    assert by_source[17].text == "approved `winget_uninstall`"


def test_a_triage_resolution_does_not_repeat_the_finding():
    """The reason on a triage `state` row is the finding, rendered above it."""

    trail = [
        ev(
            1,
            "note",
            "triage",
            summary="triage verdict: phantom - no such device",
            fields={"verdict": "phantom", "finding": "The device named is not on this PC."},
        ),
        ev(
            2,
            # Recorded as `system`, not as the investigation — so the rule has
            # to key on the server's own `triage: ` prefix, not on the actor.
            "state",
            "system",
            from_state="in_progress",
            to_state="resolved",
            summary="triage: phantom - The device named is not on this PC.",
        ),
    ]
    entries = tl.project(trail)
    assert entries[1].text == "marked this ticket resolved"


def test_an_orphan_gate_row_is_surfaced_not_swallowed():
    """A crash between authorising a change and running it must stay visible."""

    trail = [
        ev(
            1,
            "tool_call",
            tool="net_dns_flush",
            tool_class="standard_change",
            summary="net_dns_flush authorized autonomously as a standard change",
            fields={"args": {}},
        )
    ]
    entries = tl.project(trail)
    assert len(entries) == 1
    assert entries[0].kind == tl.PROBLEM
    assert entries[0].source_event_ids == (1,)


# -- seam 3: one verdict, one entry ----------------------------------------


def test_a_verdict_is_presented_exactly_once():
    entries = tl.project(full_trail())
    findings = [e for e in entries if e.kind == tl.FINDING]
    assert len(findings) == 1
    assert findings[0].fields["verdict"] == "actionable"
    assert findings[0].fields["evidence"] == "fs_disk_usage confirms 97% used."
    # The verdict's own two tool_call rows carry the same text in their args and
    # are not rendered at all.
    assert not [e for e in entries if "ticket_triage_verdict" in e.text]
    assert all(9 not in e.source_event_ids and 11 not in e.source_event_ids for e in entries)


# -- seam 4: the resolve gate reads the trail, not the view ------------------


@pytest.fixture
async def store(tmp_path):
    s = TicketStore(str(tmp_path / "kenny.sqlite"))
    await s.connect()
    yield s
    await s.close()


async def test_may_resolve_still_sees_a_call_the_view_condensed(store: TicketStore):
    """ADR-0056's evidence rule must not depend on how the timeline reads.

    The read-only call below is condensed into a single activity sentence and
    its own row is nowhere in the presented entries — and it still licenses a
    closing verdict, because ``may_resolve`` reads ``ticket_events``.
    """

    ticket = await store.create(title="linus-pc: disk", origin="alert", agent_id="linus-pc")
    await store.append_event(
        ticket_id=ticket.id,
        kind="tool_call",
        actor="triage",
        tool="fs_disk_usage",
        tool_class=READ_ONLY,
        ok=True,
        summary="fs_disk_usage succeeded",
        fields={"args": {}},
    )
    events = await store.list_events(ticket.id)

    entries = tl.project(events)
    assert [e.kind for e in entries] == [tl.ACTIVITY]
    assert "fs_disk_usage" not in entries[0].text

    allowed, why_not = await may_resolve(store, ticket, "phantom")
    assert allowed, why_not


# -- the one part of a sentence that is not this module's own words ---------


def test_a_detail_lifted_from_arguments_cannot_steer_the_sentence():
    """An argument is the model's, and on a triage ticket the model has been
    reading event-log text (ADR-0023). It may name a log; it may not write
    prose in kenny's voice."""

    hostile = "System) and the disk is fine ("
    trail = [tool_call(1, "diag_eventlog", args={"log": hostile})]
    text = tl.project(trail)[0].text
    assert "the disk is fine" not in text
    assert text == "I looked at the event log."

    # Over-long and non-plain values are dropped the same way; a plain log name
    # is kept, because naming which log was read is the point.
    assert tl.project([tool_call(2, "diag_eventlog", args={"log": "x" * 200})])[0].text == (
        "I looked at the event log."
    )
    assert tl.project([tool_call(3, "diag_eventlog", args={"log": "Setup"})])[0].text == (
        "I looked at the event log (Setup)."
    )


def test_two_gated_calls_of_one_tool_keep_both_gate_rows():
    """Pairing is a queue, not a slot: neither gate row may be overwritten."""

    def gate(i: int) -> TicketEvent:
        return ev(
            i,
            "tool_call",
            tool="net_dns_flush",
            tool_class="standard_change",
            summary="net_dns_flush authorized autonomously as a standard change",
            fields={"args": {}},
        )

    trail = [gate(1), gate(2), tool_call(3, "net_dns_flush"), tool_call(4, "net_dns_flush")]
    entries = tl.project(trail)
    assert [e.source_event_ids for e in entries] == [(1, 3), (2, 4)]

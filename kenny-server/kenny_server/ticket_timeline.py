"""The ticket's presented timeline: findings and people, not machine trail.

A ticket keeps a machine-readable trail — every tool call with its arguments,
every state change, every gate and its decision (:mod:`ticketstore`, ADR-0046).
That trail is the audit, and two things depend on it being complete: an
operator reconstructing why a call ran, and :func:`triage.may_resolve`, which
lets an unprompted verdict resolve a ticket only when a read-only call
*actually ran and actually succeeded* on it (ADR-0056).

Neither of those is a reason to *show* the trail as the ticket's history. Read
as history it buries the three lines that carry meaning — what was found, what
a person said, what kenny changed without being asked — under rows that exist
to be queried, not read. This module is the projection in between: a pure
function from trail rows to presented entries. It reads the trail and never
writes it, so nothing here can weaken what the audit or the resolve gate see.

**Every entry names its sources.** :attr:`TimelineEntry.source_event_ids` lists
the trail rows an entry stands for, and a row this module drops is dropped
under a *named* rule (:data:`DROP_RULES`). Together those make the projection
testable in the only way that matters: it can be shown to invent nothing and
lose nothing silently (``tests/test_ticket_timeline.py``).

**The prose is deterministic and strictly factual.** :func:`project` composes
sentences in kenny's own voice — there is no model in this path and no
token spent — and it says only what the trail records as having happened. It
never characterises, rates or concludes. That restraint is what makes the
prose safe to leave unmarked beside a model's own words: a reader cannot be
misled by a sentence that can only ever be true. Judgement stays where
ADR-0056 put it, in a verdict that carries its evidence next to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .ticketstore import ASSISTANT_ACTOR, TRIAGE_ACTOR, TicketEvent
from .tool_classes import READ_ONLY, STANDARD_CHANGE
from .toolloop import SURFACE_ONLY_TOOLS

# --------------------------------------------------------------------------
# The presented vocabulary
# --------------------------------------------------------------------------

#: A person's or kenny's own words, kept verbatim by the trail.
MESSAGE = "message"
#: A triage verdict — the one entry that is a conclusion rather than a record.
FINDING = "finding"
#: What kenny looked at, condensed from the read-only calls it made.
ACTIVITY = "activity"
#: A change kenny carried out, and whether anybody was asked first.
ACTION = "action"
#: A move in the ticket's life a person would recognise as a decision.
LIFECYCLE = "lifecycle"
#: Something that failed or was refused, and therefore explains a gap.
PROBLEM = "problem"

PRESENTED_KINDS: tuple[str, ...] = (MESSAGE, FINDING, ACTIVITY, ACTION, LIFECYCLE, PROBLEM)

#: How :attr:`TimelineEntry.text` must be rendered. ``markdown`` is kenny's own
#: prose, which the conversational prompts ask for and the model writes.
#: ``verbatim`` is text a person typed — their line breaks are theirs and so
#: are their ``*`` and ``_``. ``status`` is a line this module composed, which
#: parsing could only misread.
MARKDOWN = "markdown"
VERBATIM = "verbatim"
STATUS = "status"


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One row of the ticket's presented timeline."""

    at: str
    actor: str
    kind: str
    text: str
    body: str
    #: The trail rows this entry stands for, in trail order. Never empty.
    source_event_ids: tuple[int, ...]
    #: Only a :data:`FINDING` carries these — the verdict payload, unchanged.
    fields: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "actor": self.actor,
            "kind": self.kind,
            "text": self.text,
            "body": self.body,
            "source_event_ids": list(self.source_event_ids),
            "fields": self.fields,
        }


# --------------------------------------------------------------------------
# Prose tables
# --------------------------------------------------------------------------

#: What a read-only tool looked at, as a noun phrase that composes into
#: "I looked at A, B and C." Completeness against :data:`tool_classes.TOOL_CLASSES`
#: is asserted in ``tests/test_ticket_timeline.py`` — a tool added to the catalog
#: without a phrase here fails that test rather than dropping a bare tool name
#: into the middle of a sentence.
#:
#: This is a hand-maintained table, which ADR-0026 and ADR-0041 both rejected
#: for event patterns and service names. The difference is the shape of the
#: space: those are open and differ per fleet, so a table could only ever be
#: behind. The tool set is closed, lives in this repository, and changes in the
#: same commit that would update this map.
OBSERVED_PHRASES: dict[str, str] = {
    "list_agents": "which machines report in",
    "select_agent": "which machine this is about",
    "fleet_overview": "the fleet overview",
    "agent_health": "the agent's health",
    "agent_snapshot": "the host's latest snapshot",
    "fs_list": "a folder's contents",
    "fs_search": "files matching a search",
    "fs_read": "a file's contents",
    "fs_disk_usage": "disk usage",
    "winget_list": "the installed packages",
    "diag_processes": "the running processes",
    "diag_services": "the system services",
    "diag_eventlog": "the event log",
    "diag_autostart": "what starts automatically",
    "net_config": "the network configuration",
    "screen_capture": "the screen",
    "remotehelp_status": "the remote-help session",
    "telemetry_collect": "fresh telemetry",
    "webfilter_status": "whether the web filter is in force",
    "webfilter_get": "the web filter's configuration",
    "web_activity_query": "the browsing history",
    "reliability_suppression_list": "the muted event patterns",
    "ticket_rule_list": "the auto-ticket rules",
}

#: What a change-tier tool did, as a past-tense verb phrase completing
#: "I <phrase>." Same completeness rule as :data:`OBSERVED_PHRASES`.
CHANGED_PHRASES: dict[str, str] = {
    "powershell_exec": "ran a PowerShell command on the host",
    "shell_exec": "ran a shell command on the host",
    "winget_install": "installed a package",
    "winget_uninstall": "removed a package",
    "winget_update": "updated an installed package",
    "net_dns_flush": "flushed the DNS cache",
    "net_adapter_reset": "reset the network adapter",
    "remotehelp_start": "opened a remote-help session on the desktop",
    "remotehelp_stop": "closed the remote-help session",
    "agent_update": "updated the agent",
    "webfilter_apply": "applied the web filter on the host",
    "webfilter_clear": "cleared the web filter from the host",
    "webfilter_set": "changed what the web filter blocks",
    "webfilter_push": "pushed the configured block list to the host",
    "reliability_suppression_add": "muted an event pattern",
    "reliability_suppression_remove": "un-muted an event pattern",
    "ticket_rule_set": "changed an auto-ticket rule",
    "ticket_rule_remove": "removed an auto-ticket rule",
    "account_set_enabled": "enabled or disabled an account",
    "account_set_admin": "changed an account's administrator rights",
    "account_set_logon_rights": "changed who may sign in",
    "account_create": "created an account",
    "account_delete": "deleted an account",
    "account_session_action": "acted on a signed-in session",
    "password_policy_set": "changed the password policy",
    "ticket_triage_verdict": "recorded a verdict",
}

#: Tools whose arguments carry a discriminator worth naming, because the same
#: tool is typically called several times with different ones. Only the event
#: log qualifies today: three ``diag_eventlog`` calls are three different logs,
#: and "the event log" three times over would hide exactly what was read.
_DETAIL_ARG: dict[str, str] = {"diag_eventlog": "log"}

#: Block reasons written by the machinery around a turn rather than by anyone
#: deciding anything. Each is the mechanical shadow of an entry that is
#: presented in its own right: the gate card, the header's status chip, the
#: turn-cap note.
_MECHANICAL_BLOCKS: frozenset[str] = frozenset(
    {"waiting for a reply", "gate decided", "turn cap reached"}
)


# --------------------------------------------------------------------------
# Drop rules
# --------------------------------------------------------------------------

def _is_mechanical_block(event: TicketEvent) -> bool:
    if event.kind != "block":
        return False
    if event.summary in _MECHANICAL_BLOCKS:
        return True
    return event.summary.endswith((" held for operator_approval", " held for user_consent"))


#: Every reason a trail row is not presented, as ``(name, predicate)``. A row
#: that matches none of these must end up in some entry's
#: ``source_event_ids``; the completeness test asserts exactly that, so
#: "tidier" can never quietly become "gone".
DROP_RULES: tuple[tuple[str, Any], ...] = (
    (
        # The mechanical `new -> in_progress` the machinery performs before
        # kenny can work — on the first turn, on a resume, on an alert-opened
        # ticket. A person clicking START WORK is a decision and is kept; this
        # is the same move made by nobody, which is why the actor decides and
        # not the reason string.
        "automatic_start",
        lambda e: e.kind == "state" and e.to_state == "in_progress" and e.actor == "system",
    ),
    (
        # Blocking on the requester after an answer, clearing a decided gate,
        # holding for a gate: each is already visible as the header's status
        # chip or as the gate card itself.
        "mechanical_block",
        _is_mechanical_block,
    ),
    (
        # The reminder changes nothing (`TicketStore.mark_nudged`): it reports
        # that kenny poked somebody about a wait the header already shows.
        "stall_reminder",
        lambda e: e.kind == "note" and e.summary.startswith("stall reminder sent"),
    ),
    (
        # Repairing a transcript whose tool_use block was never answered is
        # session bookkeeping, not something that happened to the machine.
        "healed_tool_use",
        lambda e: e.kind == "note"
        and e.actor == "system"
        and e.summary.endswith("call was never completed"),
    ),
    (
        # "looking into this before anyone is asked to" — the one thing it
        # says is that nobody asked, and the activity line below it carries
        # that in its byline.
        "triage_opening_note",
        lambda e: e.kind == "note"
        and e.actor == TRIAGE_ACTOR
        and not (e.fields or {}).get("verdict"),
    ),
    (
        # A model-supplied agent_id the frozen target discarded. Nothing
        # happened, on purpose (ADR-0038); the audit is where that belongs.
        "discarded_retarget",
        lambda e: e.kind == "handoff" and (e.fields or {}).get("applied") is False,
    ),
    (
        # `ticket_triage_verdict` and `ticket_summary` are not calls on a
        # machine, they are how kenny speaks to the ticket
        # (`toolloop.SURFACE_ONLY_TOOLS`). Their tool_call rows would restate
        # the card or the summary word for word.
        "surface_only_tool",
        lambda e: e.kind == "tool_call" and (e.tool or "") in SURFACE_ONLY_TOOLS,
    ),
    (
        # Kenny's own reply, in full. It belongs to the conversation it was said
        # in — the drawer, the Discord thread — and it is in the trail, which
        # the audit tab shows verbatim. What the ticket *shows* of a turn is the
        # summary kenny wrote for it (`ticket_summary`) plus the deterministic
        # lines this module composes. The alternative is a ticket that is a
        # second, worse transcript of a conversation held elsewhere.
        "assistant_prose",
        lambda e: e.kind == "message" and e.actor in (ASSISTANT_ACTOR, TRIAGE_ACTOR),
    ),
    (
        # The request half of a gate. The decision is kept; while the request
        # is still open, the ticket's own approval card is showing the frozen
        # call, which is more than this row could say.
        "gate_request",
        lambda e: e.kind in ("approval", "consent") and e.ok is None,
    ),
)


def _drop_reason(event: TicketEvent) -> str | None:
    for name, matches in DROP_RULES:
        if matches(event):
            return name
    return None


# --------------------------------------------------------------------------
# Sentence composition
# --------------------------------------------------------------------------

def _join(parts: Sequence[str]) -> str:
    """``[a, b, c]`` → ``"a, b and c"``."""

    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def _observed_phrase(tool: str, details: Sequence[str]) -> str:
    base = OBSERVED_PHRASES.get(tool, tool)
    if details:
        return f"{base} ({', '.join(details)})"
    return base


#: A detail is the only part of a composed sentence that does not come from
#: this module. It is an argument the model chose, and on a triage ticket the
#: model has been reading event-log text (ADR-0023) — so it is clamped to a
#: short, plain token. Anything else is dropped and the phrase stands alone,
#: because a sentence in kenny's voice must not be steerable by the text kenny
#: was looking at. Rendering is unparsed either way; this is about the words.
_DETAIL_MAX = 32
_DETAIL_OK = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-.:"
)


def _detail_of(event: TicketEvent) -> str | None:
    key = _DETAIL_ARG.get(event.tool or "")
    if key is None:
        return None
    value = ((event.fields or {}).get("args") or {}).get(key)
    if not isinstance(value, (str, int)):
        return None
    text = str(value)
    if not text or len(text) > _DETAIL_MAX or not set(text) <= _DETAIL_OK:
        return None
    return text


def _changed_phrase(tool: str) -> str:
    return CHANGED_PHRASES.get(tool, f"ran {tool}")


def _error_code(event: TicketEvent) -> str:
    error = (event.fields or {}).get("error")
    if isinstance(error, dict):
        code = error.get("code")
        if isinstance(code, str) and code:
            return code
    return "an error"


_TRANSITION_VERBS: dict[str, str] = {
    "in_progress": "started work on this ticket",
    "resolved": "marked this ticket resolved",
    "closed": "closed this ticket",
    "cancelled": "cancelled this ticket",
    "new": "reopened this ticket",
}


def _state_text(event: TicketEvent) -> str:
    if event.from_state is None:
        # Creation. The reason names the origin ("opened from an alert").
        return event.summary or f"opened as {event.to_state}"
    verb = _TRANSITION_VERBS.get(event.to_state or "", f"moved this ticket to {event.to_state}")
    # A triage resolution's reason repeats the finding word for word, and the
    # finding is rendered in full a few rows above. The prefix is the server's
    # own (`triage.py`), so this keys on it rather than on the actor: the
    # transition is recorded as `system`, not as the investigation.
    if event.summary and not event.summary.startswith("triage: "):
        return f"{verb} — {event.summary}"
    return verb


def _decision_text(event: TicketEvent) -> str:
    """An answered gate, in the words of the person who answered it.

    The trail says ``operator_approval approved for winget_install`` because
    that is what the gate's own vocabulary calls it. A reader wants to know who
    let what happen.
    """

    tool = event.tool or "the call"
    if event.ok is None:  # pragma: no cover - request rows are dropped
        return event.summary
    if event.actor == "system":
        return (
            f"the request to run `{tool}` expired without an answer, "
            "which counts as a refusal"
        )
    if event.kind == "consent":
        return f"gave consent for `{tool}`" if event.ok else f"refused consent for `{tool}`"
    return f"approved `{tool}`" if event.ok else f"denied `{tool}`"


#: A Discord-origin message the trail records only as a one-line summary,
#: because a family member's own words are not kenny's to keep (ADR-0046).
#: Rendering the summary as if it were the message reads as though somebody
#: literally typed "opening message".
_UNRECORDED_MESSAGE = (
    "wrote in the Discord thread — kenny does not keep a family member's own wording"
)


def _block_text(event: TicketEvent) -> str:
    to = (event.fields or {}).get("to_blocked_on")
    if not to:
        return event.summary or "cleared what this ticket was waiting for"
    label = {"user": "the requester", "operator": "an operator", "approval": "an approval"}.get(
        str(to), str(to)
    )
    if event.summary and event.summary not in _MECHANICAL_BLOCKS:
        return f"parked this ticket on {label} — {event.summary}"
    return f"parked this ticket on {label}"


# --------------------------------------------------------------------------
# The projection
# --------------------------------------------------------------------------

class _ActivityRun:
    """Consecutive successful read-only calls, waiting to become one sentence."""

    def __init__(self, actor: str, at: str) -> None:
        self.actor = actor
        self.at = at
        self.ids: list[int] = []
        # Insertion-ordered: a tool called three times keeps its first position
        # and merges its details, so three event-log reads read as one phrase.
        self.tools: dict[str, list[str]] = {}

    def add(self, event: TicketEvent) -> None:
        self.ids.append(event.id)
        details = self.tools.setdefault(event.tool or "", [])
        detail = _detail_of(event)
        if detail and detail not in details:
            details.append(detail)

    def entry(self) -> TimelineEntry:
        phrases = [_observed_phrase(tool, details) for tool, details in self.tools.items()]
        return TimelineEntry(
            at=self.at,
            actor=self.actor,
            kind=ACTIVITY,
            text=f"I looked at {_join(phrases)}.",
            body=STATUS,
            source_event_ids=tuple(self.ids),
        )


def project(events: Iterable[TicketEvent]) -> list[TimelineEntry]:
    """Map a ticket's trail onto what its detail view shows.

    Order is the trail's own. An entry may stand for several rows (a run of
    read-only calls; an autonomously authorised change and its result), and a
    row may stand for none — see :data:`DROP_RULES`.
    """

    entries: list[TimelineEntry] = []
    run: _ActivityRun | None = None
    # Set by a standard-change gate row, consumed by that tool's next result:
    # the gate row is the only place the trail records that nobody was asked.
    autonomous: dict[str, list[int]] = {}

    def flush() -> None:
        nonlocal run
        if run is not None:
            entries.append(run.entry())
            run = None

    for event in events:
        if _drop_reason(event) is not None:
            continue

        if event.kind == "tool_call" and event.ok is None:
            # A standard change about to run without anyone deciding.
            flush()
            autonomous.setdefault(event.tool or "", []).append(event.id)
            continue

        if (
            event.kind == "tool_call"
            and event.ok
            and (event.tool_class or "") == READ_ONLY
        ):
            if run is None or run.actor != event.actor:
                flush()
                run = _ActivityRun(event.actor, event.at)
            run.add(event)
            continue

        flush()

        if event.kind == "tool_call":
            tool = event.tool or ""
            queued = autonomous.get(tool) or []
            gate_id = queued.pop(0) if queued else None
            sources = (gate_id, event.id) if gate_id is not None else (event.id,)
            if not event.ok:
                attempt = (
                    f"read {_observed_phrase(tool, [])}"
                    if (event.tool_class or "") == READ_ONLY
                    else _changed_phrase(tool)
                )
                entries.append(
                    TimelineEntry(
                        at=event.at,
                        actor=event.actor,
                        kind=PROBLEM,
                        text=f"I could not {attempt} (the host returned `{_error_code(event)}`).",
                        body=STATUS,
                        source_event_ids=tuple(i for i in sources if i is not None),
                    )
                )
                continue
            unasked = (
                " without being asked — that is a routine change"
                if gate_id is not None and (event.tool_class or "") == STANDARD_CHANGE
                else ""
            )
            entries.append(
                TimelineEntry(
                    at=event.at,
                    actor=event.actor,
                    kind=ACTION,
                    text=f"I {_changed_phrase(tool)}{unasked}.",
                    body=STATUS,
                    source_event_ids=tuple(i for i in sources if i is not None),
                )
            )
            continue

        entry = _single(event)
        if entry is not None:
            entries.append(entry)

    flush()
    # A gate row whose result never arrived (a crash between authorising and
    # executing). Surfacing it beats losing it: the completeness test would
    # fail on a silent drop, which is the point of having one.
    for tool, gate_ids in autonomous.items():
        for gate_id in gate_ids:
            entries.append(
                TimelineEntry(
                    at="",
                    actor=ASSISTANT_ACTOR,
                    kind=PROBLEM,
                    text=f"I was about to {_changed_phrase(tool)} without being asked, "
                    "and there is no record of how it ended.",
                    body=STATUS,
                    source_event_ids=(gate_id,),
                )
            )
    return entries


def _single(event: TicketEvent) -> TimelineEntry | None:
    """The entry for one trail row that is neither condensed nor paired."""

    def made(kind: str, text: str, body: str = STATUS, fields: dict[str, Any] | None = None):
        return TimelineEntry(
            at=event.at,
            actor=event.actor,
            kind=kind,
            text=text,
            body=body,
            source_event_ids=(event.id,),
            fields=fields or {},
        )

    fields = event.fields or {}

    if event.kind == "message":
        text = fields.get("text")
        if not (isinstance(text, str) and text):
            return made(LIFECYCLE, _UNRECORDED_MESSAGE)
        body = MARKDOWN if event.actor in (ASSISTANT_ACTOR, TRIAGE_ACTOR) else VERBATIM
        return made(MESSAGE, text, body)

    if event.kind == "note":
        if fields.get("verdict"):
            return made(FINDING, event.summary, STATUS, dict(fields))
        if fields.get("turn_summary"):
            # The one presented line kenny composed rather than this module.
            # Rendered as markdown for the same reason a reply is: it is kenny's
            # prose, written to the same light-markdown instruction.
            return made(MESSAGE, event.summary, MARKDOWN)
        return made(MESSAGE, event.summary, VERBATIM)

    if event.kind == "state":
        return made(LIFECYCLE, _state_text(event))

    if event.kind == "block":
        return made(LIFECYCLE, _block_text(event))

    if event.kind in ("approval", "consent"):
        return made(LIFECYCLE, _decision_text(event))

    if event.kind in ("assign", "handoff"):
        return made(LIFECYCLE, event.summary)

    if event.kind == "error":
        return made(PROBLEM, event.summary or "something went wrong")

    return made(LIFECYCLE, event.summary or event.kind)

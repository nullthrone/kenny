"""The unprompted ticket triage (ADR-0056) as a steward.

Describes what :mod:`kenny_server.triage` already does; it holds no behaviour of
its own. The prompt, the tool set and the iteration cap are imported from where
the running triage takes them, so the declaration cannot drift from the thing it
declares (``tests/test_steward_catalog.py`` joins the two).
"""

from __future__ import annotations

from ... import ticket_assistant, toolloop
from ...triage import DEFAULT_MAX_ITERATIONS
from ..spec import TICKET_CREATED, Budget, StewardSpec, Trigger, TriggerSet

__all__ = ["TRIAGE"]

TRIAGE = StewardSpec(
    id="triage",
    title="Ticket triage",
    description=(
        "Looks into a newly opened ticket on its own machine with read-only "
        "tools and records what it found before a person sees it."
    ),
    prompt=ticket_assistant._TRIAGE_SYSTEM_PROMPT,
    trigger=Trigger(kind="event", event=TICKET_CREATED),
    # Every new ticket, and nothing else: triage's trigger is what it is for,
    # not an install's choice.
    triggers_allowed=TriggerSet(events=frozenset({TICKET_CREATED}), editable=False),
    tools=ticket_assistant.TRIAGE_TOOLS,
    verdict_tool=toolloop.TRIAGE_VERDICT_TOOL,
    # The hash binds the default; KENNY_TRIAGE_MAX_ITERATIONS may move the live cap per install.
    budget=Budget(max_iterations=DEFAULT_MAX_ITERATIONS),
    default_mode="shadow",
)

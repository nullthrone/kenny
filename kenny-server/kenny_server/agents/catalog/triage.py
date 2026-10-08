"""The unprompted ticket triage (ADR-0056) as a specialized agent.

Describes what :mod:`kenny_server.triage` already does; it holds no behaviour of
its own. The prompt, the tool set and the iteration cap are imported from where
the running triage takes them, so the declaration cannot drift from the thing it
declares (``tests/test_agent_catalog.py`` joins the two).
"""

from __future__ import annotations

from ... import ticket_assistant, toolloop
from ...triage import DEFAULT_MAX_ITERATIONS
from ..spec import AgentSpec, Budget, Trigger

__all__ = ["TRIAGE"]

TRIAGE = AgentSpec(
    id="triage",
    title="Ticket triage",
    description=(
        "Looks into a newly opened ticket on its own machine with read-only "
        "tools and records what it found before a person sees it."
    ),
    prompt=ticket_assistant._TRIAGE_SYSTEM_PROMPT,
    trigger=Trigger(kind="event", event="ticket_created"),
    tools=ticket_assistant.TRIAGE_TOOLS,
    verdict_tool=toolloop.TRIAGE_VERDICT_TOOL,
    budget=Budget(max_iterations=DEFAULT_MAX_ITERATIONS),
    default_mode="shadow",
)

"""The dashboard copilot's two ticket tools, and the evidence a draft leaves.

A conversation in the Ask kenny drawer routinely establishes what a ticket
would have to say: the operator runs a few read-only checks against a host and
arrives at a conclusion. Without this module that conclusion is retyped by hand
in the inbox, or lost.

**The copilot proposes; it does not run the lifecycle.** Both tools here are
``READ_ONLY`` and neither writes a ticket. :meth:`CopilotTickets.draft` hands
the operator a filled-in form, which they correct and submit through the
ordinary ``POST /api/tickets`` — so there stays exactly one creation path, with
its own authorization, and the wording a ticket is filed under is the human's.
Moving a ticket afterwards (start, block, resolve, reassign) belongs to the
ticket's own chat surface and the ticket routes, which is where the rules for
who may move what already live; a second set of verbs here would be a second
answer to the same question (ADR-0050).

:func:`evidence_from_session` is the other half, and deliberately not the
model's to write: given the chat session a draft came out of, it derives which
read-only calls actually ran in it. The route writes that as one trail row, so
the ticket's record says what happened rather than what somebody typed.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .registry import AgentRegistry
from .store import TelemetryStore
from .tool_classes import READ_ONLY, classify
from .toolloop import (
    SURFACE_ONLY_TOOLS,
    TICKET_DRAFT_TOOL,
    TICKET_FIND_TOOL,
    ToolExecutor,
)
from .ticketstore import TicketStore
from .tunnel import ToolError

logger = logging.getLogger("kenny.copilot.tickets")

#: Ceiling on a drafted title. The same 80 the dashboard's own new-ticket form
#: applies (``kenny-web/src/views/inbox/NewTicketModal.tsx``) — a draft that
#: would not fit that field is a draft the operator cannot submit unedited.
MAX_TITLE_CHARS = 80

#: Ceiling on a drafted summary. Generous, because this is a ticket's
#: description rather than a one-line record, but bounded: the draft is the
#: conversation's conclusion, never the conversation.
MAX_SUMMARY_CHARS = 4_000

#: What ``ticket_find`` counts as open. Matches the inbox's own reading
#: (``webui/__init__.py``'s ``_OPEN_TICKET_STATES``): a resolved ticket is
#: finished work, and pointing at one instead of filing a new case would be
#: wrong.
OPEN_STATES: tuple[str, ...] = ("new", "in_progress")

#: Most tickets ``ticket_find`` reports. Enough to tell "there is already one
#: for this" from "there is not"; it is not a queue view.
_FIND_LIMIT = 10

#: Read-only calls that looked at no machine, and so are not a check anybody
#: did. Bookkeeping, all of it: the surface-only tools are how kenny speaks to a
#: ticket, ``select_agent`` moves a pointer, and this module's own two are the
#: conversation talking about a ticket rather than about a PC. Listing
#: `ticket_draft` as something the conversation "already checked" would be the
#: draft citing itself as evidence.
_NOT_A_CHECK: frozenset[str] = SURFACE_ONLY_TOOLS | {
    TICKET_DRAFT_TOOL,
    TICKET_FIND_TOOL,
    "select_agent",
}

#: Most distinct calls one evidence row names. A conversation that ran more
#: than this was not investigating one thing.
_MAX_EVIDENCE_CALLS = 12

#: Resolves a chat session id to the read-only calls that ran in it. Injected
#: rather than imported so ``webui/tickets.py`` never takes a dependency on the
#: copilot's session registry.
EvidenceReader = Callable[[str], Awaitable[list[dict[str, str]]]]


def evidence_from_session(session: Any) -> list[dict[str, str]]:
    """The read-only calls that ran in ``session``, oldest first, deduped.

    Reads the raw Anthropic transcript rather than any bookkeeping of our own:
    a ``tool_use`` block counts only when the matching ``tool_result`` came back
    without an error, so a call that failed is not offered as something that was
    checked. Change-tier calls are left out on purpose — this row answers "what
    had already been looked at", and what was *changed* in a chat session is the
    audit log's question, not this ticket's. So is everything in
    :data:`_NOT_A_CHECK`, which is read-only without having looked at anything.
    """

    results: dict[str, bool] = {}
    for message in getattr(session, "messages", []) or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                results[str(block.get("tool_use_id"))] = not bool(block.get("is_error"))

    seen: set[tuple[str, str]] = set()
    calls: list[dict[str, str]] = []
    for message in getattr(session, "messages", []) or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool = str(block.get("name") or "")
            if tool in _NOT_A_CHECK or classify(tool) != READ_ONLY:
                continue
            if not results.get(str(block.get("id"))):
                continue
            args = block.get("input") or {}
            host = ""
            if isinstance(args, dict):
                host = str(args.get("agent_id") or args.get("id") or "")
            key = (tool, host)
            if key in seen:
                continue
            seen.add(key)
            calls.append({"tool": tool, "agent_id": host})
            if len(calls) >= _MAX_EVIDENCE_CALLS:
                return calls
    return calls


def _clean(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    return str(value).strip() if value is not None else ""


class CopilotTickets:
    """``ticket_draft`` and ``ticket_find``, registered on the copilot's executor.

    Constructed beside the ticket assistant and handed to the chat routes, which
    call :meth:`register_tools`. Absent (no ticket store configured) the two
    tools are simply never registered, the way ``tools.py`` treats an
    unconfigured service.
    """

    def __init__(
        self,
        *,
        tickets: TicketStore,
        registry: AgentRegistry,
        store: TelemetryStore,
    ) -> None:
        self._tickets = tickets
        self._registry = registry
        self._store = store

    def register_tools(self, executor: ToolExecutor) -> None:
        executor.register_server_tool(TICKET_DRAFT_TOOL, self.draft)
        executor.register_server_tool(TICKET_FIND_TOOL, self.find)

    async def _known_hosts(self) -> set[str]:
        ids = {a.agent_id for a in self._registry.list()}
        ids.update(await self._store.known_agents())
        return ids

    async def draft(self, args: dict[str, Any], *, session: Any = None) -> dict[str, Any]:
        """Handle ``ticket_draft``: validate a proposal and hand it back.

        Writes nothing. The return value is what the dashboard renders as an
        editable form, and it says so in as many words, because a model that
        believed this had filed a ticket would report one that does not exist.
        """

        title = _clean(args, "title")
        summary = _clean(args, "summary")
        agent_id = _clean(args, "agent_id")
        if not title:
            raise ToolError("bad_args", "a draft needs a title")
        if len(title) > MAX_TITLE_CHARS:
            raise ToolError("bad_args", f"title is longer than {MAX_TITLE_CHARS} characters")
        if not summary:
            raise ToolError("bad_args", "a draft needs a summary")
        if len(summary) > MAX_SUMMARY_CHARS:
            raise ToolError("bad_args", f"summary is longer than {MAX_SUMMARY_CHARS} characters")
        if agent_id and agent_id not in await self._known_hosts():
            raise ToolError("unknown_agent", f"no such agent: {agent_id}")
        if not agent_id:
            # The conversation's own host, when it has one. A ticket is about a
            # machine far more often than not, and the model leaving the field
            # out is not a claim that this one is the exception.
            agent_id = str(getattr(session, "agent_id", "") or "")
        return {
            "drafted": True,
            "created": False,
            "title": title,
            "summary": summary,
            "agent_id": agent_id,
            "note": (
                "The draft is now in front of the operator as an editable form. "
                "No ticket exists yet and you cannot create one -- only they can, "
                "by submitting that form. Do not repeat the draft in your reply."
            ),
        }

    async def find(self, args: dict[str, Any], *, session: Any = None) -> dict[str, Any]:
        """Handle ``ticket_find``: open tickets, optionally for one host."""

        agent_id = _clean(args, "agent_id") or None
        tickets = await self._tickets.list(agent_id=agent_id, states=OPEN_STATES, limit=_FIND_LIMIT)
        return {
            "tickets": [
                {
                    "id": t.id,
                    "number": t.number,
                    "title": t.title,
                    "state": t.state,
                    "agent_id": t.agent_id,
                }
                for t in tickets
            ]
        }

"""The note a finished steward run leaves in an Ask kenny conversation.

A person starts a run from a card in the conversation (ADR-0063's pattern: the
copilot proposes, a person acts). When it finishes, the conversation is told so
that the next turn can talk about the outcome. What it is told is built here, by
the server, **from the run's recorded numbers and enumerations only**:

* the steward id and the machine id, each checked against an identifier shape;
* the run's id (server-generated), mode, status and verdict, each an enumeration
  the server defines (``verdict`` only when it is one of
  :data:`~kenny_server.toolloop.STEWARD_VERDICTS`);
* how many actions and recommendations it recorded, and the sum of the
  ``bytes_freed`` the server itself recorded on its actions.

Never the run's ``summary`` or ``error`` -- written by a model that had just read
a monitored machine's output (ADR-0023) -- and never an argument or any other
text the run holds. A note therefore cannot carry words a model or a machine
chose, whatever the run saw. It is the server's *record* of those fields, not a
verification of them: the verdict is the steward model's own call and the bytes
freed are what the machine reported, and the template labels them so. That is
what lets it sit in the transcript beside the operator's own messages
(ADR-0073). Which text in a conversation is such a note is decided by the server
having appended it (``FleetSession.notes``), never by how a message reads.

:func:`build_run_note` and :func:`parse_run_note` are the two halves of one
format; the chat's transcript replay uses the second to show the note as a
system note rather than as something the operator typed.
"""

from __future__ import annotations

import re
from typing import Any

from .toolloop import STEWARD_VERDICTS

__all__ = [
    "NOTE_HEADER",
    "build_run_note",
    "parse_run_note",
]

#: First line of every note; also what the model's prompt tells it to look for.
NOTE_HEADER = "[kenny system note: steward run result]"

_INTRO = (
    "Built by the server from the run's recorded fields. "
    "Not written by the operator or by the copilot. "
    "The verdict is the steward's own call; the bytes freed are as the machine reported."
)

#: The largest count a note reports (JSON's exact-integer range, as for a run's results).
_MAX_COUNT = 2**53

_STEWARD = r"[a-z0-9][a-z0-9_-]{0,63}"
_HOST = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_WORD = r"[a-z][a-z_]{0,31}"

_NOTE_RE = re.compile(
    re.escape(NOTE_HEADER)
    + "\n"
    + re.escape(_INTRO)
    + rf"\nsteward: (?P<steward>{_STEWARD}|unknown)"
    + rf"\nhost: (?P<host>{_HOST}|-|unknown)"
    + r"\nrun: (?P<run>[0-9a-f]{32})"
    + rf"\nmode: (?P<mode>{_WORD})"
    + rf"\nstatus: (?P<status>{_WORD})"
    + rf"(?:\nverdict \(the steward's own call\): (?P<verdict>{_WORD}))?"
    + r"\nactions: (?P<actions>\d{1,16})"
    + r"\nrecommendations: (?P<recommendations>\d{1,16})"
    + r"(?:\nbytes freed \(as the machine reported\): (?P<bytes_freed>\d{1,16}))?",
    re.ASCII,
)


def _ident(value: Any, pattern: str) -> str:
    """``value`` when it is an identifier of the given shape, else ``unknown``."""

    if isinstance(value, str) and re.fullmatch(pattern, value, re.ASCII):
        return value
    return "unknown"


def _count(value: Any) -> int | None:
    """A whole number from ``0`` to :data:`_MAX_COUNT`, else ``None`` (a bool is not one)."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= _MAX_COUNT else None


def _bytes_freed(actions: Any) -> int | None:
    """The sum of ``result.bytes_freed`` over the actions that recorded one, or ``None``."""

    total: int | None = None
    if not isinstance(actions, list):
        return None
    for action in actions:
        result = action.get("result") if isinstance(action, dict) else None
        freed = _count(result.get("bytes_freed")) if isinstance(result, dict) else None
        if freed is not None:
            total = (total or 0) + freed
    return None if total is None else min(total, _MAX_COUNT)


def build_run_note(run: Any) -> str:
    """The note for a finished ``run`` (a ``StewardRun``), as the fixed template.

    Reads only ``steward_id``, ``host_id``, ``id``, ``mode``, ``status``,
    ``verdict``, the *lengths* of ``actions`` and ``recommendations`` and each
    action's recorded ``result.bytes_freed``.
    """

    lines = [
        NOTE_HEADER,
        _INTRO,
        f"steward: {_ident(run.steward_id, _STEWARD)}",
        "host: " + ("-" if run.host_id is None else _ident(run.host_id, _HOST)),
        f"run: {_ident(run.id, '[0-9a-f]{32}')}",
        f"mode: {_ident(run.mode, _WORD)}",
        f"status: {_ident(run.status, _WORD)}",
    ]
    if run.verdict in STEWARD_VERDICTS:
        lines.append(f"verdict (the steward's own call): {run.verdict}")
    lines.append(f"actions: {min(len(run.actions), _MAX_COUNT)}")
    lines.append(f"recommendations: {min(len(run.recommendations), _MAX_COUNT)}")
    freed = _bytes_freed(run.actions)
    if freed is not None:
        lines.append(f"bytes freed (as the machine reported): {freed}")
    return "\n".join(lines)


def parse_run_note(text: Any) -> dict[str, Any] | None:
    """The fields of ``text`` when it is exactly a note :func:`build_run_note` builds, else ``None``.

    A string that merely starts like a note (or that an operator typed to look
    like one) is not one: the whole text must match the template.
    """

    if not isinstance(text, str):
        return None
    match = _NOTE_RE.fullmatch(text)
    if match is None:
        return None
    fields = match.groupdict()
    return {k: v for k, v in fields.items() if v is not None}

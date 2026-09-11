"""What an alert is *about*, as a deduplication key — and its inverse.

Pure and I/O-free, like ``health_rules``, ``findings`` and ``ticket_rules``: the
caller hands in the discriminators a ``notify.Notification`` already carries and
gets a string back.

A key is ``alert|{agent_id}|{space}|{subject}``.

``subject`` is the sections the notification is about, sorted and joined, so the
same set yields the same key whatever order the evaluation visited them in. A
producer that names no section keys on its ``event_type`` instead — that *is*
its subject. Never the free-text title: a title is a display string and would
silently change this identity whenever its wording did.

``space`` separates two kinds of subject that must not merge even when they name
the same section. ``state`` is a condition that can come back to ``ok`` and can
therefore be asked "does this still hold?" — a health verdict, a disk forecast.
``change`` is an inventory diff: a service appeared, a port opened. It never
becomes ``ok`` again, because nothing about it is wrong; it simply happened.
Keying both into one ticket would produce a case whose linked-finding view is
answerable for half its entries and structurally blank for the other half.

``parse`` is the inverse, and is what lets a ticket say which sections it is
about without storing them a second time: the ``dedup_key`` already is that
fact, and a second column would be a second place for it to be wrong.
"""

from __future__ import annotations

from typing import Iterable

__all__ = ["SUBJECT_SPACES", "dedup_key", "parse", "migrate"]

_PREFIX = "alert"
_SEP = "|"
_JOIN = "+"

#: The two subject spaces. ``change`` is named after the event type that is the
#: only member of it, which makes a change key its own migration fixed point.
SUBJECT_SPACES: tuple[str, ...] = ("state", "change")


def _space_for(event_type: str) -> str:
    return "change" if event_type == "change" else "state"


def dedup_key(agent_id: str, event_type: str, sections: Iterable[str]) -> str:
    """Build the key naming what this alert is about."""

    subject = _JOIN.join(sorted(sections)) or event_type
    return _SEP.join((_PREFIX, agent_id or "", _space_for(event_type), subject))


def parse(key: str) -> tuple[str, str, list[str]] | None:
    """Split ``key`` into ``(agent_id, space, subjects)``, or None if it is not
    one of ours.

    None covers the empty key every human-opened ticket carries, a key from
    some other producer, and any key that does not have exactly four parts —
    an ``agent_id`` containing a ``|`` is left alone rather than mis-split into
    a wrong host and a wrong subject.

    ``subjects`` are section names for a key built from sections, and the bare
    ``event_type`` for a producer that names none (``offline``). A caller
    looking them up in a health verdict finds nothing for the latter, which is
    the truthful answer: that subject has no section to still be failing.
    """

    if not key:
        return None
    parts = key.split(_SEP)
    if len(parts) != 4 or parts[0] != _PREFIX:
        return None
    _, agent_id, space, subject = parts
    return agent_id, space, [s for s in subject.split(_JOIN) if s]


def migrate(old: str) -> str:
    """Return ``old`` in the current format, unchanged if it already is.

    Idempotent by content — ``migrate(migrate(k)) == migrate(k)`` — because
    ``TicketStore._migrate`` runs on every boot and must find nothing left to do
    on the second one.

    The ``disk_forecast`` producer names the ``disk`` section, so a key it wrote
    before it did carries the empty subject its ``event_type`` stood in for.
    Mapping that to ``disk`` is what lets an already-open forecast ticket absorb
    the next forecast instead of being stranded beside a ticket for the same
    filling volume.
    """

    parsed = parse(old)
    if parsed is None:
        return old
    agent_id, middle, subjects = parsed
    if middle in SUBJECT_SPACES and subjects:
        return old
    # ``middle`` is an event type: the key predates the space axis.
    if middle == "disk_forecast" and not subjects:
        subjects = ["disk"]
    return dedup_key(agent_id, middle, subjects)

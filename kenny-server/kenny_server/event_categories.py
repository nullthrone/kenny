"""Server-side LLM categorization of reliability events (ADR-0028).

The agent reports raw Windows event groups (``source`` + ``event_id`` + a sample
message). To draw the reliability heatmaps the dashboard needs a **friendly
category** per group — but the space of Windows event sources is large and
open-ended, so instead of a hand-maintained table we ask the connected LLM (the
same Haiku model + API key the AI Recommendation uses, see ``recommend.py``).

Two things keep this cheap and safe, mirroring ``recommend.py``:

* a frozen, cacheable **system prompt** (Anthropic prompt caching), and a bounded
  in-memory **result cache** keyed by ``(source, event_id)`` — the set of distinct
  event types on a real fleet is small and stable, so after warm-up this is a
  no-op;
* the Anthropic client is **injected** (``categorize_events(client, groups)``) so
  tests pass a fake and no real key is required, and every result is validated
  against a fixed category enum (unknown / no key / API error -> ``"Other"``), so
  categorization degrades gracefully and never becomes a hard dependency.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import OrderedDict
from typing import Any

from .recommend import ai_available  # re-exported for callers

__all__ = ["CATEGORIES", "FALLBACK", "ai_available", "categorize_events", "annotate_events"]

CATEGORIZE_MODEL = "claude-haiku-4-5"
_MAX_TOKENS = 512
_CACHE_MAX = 512

# Fixed friendly categories — stable heatmap rows/colours, and the only values the
# model may return (anything else is coerced to FALLBACK). Order is display order.
CATEGORIES: list[str] = [
    "Disk & storage",
    "App crash / hang",
    "Bluescreen / bugcheck",
    "Driver & hardware",
    "Power & boot",
    "Windows service",
    "Windows Update",
    "Network",
    "Security",
    "Other",
]
_CATEGORY_SET = set(CATEGORIES)
FALLBACK = "Other"


_SYSTEM_TEXT = (
    "You are kenny's Windows event-log triage assistant. You are given a list of "
    "Windows Event Log error/critical sources (a provider name, an event id, and a "
    "sample message). Sort EACH one into exactly one of these fixed categories:\n"
    + "\n".join(f"- {c}" for c in CATEGORIES)
    + "\n\nReply with ONLY a JSON array of category strings — one element per input, "
    "in the same order as the inputs, each value copied verbatim from the list "
    "above. No prose, no keys, no markdown. If unsure, use \"Other\"."
)


def _cached_system() -> list[dict[str, Any]]:
    """System prompt as a cacheable block (Anthropic prompt caching)."""

    return [{"type": "text", "text": _SYSTEM_TEXT, "cache_control": {"type": "ephemeral"}}]


# -- result cache ---------------------------------------------------------

_cache: "OrderedDict[tuple[str, int], str]" = OrderedDict()


def _key(source: Any, event_id: Any) -> tuple[str, int]:
    try:
        eid = int(event_id)
    except (TypeError, ValueError):
        eid = -1
    return (str(source or "").strip(), eid)


def _cache_put(key: tuple[str, int], value: str) -> None:
    _cache[key] = value
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


# -- LLM call -------------------------------------------------------------


def _extract_text(resp: Any) -> str:
    """Concatenate the text blocks of an Anthropic ``messages.create`` response."""

    parts = []
    for block in getattr(resp, "content", None) or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(text)
    return "".join(parts)


def _parse_categories(text: str, expected: int) -> list[str] | None:
    """Parse the model's JSON array; validate length + enum. None on any mismatch."""

    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return None
    try:
        arr = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(arr, list) or len(arr) != expected:
        return None
    return [c if c in _CATEGORY_SET else FALLBACK for c in (str(x) for x in arr)]


def _user_message(groups: list[dict[str, Any]]) -> dict[str, Any]:
    lines = []
    for i, g in enumerate(groups, 1):
        sample = str(g.get("sample") or "").replace("\n", " ")[:200]
        lines.append(f"{i}. source={g.get('source')!r} id={g.get('event_id')} sample={sample!r}")
    return {"role": "user", "content": "Inputs:\n" + "\n".join(lines)}


async def _classify(client: Any, groups: list[dict[str, Any]]) -> list[str] | None:
    """One batched Haiku call classifying ``groups``; None on any failure."""

    try:
        resp = await asyncio.to_thread(
            client.messages.create,
            model=CATEGORIZE_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_cached_system(),
            messages=[_user_message(groups)],
        )
    except Exception:  # noqa: BLE001 - best-effort; caller falls back to "Other"
        return None
    return _parse_categories(_extract_text(resp), len(groups))


async def categorize_events(
    client: Any, groups: list[dict[str, Any]]
) -> dict[tuple[str, int], str]:
    """Map each ``(source, event_id)`` in ``groups`` to a friendly category.

    Cached pairs are returned from the cache; the rest are classified in a single
    batched call and cached. With ``client is None`` (no API key) or on any API /
    parse failure the uncached pairs resolve to ``"Other"`` (not cached, so a later
    call can still classify them once a key is present).
    """

    result: dict[tuple[str, int], str] = {}
    todo: list[tuple[tuple[str, int], dict[str, Any]]] = []
    seen: set[tuple[str, int]] = set()
    for g in groups:
        key = _key(g.get("source"), g.get("event_id"))
        if key in _cache:
            _cache.move_to_end(key)
            result[key] = _cache[key]
        elif key not in seen:
            seen.add(key)
            todo.append((key, g))

    if todo and client is not None:
        cats = await _classify(client, [g for _, g in todo])
        if cats is not None:
            for (key, _), cat in zip(todo, cats):
                _cache_put(key, cat)
                result[key] = cat

    for key, _ in todo:
        result.setdefault(key, FALLBACK)
    return result


def annotate_events(events: list[dict[str, Any]], mapping: dict[tuple[str, int], str]) -> None:
    """Stamp ``category`` onto each event group in place from ``mapping``."""

    for e in events:
        if isinstance(e, dict):
            e["category"] = mapping.get(_key(e.get("source"), e.get("event_id")), FALLBACK)

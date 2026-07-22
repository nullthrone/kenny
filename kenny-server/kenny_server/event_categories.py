"""Server-side LLM categorization of reliability events (ADR-0028).

The agent reports raw Windows event groups (``source`` + ``event_id`` + a sample
message). To draw the reliability heatmaps — and to *score* health — the server
needs a **friendly category**, a **severity**, and a short
**suspected cause** per group. The space of Windows event sources is large and
open-ended, so instead of a hand-maintained table we ask the connected LLM (the
same Haiku model + API key the AI Recommendation uses, see ``recommend.py``).

Two things keep this cheap and safe, mirroring ``recommend.py``:

* a frozen, cacheable **system prompt** (Anthropic prompt caching), and a bounded
  in-memory **result cache** keyed by ``(source, event_id)`` — the set of distinct
  event types on a real fleet is small and stable, so after warm-up this is a
  no-op;
* the Anthropic client is **injected** (``categorize_events(client, groups)``) so
  tests pass a fake and no real key is required, and every result is validated
  against fixed enums (unknown / no key / API error -> ``category="Other"``,
  ``severity="unknown"``), so categorization degrades gracefully and never
  becomes a hard dependency — and "unknown" is scored as at least notable
  rather than silently trusted as benign.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import OrderedDict
from typing import Any, Callable

from .recommend import ai_available  # re-exported for callers

__all__ = [
    "CATEGORIES",
    "FALLBACK",
    "SEVERITIES",
    "SEVERITY_FALLBACK",
    "ai_available",
    "categorize_events",
    "annotate_events",
    "annotate_snapshots",
    "default_client",
]

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

# Severity of a *recurring pattern*, independent of how many times it repeats —
# this is what lets the health rule tell "300 benign repeats" apart
# from "300 distinct novel errors". The model may also answer "unknown"
# (:data:`SEVERITY_FALLBACK`) when genuinely unsure; unlike category, an unknown
# severity is deliberately treated as at least notable by the health rule, never
# as a silent pass.
SEVERITIES: list[str] = ["benign", "notable", "serious"]
_SEVERITY_SET = set(SEVERITIES)
SEVERITY_FALLBACK = "unknown"


_SYSTEM_TEXT = (
    "You are kenny's Windows event-log triage assistant. You are given a list of "
    "Windows Event Log error/critical sources (a provider name, an event id, and a "
    "sample message). For EACH one, decide:\n\n"
    "1. \"category\" — exactly one of these fixed categories:\n"
    + "\n".join(f"   - {c}" for c in CATEGORIES)
    + "\n\n2. \"severity\" — how much a family-PC operator should care about this "
    "*specific, recurring* pattern, regardless of how often it repeats:\n"
    "   - \"benign\": a known, cosmetic, or pure-nuisance pattern (e.g. a stale "
    "DCOM permission timeout between two installed apps, a harmless driver "
    "warning) — repeating hundreds of times changes nothing.\n"
    "   - \"notable\": worth a look but not urgent (e.g. an occasional app crash, "
    "a transient network blip).\n"
    "   - \"serious\": real risk of data loss, instability, or hardware failure "
    "(e.g. a disk I/O error, a kernel bugcheck, a security-relevant failure).\n"
    "   - \"unknown\": you genuinely cannot tell from the source/message. Prefer "
    "this over guessing — never call something \"benign\" without real evidence.\n\n"
    "3. \"cause\" — a short (<=12 words) plain-language guess at what's actually "
    "happening, e.g. \"two apps colliding over a stale COM registration\".\n\n"
    "Reply with ONLY a JSON array, one object per input in the same order, each "
    "shaped exactly as {\"category\": ..., \"severity\": ..., \"cause\": ...}. No "
    "prose, no markdown, no extra keys."
)


def _cached_system() -> list[dict[str, Any]]:
    """System prompt as a cacheable block (Anthropic prompt caching)."""

    return [{"type": "text", "text": _SYSTEM_TEXT, "cache_control": {"type": "ephemeral"}}]


# -- result cache ---------------------------------------------------------

# Each cached value is a small ``{"category", "severity", "cause"}`` dict — see
# _parse_classifications / _default_classification for the shape.
Classification = dict[str, str]

_cache: "OrderedDict[tuple[str, int], Classification]" = OrderedDict()


def _key(source: Any, event_id: Any) -> tuple[str, int]:
    try:
        eid = int(event_id)
    except (TypeError, ValueError):
        eid = -1
    return (str(source or "").strip(), eid)


def _default_classification() -> Classification:
    return {"category": FALLBACK, "severity": SEVERITY_FALLBACK, "cause": ""}


def _cache_put(key: tuple[str, int], value: Classification) -> None:
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


def _parse_classifications(text: str, expected: int) -> list[Classification] | None:
    """Parse the model's JSON array of ``{category, severity, cause}`` objects.

    Validates array shape (a JSON array of the expected length) and coerces
    every field against its fixed enum — an unrecognized or missing category
    becomes :data:`FALLBACK`, an unrecognized or missing severity becomes
    :data:`SEVERITY_FALLBACK`. Only a completely unparseable / wrong-length
    response returns ``None`` (the caller then falls back to defaults for the
    whole batch); a malformed individual element degrades to safe defaults
    rather than discarding the batch.
    """

    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return None
    try:
        arr = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(arr, list) or len(arr) != expected:
        return None
    out: list[Classification] = []
    for item in arr:
        cat = item.get("category") if isinstance(item, dict) else None
        sev = item.get("severity") if isinstance(item, dict) else None
        cause = item.get("cause") if isinstance(item, dict) else None
        out.append(
            {
                "category": cat if cat in _CATEGORY_SET else FALLBACK,
                "severity": sev if sev in _SEVERITY_SET else SEVERITY_FALLBACK,
                "cause": cause.strip()[:160] if isinstance(cause, str) else "",
            }
        )
    return out


def _user_message(groups: list[dict[str, Any]]) -> dict[str, Any]:
    lines = []
    for i, g in enumerate(groups, 1):
        sample = str(g.get("sample") or "").replace("\n", " ")[:200]
        lines.append(f"{i}. source={g.get('source')!r} id={g.get('event_id')} sample={sample!r}")
    return {"role": "user", "content": "Inputs:\n" + "\n".join(lines)}


async def _classify(client: Any, groups: list[dict[str, Any]]) -> list[Classification] | None:
    """One batched Haiku call classifying ``groups``; None on any failure."""

    try:
        resp = await asyncio.to_thread(
            client.messages.create,
            model=CATEGORIZE_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_cached_system(),
            messages=[_user_message(groups)],
        )
    except Exception:  # noqa: BLE001 - best-effort; caller falls back to defaults
        return None
    return _parse_classifications(_extract_text(resp), len(groups))


async def categorize_events(
    client: Any, groups: list[dict[str, Any]]
) -> dict[tuple[str, int], Classification]:
    """Map each ``(source, event_id)`` in ``groups`` to a classification.

    Cached pairs are returned from the cache; the rest are classified in a single
    batched call and cached. With ``client is None`` (no API key) or on any API /
    parse failure the uncached pairs resolve to the safe default
    (:func:`_default_classification`: ``category="Other"``, ``severity="unknown"``)
    — not cached, so a later call can still classify them once a key is present.
    """

    result: dict[tuple[str, int], Classification] = {}
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
        classifications = await _classify(client, [g for _, g in todo])
        if classifications is not None:
            for (key, _), classification in zip(todo, classifications):
                _cache_put(key, classification)
                result[key] = classification

    for key, _ in todo:
        result.setdefault(key, _default_classification())
    return result


def annotate_events(
    events: list[dict[str, Any]], mapping: dict[tuple[str, int], Classification]
) -> None:
    """Stamp ``category``, ``severity``, and ``suspected_cause`` onto each event
    group in place from ``mapping`` (falling back to safe defaults for any group
    not present in ``mapping``)."""

    for e in events:
        if isinstance(e, dict):
            info = mapping.get(_key(e.get("source"), e.get("event_id"))) or _default_classification()
            e["category"] = info["category"]
            e["severity"] = info["severity"]
            e["suspected_cause"] = info["cause"]


def default_client() -> Any:
    """Construct the real Anthropic client (lazy import; needs ``ANTHROPIC_API_KEY``).

    A tiny, dependency-free default so callers outside the dashboard (e.g. the
    ``agent_health`` MCP tool) don't need their own Anthropic wiring.
    """

    import anthropic

    return anthropic.Anthropic()


async def annotate_snapshots(
    snapshots: list[dict[str, Any] | None],
    *,
    client_factory: Callable[[], Any] | None = None,
) -> None:
    """Stamp category/severity/suspected_cause onto every reliability event
    across ``snapshots`` (mutating the in-memory dicts in place).

    One batched LLM call for the whole set, cached and deduped by
    :func:`categorize_events`; a no-op when there are no events, and — with no
    API key — every event resolves to the safe defaults (ADR-0028).
    ``client_factory`` defaults to :func:`default_client`.
    """

    groups: list[dict[str, Any]] = []
    for snap in snapshots:
        rel = snap.get("reliability") if isinstance(snap, dict) else None
        if isinstance(rel, dict):
            groups.extend(e for e in (rel.get("events") or []) if isinstance(e, dict))
    if not groups:
        return
    factory = client_factory or default_client
    client = factory() if ai_available() else None
    mapping = await categorize_events(client, groups)
    for snap in snapshots:
        rel = snap.get("reliability") if isinstance(snap, dict) else None
        if isinstance(rel, dict) and isinstance(rel.get("events"), list):
            annotate_events(rel["events"], mapping)

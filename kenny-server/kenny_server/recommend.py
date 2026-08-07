"""Server-side "AI Recommendation" for flagged telemetry sections.

When the operator opens a section detail popup for a card in ``warn``/``crit``,
the dashboard asks Haiku for a short, fixed-shape recommendation
(``Diagnosis`` / ``Action`` / ``Urgency``) plus a machine-readable remediation
directive. The directive drives the dashboard's "Auto-Remediate" button, which
injects a prompt into the server-hosted chat (the copilot).

Two layers keep this cheap and consistent:

* a frozen, cacheable **system prompt** (Anthropic prompt caching), identical
  for every warning, so the output shape never drifts — the same mechanism the
  chat uses (see ``chat.py:_cached_system``);
* an in-memory **result cache** keyed by ``(section, status, reason||summary)``
  — the recommendation for "disk 91% full" is the same on every machine —
  replayed as streamed deltas so a cache hit looks identical to a fresh call.

The client is injected (``recommend_events(client, facts)``) so tests pass a
fake Anthropic client and no real API key is required.

**The remediation directive never widens what the copilot may do.** ``REMEDIATE:
yes`` is emitted only when the fix maps onto a catalogued capability tool, and
pressing the button injects a prompt and starts a turn — every state-changing tool
in that turn still stops at the operator confirm-gate (ADR-0009).

The directive rides in a trailing ``--- REMEDIATE/PROMPT`` block rather than a
structured-output call because the prose above it streams token by token and JSON
mode does not. The server strips that block from the visible stream, so
:data:`_SENTINEL` and the system prompt's closing lines must stay in lockstep:
change one and the directive either leaks into the operator's view or stops being
parsed.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

from . import health_rules
from .chat import CAPABILITY_TOOLS, STATE_CHANGING_TOOLS

# Haiku: fast and cheap, sufficient for a 3-line templated recommendation.
RECOMMEND_MODEL = "claude-haiku-4-5"
_MAX_TOKENS = 400
_SENTINEL = "---"
# Hold back this many trailing chars while streaming so a partial sentinel
# ("\n---") is never flushed to the operator as visible prose.
_HOLDBACK = 6
_CACHE_MAX = 256
# A tiny per-chunk delay on cache replay so a cached recommendation still
# *feels* streamed (the bytes flush incrementally rather than in one packet).
_REPLAY_DELAY = 0.012


def ai_available() -> bool:
    """True if an Anthropic API key is configured (recommendations enabled)."""

    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _tool_catalog() -> str:
    """Compact catalog of capability tools Haiku may rely on for remediation."""

    lines = []
    for name in CAPABILITY_TOOLS:
        gated = " (state-changing — needs operator confirmation)" if name in STATE_CHANGING_TOOLS else ""
        lines.append(f"- {name}{gated}")
    return "\n".join(lines)


# Frozen template: identical for every warning, so the shape never drifts and
# the block caches (prompt caching). English output, exactly 3 visible parts.
_SYSTEM_TEXT = (
    "You are kenny's maintenance advisor. A Windows PC in a small family fleet "
    "has a flagged health section. Give the operator a short, concrete "
    "recommendation — what the warning means and what to do about it.\n\n"
    "Reply in English, in EXACTLY this shape and nothing else:\n"
    "Diagnosis: <one sentence: what the warning means in plain language>\n"
    "Action: <1-3 concrete steps the operator should take>\n"
    "Urgency: <now | soon | can wait> - <short why>\n"
    "---\n"
    "REMEDIATE: <yes|no>\n"
    "PROMPT: <one line, or empty>\n\n"
    "Set REMEDIATE to yes only if the fix can plausibly be carried out with "
    "kenny's capability tools listed below. When yes, PROMPT is a single-line "
    "instruction for the kenny copilot describing what to do on the selected PC "
    "(it runs the tools, asking the operator to confirm state-changing ones). "
    "When no, leave PROMPT empty. Keep every line tight; no markdown, no extra "
    "lines.\n\n"
    "kenny capability tools:\n" + _tool_catalog()
)


def _cached_system() -> list[dict[str, Any]]:
    """System prompt as a cacheable block (Anthropic prompt caching)."""

    return [{"type": "text", "text": _SYSTEM_TEXT, "cache_control": {"type": "ephemeral"}}]


def warning_facts(snapshot: dict[str, Any] | None, section: str) -> dict[str, Any] | None:
    """Evaluated ``{section,status,summary,reason}`` for one section.

    Returns ``None`` when the section is absent, has no data, or is not flagged
    (``ok``) — the recommendation block is only ever shown for warn/crit.
    """

    if not snapshot or section not in snapshot:
        return None
    payload = snapshot.get(section)
    if not isinstance(payload, dict):
        return None
    health = health_rules.evaluate_section(section, dict(payload))
    status = health.get("status", "ok")
    if status not in ("warn", "crit"):
        return None
    return {
        "section": section,
        "status": status,
        "summary": health.get("summary", ""),
        "reason": health.get("reason", ""),
    }


def _user_message(facts: dict[str, Any]) -> dict[str, Any]:
    body = (
        f"section: {facts['section']}\n"
        f"status: {facts['status']}\n"
        f"summary: {facts['summary']}\n"
        f"reason: {facts['reason']}"
    )
    return {"role": "user", "content": body}


# -- result cache ---------------------------------------------------------

_cache: "OrderedDict[tuple[str, str, str], dict[str, Any]]" = OrderedDict()


def _cache_key(facts: dict[str, Any]) -> tuple[str, str, str]:
    # Generic per warning type: the recommendation for a given (section, status,
    # reason) is identical across machines. Fall back to summary when a section
    # has no rule-derived reason.
    return (facts["section"], facts["status"], facts.get("reason") or facts.get("summary") or "")


def _cache_put(key: tuple[str, str, str], value: dict[str, Any]) -> None:
    _cache[key] = value
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


# -- prose / sentinel handling -------------------------------------------


def _word_chunks(text: str) -> list[str]:
    """Split text into word-ish chunks so replay produces multiple deltas."""

    return re.findall(r"\S+\s*", text) or ([text] if text else [])


def _visible_split(full: str) -> tuple[str, bool]:
    """Return ``(visible_prose, sentinel_seen)``.

    ``visible_prose`` is everything before the ``\\n---`` sentinel line; once the
    sentinel is seen the visible region is frozen and the remainder is parsed for
    the remediation directive rather than shown.
    """

    idx = full.find("\n" + _SENTINEL)
    if idx != -1:
        return full[:idx], True
    if full.startswith(_SENTINEL):
        return "", True
    return full, False


def _parse_remediation(full: str, seen: bool) -> dict[str, Any]:
    """Parse ``REMEDIATE``/``PROMPT`` from the sentinel block."""

    available = False
    prompt = ""
    if seen:
        idx = full.find("\n" + _SENTINEL)
        tail = full[idx:] if idx != -1 else full
        for line in tail.splitlines():
            stripped = line.strip()
            low = stripped.lower()
            if low.startswith("remediate:"):
                available = stripped.split(":", 1)[1].strip().lower().startswith("y")
            elif low.startswith("prompt:"):
                prompt = stripped.split(":", 1)[1].strip()
    if not available:
        prompt = ""
    return {"available": available, "prompt": prompt}


async def _replay(cached: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    for chunk in _word_chunks(cached["prose"]):
        yield {"type": "text_delta", "text": chunk}
        if _REPLAY_DELAY:
            await asyncio.sleep(_REPLAY_DELAY)
    yield {"type": "remediation", **cached["remediation"]}
    yield {"type": "done"}


async def recommend_events(client: Any, facts: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """Stream a recommendation for ``facts`` as SSE-ready event dicts.

    Yields ``text_delta`` events for the visible prose (sentinel stripped), then
    a single ``remediation`` event, then ``done``. On a cache hit the stored
    prose is replayed so the UX is identical to a fresh generation.
    """

    key = _cache_key(facts)
    cached = _cache.get(key)
    if cached is not None:
        _cache.move_to_end(key)
        async for ev in _replay(cached):
            yield ev
        return

    full = ""
    emitted = 0
    try:
        with client.messages.stream(
            model=RECOMMEND_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_cached_system(),
            messages=[_user_message(facts)],
        ) as stream:
            for chunk in stream.text_stream:
                full += chunk
                visible, hit = _visible_split(full)
                if hit:
                    if len(visible) > emitted:
                        yield {"type": "text_delta", "text": visible[emitted:]}
                        emitted = len(visible)
                    continue  # keep accumulating for the parse, stop emitting
                safe = max(0, len(visible) - _HOLDBACK)
                if safe > emitted:
                    yield {"type": "text_delta", "text": visible[emitted:safe]}
                    emitted = safe
    except Exception as exc:  # noqa: BLE001 - surface in-band like the chat stream
        yield {"type": "error", "error": str(exc)}
        return

    visible, seen = _visible_split(full)
    if len(visible) > emitted:  # flush the held-back tail when no sentinel ran
        yield {"type": "text_delta", "text": visible[emitted:]}
    remediation = _parse_remediation(full, seen)
    _cache_put(key, {"prose": visible.strip(), "remediation": remediation})
    yield {"type": "remediation", **remediation}
    yield {"type": "done"}

# 0019. AI recommendations and tool-aware auto-remediation

- Status: accepted
- Date: 2026-06-06

## Context and Problem Statement

When an operator opens a flagged telemetry section (warn/crit) in the dashboard,
they see the metrics, the rule `summary`, and the rule `reason` — but no guidance
on what to *do*. We want a short, consistent recommendation per warning, and,
where the fix maps to kenny's existing capability tools, a one-click way to hand
that fix to the operator's copilot.

This introduces a **second LLM surface** beyond the server-hosted chat
(ADR-0009): a small, templated Haiku call dedicated to per-warning advice. The
questions: how do we keep the output shape stable and cheap, how does an
"auto-remediate" action flow into the chat without weakening the confirm-gate,
and how does an operator interrupt a running turn.

## Considered Options

- **Free-form chat prompt per warning** — reuse the chat with an ad-hoc prompt.
  Cheap to build but the output drifts per warning and re-thinks every time.
- **Structured output (JSON tool/`output_config.format`)** — guarantees fields
  but streams poorly (the prose is the thing we want to stream token-by-token).
- **Frozen, cached system-prompt template + sentinel directive** (chosen) — one
  immutable system prompt fixes the 3-part shape (`Diagnosis`/`Action`/
  `Urgency`); a trailing `--- REMEDIATE/PROMPT` sentinel carries the machine
  decision, which the server strips from the visible stream and emits as its own
  event. Results are cached per warning *type* and replayed as streamed deltas.

## Decision Outcome

Chosen: the frozen-template + sentinel approach, on `claude-haiku-4-5`.

- **Shape stability + cost.** The system prompt is identical for every warning,
  so Anthropic prompt caching applies and the output never drifts. An in-memory
  result cache keyed on `(section, status, reason||summary)` makes the
  recommendation for e.g. "disk 91% full" shared across machines; cache hits are
  replayed as `text_delta`s so the UX is identical to a fresh generation.
- **Auto-remediation respects the confirm-gate.** `REMEDIATE: yes` only when the
  fix maps to a catalogued capability tool. The button injects the suggested
  prompt into the server-hosted chat (scoped to that agent) and starts the turn;
  state-changing steps still pause at the confirm-gate (ADR-0008/0009). The
  injected prompt is shown as the user bubble, so the action is transparent.
- **Stop control.** Because a turn can now start from a button press, the copilot
  gains a Stop button: the browser aborts the SSE fetch, the server cancels the
  streaming generator, and the next turn *heals* the session (drops a trailing
  unanswered `tool_use` assistant turn) so history stays valid for the API.
- **Availability.** The block (and route) are offered only when an Anthropic API
  key is configured; `/api/agent/{id}` reports `ai_enabled` for the UI.

### Consequences

- Good: consistent, cheap, streamed guidance; a safe one-click remediation path
  that never bypasses operator confirmation; an interruptible copilot.
- Good: no wire-contract or `PROTOCOL_VERSION` change — server + UI only; the
  Rust agent is untouched.
- Bad: a second model/integration to maintain, and a sentinel-parsing step that
  must stay in sync with the template.
- Bad: the result cache is process-memory only (lost on restart; not shared
  across replicas) — acceptable for a single-instance, self-hosted dashboard.

## More Information

- Builds on ADR-0008 (operator auth), ADR-0009 (server-hosted Claude chat),
  ADR-0016 (Anthropic-native tool naming).
- Server: `kenny_server/recommend.py`, route `POST /api/recommendation/stream`
  and `heal_session` in `kenny_server/chat.py`. UI: the section-detail popup and
  copilot composer in `kenny_server/webui/index.html`.

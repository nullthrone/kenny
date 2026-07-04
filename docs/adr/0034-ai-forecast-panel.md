# 0034. AI Forecast panel for the agent drill-down

- Status: accepted
- Date: 2026-07-04

## Context and Problem Statement

ADR-0030 gave the agent drill-down a **"changes & forecast · 24 h"** panel: it
stacked every disk/battery trend row and a full inventory-diff table (added /
removed / changed autostart entries, services, software, accounts, …). In
practice that panel is low-signal. It is overloaded — a table that can grow
**without bound** on a busy PC — and it never answers the question the operator
actually opens the drill-down with: *is this machine heading somewhere I need to
care about?* Raw deltas are data, not a forecast.

kenny already computes the right signals server-side (ADR-0030's `trends.py`
disk-full and battery-drift forecasts, `diffs.py` inventory diff) and already has
the AI read-path plumbing to synthesize them: a server-hosted Anthropic client,
prompt caching, SSE streaming, an injected client for tests, and graceful
degradation without a key — the pattern of the **AI Recommendation** (ADR-0019)
and **LLM event categorization** (ADR-0028). The question: **should the
drill-down's top-of-page summary be a synthesized forecast rather than a raw
table, and where does that synthesis run?**

## Considered Options

- **Keep the raw changes/forecast table, just cap its length.** Bounds the worst
  case but leaves it a data dump — still no synthesized takeaway, and a truncated
  table hides exactly the tail an operator might care about. Rejected.
- **Client-side heuristic summary in the dashboard JS.** No API dependency, but
  duplicates judgement in the browser, can't phrase a genuinely readable outlook,
  and drifts from the server's own forecast/alert logic. Rejected as the primary
  mechanism (kept, in spirit, only as the no-key fallback).
- **Server-side AI forecast on the read path, streamed, cached, with a
  deterministic fallback (chosen).** Mirrors `recommend.py`.

## Decision Outcome

Replace the drill-down's "changes & forecast" panel with an **AI Forecast** card
pinned at the **top of the drill-down, directly above the health-trend
sparkline**. A new pure module `forecast.py` (patterned after `recommend.py`)
owns it:

- `build_facts(snapshot, disk, battery, changes)` assembles a **compact, bounded**
  fact set from already-computed `trends.disk_forecast` / `trends.battery_trend`
  output, the `diffs.diff_snapshots` list (**capped**, with a `truncated` count,
  so neither the prompt nor the summary can grow without bound — the old panel's
  failing), and the current health roll-up (`tools.build_health`).
  `local_accounts` changes are counted as high-priority, matching the alert loop.
- A **frozen, cacheable system prompt** (Anthropic prompt caching) asks Haiku —
  the cheap model the AI Recommendation already uses — for **2–4 sentences of
  plain prose**, most-urgent-first, no markdown/tables, treating the fact block as
  untrusted data (ADR-0024). `forecast_events(client, facts)` streams `text_delta`
  events and caches the prose by a digest of the facts, replaying identically on a
  reopen of the same snapshot. The client is **injected** so tests use a fake and
  no real key is needed.
- **Read-path, not ingest-path.** The endpoint `POST /api/forecast/stream` reads
  `store.latest` + `store.daily_latest` at request time (the same windows as the
  `/trends` and `/changes` endpoints), never mutating stored snapshots.
- **Graceful degradation (ADR-0028 posture).** The endpoint **always streams
  200** — unlike the recommendation route's 503. With no `ANTHROPIC_API_KEY` it
  streams `deterministic_summary(facts)`: a bounded **prose** summary (not a data
  dump) of the same signals, so the card is always useful, never empty, and never
  unbounded. The dashboard marks the AI-generated variant with the ✦ sparkle and
  an **"AI Forecast"** heading; the deterministic variant reads **"Forecast"**.

This **supersedes only the *panel surfacing*** of ADR-0030. Its diff/trend engine
(`diffs.py`, `trends.py`), the `/api/agent/{id}/changes` and `/trends` endpoints,
the "Disks filling <30 d" Overview KPI, and the alert-loop forecast/diff
notifications all stand unchanged — this ADR changes only how those signals are
presented in the drill-down.

### Consequences

- **Good.** The drill-down opens with a synthesized, human-readable outlook
  instead of a table to scan; the unbounded-length problem is gone by
  construction (facts and fallback are both capped). Cost is negligible (a few
  sentences of Haiku, cached per snapshot). Testable without a key.
- **Good.** Reuses existing plumbing — no new model, no new trust boundary, no
  wire-contract change; the diff/trend engine keeps its other consumers.
- **Bad / accepted.** The prose is non-deterministic across model versions
  (bounded by the short shape + cache) and a cold reopen of a changed snapshot
  incurs one Haiku call's latency. Fact data — mount names, software/service/
  account change keys — is sent to the API; it is no more than the reliability
  samples ADR-0028 already sends and is skipped entirely with no key. Folding the
  raw change rows into a bounded highlight means the full per-row diff is no
  longer shown in the drill-down; it remains available via the `/changes` endpoint
  and the change notifications (ADR-0029/0030).

## More Information

- Related: ADR-0019 (AI Recommendation — the pattern), ADR-0028 (LLM read-path
  feature, graceful degradation), ADR-0030 (the diff/trend engine and the panel
  this supersedes), ADR-0024 (untrusted agent data in model context),
  ADR-0029 (the alerting these forecasts also feed).
- Code: `kenny-server/kenny_server/forecast.py`,
  `kenny-server/kenny_server/webui/__init__.py` (`api_forecast_stream`),
  `kenny-server/kenny_server/webui/index.html` (`mountForecast`, the top card),
  `kenny-server/tests/test_forecast.py`.

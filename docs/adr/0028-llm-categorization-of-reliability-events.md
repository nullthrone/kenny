# 0028. LLM categorization of reliability events

- Status: accepted
- Date: 2026-07-02

## Context and Problem Statement

The `reliability` telemetry section reported a single number — "192
error/critical events in 7d". For an operator that is useless: it says nothing
about *what* is actually failing on the PC. The redesign has the agent report a
**breakdown** of the Error/Critical Windows event log entries (grouped by source
+ event id, each with a sample message and per-day counts), and the dashboard
shows two heatmaps — hosts × problem-category across the fleet, and category ×
day for one PC.

Those heatmaps need a **friendly category** per raw event group ("Application
Error / 1000" → *App crash / hang*, "disk / 51" → *Disk & storage*). The space of
Windows event sources is large, messy, and open-ended; a hand-maintained mapping
table goes stale and never covers the long tail. kenny already has an Anthropic
API key wired up for the AI Recommendation feature (`recommend.py`). The question:
**where does the raw-source → friendly-category mapping come from, and where does
it run?**

## Considered Options

- **Hand-maintained static mapping table (server-side dict of provider/id →
  category).** Deterministic, no API dependency, testable. But it must be curated
  forever, and unknown sources — the exact long tail an operator cares about —
  fall through to "Other". Rejected as the primary mechanism.
- **Categorize in the agent (Rust).** Keeps the server dumb, but bakes a
  classification policy into a binary that ships to every PC and can't be updated
  without a redeploy — the opposite of ADR-0007's "judgement lives server-side so
  it can change without touching agents". Rejected.
- **LLM categorization, server-side, at read time, cached (chosen).** The server
  asks the connected LLM (Haiku, the cheap model `recommend.py` already uses) to
  sort each distinct raw event group into one of a small fixed enum of friendly
  categories, and caches the result by `(source, event_id)`. The set of distinct
  event types on a real fleet is small and stable, so after warm-up the classifier
  is a no-op. Chosen.

## Decision

Categorize reliability event groups with the connected LLM, server-side, on the
telemetry **read path**, with a persistent-for-the-process cache and graceful
degradation. Concretely (mirroring `recommend.py`):

- A **fixed category enum** (~10: *Disk & storage*, *App crash / hang*,
  *Bluescreen / bugcheck*, *Driver & hardware*, *Power & boot*, *Windows service*,
  *Windows Update*, *Network*, *Security*, *Other*). Fixed so heatmap rows/colours
  are stable and the model can't invent categories; results are validated against
  the enum, unknown → *Other*.
- `categorize_events(client, groups)` classifies only the **uncached**
  `(source, event_id)` pairs in **one** batched Haiku call (source + sample as
  context), then caches. The Anthropic client is **injected** so tests pass a fake
  and no real key is needed — the same testability pattern as `recommend_events`.
- **Read-path, not ingest-path.** Classification runs when the dashboard reads
  (`api_agent`, `api_fleet_overview`), stamping a `category` onto each event group
  before aggregation — never mutating stored snapshots. This keeps ingestion
  independent of the AI and `fleet_stats` pure (it just reads `category`).
- The raw category label is a **server-side annotation**, not part of the wire
  contract — exactly like the `web_activity.flagged` annotation (ADR-0026). The
  agent never sends `category`.

## Consequences

- **Good.** The long tail of event sources is categorized without a maintained
  table; categories improve as the model does; the heatmaps read as one system.
  Cost is negligible (few distinct event types, cached). Testable without a key.
- **Graceful degradation.** With no `ANTHROPIC_API_KEY` (or on an API error) every
  group falls back to *Other*; the heatmaps still work and the operator can still
  expand a category to the raw sources / event ids / sample messages. Categorization
  is an enhancement, never a hard dependency.
- **Bad / accepted.** Categories are non-deterministic across model versions
  (bounded by the fixed enum + cache). A first, cold dashboard load on a new event
  type incurs one Haiku call's latency. Event **sample messages** are sent to the
  API; they can contain app names and file paths (never more than the event log
  already holds, and less sensitive than the `web_activity` data ADR-0026 already
  sends) — acceptable for a self-hosted family deployment, and skipped entirely
  when no key is set.

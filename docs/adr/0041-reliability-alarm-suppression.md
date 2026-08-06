# 0041. Operator-managed reliability alarm suppression

- Status: accepted
- Date: 2026-07-29

## Context and Problem Statement

Issue #166: on a monitored Windows 11 host, `reliability` reports `overall=crit` because
one single, well-known-benign event pattern dominates the window —
`Microsoft-Windows-CAPI2 / 4176` ("PFX operation failed as AuthSafes count doesn't lie in
expected range"), emitted continuously by CryptSvc, a documented Windows quirk with no known
fix (Microsoft support forums, 2022–2026, no root cause ever identified). 3439 of 3743
events in 7 days are this one pattern; the one event that actually matters in the same
window — a single Kernel-Power/41 unclean shutdown — is lost in the noise. Over the
dashboard or `agent_health`, "3439 identical harmless lines" is indistinguishable from
"3439 individually relevant errors" without a manual `diag_eventlog` lookup.

We need a way for the operator to exclude a specific `(source, event_id)` pattern from
health *scoring* — without hiding the raw event counts the issue explicitly wants
preserved (the reporter is also concerned about the pattern's volume rotating the Windows
Application log within ~8 days; that concern is addressed separately, see *Consequences*).

A key finding while designing this: `reliability` health is evaluated at ~8 call sites
(the dashboard's two annotated read paths, but also `alerting.py`'s push-notification loop,
`digest.py`'s weekly digest, the fleet list, the trend sparkline, and the copilot chat), and
only the two dashboard paths run the ADR-0026 LLM categorization that carries a `severity`
field. The rest take `health_rules._rule_reliability_by_volume`, where 3743 ≥ the crit
threshold (50) regardless of any severity annotation. A suppression mechanism that only
reached the annotated scoring path would fix the dashboard modal and leave the fleet tile
and the push alerts still screaming `crit`.

## Considered Options

- **Teach the LLM classifier about this pattern (extend the ADR-0026 prompt, or a
  hand-maintained known-benign table).** Rejected: ADR-0026 exists precisely because the
  space of Windows event sources is open-ended and unmaintainable by hand, and whether a
  pattern should be muted is a per-fleet operator decision, not a global classification
  fact — the same message can be a genuine problem on a different operator's fleet.
- **Raise the volume thresholds, or cap how much one pattern can contribute to the total.**
  Rejected: this hides real diversity and cannot distinguish "3439 identical harmless
  lines" from "3439 individually relevant errors", which is the issue's core complaint.
- **Let the operator override the LLM `severity` to `benign`.** Rejected: this conflates
  two different claims ("the model classified this as harmless" vs. "the operator decided
  to ignore this") behind one field, is indistinguishable in the UI from a model verdict,
  and loses the audit trail of who decided and why.
- **A new setting in the ADR-0032 `Settings` catalog.** Rejected: settings are scalar
  key/value knobs; this needs a scoped, growable list of rules with per-row provenance
  (who, when, why), not a single value.
- **Stamp suppression at telemetry-insert time, like `web_activity.flagged` (ADR-0024).**
  Rejected: a rule added or removed by the operator would not take effect until the agent's
  next telemetry push, so the dashboard's suppress/unsuppress toggle would appear broken
  for minutes at a time.
- **Chosen: a new operator-authored suppression-rule table, mirrored in-memory, stamped
  onto reliability event groups at the `TelemetryStore` read-path boundary.**

## Decision Outcome

Chosen option: a new `reliability_suppressions` SQLite table
(`agent_id`, `source`, `event_id`, `note`, `created_at`, `created_by` — empty-string
sentinels, not `NULL`, for "fleet-wide" / "any source", since SQLite treats `NULL`s as
pairwise-distinct in a uniqueness check) plus `ReliabilitySuppressionStore` (mirrors
`PolicyStore`'s connect/list/add/remove idiom). A rule's precedence is most-specific-wins:
`(host, exact source)` › `(host, wildcard)` › `(fleet, exact source)` › `(fleet, wildcard)`.

`reliability_suppression.SuppressionList` is the in-memory mirror (the same
single-process/single-event-loop argument ADR-0032 makes for `Settings`): loaded at
startup, replaced on every write, matched with a synchronous, lock-free dict lookup —
`(source, event_id)` matching needs no LLM and no API key, so unlike the ADR-0026
categorization it is cheap enough to run on **every** health read, not just the two
annotated dashboard paths.

The key mechanism: `TelemetryStore` gained an optional `annotate: Callable[[agent_id,
snapshot], None] | None` hook, called from `_row_to_record`/`daily_latest` — i.e. every
`latest()`, `history()`, and `daily_latest()` call. `main.py` wires
`store.annotate = suppression.mark`. Because `alerting.py`, `digest.py`, the fleet list,
the chat tool-use loop, and every MCP tool all read snapshots through these same
`TelemetryStore` accessors, suppression reaches all of them automatically — no per-call-site
threading of an `agent_id` parameter through `health_rules.evaluate_snapshot`/
`evaluate_section`, and no risk of a missed call site silently staying unsuppressed. This
was chosen over threading an explicit `agent_id` through every health-evaluation call
(rejected: ~8 signature changes, easy to miss one — the exact bug class this ADR fixes) and
over routing `alerting`/`digest` through a single `tools.build_health` choke point
(rejected: a real import cycle — `tools.py` already dodges `tools -> chat -> ... -> tools`
with a deferred import; routing the alert loop through `tools` would drag the chat/Anthropic
path into it).

`mark()` stamps `suppressed: true` and a `suppressed_by` descriptor (rule id, scope, source,
event id, note) onto each matched event group **without touching** `category`/`severity`/
`suspected_cause` — the LLM's verdict and the operator's suppression are different claims
and must stay visually and semantically distinct (never folded into a `benign` severity).
`health_rules.py` stays pure (no store, no I/O): both `_rule_reliability_by_severity` and
the volume fallback `_rule_reliability_by_volume` read the `suppressed` field already
present on the payload and exclude those groups from every scoring list — but a suppressed
Windows-`level: critical` group also skips the automatic "critical -> serious" escalation,
because explicit operator intent overrides the automatic one (otherwise a suppressed
Kernel-Power/41 could never actually be muted). Raw counts (`recent_crashes`, per-event
`count`, the fleet heatmap's cell count) are never altered — only the *scoring* total is
reduced by suppressed volume. The Windows Reliability Index overlay
(`_RELIABILITY_SI_CRIT`/`_WARN`) is deliberately **not** suppressible: it is an independent,
agent-computed signal that a `(source, event_id)` rule carries no information about, and
letting suppression silence it would let one muted pattern hide a genuinely unstable
machine.

The dashboard's reliability section detail gained: a `suppressed` badge per event row
(distinct from the `benign` severity pill), a per-row suppress/unsuppress button (always
fleet-wide with the exact source — the decided default, no scope dialog on a one-click
affordance), and a rule-management panel with a manual-add form (**event id required,
source optional** = wildcard, plus a fleet/host scope selector and a free-text note). New
`/api/reliability/suppressions` routes (GET any role, POST/DELETE operator+) and three MCP
tools (`reliability_suppression_list/add/remove`, the two mutators behind the ADR-0009
confirm-gate) give Claude the same read/write access as the dashboard. `agent_snapshot`
picks up the `suppressed` marker automatically through the same `TelemetryStore` hook, with
no LLM call and no extra latency — closing exactly the gap the issue's repro steps describe
(comparing `agent_snapshot`'s raw breakdown against known-noisy patterns).

No wire-contract change: `suppressed`/`suppressed_by` are server-internal read-path
annotations, exactly like `category`/`severity`/`suspected_cause` (ADR-0026) and
`web_activity.flagged` (ADR-0024) — the agent never sends or sees them, and
`kenny-agent/` needs no changes.

### Consequences

- Good, because a fleet-wide, well-known-benign Windows quirk (or any single dominant
  pattern) can be muted once and stops distorting severity scoring everywhere — the
  dashboard, push alerts, the weekly digest, and the fleet list alike — while its raw
  volume stays fully visible for anyone who wants to audit it.
- Good, because the `TelemetryStore.annotate` seam is a single, well-tested integration
  point rather than ~8 scattered call-site edits, and it composes cleanly with the existing
  ADR-0026 LLM annotation (different fields, no conflict, independently degradable).
- Good, because the Windows Reliability Index overlay and the "an `unknown` severity is
  never silently benign" property from ADR-0026 are both preserved unconditionally —
  suppression can only ever narrow *pattern-based* scoring, never the independent signals.
- Bad / accepted, because an operator can now suppress a pattern that later turns out to be
  genuinely serious. Mitigated by: the pattern stays fully visible with its raw count and
  its LLM severity (never hidden), the audit trail (`created_by`, timestamp), and the health
  reason explicitly naming how many patterns are suppressed rather than silently going
  quiet.
- Bad / accepted, because suppression rules are yet another small piece of durable operator
  state (alongside `operator_policy_rules` and `webfilter_domains`) that must be considered
  whenever a host is removed from inventory — handled by purging only that host's own rules
  on removal, while fleet-wide rules (which mute a Windows quirk, not a specific PC) survive.
- Explicitly out of scope: the issue's second concern — that ~490 events/day from one
  source rotates the 20 MB Windows Application log within ~8 days, overwriting genuine
  diagnostic history — is **not** addressed here. Suppression is a server-side scoring and
  presentation change; it does not reduce what the agent collects, what it pushes, or how
  Windows manages its own event log. That is a log-retention problem on the monitored host,
  not a severity-scoring problem, and deserves its own design (e.g. a read-only "log
  rotation pressure" signal, or a guarded, confirm-gated `MaximumSize`/retention change via
  `powershell_exec`). Tracked as a follow-up issue rather than folded in here.
- Also out of scope: suppression matches on `(source, event_id)` only, not on message
  content — it cannot distinguish "this specific AuthSafes value is harmless" from "a
  different value in the same event id would be a real problem". Nor does it affect
  `diag_eventlog`, a raw, deliberately unfiltered diagnostic tool.

## More Information

- [ADR-0026](0026-llm-categorization-of-reliability-events.md) — the `category`/`severity`/
  `suspected_cause` read-path annotation this decision sits alongside.
- [ADR-0024](0024-parental-controls-web-activity-and-webfilter.md) — the closest prior art
  for an operator-editable, per-host rule list with a dashboard editor and a store/mirror
  pair (`WebFilterStore`/`WebFilterService`).
- [ADR-0020](0020-shared-policy-catalog-operator-rules-and-server-mirror.md) — the
  operator-append-only rule table + in-memory mirror pattern this reuses
  (`PolicyStore`/`PolicyEngine`).
- [ADR-0032](0032-runtime-settings-in-the-dashboard.md) — the "single process, one event
  loop, so a synchronous in-memory mirror is safe and cheap" argument this decision borrows.
- Issue [#166](https://github.com/t11z/kenny/issues/166).

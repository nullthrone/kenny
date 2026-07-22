# 0042. Semantic scoring of reliability events by severity, not volume

- Status: accepted
- Date: 2026-07-22

## Context and Problem Statement

The `reliability` health rule (`_rule_reliability`) scores a host's Error/Critical
Windows-eventlog breakdown by **volume**: `crit` at ≥50 events in the 7-day
window, `warn` at ≥15, regardless of what those events actually are. In
practice this misjudges the thing an operator cares about: a host reporting
304 repeats of one recurring, benign DistributedCOM permission timeout — one
installed app colliding with a Windows component every few minutes — scores
identically to a host with 300 distinct, novel failures. The metric measures
how much a host talks, not what it's saying.

ADR-0028 already gives the server a **friendly category** per raw event group
(`(source, event_id)` → e.g. "App crash / hang"), classified by a cached,
injected LLM call on the telemetry read path. But that annotation is purely
cosmetic: it drives the dashboard heatmap and the health rule's *reason*
string, never the crit/warn decision itself, and it carries no notion of
severity or "this is a known nuisance."

The question: how does the reliability check tell a host drowning in one
harmless, repeating pattern apart from a host accumulating genuinely new or
serious problems — without a manual `diag_eventlog` call, and without losing
sensitivity to real incidents?

## Considered Options

- **A — Deterministic server-only heuristic.** Score on the count of distinct
  `(source, event_id)` patterns plus a hand-maintained allowlist of
  known-benign ones. Fully transparent and testable, no LLM in the scoring
  path — but the allowlist needs ongoing curation and has no way to judge the
  severity of a source it has never seen, which is exactly the case that
  matters most (the novel/unknown error). Rejected as the sole mechanism.
- **B — Extend the existing ADR-0028 LLM layer (chosen).** Have the same
  cached, injected Haiku call additionally classify a **severity** (`benign` /
  `notable` / `serious`, or `unknown` when genuinely unsure) and a short
  **suspected cause** per pattern, alongside the friendly category. Rewrite the
  rule to score on weighted **distinct patterns** rather than raw volume.
  Reuses existing infrastructure (cache, injected client, graceful
  degradation) with no new dependency and no wire-contract change.
- **C — Stateful per-fleet novelty baseline.** Persist every seen pattern with
  a rolling frequency and escalate on statistical novelty. The strongest
  novelty detection in principle, but requires new storage, a schema, and a
  maintenance/retention job — disproportionate for a family fleet of a
  handful of PCs. Rejected.

## Decision Outcome

Chosen option: **B**, because it directly fixes the volume-vs-meaning problem
by promoting the ADR-0028 categorization from cosmetic to load-bearing,
without adding a new dependency, a stateful store, or a wire-contract change.

Concretely:

- `event_categories.py`'s classification call now returns, per `(source,
  event_id)` pattern, `{category, severity, cause}` instead of just a category
  string. `severity` is one of `benign | notable | serious`, or the explicit
  fallback `unknown` for "genuinely can't tell" — the model is instructed to
  prefer `unknown` over guessing, and `unknown` is deliberately **never**
  scored as if it were benign. `cause` is a short plain-language guess (e.g.
  "two apps colliding over a stale COM registration"). Both are annotated at
  the same read-path step, with the same cache, injected client, and
  graceful no-key/API-failure degradation as ADR-0028 (falling back to
  `category="Other"`, `severity="unknown"`).
- `health_rules._rule_reliability` scores on **weighted distinct patterns**
  once events carry this annotation: a single `benign` pattern never escalates
  the host regardless of its count; a single `serious` pattern recurring
  meaningfully, or enough distinct non-benign (`notable`/`unknown`/`serious`)
  patterns, escalates to `crit`; any non-benign pattern at all is at least
  `warn`. The Windows Reliability Index and an agent-reported `level ==
  "critical"` still apply independently on top.
- **Sensitive fallback, not a cliff.** When events carry no `severity` field
  at all (the annotation step hasn't run — e.g. a raw stored snapshot, or a
  caller that skips it), the rule uses the original volume thresholds
  (≥50 crit / ≥15 warn / any `critical` level / stability index), **plus** an
  added distinct-pattern escalation (many low-count distinct patterns now
  warn even under the old bare-count threshold). This fallback path is
  strictly at least as sensitive as the pre-0042 rule, never less.
- The health-rule **reason** now names the dominant significant pattern —
  `source/event_id ×count (cadence) — suspected cause` — or, for an all-benign
  host, says so explicitly (`"N events, all known-benign (category)"`) instead
  of surfacing a bare, alarming count. This is read directly via the
  `agent_health` MCP tool (annotation now also runs on that path, not only the
  dashboard), so the pattern is judgeable without a manual `diag_eventlog`
  call.
- `severity` and `suspected_cause` are **server-internal annotations**,
  exactly like `category` (ADR-0028) — not part of the wire contract. The
  agent and `docs/protocol.md`/`docs/fixtures/` are unchanged; only
  `kenny-server` changes.
- Consumers of the reliability format are migrated additively: the fleet
  heatmap (`fleet_stats._reliability_categories`) also flags a cell `crit`
  when a group's `severity == "serious"`, in addition to the existing
  `level == "critical"` check; the dashboard detail view shows a severity
  badge and the suspected cause per event row. Both fall back to today's
  behavior on unannotated data, so nothing breaks.

### Consequences

- Good, because a host repeating one benign pattern hundreds of times no
  longer reads as `crit`, while a host accumulating distinct or serious
  patterns still does — directly matching the illustrative DistributedCOM
  symptom that motivated this change.
- Good, because the `agent_health` reason now names the dominant pattern and a
  plain-language suspected cause, letting an operator judge severity without
  a manual diagnostic call.
- Good, because detection quality cannot regress: the annotated path treats
  `unknown` severity as never-benign, and the unannotated fallback path is a
  strict superset of the previous thresholds.
- Good, because no wire-contract or agent change is required — the Rust
  agent, `docs/protocol.md`, and `docs/fixtures/` are untouched, so this
  cannot cause Python/Rust drift.
- Bad, because reliability scoring now has an LLM-classification dependency
  in its `warn`/`crit` decision for annotated snapshots (previously purely
  deterministic). Mitigated by the existing `(source, event_id)` cache (a
  real fleet's distinct event types are few and stable, so after warm-up
  classification is a cache hit) and by the deterministic fallback, which
  guarantees the rule never becomes *less* sensitive without a key.
- Bad, because the severity/cause judgment is inherently approximate and can
  be wrong for a truly novel pattern the model misjudges as benign. Bounded
  by instructing the model to prefer `unknown` over guessing, and by
  `unknown` never being treated as a pass.

## More Information

Extends ADR-0028 (LLM categorization of reliability events) rather than
replacing it — the category enum, cache, and injected-client testing pattern
are unchanged. See `kenny_server/event_categories.py`,
`kenny_server/health_rules.py` (`_rule_reliability`), and
`tests/test_health_rules.py` for the scoring tests.

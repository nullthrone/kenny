# 0055. Scheduled web-filter enforcement: a standing rule the server enacts on a clock

- Status: accepted
- Boundary moved: **the authorization model** — where an operator's consent attaches to a
  state-changing push. Until now consent named either one action (ADR-0009's confirm-gate)
  or one pinned artifact delivered once (ADR-0040's campaign). A filter schedule is the
  first *standing, recurring* rule kenny enacts against a managed machine, on a wall clock,
  indefinitely, with no human present.
- Date: 2026-08-17

## Context and Problem Statement

Parental web filtering (ADR-0024) is per-host and static: an operator edits a list, clicks
apply, and the agent writes it into the hosts file. The obvious next ask is time-of-day —
"block social media on school nights, allow it at the weekend". The categories that make
this expressible are a purely additive server-side layer, but the *time* dimension is not:
something has to change a machine's state at 21:00 when nobody is looking. That is the
whole point of the feature, and it is exactly the property that needs deciding rather than
assuming.

Two standing decisions bear on it, and they point different ways.

ADR-0009 established kenny's confirm-gate: state-changing actions pause for explicit
operator confirmation and never fire on their own. Read strictly, a scheduled push violates
it outright.

ADR-0040 already moved that line once, deliberately. Its `KENNY_AGENT_ROLLOUT_ON_CONNECT`
lets the server push an `agent_update` to a machine as it reconnects, hours after the
operator approved a campaign — an operator authorizes a policy once, and the server acts on
it later unattended. That is the same *shape* as a filter schedule, so the honest question
is whether ADR-0040 already precedents this, or whether a schedule is different in kind.

Two verified facts sharpen it:

- On the web-filter path there is no autonomous push today at all.
  `_webfilter_refresh_loop` refreshes only the external-list *cache*; it has never called an
  agent. Every `webfilter_apply` to date has had a human behind it.
- ADR-0040's precedent is narrower than it first looks. Its Option B — a campaign as a
  standing "track latest" subscription — was **considered and rejected**, because "the
  operator's approval names an artifact, not a subscription", and its campaigns expire
  precisely so one "never lingers as a standing rule that outlives the operator's
  attention". Its trigger is also an event about the machine (it became reachable), not a
  clock.

A recurring weekly window is, unavoidably, a standing rule that outlives the operator's
attention and fires on a clock. That is the thing ADR-0040 named and declined to build.

## Considered Options

- **A. A server-side schedule of per-host windows, enacted by a background loop that pushes
  only when the effective list differs from what the host already has applied.** Windows
  live in `webfilter_windows`, are evaluated in a named IANA timezone, and name extra
  categories that apply for their duration. The loop mirrors the existing
  `_webfilter_refresh_loop` / `_backup_loop` / `alert_engine.run` lifespan pattern.
- **B. A timer inside the agent.** The agent would hold the windows and re-splice its own
  hosts file on a clock. Rejected, and not for the first time: `handlers/accounts.rs`
  records an explicit prior refusal to put a timer in the agent, because collectors are
  stateless `fn()`s isolated by `catch_unwind` (ADR-0007) and the enforcer is deliberately
  dumb and idempotent (ADR-0024). It would also put policy in the one place that cannot be
  audited, corrected or rolled back from the server, and would need new wire fields.
- **C. No schedule; the operator applies the stricter list by hand at the right times.**
  Rejected: the hours that matter are the hours the parent is asleep. A control that
  requires the supervisor to be awake is not a control, and the manual path already exists.
- **D. Treat a schedule as already precedented by ADR-0040 and record only a note under
  ADR-0024.** Rejected: ADR-0040 bought its unattended push with pinning, expiry and a
  revoke, explicitly so that approval could not become a subscription. A weekly window *is*
  a subscription. Reusing that precedent without saying so would move the authorization
  boundary silently, which is the failure mode the ADR discipline exists to prevent.

## Decision Outcome

Chosen option: **A**, with the boundary move stated rather than borrowed. ADR-0040
precedents *deferred* unattended action; it does not precedent a *standing recurring* one.
This decision extends the authorization model to admit a standing rule, and pays for it
with bounds chosen to match the ones ADR-0040 used for its own move.

**What does not move.** ADR-0024's server/agent split is untouched and this decision
depends on it: the server remains the authoritative matcher, the agent the dumb, idempotent
enforcer. The agent has no clock and no concept of a category. Categories and windows
resolve entirely server-side into the one unchanged payload it already understands,
`webfilter_apply({domains, doh_policy, list_hash})`. `docs/protocol.md`, `docs/fixtures/`,
`protocol.py`, `protocol.rs` and everything under `kenny-agent/` are unchanged, and
`PROTOCOL_VERSION` does not move.

The bounds that make the standing rule acceptable:

- **A schedule only ever adds.** A window can name extra categories for its duration; it
  can never remove one the host has, and cannot turn the feature or block mode on. The
  worst thing an incorrect schedule does is over-block — visible to the person at the PC
  immediately, and reversible in one click. Compare `agent_update`, where ADR-0040 had to
  concede that an in-flight rollout cannot be recalled.
- **Authoring an enabled window is the consent, and it is per host.** The loop's fleet is
  exactly `agents_with_windows()`. A host with no enabled window is never touched by it —
  so this decision does not make the server free to push web-filter state to the fleet, only
  to the hosts an operator has explicitly put on a schedule. Disabling the last window ends
  the licence.
- **The pre-existing gates still gate.** The loop pushes only when the host's `enabled`
  *and* `block_mode` are both on. It turns nothing on by itself.
- **Idempotent and hash-guarded.** Each pass computes the list for *now* and pushes only
  when its `list_hash` differs from the stored `applied_hash`, so a pass over an unchanged
  fleet costs nothing on the wire and re-running one is a no-op. The revert at the end of a
  window is the same mechanism as the tightening at its start, not a special case — nothing
  has to remember that a window was ever open.
- **The kill switch still wins.** `webfilter_apply` is mutating, so a person at the PC who
  has switched remote control off refuses a scheduled push exactly as they refuse a manual
  one (ADR-0011, ADR-0024's honest limitation). A failure is contained to its host, logged,
  and retried on the next pass; it never ends a pass.
- **Observable, not inferable.** `schedule_state` answers, in one payload, the two questions
  an operator has when looking at a host: whether the list in force is currently the
  stricter one, and when it reverts (`stricter`, `extra_categories`, `active_windows`,
  `reverts_at`). Deleting the last window deliberately does *not* trigger an unattended
  revert — the host leaves the loop's fleet and surfaces the ordinary `drift` signal
  instead, so the licence to push and the licence to schedule end together.

**The 10 000-domain ceiling becomes reachable, so it is enforced server-side.** With more
categories the effective list can genuinely exceed the agent's cap. The agent rejects an
over-cap list with `bad_args` and does not truncate, so `build_apply_args` now raises
`ListTooLargeError` with the count and the cap instead of shipping a silent prefix — a
prefix would be a filter the operator believes is complete and is not. The manual push
returns `list_too_large` (400), the MCP tool raises `bad_args`, and the schedule loop skips
and logs the host rather than pushing. The host overview still *reads* in that state, so the
operator can see which category to turn off. Matching is unaffected: an over-cap list still
flags, so the alarm — ADR-0024's primary control — survives a list too large to enforce.

**A bypass request is a ticket, not a new entity.** `web_filter` joins
`tickets.KNOWN_CATEGORIES`; the requester is the child, the decision is the ticket's
existing operator-approval gate, and granting one is the operator's ordinary
`webfilter_set(add_domain, action="allow")` + `webfilter_push`. A `user`-role principal
already cannot call either, so no new authorization exists here. There is deliberately no
second pending-request table with its own lifecycle — ADR-0024's own consequences credit
the design for reusing rather than inventing parallel machinery, and duplicating the ticket
lifecycle is exactly what that warns against. Not to be confused with the `bypass`
*category*, which blocks VPN/proxy/DoH domains to stop a child circumventing the filter:
one asks permission, the other refuses to be evaded.

### Consequences

- Good, because the ask is met where it matters — the hours nobody is watching — without
  putting a clock, a category or any state into the agent, and without touching the wire
  contract or bumping `PROTOCOL_VERSION`.
- Good, because the boundary move is written down with its bounds, so the next feature that
  wants a standing rule argues against this record rather than quietly citing ADR-0040 for
  something ADR-0040 refused.
- Good, because add-only, hash-guarded, per-host-opt-in and kill-switch-respecting together
  keep the blast radius of a bad schedule to "too much is blocked on one machine", which is
  the cheapest failure this system can have.
- Good, because refusing an over-cap list server-side turns a guaranteed agent-side
  `bad_args` into an actionable message, and keeps the alarm working even when the block
  cannot be applied.
- Bad, because kenny now changes a managed machine's state with no human present on a path
  where it previously never did. That is the decision, not a side effect; the bounds above
  are the price, and `KENNY_WEBFILTER_SCHEDULE_SECS=0` turns the loop off entirely.
- Bad, because schedule evaluation is wall-clock in a named zone, so a window spanning a DST
  transition keeps its local start and end and is therefore an hour longer or shorter in
  real time once a year. Chosen deliberately: an operator writing "21:00–07:00" means the
  clock on the wall, not an elapsed duration.
- Bad, because deleting the last window leaves the host on whatever was last applied until
  someone applies again. Accepted: it surfaces as `drift`, the same signal every other
  config edit produces, and the alternative — keeping a licence to push after the operator
  removed the thing that granted it — is worse.
- Bad, because the loop's cadence and initial delay are read from the environment
  (`KENNY_WEBFILTER_SCHEDULE_SECS`, `KENNY_WEBFILTER_SCHEDULE_INITIAL_DELAY`) rather than the
  ADR-0032 settings catalog, so changing them needs a restart. A follow-up can add catalog
  specs and make the cadence live, exactly as the other loops already are.

## More Information

- Builds on: [ADR-0024](0024-parental-controls-web-activity-and-webfilter.md) (the
  server-authoritative matcher / dumb enforcer split this decision does *not* move, and the
  layered list model the categories extend), [ADR-0040](0040-scheduled-update-detection-and-operator-approved-rollout.md)
  (the partial precedent — deferred unattended action after one authorization — and the
  source of the bounds pattern), [ADR-0009](0009-server-hosted-claude-chat.md) (the
  confirm-gate this decision qualifies), [ADR-0007](0007-telemetry-push-model-and-sqlite-storage.md)
  (stateless collectors — why option B is not available),
  [ADR-0011](0011-local-remote-control-kill-switch.md) (the kill switch a scheduled push
  still obeys), [ADR-0046](0046-ticket-as-entity-chat-thread-as-binding.md) (the ticket
  entity a bypass request reuses), [ADR-0051](0051-process-wide-sqlite-write-serialization.md)
  (write serialization), [ADR-0033](0033-multi-user-authentication.md) (the role/scope
  guards every new route goes through).
- Code (server only): `kenny-server/kenny_server/webfilter.py` (`CATEGORY_CATALOG`,
  `ScheduleWindow`, `schedule_state`, `ListTooLargeError`, `WebFilterService.schedule_due`),
  `store.py` (`webfilter_windows`, the `categories`/`category` columns and their migration),
  `main.py` (`webfilter_schedule_pass`, `_webfilter_schedule_loop`), `tools.py`
  (`webfilter_get`/`webfilter_set` extended in place — no new tool names, so the tier map
  and its agent-parity test stay untouched), `tickets.py` (`web_filter` category),
  `webui/__init__.py` (`/api/agent/{id}/webfilter/schedule`, `/webfilter/requests`).
- No agent-side change; no `docs/protocol.md`, `docs/fixtures/`, `protocol.py` or
  `protocol.rs` change; no `PROTOCOL_VERSION` bump.
- Follow-ups, additive and not built now: settings-catalog specs for the loop's cadence;
  push-alerting (ADR-0027) when a scheduled push is skipped for an over-cap list or fails
  repeatedly against the same host.

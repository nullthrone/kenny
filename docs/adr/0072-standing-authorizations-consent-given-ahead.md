# 0072. Standing authorizations: autonomy is consent given ahead, never a tier

- Status: accepted
- Boundary moved: **the authorization model** — consent can now attach to a *predicate over
  future calls a model chooses*. Until now it named one action (the confirm-gate,
  ADR-0009), one pinned artifact (an update campaign, ADR-0040) or a deterministic rule the
  server enacts (a filter schedule, ADR-0055).
- Amends: [ADR-0045](0045-tiered-tool-classification.md),
  [ADR-0055](0055-scheduled-web-filter-enforcement.md),
  [ADR-0040](0040-scheduled-update-detection-and-operator-approved-rollout.md),
  [ADR-0071](0071-specialized-agents-purpose-bound-unattended-sessions.md)
- Date: 2026-10-08

## Context and Problem Statement

ADR-0071 lets an agent in `act` make a `standard_change` within the constraints of its
spec, and refuses every `normal_change` unless an authorizer says yes. The household's
routine work includes `normal_change`s that nobody wants to approve one by one — dropping a
suppression rule nothing has matched for months, say — while ADR-0045 forbids the tier
itself from ever being the permission. The architect decided that such changes may run
autonomously under policy. The question is what that policy is, so that "autonomous" still
means a person consented to exactly this, and nothing more.

## Considered Options

- **Lower the tool's tier.** Rejected by ADR-0045: re-tiering would silently change every
  surface at once.
- **Approve each call later, by the agent consuming an approval.** Rejected: the host's
  state at approval is not its state at execution, and it builds a second path by which an
  approval is spent.
- **Chosen: a standing authorization** — a superuser's grant, made ahead, that one agent,
  bound to what that agent is, may make one kind of `normal_change` on named hosts.

## Decision Outcome

An authorization names an agent, the agent's **effective hash**, one tool, an explicit
scope (a list of hosts, or the sentinel `server` for a server-side change; an empty list is
never "all"), an attempt budget (attempts per host per rolling 24 h) and an expiry. The
binding rules:

1. **Never authorizable: `shell_exec`, `powershell_exec`, `agent_update`.** A free-text
   script cannot be bound to values; an agent binary's consent model is the pinned
   campaign (ADR-0040). The list is a literal in code, refused at grant and at match.
2. **Only a person grants, and only for what they saw.** Granting, revoking, editing an
   agent's parameters and promoting it to `act` need a superuser signed in to the
   dashboard — not a token, which is what a model holds — and name the effective hash the
   person was shown; a different live hash refuses the request. Switching unattended
   action on through a setting — triage resolving tickets, the global agent switch — is
   the same consent and takes the same person; switching it off takes any superuser. Every grant expires (at
   most 180 days). Operators may read authorizations; users may not. None is ever shown to
   a model: the refusal message is the boundary (ADR-0023).
3. **It narrows, never widens.** It is consulted at gate step 8 only, after the spec's
   constraints (ADR-0071) have bound every argument; it cannot pre-authorize a run or
   reach a tier the agent's spec does not name. Only the agent surface consults it — no
   ticket, copilot or MCP gate ever does.
4. **Consume, then execute.** A match records an attempt atomically before the call runs,
   and a failed call still counts, so two runs cannot both spend the last attempt.
   Revoking reaches the next call; a call already forwarded cannot be recalled. A
   scheduled agent's maintenance window bounds its consent in time: no host is started,
   and no change is made, once the window has closed.
5. **Every autonomous change names its authorization.** The id is stamped into the run's
   actions and the audit entry. A refused change becomes a recommendation, which names
   none: a person may act on it themselves through the ordinary confirm-gate, as
   themselves; an agent never resumes it.
6. **`act` and authorizations bind to the effective hash** — the spec hash (which now also
   fingerprints the tier of every tool the spec names, so a re-tier unbinds) combined with
   the agent's parameters. Parameters are what an install must set without a code change
   (a package allowlist, a maintenance window); only a superuser edits them. Any change
   of the effective hash drops the agent to `shadow` — for a run in flight too — and voids
   its authorizations for good: rolling the code back does not revive them. A
   constraint's values may also be computed at run start by server code from the
   server's own records — the rules nothing has matched, say — and frozen on the run;
   such evidence only ever narrows what the parameters and the spec allow.

### Consequences

- Good, because "autonomous" stays a statement about a person: who granted what, for which
  hosts, until when, is a row, and every change it permitted points back to it.
- Good, because the cases that make standing consent dangerous — a re-tiered tool, an
  edited spec, a widened parameter — each unbind it rather than silently inherit it.
- Bad / accepted, because a parameter edit sends the agent back to `shadow` and needs a
  superuser to promote it again; editing what an agent may touch is meant to cost that.
- Bad / accepted, because an authorization is a standing risk until it expires; the
  ceiling on expiry and the per-host attempt budget bound it.
- Deliberately cut for a household fleet: a time window per authorization (the agent's
  trigger is its window) and per-authorization argument narrowing (the agent's parameters
  carry that, and it can be added later because the gate already intersects).

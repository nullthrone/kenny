# 0074. Stewards as data: the dashboard composes from what code provides

- Status: accepted
- Boundary moved: **the agent/session model** — a steward is no longer only code. A
  superuser can define one in the dashboard, and it runs through the same runner and gate as
  a catalog steward.
- Amends: [ADR-0071](0071-stewards-purpose-bound-unattended-sessions.md)
- Touches: [ADR-0063](0063-the-copilot-proposes-a-ticket-it-does-not-open-one.md),
  [ADR-0072](0072-standing-authorizations-consent-given-ahead.md)
- Date: 2026-11-01

## Context and Problem Statement

ADR-0071 left "a dashboard builder for stewards defined as data" out of scope: every steward
is a spec in the code catalog, reviewed with the code. A household wants its own small
routines — read a machine's services weekly and report, keep a named package current —
without a release. A spec held in the database would be the first steward no code review
saw. What may such a steward be, and what stops the dashboard becoming the place where the
code's limits are worked around?

## Considered Options

- **Code only.** Rejected: every routine would wait for a release.
- **Data with the full spec language**, evidence providers and sensitive tools included.
  Rejected: an evidence provider is server code by nature, and an unchecked sensitive tool is
  exactly what the catalog's review exists to catch.
- **Chosen: data may only select and narrow what code provides.**

## Decision Outcome

1. **Code provides, data composes.** Tools, their tiers, preconditions, the verdict tool,
   event names and the gate are code. A built steward chooses among them and narrows them;
   it adds nothing a catalog steward could not have.
2. **Narrower than the catalog, never wider.** A built steward is validated exactly as a
   catalog spec is, and additionally may not name a shell, `agent_update`, a copilot, ticket
   or fleet-wide tool, or a steward-only tool; names a sensitive tool only with its flag set;
   constrains arguments only with literal values or its own parameters, never with evidence;
   ends with `steward_verdict`; and is never started by a new ticket. Its id cannot collide
   with the catalog's.
3. **Only a person at the dashboard defines one.** Creating, editing and deleting need a
   superuser signed in to the dashboard. Every save is a new spec hash: the steward returns to
   `shadow` and its authorizations are void for good (ADR-0072 rule 6). Deleting it is removing
   it from the catalog; re-creating the same id revives nothing.
4. **The model drafts, never saves.** Ask kenny may propose a draft, validated by the same
   rules, as a card that opens the editor; only the superuser's save makes it a steward.
5. **A release may withdraw what a built steward used.** A built steward that no longer
   validates is kept, shown, and does not run until a person fixes it; it never blocks start-up.

### Consequences

- Good, because a routine can be added without a release and still runs under every rule a
  catalog steward does.
- Good, because what a built steward may do is a closed list a superuser saw and saved, and
  any change unbinds the consent given to it.
- Bad / accepted, because a superuser's prompt is not code-reviewed; the gate, constraints and
  authorizations — not the prompt — are the boundary (ADR-0023).
- Bad / accepted, because evidence-fed constraints and new preconditions still need code.

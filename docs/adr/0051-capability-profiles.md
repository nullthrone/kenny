# 0051. Capability profiles: named per-user tool allowlists that only narrow

- Status: proposed
- Date: 2026-08-01
- Amends: [ADR-0037](0037-multi-user-authentication.md)

## Context and Problem Statement

ADR-0037 gave kenny two authorization axes: a role (how much of the fleet you govern) and a
host scope (which machines are yours). Neither answers a third question — *which
capabilities* — and until now nothing needed it, because everyone who could reach a host
could reach every tool on it, and everyone with that reach was an operator.

Putting a household member in front of the tool loop (ADR-0048) makes the question
unavoidable: this person may look at their own PC and fix routine things on it, and must
never reach a shell or account governance — **even on their own machine**, where their host
scope says yes and their role says yes.

## Considered Options

- **A fourth role below `user`.** Rejected: the role model is a hierarchy, and a fourth rung
  only moves the line. The moment two people need different capabilities at the same scope
  you need a fifth, and roles multiply. Worse, the question is orthogonal to the hierarchy —
  a role says where you sit, not what you may touch — so folding it in makes both harder to
  reason about.
- **Per-user, per-tool grants.** Rejected: unbounded, unreviewable, and it drifts silently
  as the tool catalog grows.
- **Named, reusable allowlists attached to the account, beside the role.** Chosen.

## Decision Outcome

Chosen option: **a capability profile — a named set of tools attached to an account, as a
third axis beside role and host scope.** The role hierarchy from ADR-0037 is left exactly
as it is; profiles *compose* with it instead of multiplying it, and the same mechanism
carries any other need to narrow a surface's reachable tool set.

**A profile only ever narrows. It grants nothing.** A tool inside a profile is still
subject to the caller's role, to their host scope, and to whatever gate the surface applies
to its tier (ADR-0049).

> **Effective permission = role ∩ host scope ∩ profile ∩ tier.** Intersecting, never
> additive.

That invariant is recorded because the temptation with any named grant is to let it add
something ("this profile also lets you…"). The first such exception turns profiles into an
escalation path and makes every other check bypassable by attaching one.

Two defaults keep it safe at the edges: **no profile means today's behaviour exactly** —
unrestricted, subject to role and scope — so existing accounts were deliberately not
backfilled and nothing changed for them; and an **unknown profile name allows nothing**, so
a typo fails closed rather than widening access. On the autonomous surface a profile is
applied twice: the tools it excludes are not offered to the model at all, and they are
refused again at dispatch. The first is ergonomics — it stops the model proposing what it
cannot have; the second is the control.

**Consent is a separate axis, not a fourth term in that intersection.** Some tools need the
agreement of the person whose privacy is at stake. Authorization asks *who may act*;
consent asks *whose privacy is this*. They are different questions and must not be folded
together, because an operator is fully authorized to look at a family member's screen and
is still not the person who can agree to it. Two consequences follow and are load-bearing:
consent can never be granted on someone's behalf — **including by an operator, who is
refused precisely because seniority is irrelevant to the question** — and a granted consent
never satisfies an authorization gate. Where both apply to one call, both must be answered.

### Consequences

- Good, because the ADR-0037 role hierarchy stays intact and keeps its meaning; capability
  narrowing lives beside it rather than inside it.
- Good, because profiles are reviewable — a named set an operator can read in one go,
  unlike per-user grants scattered across a catalog.
- Good, because the same structure serves any surface that needs to narrow, so a future
  surface does not invent a fourth mechanism.
- Bad, because "why can't I do this?" is now harder to answer: four independent things can
  refuse a call, and the caller sees only that it was refused. A surface that narrows people
  therefore has to be able to show them what they are bound to, or a misconfiguration acts
  silently.
- Bad, because a profile is a curated list and will lag the tool catalog: a new capability
  is in no profile until someone adds it. That fails closed, but a narrowed user silently
  misses a capability that was meant for them.
- Neutral: profiles are optional. An account without one behaves as it did before this
  record, which is why the change carried no migration risk.
- Neutral: consent sits outside the permission intersection deliberately — it is a further
  condition on top of an already-authorized call, never a substitute for one.

## More Information

- Amends [ADR-0037](0037-multi-user-authentication.md) by adding a third axis to its
  role + host-scope model; nothing in that record is reversed.
- Depends on [ADR-0049](0049-tiered-tool-classification.md) for the tier term of the
  intersection, and is one of the four controls required by
  [ADR-0048](0048-delegated-identity-from-a-chat-platform.md).
- Related: [ADR-0026](0026-parental-controls-web-activity-and-webfilter.md) and
  [ADR-0018](0018-screenshots-captured-in-user-session-via-tray.md) — the capabilities whose
  privacy weight motivates the consent axis.

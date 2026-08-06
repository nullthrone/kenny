# 0045. Tiered tool classification: the tier belongs to the tool, the gate to the surface

- Status: accepted
- Date: 2026-08-01
- Amends: [ADR-0009](0009-server-hosted-claude-chat.md)

## Context and Problem Statement

ADR-0009 classified every tool as either read-only or state-changing, and the dashboard
copilot confirms the second kind. That binary was sufficient while every surface treated
"state-changing" identically: the dashboard confirms it, and MCP is driven by an operator
who is already the human in the loop.

A surface where a non-operator drives tools (ADR-0044) breaks the assumption. Flushing a
DNS cache and installing software are both "state-changing", but only one of them is worth
waking the operator for. Without a middle ground, the new surface either confirms
everything — which makes self-service useless — or confirms nothing, which is not
defensible.

The dangerous way to fix this is the obvious one: add a tier that *means* "runs without
confirmation". That makes the classification a permission, and re-tiering a tool then
silently changes behaviour on every surface at once.

## Considered Options

- **Keep the binary flag** and give the new surface its own private list of what it may run
  autonomously. Rejected: a second list, maintained separately, guaranteed to drift from
  the first.
- **Three tiers, where the tier decides the gate.** Simple, and wrong for the reason above.
- **Three tiers as a property of the tool, plus a per-surface policy** deciding what to do
  with each tier. Chosen.

## Decision Outcome

Chosen option: **three shared tiers describing the tool, and a separate per-surface
decision about what to do with them.** The tier says what a tool *is* — whether it changes
the world, and how consequential and reversible that change is. It never says what a
surface must do. **A tier is never permission to skip a confirmation.**

That separation is the whole decision. Because the dashboard holds *both* change tiers, its
observable behaviour is deliberately unchanged by this ADR: for every known tool the
state-changing answer is identical to what ADR-0009 established, and the confirm dialog and
audit annotation are untouched. Had the tier defined the gate, promoting a tool to the
routine tier would have silently deleted its dashboard confirmation — precisely the drift
ADR-0023 exists to prevent. Loosening the dashboard later is therefore its own visible
decision, not a side effect of re-tiering something.

Two properties keep the classification honest:

- **Unknown tools fail closed** to the most consequential tier. A capability that reaches a
  catalog without being classified is treated as the worst thing it could be, not the
  least.
- **Parity with the agent.** The agent enforces its own deterministic notion of what
  mutates (ADR-0019/0020). The server's tiers must never call something read-only that the
  agent considers mutating; a test holds a literal copy of the agent's list and fails the
  build if a mutating tool is unclassified or mis-tiered. This is ADR-0023's "one source of
  truth for what mutating means", carried forward from two states to three.

### Consequences

- Good, because a surface can be autonomous for routine work without maintaining a private
  allowlist that drifts away from the shared classification.
- Good, because the dashboard and MCP surfaces are provably unaffected — the change is a
  refinement of the vocabulary, not of anyone's behaviour.
- Good, because fail-closed defaults plus the agent-parity test mean the classification
  cannot silently lag the tool catalog.
- Bad, because where the line between "routine" and "consequential" falls is a judgement
  call, and it is now made once for every surface. Getting it wrong is wrong everywhere at
  once — which is the point, and it raises the cost of the judgement.
- Bad, because answering "will this prompt me?" now needs two lookups (the tool's tier, the
  surface's policy) where a boolean needed one.
- Neutral: the tier is surfaced additively in the audit view; every existing caller of the
  old binary answer sees the same value it saw before.

## More Information

- Refines [ADR-0009](0009-server-hosted-claude-chat.md)'s binary classification and
  confirm-gate; reinforces [ADR-0023](0023-untrusted-agent-data-in-chat-context.md), whose
  gate-parity requirement this generalises.
- Depends on the deterministic agent-side guard,
  [ADR-0019](0019-agent-side-deterministic-tool-guard.md) /
  [ADR-0020](0020-shared-policy-catalog-operator-rules-and-server-mirror.md), as the
  authority the server's tiers are checked against.
- Consumed by [ADR-0044](0044-delegated-identity-from-a-chat-platform.md) as one of the four
  controls that make a non-operator surface defensible.

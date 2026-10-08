# 0071. Specialized agents: kenny runs purpose-bound unattended sessions

- Status: proposed
- Boundary moved: **the agent/session model** — the unattended session stops being one
  hand-built special case (triage) and becomes a declared kind: any number of agents, each
  with its own prompt, trigger, closed tool set and budget, run under a service identity
  of its own.
- Amends: [ADR-0056](0056-unprompted-ticket-triage.md)
- Touches: [ADR-0045](0045-tiered-tool-classification.md),
  [ADR-0017](0017-observability-logging-and-event-store.md)
- Date: 2026-10-08

## Context and Problem Statement

Triage (ADR-0056) showed that an unprompted session is worth running when three controls
bound it and none is the model's to observe: what it may touch (withheld, not refused),
where it may look (one frozen host), and what it may conclude (a server-side predicate over
facts on the record). The household's remaining routine work has the same shape — apply
pending package updates in a quiet hour, review a host's autostart and admin accounts,
prune suppression and ticket rules nothing matches any more — and each would today be a
second, third and fourth bespoke copy of `triage.py`.

Copies drift, and here drift is a security property: the next copy that derives its tools
from a profile instead of intersecting them hands a background turn a shell
(`profile_allows(None, …)` allows everything). So: what is an agent, and what bounds every
one of them identically?

## Considered Options

- **One bespoke module per agent**, as triage is today. Rejected: the safety argument
  would be re-made, and re-checked, once per agent.
- **Let the copilot do it on a timer** — schedule Ask kenny prompts. Rejected: the copilot
  is built for a person in the loop (operator-held gate, in-memory holds, the full tool
  catalog); with nobody there each of those is either a stall or a hole.
- **Chosen: a declared agent kind** — `AgentSpec` — run by one runner through the existing
  tool loop, with one gate shared by every agent.

## Decision Outcome

An agent is an `AgentSpec` (`kenny_server/agents/spec.py`): id, prompt, trigger
(`event` | `schedule` | `on_demand`), an **explicit, closed tool set**, an optional verdict
tool, and a budget. Specs are validated at load and a bad one fails closed:

1. **Every tool must be classified** in `TOOL_CLASSES`; an unknown name refuses the spec.
   Nothing is derived from a profile.
2. **Sensitive tools are opt-in** (`sensitive_ok`).
3. **`spec_hash` fingerprints what the agent does** (prompt, trigger, tools, budget,
   version). Anything later granted to an agent binds to that hash, so editing a spec is
   never a silent widening of something a person agreed to.

Each agent has a **mode**: `off`, `shadow` (the whole run happens; a change is refused at
the gate and kept as a recommendation) or `act`. A new agent starts in `shadow`; moving one
to `act` is a superuser's decision. Every run is **frozen to one host** (or to none, for a
server-only agent), runs as the service actor `agent:<id>`, and leaves an `agent_runs`
record with its outcome and token usage. A global switch stops every agent at once.

The generic gate (`agents.policy.AgentPolicy`) answers in one fixed order: not in the spec
→ deny; outside the frozen host → deny; read-only → allow; change in `shadow` → deny and
recommend; `standard_change` in `act` → allow; `normal_change` in `act` → allow only when
an authorizer says yes, else deny and recommend. **It never holds**: there is nobody present
to answer a hold, and a held call parks a run forever (ADR-0056's reason for withholding).

Triage becomes catalog entry `triage`, unchanged in behaviour: it keeps its ticket surface,
its prompt and `may_resolve`; its existing `KENNY_TRIAGE_*` settings remain the source of its
mode (`ENABLED` off → `off`; `RESOLVE` off → `shadow`; on → `act`).

The forwarded-call audit now names who acted — the agent actor, the operator driving the
copilot, the MCP principal — with the run id and redacted arguments, because "kenny did
it" is not an answer once kenny acts on its own.

### Consequences

- Good, because the three controls of ADR-0056 are now properties of the framework, tested
  once against the real loop, rather than conventions each agent must remember.
- Good, because `shadow` lets every agent's judgement be read off real runs before it acts.
- Good, because token usage is measured for the first time, per run.
- Bad / accepted, because kenny now spends tokens on work nobody asked for in proportion to
  the number of enabled agents; bounded per run by the budget and globally by the switch.
- Bad / accepted, because a `standard_change` runs in `act` without a per-call decision.
  That is the meaning ADR-0045 gave the tier — routine, reversible, low blast radius — and
  the mode switch is the person's decision that this agent may use it.
- Out of scope here: what may authorize a `normal_change` (ADR-0072), and a dashboard
  builder for agents defined as data.

## More Information

- [ADR-0045](0045-tiered-tool-classification.md) — the tier is the tool's, the gate the
  surface's; an agent is a surface.
- [ADR-0023](0023-untrusted-agent-data-in-chat-context.md) — tool output is data; the gate,
  not the prompt, is the boundary.

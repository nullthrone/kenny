# 0056. Unprompted ticket triage, and what may let it act

- Status: accepted
- Boundary moved: the agent/session model — kenny gains a session no human is in, started
  by kenny, which can change server state without anyone approving it.
- Date: 2026-08-18

## Context and Problem Statement

Every assistant session kenny has ever run began with a person: a Discord message, a
dashboard chat, an operator answering a gate. The ticket assistant's whole authorization
model is built on that — `session_for` narrows *the acting principal's* authority, the
gate holds a consequential call for an operator and a privacy-touching one for the
affected person, and the turn cap bounds "autonomous work" meaning work between two
things a person said.

A month of the operator's own data says that model has the wrong shape for the tickets
that actually dominate. Of 38 alert-opened tickets, 26 were cancelled and 8 were never
touched; all 7 the household opened themselves were worked. The machine-opened tickets
were not asking for judgement. They were asking somebody to go and *look*.

And looking is the part the signal cannot do for itself. The most alarming finding in
that data — `disk/7`, "bad block on device `\Device\Harddisk1\DR17`", 650 occurrences,
newly appeared — names a device that does not exist on that host. Novelty, rate, volume
and fleet correlation all rank it highest; a prototype baseline scorer did exactly that.
Whether `Harddisk1` is there is not in the event and cannot be derived from it. It is
only on the machine.

So: should kenny investigate a ticket before a person is asked to, and if it does, what
may it conclude on its own?

## Considered Options

- **Leave it to the statistical layer** (per-host baselines, novelty and rate scoring).
  Rejected as sufficient: it reduces how *many* tickets appear and cannot improve what
  any one of them means. It would have ranked the phantom disk highest.
- **Investigate, but always hand the result to a person.** The safe version, and where
  this starts (`KENNY_TRIAGE_RESOLVE` defaults off). Rejected as the end state: a
  ticket per host per week that says "nothing here, here is the proof" is still a ticket
  the admin opens, and the cognitive load this exists to remove is exactly that.
- **Let the model close what it is confident about.** Rejected. A model's stated
  confidence is not calibrated, and it is the one input a wrong-but-fluent run controls
  completely. The author of this change believed the phantom disk was a dying drive and
  said so with conviction.
- **Chosen: investigate unprompted with read-only tools, and let the *server* decide
  whether the verdict may resolve the ticket — on evidence the server can see for
  itself.**

## Decision Outcome

On ticket creation, `TicketService` schedules `triage.TriageService.run` (registered via
`set_triage`, unset by default and in every test that does not ask for it). One
investigation is an ordinary ticket turn — same `TicketAssistant`, same
`toolloop.drive_events` — with a different session and a different prompt, not a second
engine.

Three controls bound it, and **none of them is the model's to observe**:

1. **What it may touch.** `TRIAGE_TOOLS` is `READ_ONLY_TOOLS - SENSITIVE_TOOLS` plus the
   verdict tool: 15 names in practice. Withholding beats refusing — a tool absent from
   `allowed_tools` is absent from the schemas, so it is never a call to gate. Both
   subtractions exist for the same reason: **the gate's two holds each wait for a human,
   and there is no human here.** A `normal_change` would hold for an operator and a
   sensitive tool for the affected person; either would park the ticket on an open gate
   nobody is coming to answer. Dropping the sensitive tools is independently right — an
   investigation nobody asked for must not read files or look at a screen.
   The narrowing is an explicit intersection, never inferred: a triage session has no
   account, hence no capability profile, and `profile_allows(None, …)` allows
   *everything*. Deriving it from the profile would hand a background turn a shell.
2. **Where it may look.** The principal is `role="user"` scoped to the ticket's frozen
   `agent_id`, with `user_id=None`. Deliberately not an operator: the exemptions written
   for a human present in the session (the turn cap; the `normal_change` gate) must not
   apply to one where nobody is.
3. **What it may conclude.** The turn ends by calling `ticket_triage_verdict` with one of
   a fixed five (`phantom`, `benign_known`, `resolved_itself`, `actionable`,
   `inconclusive`). `triage.may_resolve` then decides, in code: the verdict must be a
   closing one, the ticket must be `origin="alert"`, and **a read-only tool call must
   have actually run and actually succeeded on this ticket**.

That third condition is the load-bearing one, and it is deliberately not a confidence
score. The trail already records every tool call with its tier and whether it succeeded
(`ticket_events.tool_class`/`ok`), so "did kenny look?" is a fact the server owns.
Reasoning alone — however plausible — cannot move a ticket; only having looked can.
"Sure" is an adjective; "ran `diag_services` and it returned" is a fact.

Resolution goes to `resolved`, never `closed`, so the undo is the one already built and
tested: the reopen window (`auto_close_resolved`) and the `resolved -> in_progress`
transition any requester or operator may make. The verdict tool is therefore a
`standard_change`, not a `normal_change` — routine, reversible, low blast radius, and it
*must* run without a decision because the session has nobody in it to make one.

The prompt is separate rather than a variation. The support prompt promises "you cannot
resolve, close, cancel or reassign this ticket yourself — there is no tool for it", and
triage is the one session where that is false; two prompts keep the promise true wherever
it is made.

Two knobs, both live settings: `KENNY_TRIAGE_ENABLED` (default on) and
`KENNY_TRIAGE_RESOLVE` (default **off**). With resolve off the whole investigation runs
and records a recommendation, so the hit rate can be read off real tickets before
anything acts on it. Wiring is additionally gated on `recommend.ai_available()` — the
Anthropic client constructs happily without a key and only fails when used, so binding
triage to construction would fire one doomed investigation per ticket created.

### Consequences

- Good, because the ticket the admin opens now answers the question it used to ask. The
  phantom-disk case reads "the device this names is not on this PC" instead of
  "reliability: crit, 2318 events".
- Good, because the safety property is structural and testable without a model: the
  tools that could stall or escalate a session are not in it, and the resolve gate is a
  function over the trail. A test can assert what an investigation *cannot* do without
  predicting what a model will say.
- Good, because a triage verdict on a reliability pattern can propose a suppression rule
  — which is the manual curation the data caught the operator doing by hand (two rules,
  90 seconds apart). Proposing only: `reliability_suppression_add` is a `normal_change`
  and stays an operator's to make.
- Bad / accepted, because kenny now spends tokens on tickets nobody asked about, bounded
  by `KENNY_TRIAGE_MAX_ITERATIONS` (8) per ticket rather than by anyone's attention. A
  run that spends its budget produces no verdict at all — the ticket stays open with what
  was found, which is the honest outcome and not a disguised `inconclusive`.
- Bad / accepted, because event-log text now reaches an unprompted tool-use loop, which
  is the prompt-injection surface ADR-0023 exists for. The blast radius is bounded by
  control 1: the worst a crafted message can achieve is a wrong verdict, never a change
  to the machine. A wrong *closing* verdict additionally has to get past control 3, which
  no amount of text can satisfy on its own.
- Bad / accepted, because an alert-opened ticket can now be resolved without a person
  ever seeing it. Mitigated by `resolved` rather than `closed`, the reopen window, the
  trail row naming the verdict and its evidence, and the recommendation-only default.
- Explicitly out of scope: resolving a ticket a *person* opened. They get the analysis;
  finishing their case stays theirs. And the statistical layer (per-host baselines,
  novelty scoring) is deferred, not abandoned — after some weeks of triage it will be
  clear whether it is still needed to reduce volume or only to reduce cost.

## More Information

- [ADR-0045](0045-tiered-tool-classification.md) — the three tiers this leans on; the
  tier is a property of the tool, the gate a property of the surface, and triage is a
  surface that holds only one tier.
- [ADR-0023](0023-untrusted-agent-data-in-chat-context.md) — tool output is data, never
  instructions; the confirm-gate is the hard boundary. Read-only-only is how that
  boundary keeps holding with no operator present.
- [ADR-0046](0046-ticket-as-entity-chat-thread-as-binding.md),
  [ADR-0050](0050-the-ticket-is-its-own-chat-surface.md) — the ticket and its trail, which
  this writes into and reads its evidence from.
- [ADR-0027](0027-push-alerting-ntfy-webhook-and-weekly-digest.md) — the best-effort
  bargain triage copies: a failed investigation costs the analysis and never the ticket.
- [ADR-0041](0041-reliability-alarm-suppression.md) — the manual suppression this can
  propose an end to.

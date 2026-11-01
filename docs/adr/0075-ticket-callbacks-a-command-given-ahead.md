# 0075. Ticket callbacks: a command given ahead, judged by the server

- Status: accepted
- Boundary moved: **the agent/session model** — a command run (ADR-0073 rule 3) can now be
  armed by a person and started by the machine's return, with no person present when it
  starts. Consent in time gains a fourth case beside schedule, event and command.
- Amends: [ADR-0073](0073-steward-triggers-are-configuration.md)
- Touches: [ADR-0071](0071-stewards-purpose-bound-unattended-sessions.md),
  [ADR-0072](0072-standing-authorizations-consent-given-ahead.md),
  [ADR-0056](0056-unprompted-ticket-triage.md),
  [ADR-0067](0067-alert-delivery-is-routed-by-actionability.md)
- Date: 2026-11-01

## Context and Problem Statement

A command run needs its machine online at the moment a superuser clicks. A household PC that
is online a few percent of the time is almost never online while its admin looks at its
ticket, so its findings wait for a coincidence. The admin wants to hand the ticket to a
steward once and have the steward work it when the machine is back, then learn whether it
worked. What may such a hand-over do, how long does it bind, and who decides that it
worked?

## Considered Options

- **A general ticket fixer.** One steward that reads the ticket and does what helps.
  Rejected: an open tool set is exactly what ADR-0071 rules out.
- **An `agent_online` event trigger.** Rejected: an event is configured per steward, not
  per ticket, so nobody's consent would cover this ticket on this machine, and a machine
  that reconnects often would start runs nobody asked for.
- **Chosen: a ticket callback** — a command run armed on one ticket, started by the
  machine's next telemetry push, and judged by the alert engine.

## Decision Outcome

1. **It is a command run, given ahead.** Arming takes what a command run takes: a superuser
   signed in to the dashboard, a steward in `act` with its command trigger on, the
   effective hash the person was shown, and the ticket's machine among the steward's
   `hosts`. Whatever needs the machine present — online, its OS, the spec's preconditions,
   idle, runs in flight — is asked when the run starts. The run is started as that person,
   through the same gate, budget, caps and standing authorizations; a callback grants
   nothing a click could not.
2. **Only a ticket a server predicate can check.** An alert ticket whose subject is health
   sections of its own machine. A ticket a person wrote has no predicate, so no callback
   may claim to have fixed it.
3. **It binds narrowly and briefly.** One live callback per ticket, for 14 days and at
   most 3 runs that reached the machine. A run a cap skipped or a restart interrupted
   spends no attempt, and neither does a refusal that waiting can lift (the machine busy,
   a daily limit, the global switch). A refusal that waiting cannot lift ends it: the
   steward left `act`, its effective hash moved, its command trigger went off. A ticket
   that closes ends it. Any superuser may withdraw it; a run already started is not
   recalled.
4. **It starts on fresh data.** On a telemetry push from the machine, never on the
   connection alone.
5. **The server decides whether it worked, never the model.** After a run the server
   collects telemetry and re-evaluates the machine through the alert engine, whose recovery
   path closes the ticket. Success is the ticket's sections reading healthy on a snapshot
   received after the run ended; if the machine is gone first, its next push decides. A
   person closing the ticket while the sections still fail is not a success.
6. **People hear it from records.** Every step is a note on the ticket built from records:
   state, counts, the run's id. A callback that ends without fixing its ticket is announced
   on the alert channels as a `push`. The announcement opens no ticket and starts no
   steward (ADR-0071 rule 6).

### Consequences

- Good, because a rarely-online machine is worked when it is there, under a consent a person
  gave for this ticket and this steward, and never under one they did not.
- Good, because "fixed" means the same thing as in alerting, so the ticket closes by the path
  every alert ticket closes by.
- Bad / accepted, because a finding with no steward that may change it — Defender's
  real-time protection, without a narrow tool for it — cannot be handed over; a callback
  adds no tool.
- Bad / accepted, because an armed callback is a standing consent for up to 14 days; the
  attempt budget, the ticket's lifetime and the binding to the effective hash bound it.

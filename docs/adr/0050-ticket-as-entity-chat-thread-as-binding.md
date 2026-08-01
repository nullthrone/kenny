# 0050. The ticket is the entity; the chat thread is a binding

- Status: proposed
- Date: 2026-08-01
- Amends: [ADR-0027](0027-persistent-chat-history.md)

## Context and Problem Statement

The chat surface from ADR-0048 needs somewhere to keep a support case: what was asked, what
kenny did about it, what is still pending, and who answered which gate. The cheap answer is
the chat thread itself — the platform already stores messages, ordering and participants,
and a thread reads like a case history. The question this record answers is whether a case
**is** its conversation or merely **has** one.

A second question arrives with the same surface. A consequential call now pauses waiting
for a human who may be asleep, and the answer may come from a different surface than the
one that asked. ADR-0027 deliberately kept all transient turn state process-local — pending
calls and in-flight results are "never written to the store" — because a dashboard operator
watching a stream can simply retry. That reasoning does not survive a gate answered hours
later, or across a restart.

## Considered Options

- **The thread is the record.** The platform stores the case; kenny keeps pointers.
- **The case is an entity in kenny's store; the thread is one binding to it.** Chosen.
- **The case is an entity, and kenny keeps the verbatim conversation as its record.**

## Decision Outcome

Chosen option: **the case lives in kenny's own store, and a chat thread is one channel
bound to it.** A case arises equally from the chat surface, from the dashboard, or from an
alert; nothing in its lifecycle knows what opened it.

Thread-as-record was rejected on four counts, any one of which is disqualifying: a case
raised by an alert would have nowhere to live; retention, visibility and deletion of the
household's support history would be governed by a third party; the operator's view of a
case would depend on that party being reachable; and there would be no place to enforce
*ownership* — the check that stops one family member reading another's diagnostics. The
decoupling also keeps the platform optional: with no bot configured, case management still
works end to end.

**The record is deliberately two things, and neither is a transcript.** A machine-readable
trail records what happened — state changes, which tool ran with which arguments, who
answered which gate — and a generated paraphrase explains the case in prose. A verbatim log
of a family member's conversation is neither needed to operate the system nor wanted: the
operator needs to know what kenny *did* and why, not everything that was said. The trail is
the audit and the paraphrase is the explanation; where they disagree, the trail is the
authority. Tool arguments are recorded in the trail even though the general audit log does
not record them, because on a surface that acts without a human present, "why did this
run?" is otherwise unanswerable.

**This reverses ADR-0027's "pending state is never persisted" — for this surface only.**
A case's resume state is durable; dashboard chat sessions stay transient, and ADR-0027's
reasoning is untouched there. Persisting the *held call alone* is insufficient and was
tried: at the moment a gate opens there may already be a second gated call queued behind
it, and an unanswered tool-use block in the model's transcript. Resuming from the held call
alone would silently drop the queued one and leave the conversation structurally invalid.
The durable unit is therefore the whole settled turn, not the gate. Complementing this, at
most one gate may be open per case at a time, enforced by the store rather than by
application logic — the durable counterpart to the dashboard's single in-memory pending
slot.

The resume state is separated from the case itself so that the raw conversation kept only
to resume can be pruned on its own clock while the case and its trail survive.

### Consequences

- Good, because one lifecycle serves all three origins, and an alert can open a case
  instead of only pushing a notification.
- Good, because kenny owns retention, visibility and deletion of the record; the chat
  platform is transport — replaceable, and optional.
- Good, because an approval can be answered hours later, from either surface, across a
  restart.
- Good, because the household's conversations are not archived verbatim on kenny's disk;
  what is kept is what the operator needs to review a decision.
- Bad, because kenny now duplicates a little of what the platform already stores, and the
  two can diverge — a thread deleted on the platform, a case still open in kenny.
  Reconciliation is best-effort.
- Bad, because the human-readable half of the record is generated, so it is only as
  accurate as the model that wrote it.
- Bad, because durable resume state is a new class of stale state: a case can sit on an
  unanswered gate indefinitely unless something expires it, and expiry has to count as a
  denial rather than as a pause.
- Neutral: ADR-0027 stands as written for the dashboard. This record carves out an
  exception for a surface whose humans are not watching, it does not reverse the decision.

## More Information

- Amends [ADR-0027](0027-persistent-chat-history.md) (the process-local-transient-state
  consequence) and builds on [ADR-0009](0009-server-hosted-claude-chat.md) (the tool-use
  loop and its confirm-gate).
- Related: [ADR-0017](0017-observability-logging-and-event-store.md) — the per-case trail is
  distinct from the global event log and deliberately records more;
  [ADR-0029](0029-push-alerting-ntfy-webhook-and-weekly-digest.md) — alerts as a case
  origin; [ADR-0048](0048-delegated-identity-from-a-chat-platform.md) — the surface this
  serves.
- Storage follows the established multi-store convention (own connection, idempotent
  schema, one SQLite file) introduced in
  [ADR-0007](0007-telemetry-push-model-and-sqlite-storage.md); no new infrastructure and no
  wire-contract change.

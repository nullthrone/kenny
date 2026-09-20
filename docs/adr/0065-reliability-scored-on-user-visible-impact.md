# 0065. Reliability is scored on user-visible impact

- Status: accepted
- Boundary moved: **the observability/health model** — a `reliability` finding is no longer
  an event-log pattern graded by log severity but a user-visible impact, so the default for
  a Windows Error/Critical entry inverts from "problem candidate to be scored down" to "not
  a finding unless someone noticed something". The LLM verdict's key space gains an impact
  axis, and the alert loop gains confirmation-before-alarm and ticket reconciliation.
- Amends: [ADR-0026](0026-llm-categorization-of-reliability-events.md),
  [ADR-0041](0041-reliability-alarm-suppression.md),
  [ADR-0058](0058-time-aware-findings-and-incident-posture-split.md)
- Date: 2026-09-20

## Context and Problem Statement

On the live fleet `thomas-pc` reported `crit`. The three drivers were `Volsnap/25`, `VSS/13`
and `VSS/8193`, all emitted between 19:34 and 19:40 on one evening, all carrying
`0x8007045b` — `ERROR_SHUTDOWN_IN_PROGRESS`, which the German message states outright: *"Der
Computer wird heruntergefahren."* No `Kernel-Power/41` appears anywhere in that window, which
is Windows' own way of saying the shutdown was clean.

The event being reported as critical was: the user shut the PC down. VSS did not finish on
the way out and said so three times.

ADR-0058 had already moved this section from volume to activity-and-persistence, and the
result was still this. That is the signal that the defect is not in the thresholds.

**The premise is wrong.** The section assumed *Error/Critical in the Windows event log ⟹
problem candidate*. That is false for the overwhelming majority of entries. The log is a
record of internal component failures, most of which Windows tolerates, retries or recovers
from; `CAPI2/4176`, `DCOM/10010`, `DeviceAssociationService/3503` and the shutdown-ordering
complaints above have no human-visible counterpart at all. Better grouping, correlation and
recurrence-scoring applied to a false premise yield well-sorted false alarms, not findings.

**The vocabulary is wrong too.** The section's categories (*Disk & storage*, *Windows
service*, *Driver & hardware*) are a taxonomy of the log, and its reasons named providers,
event ids and hex codes. An operator could not tell whether a red host needed intervention
without looking each entry up — so the section delegated its own judgement back to the
person it exists to spare.

Two supporting defects made any fix on the server unimplementable:

- `by_day` keys were **local** dates while the server parsed them as **UTC**, so
  `active_days` — the input to recurrence — depended on the host's timezone.
- The agent capped event groups at 20 **sorted by count**, so on the ADR-0041 host a single
  `Kernel-Power/41` sorted below 3439 `CAPI2/4176` lines and never reached the server at
  all. Suppression cannot help with a pattern that does not arrive.

And nothing ever resolved: `crit → warn` was silent, `→ ok` emitted a recovery that
ticketing excludes by design, `tickets.sweep` never read health, and a suppression rule
touched no ticket. Every finding needed a person to die, which is the literal mechanism
behind *"it still reports red even though I suppressed it"* — the status did go quiet, the
work it had already created did not.

## Considered Options

- **Tune the thresholds again.** Rejected: this is the third attempt (volume → severity →
  activity), and each one relocated the noise rather than removing it.
- **Correlate patterns into incidents and split generic event ids by message signature.**
  Both are real improvements, and both were designed out in favour of this record first:
  they make the *unit* of a finding correct without making the *premise* correct, and under
  the impact model most of what they would correct stops being a finding at all. Held for a
  later record if measurement still asks for them.
- **Demote any pattern last seen before the current boot.** Rejected: `Kernel-Power/41`,
  `BugCheck/1001` and the VSS chain are *by definition* written before the boot that
  follows, so the rule would permanently silence 100% of crashes — precisely what this
  section is for.
- **Score `unknown` severity as nothing.** Rejected: it reverses ADR-0058's explicit trade.
  Without an API key every pattern is unclassified, so the section would be permanently `ok`
  on a keyless deployment, including on a machine with a dying disk.
- **Chosen: ask the classifier the operator's question, and let only its answer score.**

## Decision Outcome

**`user_impact` is the only field health is scored on.** The ADR-0026 classifier already
runs; it was being asked the wrong question. It now returns `user_impact` ∈
`none | degraded | crashed | data_at_risk` (plus `unknown` for "cannot tell") and a
plain-language `symptom` written from the point of view of the person at the machine.
`category`, `severity` and `cause` are still asked for and still stamped — they drive the
heatmap rows and ADR-0041's guarantee that a suppressed pattern stays fully visible with its
own verdict — but they decide nothing.

Because the verdict is a function of the question as much as of the model, the stored `model`
tag becomes `"<model>/<revision>"`, and `delete_model_except` invalidates on either changing.
A prompt edit previously invalidated nothing at all.

The verdict shape: an impact must have happened more than once before it is critical. One
unexpected restart on an otherwise healthy machine is worth saying once, and `warn` already
notifies.

This rule started with an exemption — `data_at_risk` would not wait, because the second
occurrence of data loss is the loss. Replaying the live fleet through it removed the
exemption: on `thomas-pc` the `Volsnap/25` shadow-copy cleanup reads as
*"restore points were deleted because the drive is nearly full"*, which is a fair
`data_at_risk`, and the exemption turned one such event — 33 hours old, already
self-corrected, and caused by a disk the `disk` section was reporting at 87% — straight back
into a red host. That is the failure this record exists to remove, with a better sentence
attached. A single data-risk event still warns, and so notifies; a disk that is actually
failing logs again well inside one window, with `disk_smart` carrying the hardware signal
independently of the event log.

Counts become usable again, which ADR-0041 and ADR-0058 had rejected them for. Their
objection was that a count cannot tell "3439 identical harmless lines" from "3439
individually relevant errors". Applied to a pattern whose user-visible impact is already
established, that ambiguity is gone: two unexpected restarts are two unexpected restarts. So
recurrence reads the count directly instead of approximating it from distinct days.

**A missing verdict is never a clean one.** Every group carries a `classification_state`
(`classified` / `pending` / `unavailable`, normalised to `unclassified` when no annotator
reached it), so "the model judged this harmless" and "nothing has looked at this" stop being
the same silence, and the reason line says which it is.

**A closed three-entry crash-marker set** (`Kernel-Power/41`, `BugCheck/1001`,
`WER-SystemErrorReporting/1001`) floors the impact at `crashed` whatever the classifier said
or whether it ran at all — the fallback that keeps the section working without an API key.
This is a hand-maintained table, which ADR-0026 and ADR-0041 both rejected for this space,
and the distinction is load-bearing: those rejected enumerating the *open-ended set of event
sources* in order to classify them. This enumerates the one unambiguous *event* whose absence
would otherwise be read as health. It does not grow with the fleet. Operator suppression
still overrides it, per ADR-0041's rule that explicit intent beats an automatic escalation.

**Reasons are written in symptoms** and name no provider, event id or error code. The Windows
stability index is named when it decides, so a red status is never unexplained. A ruled
section no longer carries the agent's `summary` beside the server's `reason` — the agent's
line knows nothing of suppression or classification and contradicted the verdict it sat
under.

**Findings end on their own.** A recovery resolves the ticket its alert opened, found by the
same `alert_subject.dedup_key` the open was keyed on rather than by matching prose; this is
deliberately generic, so `disk` and `win_update` close the same way. A suppression write
re-evaluates the affected hosts immediately, so the ordinary recovery transition fires
through that same path instead of waiting for the next loop pass. Sections listed in
`health_rules.CONFIRM_BEFORE_ALARM` must report the same incident on a *newer* `collected_at`
before notifying, and an escalation that cannot be sent yet is held as a candidate in a
`pending:section:*` scope rather than emitted-or-dropped — a cooldown used to swallow a warn
outright instead of delaying it.

**Wire contract (v0.19).** `by_day` keys are UTC, matching `last_seen`. Group selection
reserves slots for `level: "critical"` groups and for the most recently seen before filling
by count, up to 40, and reports `truncated_count`. New `boot_sessions` carries the boot
instants in the window, read in the same query and the same clock as the events themselves.

### Consequences

- Good, because the shutdown cluster that made the live fleet red produces nothing at all,
  and what remains red is something its owner would act on.
- Good, because every remaining message is legible without a lookup, which is the difference
  between a section that takes work off the operator and one that hands it back.
- Good, because the section stops producing work it cannot take back: the ticket its alert
  opened closes when the condition goes, including when it goes because the operator muted
  it.
- Good, because the crash-marker floor means a deployment with no API key still reports the
  one thing that unambiguously matters, instead of reporting silence as health.
- Bad / accepted, because this trades false positives for false negatives: a real problem
  with no user-visible symptom *yet* stops being a `reliability` finding. That is the
  deliberate division of labour — `disk`, `disk_smart`, `backup_status` and `win_update` own
  the non-symptomatic risks and already speak in terms an operator can act on — but it is a
  decision, not a free improvement.
- Bad / accepted, because the section now leans harder on the classifier than ADR-0058 left
  it. The crash-marker floor bounds the downside rather than removing it.
- Bad / accepted, because a verdict is still keyed `(source, event_id)`, so a generic id
  (`Application Error/1000`) carries one impact for every application that ever crashes under
  it. Splitting that key by a normalised message signature is the obvious next move and is
  deliberately not in this record.
- Bad / accepted, because confirmation-before-alarm costs `reliability` one push interval
  (~15 min) of latency on a genuine incident.
- Bad / accepted, because the first occurrence of anything is only ever a `warn`, so a
  single catastrophic event notifies without opening a ticket. The recurrence bar is what
  keeps a self-correcting one-off from going red, and it cannot be had selectively without
  reintroducing the exemption described above.
- Deliberately not done: an operator override of a wrong `user_impact`. ADR-0041 rejected
  folding that into suppression, correctly — but rejecting the conflation is not the same as
  deciding the capability should not exist.

## More Information

- [ADR-0026](0026-llm-categorization-of-reliability-events.md) — amended: the classifier is
  asked about impact, and its verdict is invalidated by a question change as well as a model
  change.
- [ADR-0041](0041-reliability-alarm-suppression.md) — amended: suppression now also overrides
  the crash-marker floor, and a rule write reconciles the alert state and the ticket it
  opened. Its raw-count and stability-index guarantees are unchanged.
- [ADR-0058](0058-time-aware-findings-and-incident-posture-split.md) — amended: activity and
  persistence still decide whether a finding is current; what makes it a finding is now
  impact rather than severity.
- [ADR-0027](0027-push-alerting-ntfy-webhook-and-weekly-digest.md) — the alert loop this
  record gives a confirmation gate and a closing path.
- Thresholds, the crash-marker set and the reason format:
  `kenny-server/kenny_server/health_rules.py`, [`docs/telemetry.md`](../telemetry.md),
  [`docs/alerting.md`](../alerting.md).

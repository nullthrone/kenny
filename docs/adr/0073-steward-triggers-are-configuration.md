# 0073. Steward triggers are configuration; a person may start one now

- Status: accepted
- Boundary moved: **the agent/session model** — what starts an unattended session is no
  longer fixed in a steward's code but chosen per install from what the code allows, and
  the consent-in-time of ADR-0072 rule 4 now has three cases instead of one.
- Amends: [ADR-0071](0071-stewards-purpose-bound-unattended-sessions.md),
  [ADR-0072](0072-standing-authorizations-consent-given-ahead.md)
- Touches: [ADR-0063](0063-the-copilot-proposes-a-ticket-it-does-not-open-one.md)
- Date: 2026-10-31

## Context and Problem Statement

A steward's trigger was part of its spec: patch, posture and config hygiene ran in their
maintenance window, triage on a new ticket. A household wants the same routine started by
what happens — a disk filling up — or by a person who asks for it now, from Ask kenny,
without a code change. ADR-0072 rule 4 bound consent in time by the window alone; a run
that starts on an event or on a click has no window to be bound by. What decides when a
steward may act, and who consents to that?

## Considered Options

- **Trigger stays code.** Rejected: every install would carry the author's idea of when.
- **Any trigger, any time, in `act`.** Rejected: an alert at 3 pm would delete files while
  the family works, and a click from any operator would spend a superuser's consent.
- **Chosen: the spec declares the ceiling, the install configures within it, and each
  trigger kind has its own consent-in-time.**

## Decision Outcome

1. **The code declares what a steward may be started by** — a schedule, named events from
   a closed set (`ticket_created`, `alert:<type>`, `alert:health:<section>`), a command —
   and whether that is editable at all. Triage is not: it runs on new tickets only.
2. **The install chooses within that ceiling.** The trigger configuration is part of the
   effective hash, like the parameters: changing it drops the steward to `shadow` and voids
   its authorizations for good, and only a superuser signed in to the dashboard changes
   it. The configuration a steward ships with leaves its hash as it was.
3. **Consent in time, by trigger.**
   - *Schedule:* bounded by the maintenance window, as before.
   - *Event:* a steward with a window acts only inside it; outside, the run happens and
     every change becomes a recommendation. A steward without a window (triage) acts.
   - *Command:* the person who clicks is the consent in time. Starting a run that may act
     needs a superuser signed in to the dashboard, the steward in `act`, its command
     trigger on, the effective hash the person was shown, and a host among the steward's
     `hosts`; an operator may start a preview. Whoever started it is recorded on the run.
   A `normal_change` still needs a standing authorization in every case.
4. **An event never cascades.** Rule 6 of ADR-0071 holds for alerts by time, not origin:
   an alert carries no provenance, so an event on a host a steward changed within the quiet
   period starts nothing and is recorded as skipped. Each event is rate-limited per
   steward and host by a cooldown, and counts against the global caps.
5. **The model never starts a run** (ADR-0063 stands). Ask kenny proposes a command run as a
   card; a person presses the button. What a finished run reports back to the
   conversation is built by the server from the run's recorded numbers, never from text a
   model wrote.

### Consequences

- Good, because when a steward may act is now a reviewed, hashed setting with a person
  behind it, not an author's default.
- Good, because a person can run a routine now and still have it bounded by the steward's
  tools, constraints, budget and authorizations.
- Bad / accepted, because a genuine alert inside the quiet period after a steward's change
  starts nothing; the skip is recorded so a person can see it.
- Bad / accepted, because toggling a trigger costs a re-promotion to `act`, as editing a
  parameter does.

# 0053. Operator-configurable rules for which events open a ticket

- Status: proposed
- Date: 2026-08-04
- Amends: [ADR-0050](0050-ticket-as-entity-chat-thread-as-binding.md)

## Context and Problem Statement

ADR-0050 gave alerts a third way to open a ticket alongside a Discord mention and the
dashboard, so a genuine alert arrives with somewhere to work it. Which alerts do that has
been hardcoded from day one — `alerting.AlertEngine._dispatch` opens a ticket for every
notification of `kind == "alert"` and for nothing else. That single predicate cannot express
what different fleets need: a family PC that is simply switched off overnight opens an
offline ticket on every cooldown window, while a new local administrator account — arguably
the most ticket-worthy thing the fleet can report — is only a `kind == "change"` notification
and never opens one at all. `docs/alerting.md` already names the gap: *"Nothing about this is
configurable per alert type today."*

The operator needs a way to say, per fleet or per host, which events should and should not
open a ticket automatically — without touching code, and without changing what "genuine
alert" means for delivery (ntfy/webhook/Discord, the events-table audit trail, and the
weekly digest all stay exactly as ADR-0029 defined them).

## Considered Options

- **A settings-catalog knob** (a boolean or CSV list in `config.py`). Rejected: this policy
  is naturally per-host and per-event-type, and a settings row is coarse (on/off for
  everything) and invisible next to the tickets it actually affects.
- **Teach `health_rules.py` a `ticketable` flag per section.** Rejected: thresholds and
  ticket policy are different concerns with different owners — `health_rules.py` is
  deliberately the *only* home of severity thresholds (`kenny-server/CLAUDE.md`), and mixing
  in "should this open a ticket" would blur that.
- **Open one ticket per escalated section instead of one per notification.** Rejected: it
  breaks the "empty config reproduces today's behavior" requirement outright — a bundled
  three-section escalation opens one ticket today, and would open three.
- **An operator-authored rule table + an in-memory matcher, consulted by the alert engine
  before it calls `open_ticket`.** Chosen — the same shape already used for reliability
  alarm suppression (ADR-0045) and the shared policy catalog's operator deny rules.

## Decision Outcome

Chosen option: an operator-authored `ticket_rules` table, mirrored in memory
(`kenny_server.ticket_rules.TicketRuleList`) and consulted by a pure function
(`ticket_rules.decide`) from `AlertEngine._dispatch`, because it lets the operator narrow or
widen ticket-opening per fleet or per host without touching code, while an **empty table
reproduces the pre-existing behavior exactly** — the table records only *deviations* from the
coded default, the same discipline `operator_policy_rules` and `reliability_suppressions`
already follow.

A rule names `(agent_id?, event_type, section?) -> decision`, where `event_type` is one of
`health` / `offline` / `disk_forecast` / `change` (the four notification producers in
`alerting.py`), `agent_id` empty means fleet-wide and `section` empty means any section, and
`decision` is one of `open_all` (always open), `open_crit` (only for a `crit`-severity
subject) or `never`. Matching is most-specific-wins — host beats fleet, a named section beats
any-section — mirroring `SuppressionList.match`'s nested-loop idiom. A notification can name
several subjects at once (a bundled health escalation, several changed inventory sections);
the first subject that resolves to "open" wins, so one notification still opens at most one
ticket, matching today's granularity.

`Notification` (`notify.py`) gained the structural discriminator this required:
`event_type: str = ""` and `sections: dict[str, str] = {}` (section name -> the severity this
notification is about, or `""` for a producer with no severity axis). Both default empty, so
every existing construction site keeps compiling, and an unlabelled notification (`event_type
== ""`) matches no rule and falls through to the legacy `kind`-based default — absence of the
discriminator *is* back-compat, not an edge case to special-case.

`recovery` and the weekly `digest` can never open a ticket, **no matter what any rule says**.
This is checked once, unconditionally, before any rule is consulted
(`ticket_rules.NEVER_TICKETED_KINDS`), because a recovery legitimately carries
`event_type="health"` and populated `sections` (the webhook payload wants that detail) and it
must never be possible for a hand-forged or mis-scoped rule to turn a recovery into a ticket.

The decision runs **after** delivery (the notifier fan-out and the `events` table insert) and
inside the same best-effort `try`/`except` that already wraps `open_ticket` — so neither a
slow nor a raising rule lookup can ever make a notification late or lost. ADR-0029's
guarantee is unchanged; this ADR only narrows *when the side effect fires*, never the
delivery it rides on.

Section names are validated **leniently**, not against a closed list: `health_rules.RULES`
covers only the sections that carry a dedicated threshold rule, but
`health_rules.evaluate_section` scores *every* section a snapshot reports a `status` for,
ruled or not (see `docs/protocol.md` § Telemetry sections for the full catalog). Rejecting an
unlisted section would block a legitimate rule for, say, `firewall` or `time_sync`; instead
`ticket_rules.KNOWN_SECTIONS` is an *advertised* vocabulary — derived from `health_rules.RULES`
∪ an explicit list of ruleless-but-scoreable sections, and from `diffs.SPECS` for `change` —
used to populate the dashboard's dropdowns and to warn (not reject) on an unrecognized
section.

Auto-ticket rules are operator-only on every surface (API, MCP, dashboard panel), including
the read path — an alert-origin ticket already has no requester and is operator-only by the
ticket API's own listing rule, so a scoped `user` has no legitimate use for the rules that
decide when one gets minted.

### Consequences

- Good, because the default behavior is unchanged for every server that configures nothing —
  the empty-table case is not a special branch, it is `ticket_rules.decide({}, ...)`, the same
  call every ruled case goes through.
- Good, because the operator can now silence the single noisiest source (an offline PC that
  is simply switched off overnight) without silencing alert delivery altogether, and can
  promote an inventory change (a new local admin account) into a ticket where today it never
  can.
- Good, because `recovery`/`digest` are structurally excluded rather than merely
  undocumented, and a seam test asserts every notification the engine actually emits carries
  a vocabulary the rule validator and the API both agree on.
- Bad, because this ADR does **not** solve ticket volume: a section flapping `warn<->crit`
  still re-opens on every escalation past the alert cooldown (`_health_transitions`'s
  crit-always-fires bypass, unrelated to this feature), and an `open_all` rule on `change`
  can raise ticket volume sharply on a large fleet. `open_crit` and a narrow section scope are
  the mitigation available today; deduplicating against an already-open ticket for the same
  `(agent_id, section)` is a real idea, deliberately deferred to its own ADR because it
  changes ticket semantics, not alerting policy.
- Bad, because an offline-alert ticket still targets a host that is, by definition,
  unreachable — this ADR makes it easy to turn off (`never` on `offline`) but does not change
  what such a ticket can do once opened.

## More Information

- Amends [ADR-0050](0050-ticket-as-entity-chat-thread-as-binding.md) (an alert can open a
  case) by making that origin's trigger operator-configurable instead of a fixed predicate.
- Constrained by [ADR-0029](0029-push-alerting-ntfy-webhook-and-weekly-digest.md): alerting
  stays best-effort; the ticket decision must never delay or drop a notification.
- Patterned on [ADR-0045](0045-reliability-alarm-suppression.md): an operator rule table +
  most-specific-wins in-memory mirror, deviation-only storage, empty-string wildcard
  sentinels instead of NULL.
- Tool tiers per [ADR-0049](0049-tiered-tool-classification.md); roles per
  [ADR-0037](0037-multi-user-authentication.md).
- Code: `kenny-server/kenny_server/ticket_rules.py`, `kenny-server/kenny_server/alerting.py`
  (`_dispatch` and the four notification producers), `kenny-server/kenny_server/notify.py`
  (`Notification.event_type`/`sections`), `kenny-server/kenny_server/store.py`
  (`TicketRuleStore`), `kenny-server/kenny_server/webui/tickets.py` (`/api/ticket-rules*`),
  `kenny-server/kenny_server/tools.py` (`ticket_rule_list`/`ticket_rule_set`/
  `ticket_rule_remove`).

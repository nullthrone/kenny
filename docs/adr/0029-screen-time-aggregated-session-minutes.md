# 0029. Screen time as aggregated whole-machine session minutes

- Status: accepted
- Date: 2026-07-02

## Context and Problem Statement

Parents operating kenny ask a simple question: *how long was this PC actually in use
this week?* kenny has no answer today. The obvious implementations are also the
invasive ones — per-user session tracking, foreground-app timing, activity
screenshots — and ADR-0024 established kenny's data-minimization stance for exactly
this kind of feature: extract the minimum identifying token, no per-user attribution,
and let the server do the judging. This ADR decides what a `screen_time` telemetry
section may and may not contain.

## Considered Options

- **Per-user session minutes.** Rejected: names the child on the wire and in the
  store; ADR-0024 deliberately avoided per-user attribution even where technically
  free, and the whole-machine number answers the parental question on
  effectively-single-user family PCs.
- **Per-app foreground time.** Rejected: an order of magnitude more invasive
  (a browsing/gaming profile per person), requires a user-session agent component
  (session-0 cannot see the foreground window; ADR-0018), and turns kenny from a
  monitoring tool into a surveillance tool.
- **Fine-grained session timestamps (start/end pairs).** Rejected: reveals *when*
  someone was at the PC at minute resolution (e.g. 02:00–04:00), which is more than
  the question needs; day-bucket totals suffice.
- **Whole-machine interactive minutes per calendar day, 7-day window.** Chosen.

## Decision Outcome

A `screen_time` telemetry section (part of the v0.10 batch, ADR-0028) reporting, for
the whole machine, **aggregated interactive minutes per calendar day** over the last
7 days — nothing else. Hard privacy rules, stated in the contract and enforced by the
payload shape: **no usernames, no per-user split, no app names or titles, and no
timestamps finer than the day bucket**; each day is clamped to [0, 1440].

The source is the Windows event log (Winlogon logon/logoff events 7001/7002, refined
by lock/unlock where readable), paired chronologically and summed per local calendar
day. The collector is stateless (ADR-0007): the 7-day window is recomputed on every
push, and the server's existing `daily_latest` history provides longer trends for
free — no new storage, no agent-side accumulation.

**No health rule judges screen time.** A number of hours is not "warn" or "crit" —
kenny reports, parents judge. The section always carries `status: "ok"` and surfaces
in the dashboard drill-down (7-day bars) and the weekly digest (ADR-0027), not in
alerts.

### Consequences

- Good, because parents get the actual answer ("21 h on the kids-PC this week") with
  the least collectable data that can produce it.
- Good, because the deliberate omissions are structural: the payload *cannot* express
  who was logged in or which apps ran, so scope creep requires a new ADR rather than
  a quiet field addition.
- Bad, because whole-machine totals blur shared PCs (parent's evening use counts
  toward the same number). Accepted: family PCs are effectively per-person in
  practice, and the alternative is per-user attribution.
- Bad, because event-log-derived minutes are approximate (sleep without logoff,
  unreadable Security log entries); good enough for a weekly picture, not for
  enforcement — and kenny deliberately has no screen-time *enforcement*.

## More Information

- Related: ADR-0024 (data-minimization precedent and "monitoring is the guarantee"
  principle), ADR-0028 (the v0.10 section batch this ships with), ADR-0018 (session-0
  isolation that rules out foreground-app timing), ADR-0027 (weekly digest surfacing).
- Contract: `docs/protocol.md` § Telemetry sections (v0.10 block, `screen_time`),
  `docs/fixtures/telemetry_snapshot.json`.
- Code: `kenny-agent/src/telemetry/collectors/screen_time.rs`,
  `kenny-server/kenny_server/webui/index.html` (drill-down renderer).

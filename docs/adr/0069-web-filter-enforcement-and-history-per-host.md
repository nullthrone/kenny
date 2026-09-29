# 0069. Web filtering is set per host on two axes: enforcement and history

- Status: accepted
- Boundary moved: **the storage and observability model of web activity.** What kenny
  gathers from a host and what it keeps is now a per-host decision the server makes and
  the agent obeys, instead of a fixed property of the telemetry pipeline: a host can be
  told not to collect at all, and the server can keep only the matches against the list.
- Amends: [ADR-0024](0024-parental-controls-web-activity-and-webfilter.md),
  [ADR-0055](0055-scheduled-web-filter-enforcement.md)
- Date: 2026-09-29

## Context and Problem Statement

ADR-0024 gave each host two booleans, `enabled` and `block_mode`. Neither governs what is
recorded. The `web_activity` collector runs on every Windows host, reads every user's
browser history as LocalSystem, and the server stores every observed domain — in
`web_activity_events` and again inside the stored telemetry snapshot — whether or not the
feature is on for that host. `enabled` only decides whether matches are flagged. A host with
parental controls "off" therefore keeps a full 30-day browsing profile, which is exactly the
record an operator switching the feature off would assume does not exist.

The obvious fix — keep only the matches — takes something away: the full profile is how an
operator discovers sites they did not know to list. Existing installations use it, and it
must stay available. The question is how to express "what does the filter do" and "what is
kept" without either losing that or collapsing two questions into one control.

## Considered Options

- **A single logging level** (`off | flagged | full`) beside the existing booleans. Rejected:
  it leaves `enabled`/`block_mode` meaning something subtly different from the level, and
  admits combinations such as "alarm without collection".
- **A single enforcement level with a fourth rung for full history.** Rejected: the two
  questions do not order. "Protect, matches only" and "Log only, full history" have no
  natural rank, so a single scale either needs five or six rungs, each saying two things at
  once, or drops a combination someone needs.
- **Two independent settings** — enforcement and history. Chosen.

## Decision Outcome

Each host has:

- **`enforcement` ∈ `off | log_only | protect`** — what the filter does. `off` matches
  nothing. `log_only` matches observed domains against the host's effective list, records
  the matches, and raises the alarm. `protect` does the same and also blocks
  (`webfilter_apply`). It replaces `enabled`/`block_mode`, which remain readable as its
  derived view (`enabled` ⇔ not `off`, `block_mode` ⇔ `protect`), so existing API callers
  keep working; a stored `block_mode` without `enabled` is `off`, as it always behaved.
- **`history` ∈ `violations | full`** — what the server keeps. `violations` stores only the
  matches: unmatched domains are dropped on insert, and the stored snapshot's `domains` is
  emptied, leaving the matches in `flagged`. `full` keeps every observed domain, as before.

**Collection follows from both.** The agent collects `web_activity` only when enforcement is
not `off` or history is `full`; under `off` + `violations` nothing is gathered. The agent is
told this over the `policy` frame's per-host `collect` map (protocol v0.20) and learns only
that boolean — never the enforcement level, never the list — so ADR-0024's split stands: the
server is the matcher, the agent a dumb collector and enforcer. The server does not trust the
gate. It discards on insert whatever it did not ask for, which covers agents predating the
field and any snapshot taken before the first `policy` frame arrived. If enrichment fails,
the snapshot is stored without `domains` rather than unfiltered.

`log_only` and `protect` both record matches: monitoring stays the guarantee and blocking
the best effort, as ADR-0024 requires. Under `protect` a recorded match therefore means the
block was circumvented, or the entry is a `watch` entry. For that to hold, the collector drops
DNS-cache entries that resolve to a sinkhole (`0.0.0.0`, `::`): Windows preloads the hosts
file into the cache, so every blocked name would otherwise read as reached.

**Migration changes nothing that is recorded.** Every host with stored web activity is
materialised with `history: full` and the enforcement level its booleans already meant. A host
heard from for the first time gets `off` + the default history — `violations` unless the live
setting `KENNY_WEBFILTER_DEFAULT_HISTORY` says otherwise — and keeps it: the default is written
into the host's configuration at first contact, so changing the setting later affects new hosts
only and never rewrites what a known host records. An update must not
silently change what is recorded in either direction; removing a capability without telling
anyone is as wrong as widening surveillance without telling anyone.

**Asymmetry.** Raising enforcement or turning history to `full` widens what is recorded, so on
the AI surfaces it goes through `webfilter_set`'s existing confirm-gate. Turning history to
`violations` purges that host's unmatched events and empties `domains` in its stored
snapshots immediately — otherwise "no browsing profile" would stay false for up to 30 days.
A level change is configuration: it surfaces as `drift` and is applied explicitly, like any
other edit. ADR-0055's schedule loop now runs only under `protect`, and a window still cannot
raise the level.

### Consequences

- Good, because a host with filtering off can now actually be unobserved, and the promise
  holds on the host, on the wire and in storage rather than only in the dashboard.
- Good, because the privacy-preserving mode (`violations`) keeps the alarm fully working —
  the alarm only ever needed the matches.
- Good, because nothing an existing installation relied on is removed, and the full profile is
  now an explicit, visible choice instead of an invisible side effect.
- Bad, because under `violations` every observed domain still crosses the wire: the agent
  holds no list, so data minimisation happens at the server's insert, not at the source. Only
  `off` + `violations` is enforced on the host.
- Bad, because existing hosts without filtering keep recording a full profile until someone
  turns it off. Accepted as the price of not changing recorded state silently; the dashboard
  shows the history setting beside the enforcement level so it is not hidden.
- Bad, because the purge rewrites that host's stored snapshots and is irreversible; the
  dashboard asks before doing it.

## More Information

- Contract: `docs/protocol.md` § `policy.collect`, § Telemetry sections (`web_activity`),
  § Versioning (`0.20`); fixture `docs/fixtures/policy_collect.json`.
- Code (server): `kenny_server/webfilter.py`, `store.py` (`WebFilterStore`,
  `TelemetryStore`), `tunnel.py` (per-host `policy` frame, insert-time filtering),
  `tools.py`, `webui/__init__.py`, `config.py`.
- Code (agent): `src/protocol.rs`, `src/tunnel.rs`,
  `src/telemetry/collectors/web_activity.rs`.

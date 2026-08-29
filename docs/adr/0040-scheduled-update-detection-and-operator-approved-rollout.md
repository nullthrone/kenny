# 0040. Scheduled update detection with a pinned, operator-approved rollout

- Status: accepted
- Date: 2026-07-23

## Context and Problem Statement

kenny already ships a complete agent self-update *mechanism*: the `agent_update` wire tool
(`{version, url, sha256}`) downloads, SHA-256-verifies, stages, swaps, and restarts the
agent binary on both Windows and Linux (ADR-0013/0034). Today it only runs when an operator
clicks "update" for one specific agent, which calls `trigger_update`
(`kenny-server/kenny_server/distribution.py:493`) — no scheduling, no fleet-wide rollout, no
awareness of which agents are behind.

Separately, `kenny-server` ships as a container published to `ghcr.io/nullthrone/kenny-server` on
every git tag (ADR-0010), and the running process already knows its own version
(`KENNY_SERVER_VERSION`, exposed at `/api/about`). There is no mechanism today to notice a
newer server image exists, and a container cannot replace its own running image from the
inside — applying a server update inherently needs help from outside the container.

The architect asked for three things: an admin-configurable check interval, installation
from GHCR, and optional automated rollout to reachable agents as they connect — for **both**
the agent fleet and the server. Clarified scope: GHCR applies only to the **server** image
(agents keep using GitHub Releases exactly as today — no wire-contract or agent code
change); the rollout model is **auto-staging, operator approval** — detection and artifact
fetch can run unattended, but no rollout happens without an explicit operator decision.

This has to be reconciled with two standing project decisions. ADR-0009 established a
project-wide confirm-gate: state-changing actions always pause for explicit operator
confirmation, never fire on their own. ADR-0034 went further and *explicitly considered and
rejected* "agent-side autonomous auto-update... polling a version source on a timer and
updating itself" (its Option C), specifically because it "introduces a second, divergent
update philosophy... and rolls updates out without operator review" — while noting it "can
be added additively later as an opt-in if a fleet ever wants it." Any redesign here must not
quietly reintroduce that rejected option wearing a scheduling hat.

## Considered Options

- **A. Detection loop + pinned, expiring, revocable rollout campaigns; server apply is
  detect-and-show-command only (no docker-socket sidecar in this iteration).** A background
  loop (mirroring the existing `_backup_loop`/alert-loop lifespan pattern) checks GitHub
  Releases for agents and GHCR tags for the server on an admin-configurable interval.
  Approving a rollout snapshots one exact artifact (`version` + `url` + `sha256`, and for the
  server a manifest digest) at approval time; only that pinned artifact is ever pushed, the
  campaign expires once satisfied or after a max age, and the operator can revoke it. Server
  apply stays a shown, digest-pinned `docker compose` command the operator runs by hand.
- **B. "Track latest" auto-rollout.** A campaign is just an on/off switch scoped to an agent
  group; whatever the detection loop currently considers "latest" is what gets pushed on
  every connect, with no per-approval snapshot. Rejected: `trigger_update` resolves whatever
  is currently cached as latest, so a release published *after* the operator's approval
  click would ship without ever having been seen — one indirection away from exactly the
  autonomous auto-update ADR-0034 already rejected, just with an extra checkbox in front of
  it.
- **C. Docker-socket sidecar (e.g. Watchtower scoped to `kenny-server`, or a small
  purpose-built helper) as the headline server-apply mechanism**, wired into an optional
  compose profile that pulls-and-recreates on operator approval. Considered and **deferred**,
  not rejected outright: docker-socket access is root-equivalent on the host, and it adds a
  new approval-signal channel (server process → sidecar) that is itself forgeable attack
  surface. For a family-scale, single-operator, single-host deployment where a server
  release lands every few weeks, that trust boundary buys roughly the time it takes to type
  two commands. The always-available floor (Option A's shown command) covers the same case
  with no new privilege. Worth revisiting, additively, the same way ADR-0034 left autonomous
  agent auto-update as a future opt-in — but not chosen now.
- **D. Do nothing beyond what exists; keep updates fully manual, per-agent, forever.**
  Rejected: it does not answer what the architect asked for (an admin-configurable interval
  and a way to roll out to a fleet as machines become reachable, which is a real operational
  need once more than a couple of agents exist).

## Decision Outcome

Chosen option: **A**, because it delivers everything asked for — a configurable check
interval, GHCR as the server's update source, and rollout to agents as they reconnect —
while keeping the confirm-gate real rather than nominal, and without adding a trust boundary
the deployment's actual scale doesn't justify.

- **Detection** is a new background loop (`update_manager.py`), started from the app
  lifespan exactly like the existing backup/alert loops, reading its cadence live from the
  runtime-settings layer (`KENNY_UPDATE_CHECK_INTERVAL_SECS`, ADR-0032; `0` disables it, a
  `restart`-lifecycle initial-delay pairs with it). Each pass refreshes the agent-release
  cache (`agent_release.py`, unchanged) to learn the latest agent version, and separately
  polls the GHCR Registry v2 tags API (read-only, anonymous or `KENNY_GITHUB_TOKEN`) for the
  server image, fetching the **manifest digest** alongside the tag — tags are mutable, so
  anything shown to the operator or ever passed to `docker compose` pins `@sha256:…`, not a
  floating tag. Non-semver tags and anything not newer than the running version are ignored;
  an unreachable or rate-limited GHCR is a skipped pass with backoff, never a downgrade
  prompt, and never fatal to the loop.
- **A rollout is a pinned campaign, not a live "track latest" toggle.** Approving a campaign
  snapshots one exact artifact — `{version, url, sha256}` for an agent rollout, a digest for
  a server rollout — at approval time into a new `UpdateCampaign` record. Every trigger under
  that campaign, whether a one-shot "update online agents now" click or an on-connect
  auto-apply, sends that snapshot and nothing else; a release the detection loop finds
  *after* approval is a new, separately-approvable candidate. This is the load-bearing
  distinction from Option B and the reason this design does not collapse into ADR-0034's
  rejected autonomous auto-update: the operator's approval names an artifact, not a
  subscription.
- **`KENNY_AGENT_ROLLOUT_ON_CONNECT`** (default **off**) gates whether an active campaign
  auto-applies to agents as they become reachable, via the existing on-connect/mark-online
  path in `registry.py`/`tunnel.py` (delayed past handshake, not fired at the connect
  instant). With it off, campaigns only apply via the explicit one-shot action — still
  useful for "push to everyone currently online," still fully operator-driven.
- **Campaigns expire and can be revoked.** A campaign auto-completes once every known agent
  is on the target version, or after a configurable max age — it never lingers as a standing
  rule that outlives the operator's attention. Revoking stops future triggers only; an
  already-in-flight `agent_update` call (bounded by its own request timeout and nonce TTL)
  cannot be recalled, the same kind of can't-undo boundary ADR-0039 already named for
  in-flight backup pushes.
- **A per-agent attempt budget prevents error-loops.** `agent_update` is a mutating tool
  gated by the local kill-switch (ADR-0011): an agent with remote control off will refuse it
  on every reconnect, and a bad release that crashes on startup reconnects on the old binary
  and would otherwise retrigger forever. Each agent gets a bounded number of attempts (with
  backoff) under a campaign; exhausting it marks the agent **held**, surfaced in the
  dashboard, with no further auto-trigger under that campaign. An anti-cheat `paused`
  response (ADR-0035) is treated as "retry later," not counted against the budget, since it
  is expected to clear on its own; `disabled`/`blocked` responses do count against it.
- **Server apply is detect-and-show-command only in this iteration (Option A, not C).** The
  Updates dashboard tab shows the current vs. latest server version and the exact,
  digest-pinned `docker compose pull && docker compose up -d` for the operator to run by
  hand — the same graceful-degradation shape ADR-0015 already uses for agent-binary
  auto-fetch. After the operator applies it, the new image's baked `KENNY_SERVER_VERSION`
  makes `/api/about` report the new version, closing the loop. The docker-socket sidecar
  (Option C) is recorded as a deferred, additive follow-up, not built now.
- `trigger_update` gains an optional server-internal `version` parameter so campaign code can
  pin an artifact instead of always resolving "latest"; the existing manual per-agent button
  keeps its current "resolve latest" behavior when the parameter is omitted. The wire frame
  is unchanged (`{version, url, sha256}` already exists) — **no `PROTOCOL_VERSION` bump, no
  `docs/protocol.md` or fixture change.**

### Consequences

- Good, because the architect's ask is met on both axes — a configurable interval and
  rollout to agents as they reconnect — without silently reversing ADR-0009's confirm-gate or
  reopening the autonomous auto-update ADR-0034 deliberately closed.
- Good, because campaign pinning + expiry + revoke + attempt-budget make the operator's
  approval bind to one concrete artifact with a bounded blast radius, not an open-ended
  subscription — the gate is real, not a rubber checkbox in front of the old behavior.
- Good, because nothing here touches the wire contract, the agent's own update logic, or
  Windows/Linux dispatch — every mechanism reused (`agent_update`, `trigger_update`,
  `agent_release.py`, the settings layer, the lifespan-loop idiom) is unchanged in shape.
- Good, because the server side adds no new trust boundary: GHCR polling is read-only
  metadata plus a digest, and applying a server update still requires the operator to run a
  command on the host — nothing here can pull-and-recreate the server container by itself.
- Bad, because server self-update is not actually automated in this iteration — the operator
  still runs a command by hand. Accepted: the docker-socket sidecar that would close that gap
  is a real, root-equivalent trust boundary this family-scale, single-host deployment does
  not need yet, and is left as a named, additive follow-up rather than built speculatively.
- Bad, because an in-flight `agent_update` triggered by a since-revoked campaign cannot be
  recalled — the operator must understand revoke means "no more," not "undo."
- Bad, because a held agent (attempt budget exhausted) needs an operator to notice and act —
  no channel wiring to push-alerting (ADR-0027) is built in this iteration; the dashboard
  state is the only signal for now.

## More Information

- Builds on: ADR-0013 (agent self-update mechanism), ADR-0015 (agent binary auto-fetch and
  version resolution), ADR-0032 (runtime settings — interval/GHCR-ref knobs), ADR-0034
  (Linux distribution + self-update — the ADR whose rejected Option C this decision must not
  reintroduce), ADR-0011 (local kill-switch — source of the attempt-budget requirement),
  ADR-0035 (anti-cheat pause — source of the retry-without-penalty requirement), ADR-0039
  (backup/restore — precedent for naming a new trust boundary and for a can't-undo
  in-flight-operation caveat), ADR-0009 (confirm-gate principle), ADR-0010 (GHCR
  containerization).
- Implementation (planned): `kenny-server/kenny_server/update_manager.py` (new detection
  loop), `config.py` ("Updates" settings group), `store.py` (`UpdateStore`/`UpdateCampaign`),
  `distribution.py` (`trigger_update` pinning, campaign approve/revoke/list routes),
  `registry.py`/`tunnel.py` (on-connect campaign hook), `webui/` (`/api/updates*` + Updates
  tab). No agent-side change; no `docs/protocol.md`/fixture change.
- Deferred, additive follow-up (not built now): an optional off-by-default docker-socket
  sidecar (compose profile) for automated server apply (Option C above); alerting
  (ADR-0027) wiring for held agents and failed detection passes.
- A campaign can also be **suspended** (`active → suspended`) and later **resumed**; unlike
  revoke it keeps the pinned artifacts and the per-agent attempt/held bookkeeping, since
  revoke-then-recreate would hand a held agent a fresh attempt budget under a new `campaign_id`.

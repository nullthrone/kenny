# 0024. Parental controls: web-activity observability and on-demand web filtering

- Status: accepted
- Date: 2026-07-02

## Context and Problem Statement

kenny is operated in a family setting. A recurring need is **Jugendschutz** (child
protection): a parent wants to *see* which sites a child's PC has been reaching and, on
demand, *block* a set of known-harmful domains — with a list that ships pre-populated,
stays editable, and can reference a maintained external source. Nothing in kenny touches
web activity, browsing, DNS content, or content filtering today, so this is greenfield and
crosses the wire contract: it needs a new telemetry section (observability), new capability
tools (blocking), a per-host editable list with a server-side store, and a dashboard
surface.

Three properties shape the design:

1. **Observability must not depend on blocking.** A child with local admin rights can edit
   the hosts file or a registry policy; a VPN or portable browser defeats host-level
   blocking entirely. So the **alarm** (a parent finding out a listed site was reached) has
   to work *regardless* of whether blocking is applied or was circumvented. That makes the
   telemetry + server-side matching the primary control and blocking a best-effort add-on.
2. **DNS-over-HTTPS is the blind spot.** Modern Chrome/Edge/Firefox resolve names over DoH,
   bypassing both the OS DNS client cache (our cheapest observation source) and hosts-file
   blocking. Any serious version of this feature must both *observe* through a second source
   (browser history) and, when blocking, *disable DoH* via browser policy.
3. **The existing safety architecture must absorb this, not be worked around.** The local
   kill switch ([ADR-0011](0011-local-remote-control-kill-switch.md)), the confirm-gate
   ([ADR-0009](0009-server-hosted-claude-chat.md)), and the deterministic guard
   ([ADR-0019](0019-agent-side-deterministic-tool-guard.md)) all already gate *mutating*
   tools by name. Blocking should ride those, not add a new bypass.

## Considered Options

- **Observation source: DNS client cache only / browser history only / both (chosen).**
  The DNS cache (`Get-DnsClientCache`) is cheap and catches every app, but is blind to DoH
  and short-lived. Browser history (`History` for Chromium, `places.sqlite` for Firefox)
  catches DoH-resolved visits and carries timestamps for a real 24 h window, but only sees
  browsers and requires reading per-user profile DBs. Together they cover each other's gaps;
  chosen. Deferred: the DNS-Client **operational event log**, which must be explicitly
  enabled and is high-volume — a later addition if the two chosen sources prove insufficient.
- **List push mechanism: new `policy`-style frame / capability tool call (chosen).** A frame
  is fire-and-forget with no ack and would either bypass the kill switch or need new gating.
  A `request`/`response` tool call gives an ack + applied-state (for drift display), reuses
  the audit log and confirm-gate, and — being *mutating* — is refused with `disabled` under
  the kill switch for free. Chosen.
- **24 h rolling window: agent-side accumulation / server-side accumulation (chosen).** The
  collector registry relies on collectors being stateless `fn()`s isolated by `catch_unwind`;
  a ring buffer in the agent would break that invariant and be lost on restart. The server
  already owns a retained store. The agent reports what is observable *now*; the server
  dedups and accumulates. Chosen.
- **Blocking: hosts file / DNS sinkhole / proxy / hosts + DoH-off + bypass category (chosen).**
  A local DNS sinkhole or proxy is a much larger, always-on component to secure and deploy. A
  hosts-file block plus registry policies that disable browser DoH is simple, reversible, and
  needs no new transport; adding a **bypass-protection** category (VPN/proxy/DoH-provider
  domains) raises the effort to circumvent. Chosen, with eyes open about its limits (below).
- **External source: none (seed only) / StevenBlack porn-only hosts (chosen) + hagezi bypass.**
  A hand-maintained seed alone goes stale. StevenBlack's `porn-only` hosts list is MIT,
  hosts-format, curated from several sources, and stably hosted; used server-side for alarm
  matching and, capped, for agent blocking. The hagezi `doh-vpn-proxy-bypass` list backs the
  bypass category. Both are fetched and cached server-side with a seed fallback.

## Decision Outcome

Chosen: **a `web_activity` telemetry section for observability plus `webfilter_*` capability
tools for on-demand blocking, driven by a per-host editable list held server-side, with the
server as the authoritative matcher and the agent a dumb, idempotent enforcer.**
`PROTOCOL_VERSION` bumps `0.8 → 0.9` (additive: one section, three tools, no frame changes).

- **Observation — `web_activity` section.** The agent collector reads the DNS client cache
  and each user's browser history (Chromium `History`, Firefox `places.sqlite`; copied to a
  temp file first to dodge SQLite locks, opened read-only), extracts **host names only**,
  merges/dedups into a bounded list (cap 250, `truncated` flag) covering the last 24 h, and
  reports `status: "ok"` — it has no list and does not judge. Off Windows it returns the
  standard `n/a on this platform` stub. Browser **credential** stores stay protected by the
  existing `fs_read` deny rules; history DBs are distinct files read only inside the collector.
- **Alarm — server-side matching, thresholds in `health_rules.py`.** On telemetry insert the
  server matches observed domains (suffix match, so `sub.bad.example` hits `bad.example`)
  against that host's **effective list** and annotates the *stored* payload with a `flagged`
  array (domain, category, matched entry, timestamps). The `web_activity` health rule reads
  `flagged` and decides `warn`/`crit`; thresholds stay only in `health_rules.py`, per the
  telemetry contract. The `flagged` annotation is **server-internal and never on the wire**.
- **List model — layered, per host.** A built-in **seed** of well-known adult domains ships
  with the server (read-only, upgradable), external sources (StevenBlack adult, hagezi bypass)
  are referenced read-only and toggled per host, and per-host **custom** entries
  (`watch` | `block` | `allow`) layer on top; an `allow` entry overrides a block on an
  equal-or-more-specific suffix. This mirrors the ADR-0020 split of a shared catalog vs.
  operator additions. Each host independently turns the feature on/off and toggles blocking.
- **Blocking — `webfilter_*` tools.** The server pre-merges the effective block set (custom
  blocks + seed + capped external adult extract + bypass domains − allows) into a flat domain
  list and calls:
  - `webfilter_status` (`{}`) — **read-only**, works under the kill switch: reports the
    applied block count, a `list_hash` for drift detection, and current browser DoH policy.
  - `webfilter_apply` (`{domains, doh_policy, list_hash}`) — **mutating**: writes a
    marker-delimited block to the hosts file (`0.0.0.0`, hard cap 10 000, atomic replace),
    optionally sets registry policies disabling DoH in Chrome/Edge/Firefox, and flushes DNS.
  - `webfilter_clear` (`{}`) — **mutating**: removes only kenny's marker block and the
    kenny-written policy values.
  The agent stays dumb and idempotent (no bypass logic on the wire) and **refuses** any list
  that would blackhole self-protected names (localhost, the configured server host, core
  Microsoft-update infrastructure) with `blocked`, so a bad list can never sever the tunnel
  or OS updates.
- **Kill-switch interplay (honest limitation).** `webfilter_apply`/`clear` are classified
  mutating in `control::is_mutating`, so when the person at the PC switches remote control off
  they are refused with `disabled` — **the child can locally refuse *new* rules.** Rules
  already written to the hosts file persist until cleared, and `web_activity` telemetry keeps
  flowing regardless. This is by design: monitoring is the guarantee, blocking is best-effort.
- **External sources.** The server fetches StevenBlack `porn-only` hosts and the hagezi
  `doh-vpn-proxy-bypass` list on a timer (default 24 h), with size guards, a write-through
  disk cache, and a seed-only fallback when offline. URLs are environment-overridable.
- **Portability.** The collector's parsing/merging core and the handler's hosts-splicing and
  validation core are `#[cfg(windows)]`-free and unit-tested on Linux CI; only the OS probes
  (DNS cache, registry, real hosts path) are Windows-gated. Off Windows the tools return
  `unsupported`.

### Consequences

- Good: a parent gets a real alarm the next snapshot after a listed site is reached, whether
  or not blocking is on or was bypassed — the primary goal. Blocking is a reversible,
  transport-free add-on that also closes the DoH hole for the common (non-adversarial) case.
- Good: the feature reuses the telemetry pipeline, the health-rule pattern, the ADR-0020 list
  model, the kill switch, and the confirm-gate rather than inventing parallel machinery. The
  first editable-list UI in the dashboard establishes a reusable custom-section renderer.
- Bad / trade-offs: host-level blocking is defeated by admin rights, a VPN, or a portable
  browser; we state this plainly in the UI and rely on the alarm, not the block. Flags are
  computed at insert time, so editing the list re-flags only from the next snapshot (~15 min);
  the activity API matches live so the detail view is always current. Reading all users'
  browser profiles as LocalSystem is a real capability — mitigated by taking **host names
  only**, never URLs, titles, or per-user attribution, with 30-day retention and operator-token
  gating. A large hosts file can slow the Windows DNS Client service, so the block list is
  capped (10 000 hard, ~5 000 default external extract).
- Privacy stance: only **domains** cross the wire and are stored — never full URLs, page
  titles, form data, or which user visited them. Retention matches the rest of telemetry
  (30 days). The data is visible only to the operator behind the operator token (ADR-0008),
  and — like all agent-returned data placed in the chat context — is treated as untrusted
  ([ADR-0023](0023-untrusted-agent-data-in-chat-context.md)).

## More Information

- Contract: `docs/protocol.md` § Tool catalog, § Telemetry sections, § Versioning
  (`PROTOCOL_VERSION = "0.9"`); fixtures `docs/fixtures/request|response_webfilter_{status,apply,clear}.json`
  and the `web_activity` section in `docs/fixtures/telemetry_snapshot.json`.
- Code (server): `kenny-server/kenny_server/webfilter.py`, `store.py` (`WebFilterStore`),
  `tunnel.py` (insert-time enrichment), `health_rules.py` (`web_activity` rule),
  `webui/__init__.py` (webfilter API), `tools.py` + `chat.py` (MCP tools),
  `data/webfilter_seed.json`.
- Code (agent): `kenny-agent/src/telemetry/collectors/web_activity.rs`,
  `kenny-agent/src/handlers/webfilter.rs`, `src/dispatch.rs`, `src/control.rs`.
- Extended by [ADR-0055](0055-scheduled-web-filter-enforcement.md): named category layers, a
  per-host time schedule, and bypass requests as tickets. It leaves this ADR's server/agent
  split exactly where it is — the server is still the authoritative matcher, the agent still
  a dumb enforcer receiving one flat `domains` list with no clock and no category — and moves
  the *authorization* boundary instead, to admit a standing rule the server enacts unattended.
- Related: [ADR-0007](0007-telemetry-push-model-and-sqlite-storage.md) (telemetry push + store),
  [ADR-0011](0011-local-remote-control-kill-switch.md) (kill switch),
  [ADR-0019](0019-agent-side-deterministic-tool-guard.md) (safety guard),
  [ADR-0020](0020-shared-policy-catalog-operator-rules-and-server-mirror.md) (layered list model),
  [ADR-0023](0023-untrusted-agent-data-in-chat-context.md) (untrusted agent data).

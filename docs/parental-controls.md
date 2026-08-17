# Parental controls

kenny has an **optional** parental-awareness layer for the family PCs you administer. It
can observe which **domains** a PC has been reaching, match them against a per-host list,
and — on demand — block a set of known-harmful domains. There are two features:

- **Web activity + web filter** — see (and optionally block) the domains a PC visited.
- **Screen time** — how many interactive minutes per day a PC was actually in use.

!!! warning "Consent and scope"
    This is for machines **you** administer, in a family setting, with the knowledge and
    consent of the people who use them. kenny takes **host names only** — never full URLs,
    page titles, form data, or which user visited. Data lives behind the operator token,
    is treated as untrusted in chat context, and is retained like the rest of telemetry
    (~30 days). See [ADR-0024](adr/0024-parental-controls-web-activity-and-webfilter.md).

## The server matches, the agent enforces

The **server** holds each host's config and lists and does all the matching and
classification. The **agent** stays dumb: its collector reports the domains it can observe
right now (from the OS DNS client cache and each user's browser history), and — only when
**block mode** is on — it applies a single flat block list idempotently (a marker-delimited
hosts-file block plus optional browser DoH-off), refusing any list that would blackhole
self-protected names. No matching logic ever rides the wire.

!!! note "Monitoring is the guarantee, blocking is best-effort"
    Observability does not depend on blocking. A parent gets an **alarm** the next snapshot
    after a listed site is reached, whether or not blocking was on or was bypassed
    (admin rights, a VPN, or a portable browser defeat host-level blocking). See
    [ADR-0024](adr/0024-parental-controls-web-activity-and-webfilter.md).

## Categories and precedence

Each host's **effective list** is layered. The layers are a fixed catalog of named
categories, each **external** (backed by a maintained upstream list, fetched over HTTPS) or
**local** (no upstream — it contributes only this PC's own custom entries tagged with it):

| Category | Kind | Notes |
|----------|------|-------|
| `adult` | external | the StevenBlack porn-only hosts list. Driven by the `use_external_adult` toggle. |
| `bypass` | external | a hagezi DoH / VPN / proxy bypass list. Driven by the `use_bypass_protection` toggle. Uncapped — see [Over-cap state](#over-cap-state). |
| `gambling` | external | a hagezi gambling-domains list |
| `piracy` | external | a blocklistproject piracy/torrent list |
| `social`, `gaming`, `streaming`, `shopping`, `chat` | local | no upstream list — each gathers only the custom entries this PC has tagged with it |

`adult` and `bypass` predate the catalog and keep their own boolean columns
(`use_external_adult`, `use_bypass_protection`) as the source of truth for those two
categories specifically — nothing else. Every other category lives in the config's
`categories` list. The two representations are merged server-side into one canonical
`categories` field on every read, so there is one list an operator or a schedule window
reasons about, not two that could drift apart.

A host also has its own **custom** entries (`watch` / `block` / `allow`), and a **seed**
layer — a shipped, read-only list of well-known adult domains that always contributes,
regardless of which categories are turned on. When the same domain comes from several
layers, the highest-priority provenance wins for display purposes:

| Provenance | Priority |
|------------|----------|
| `custom` | highest |
| `seed` | |
| any external category (`external_adult`, `bypass`, `gambling`, …) | lowest |

Matching is a **suffix match**: `sub.bad.example` hits an entry for `bad.example`. When
several block entries match, the **most specific (longest) block wins**. A custom `allow`
entry overrides a block only when it is **equal-or-more-specific** — a broader allow does
not unblock a narrower block. Use `allow` for exceptions (e.g. a homework or reference
site that would otherwise be caught by a broad list).

Only `block` entries (and the enabled categories) are enforced on the agent; `watch`
entries are matchable for the alarm but never blocked.

The dashboard's per-host [Web filter section](dashboard.md#web-filter) edits the whole
catalog: every category is one row, including `adult` and `bypass`, so there is a single
control per category rather than a legacy toggle competing with a catalog entry. The
schedule and pending bypass requests live in the same panel. The same state is reachable
through the API (`GET`/`PUT /api/agent/{id}/webfilter/config`, `categories: [...]`) and the
`webfilter_get` / `webfilter_set` MCP tools.

The panel leads with the filter's current state rather than making you infer it from the
window table: whether the stricter list is in force right now, when it reverts, and — if
the effective list has outgrown the agent's cap — that the block did not go out, that
matching continues, and which category to turn off.

## Schedule

A host can carry a **schedule**: one or more recurring per-host windows (e.g. "21:00–07:00,
Monday through Friday, add `social` and `gaming`"). A window names its weekdays, a
`HH:MM`–`HH:MM` start/end range, one or more extra categories, and an IANA timezone
(defaulting to the server's own `TZ`, else UTC).

A schedule can only make the filter **stricter for its duration**. A window **adds**
categories on top of whatever the host's own toggles already have; it never removes one,
and it cannot turn the feature or block mode on by itself — both still have to be on for
anything to be pushed. Authoring an enabled window is what opts a host into unattended
pushes: a host with no enabled window is never touched by the schedule loop. Deleting the
last window does not trigger an unattended revert to the base list either — the host simply
leaves the loop's fleet and shows up as ordinary `drift` until someone applies again.

The schedule is entirely server-side. The agent still receives one flat `domains` list and
has no clock and no notion of a category or a window — reverting at the end of a window uses
the exact same push as tightening at its start. A background loop checks each scheduled
host and pushes only when the list the schedule computes for *right now* differs from what
was last applied, so a pass over an unchanged fleet costs nothing on the wire. Set
`KENNY_WEBFILTER_SCHEDULE_SECS=0` to disable the loop entirely.

The schedule **only ever adds a category to what already blocks**. It is not screen-time
enforcement: it does not limit how long a PC may be used, does not lock anyone out, and
carries no concept of a session or a time budget. kenny deliberately has no screen-time
enforcement at all — see [ADR-0029](adr/0029-screen-time-aggregated-session-minutes.md).

The rationale for making this a standing, unattended rule — where kenny had previously
never changed a machine's state without a human present — is recorded in
[ADR-0055](adr/0055-scheduled-web-filter-enforcement.md); read it there rather than here.

## Bypass requests

A child can ask to have a domain unblocked. A bypass request is not a separate entity —
**it is a ticket**, opened with category `web_filter`. The requester is the child, and the
decision goes through the ticket's existing operator-approval gate exactly like any other
ticket step; there is no second pending-request queue with its own lifecycle to keep in
sync with the first.

Granting one is the operator's ordinary web-filter action: `webfilter_set(..., add_domain=…,
action="allow")` followed by a push. Nothing about granting a bypass request is special —
it is the same allow-domain call an operator would make for any other exception.

Not to be confused with the `bypass` **category** above, which blocks VPN/proxy/DoH domains
to stop the filter being circumvented — a bypass *request* asks permission; the `bypass`
*category* refuses to be evaded. They point in opposite directions.

## Over-cap state

The agent enforces a hard cap of **10,000 domains** on the list it will accept. Turning on
more categories, or a schedule window adding one, can push a host's effective block list
past that cap. When it does, the server **refuses to push the list rather than truncating
it**. A silently trimmed list is a filter the operator believes is complete and is not; a
push the server knows the agent will reject with `bad_args` teaches the operator nothing a
clear refusal doesn't teach faster.

This is a real, visible operational state, not a hidden failure:

- The host's own web-filter overview still reads normally and reports the state:
  `oversize: {count, cap, over_by}` alongside the usual config. It reports which category to
  turn off, not just that something is wrong.
- A manual push returns `list_too_large` (400) with the count and the cap; the
  `webfilter_push` MCP tool raises `bad_args`; the schedule loop skips the host and logs it
  rather than failing the whole pass.
- **Matching is unaffected.** The over-cap list still flags domains for the alarm — the
  filter's primary control — even while it cannot be enforced. Monitoring does not stop
  because blocking cannot proceed.
- **The remedy**: turn off a category, add `allow` entries to narrow the effective list, or
  lower `KENNY_WEBFILTER_MAX_BLOCK_DOMAINS` (the soft cap the capped categories share — see
  [External lists](#external-lists)).

## Health signal

When the feature is enabled for a host, the server annotates each snapshot with the domains
that matched, and the `web_activity` health rule escalates the PC:

| Condition (last 24h) | Status |
|----------------------|--------|
| a serious flagged hit — `custom`, `seed`, or `external_adult` | 🔴 `crit` |
| a `bypass` hit | 🟡 `warn` |
| nothing flagged | 🟢 `ok` |

If the host is not configured for parental controls, the rule simply defers. The flagged
hits appear in the section detail with **domain, category, matched entry, and last seen**.

## The list editor

Open a PC's [host page](dashboard.md#the-host-page), then click its **Web filter** card or
checklist entry to open the section modal.

![The Web filter section modal on a host page — flagged domains, observed domains, and the per-host parental-controls list editor.](assets/screenshots/host.png)

The modal shows three things:

1. **Flagged** — domains that matched this PC's list (domain, category, matched entry, last seen).
2. **Observed domains (24h)** — everything the agent saw, with hit counts and sources.
3. The per-host **parental-controls list editor**.

The editor's toggles:

| Toggle | Config field | Effect |
|--------|-------------|--------|
| monitor this PC | `enabled` | observe web activity and match it against this list |
| block listed sites | `block_mode` | push the block list to the agent (hosts file + DoH off) |
| use adult blocklist | `use_external_adult` | reference the StevenBlack porn-only list |
| block VPN/proxy bypass | `use_bypass_protection` | also block DoH / VPN / proxy domains |
| disable browser DoH | `doh_policy` (`disable` / `leave`) | turn DNS-over-HTTPS off in browsers so the hosts block can't be bypassed |

Below the toggles you can **add a custom domain** with an action — **block**, **watch
(alarm only)**, or **allow (exception)** — remove entries, and press **apply now** to push
the current block set to the agent.

!!! note "Drift and the kill switch"
    The panel shows **drift** ("list changed since last apply") when the effective list has
    changed since the last successful push — a reminder to press **apply now**. If the local
    kill-switch is off at the PC, the agent **refuses** to apply new rules (the apply comes
    back `disabled`), and the panel says so — but **monitoring keeps working** and any rules
    already written to the hosts file persist until cleared.

**Defaults:** `use_external_adult` is **on** and `doh_policy` is **disable**. Editing the
list re-flags from the next snapshot (~15 min); the detail view's activity list matches live,
so it is always current.

## External lists

The server fetches every **external** category's source (`adult`, `bypass`, `gambling`,
`piracy`) over HTTPS on a timer (default **every 24h**), with a write-through disk cache and
a **seed/stale fallback** when offline, guarded by size caps against an oversized upstream.

The **capped** categories (`adult`, `gambling`, `piracy`) share one budget pushed to the
agent — **default 5000** domains, **hard cap 10000** — so turning on a second content
category does not silently double what gets pushed. **`bypass` is deliberately uncapped**:
it is the layer that stops the filter being circumvented, so silently dropping half of it
would defeat its purpose.

The URLs, refresh interval, and cap are environment-overridable — see [`setup.md`](setup.md):

- `KENNY_WEBFILTER_REFRESH_SECS`
- `KENNY_WEBFILTER_ADULT_URL`
- `KENNY_WEBFILTER_BYPASS_URL`
- `KENNY_WEBFILTER_MAX_BLOCK_DOMAINS`

The local categories (`social`, `gaming`, `streaming`, `shopping`, `chat`) have no source
URL and nothing to fetch — they carry only the custom entries a host has tagged with them.

## Screen time

The `screen_time` section reports, for the **whole machine**, aggregated **interactive
minutes per calendar day** over the last 7 days — and deliberately nothing finer. There is
**no per-app, per-window, or per-user tracking** and no timestamps below the day bucket; the
payload shape structurally cannot express who was logged in or what ran.

It appears as horizontal per-day bars in the `screen_time` section detail and is summarized
in the **weekly digest**. No health rule judges it — the section is always `ok`; kenny
reports, parents judge. See [ADR-0029](adr/0029-screen-time-aggregated-session-minutes.md).

## Driving it from Ask kenny

The [Ask kenny overlay](dashboard.md#ask-kenny) — scoped to the host when opened from its
page — and any MCP client can drive parental controls too. The server-only tools:

| Tool | Args | Changes state? |
|------|------|----------------|
| `webfilter_get` | `id` | read-only — includes the category catalog and schedule state |
| `web_activity_query` | `id`, `hours?`, `flagged_only?` | read-only |
| `webfilter_set` | `id`, plus config toggles / `categories` / `add_domain` / `remove_domain` / `window_*` / `remove_window` | ✅ |
| `webfilter_push` | `id` | ✅ |

`webfilter_set`'s `window_days`/`window_start`/`window_end`/`window_categories` (with
optional `window_label`/`window_tz`) add one schedule window; `remove_window` takes a
window id. `webfilter_push` forwards the mutating **`webfilter_apply`** /
**`webfilter_clear`** calls to the agent (refused with `disabled` under the kill switch),
and returns `bad_args` when the effective list is over the agent's cap — see
[Over-cap state](#over-cap-state). See [`tools.md`](tools.md).

## See also

- [`user-guide.md`](user-guide.md) — the operator's tour of Fleet and the host page.
- [`dashboard.md`](dashboard.md) — Fleet, the host page, and the Log page.
- [`telemetry.md`](telemetry.md) — how sections, collectors, and health rules fit together.
- [`alerting.md`](alerting.md) — how `warn` / `crit` surface and the weekly digest.
- [ADR-0024](adr/0024-parental-controls-web-activity-and-webfilter.md) — web activity + web filter.
- [ADR-0029](adr/0029-screen-time-aggregated-session-minutes.md) — screen time.
- [ADR-0055](adr/0055-scheduled-web-filter-enforcement.md) — scheduled web-filter enforcement.

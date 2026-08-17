# View → endpoint coverage map

The redesign consolidates eleven destinations into five. The old dashboard makes **106
distinct HTTP calls across 20 functional areas**, and the original requirement was that
*all functionality is preserved*. This file assigns every one of those calls to a view in
the new information architecture, so that "the redesign is done" means the same thing as
"nothing was dropped".

Source of truth for the old behaviour: `notes/api-contract-actual.md`.
Source of truth for the new response shapes: `kenny-web/src/api/types.ts`.

A call listed here is that view's responsibility. If a view agent finds a call in the
archaeology report that is not listed here, that is a gap in this map — report it rather
than silently leaving the capability behind.

---

## Global shell (owned by the scaffold, used everywhere)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/me` | Identity, role, host scope. Fetched before the first render. |
| GET | `/api/users/directory` | Resolves user ids to names. Operator+ only. |
| GET | `/api/about` | Server/protocol version for the sidebar version line and About. |
| GET | `/api/changelog` | Release notes in About. |
| GET | `/api/agent-binary` | Whether an installer is available; drives the Fleet banner. |
| POST | `/api/agent-binary/fetch` | "Retry GitHub fetch" banner action. |

`/login` and `/logout` stay **server-rendered**, outside the SPA. Logout is a plain link,
not a fetch. A 401 from any call is a full-page navigation to `/login`.

---

## Today — `#/today`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/today` | **New.** The whole view. Ranked items capped at three, donut, 30-day trend, KPIs, verdict sentence. |

An empty `items` array is the "all quiet" state — a designed, first-class outcome, not an
empty-state placeholder. The old Overview's cache-first behaviour (repaint from cache, refetch
only after a 15s staleness window, keep showing stale data on a failed refetch rather than
erroring) is worth preserving as React Query `staleTime`.

---

## Fleet — `#/fleet`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/fleet` | The card grid. Needs the new `severity_label`. |
| GET | `/api/agent-binary` | Installer availability for the Add-a-PC wizard. |
| POST | `/api/agents/share-link` | **Changed** — body-based, single-use, 24h. Wizard step 3. |
| GET | `/api/agents/{id}/installer?arch=` | Direct browser navigation, not a fetch. |

---

## Host — `#/fleet/:host`

The prototype shows problem cards, a healthy checklist, and a section modal. Everything the
old three-pane agent detail could do lives here; the panels that were always-visible sidebars
become section modals reached from the section they belong to.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/agent/{id}` | Health, sections, meta, raw snapshot, governance. Needs the new per-section `attention` flag. |
| POST | `/api/agent/{id}/refresh` | "Refresh" action. |
| POST | `/api/agent/{id}/remotehelp` | "Remote help" action. |
| POST | `/api/agent/{id}/screenshot` | "Recapture" on the screenshot card. |
| GET | `/api/agent/{id}/screenshot?t=` | The image itself. Cache-busted on every render; `onerror` collapses to "none yet". |
| PUT | `/api/agent/{id}/channel` | Update-channel selector. |
| POST | `/api/agents/{id}/update` | "Update agent" action. |
| DELETE | `/api/agent/{id}` | "Remove" action. Destructive — confirm first. |
| POST | `/api/forecast/stream` | SSE. Fills the inverse-ink Forecast panel. |
| POST | `/api/recommendation/stream` | SSE. Fills the section modal's Recommendation block. Emits an extra `remediation` event carrying a prompt; that is what "Fix via Ask kenny" hands to the chat drawer. |
| GET | `/api/agent/{id}/webfilter` | Web filter section modal. |
| PUT | `/api/agent/{id}/webfilter/config` | Single-key patch. |
| POST | `/api/agent/{id}/webfilter/domains` | Add domain. |
| DELETE | `/api/agent/{id}/webfilter/domains/{domain}` | Remove domain. |
| POST | `/api/agent/{id}/webfilter/apply` | Apply rules. `error === "disabled"` means remote control is off at the PC — say so plainly; monitoring continues. |
| POST | `/api/agent/{id}/accounts/{tool}` | Local accounts section modal. `tool` ∈ `account_set_enabled`, `account_set_admin`, `account_set_logon_rights`, `account_session_action`, `account_delete`. |
| GET | `/api/reliability/suppressions` | Reliability section modal. |
| POST | `/api/reliability/suppressions` | Suppress an event. Empty `agent_id` means fleet-wide. |
| DELETE | `/api/reliability/suppressions/{id}` | Remove. Confirm first if fleet-wide. |

After any account or webfilter mutation the old UI re-pulls telemetry rather than trusting
optimistic state. Keep that: invalidate the host query instead of patching the cache.

---

## Inbox — `#/inbox`, `#/inbox/:group`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/inbox?group=` | **New.** The merged queue. |
| GET | `/api/tickets/vocabulary` | Server-authoritative lifecycle vocabulary, cached for the session. **No client-side legality logic** — which buttons render is decided by `allowed_transitions` / `allowed_blocks` / `can_unblock` on the item. |
| GET | `/api/tickets/summary` | Group counts for the chips and the nav badge. |
| POST | `/api/tickets` | New-ticket modal. Gains `start_immediately`. |
| GET | `/api/approvals` | Held gates. Operator-gated. |
| POST | `/api/approvals/{id}` | Approve/deny. Response `{resumed}` — when `resumed === false`, say the decision was recorded but the ticket could not be continued automatically. Do not report that as plain success. |

---

## Ticket detail — `#/inbox/ticket/:id`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/tickets/{id}` | The ticket. |
| GET | `/api/tickets/{id}/events` | The timeline, including approval rows. Fetched in parallel. |
| POST | `/api/tickets/{id}/transition` | State changes. |
| POST | `/api/tickets/{id}/reassign` | Move to another host. |
| POST | `/api/tickets/{id}/close` | Close. |
| POST | `/api/tickets/{id}/unblock` | Unblock. |
| POST | `/api/tickets/{id}/assign` | Assign to an operator. |
| PATCH | `/api/tickets/{id}` | Single-field inline edit. |
| POST | `/api/tickets/{id}/note` | Note composer. |
| POST | `/api/tickets/{id}/chat/stream` | SSE. The ticket's own Ask-kenny composer, `{message, mirror_to_discord}`. |
| POST | `/api/approvals/{id}` | The inline gate in the timeline. |

The ticket gate is **dismissible** ("Decide later" closes the dialog; the durable row in the
timeline remains the real path back). The fleet chat gate is **not**. That asymmetry is
deliberate: dismissing must never be confusable with denying.

---

## Log — `#/log`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/log?kind=&q=&cursor=` | **New.** Replaces `/api/audit` + `/api/events`, which the old UI fetched whole and filtered client-side with no server-side search or pagination. Filtering and paging now happen server-side. |

---

## Admin — `#/admin/:section`

Nine sections. Every config row shows value + source badge; env-derived rows are read-only
because the server rejects those writes with 403 — do not render a control guaranteed to fail.

**Alerting & Digest, Chat & AI, Environment** — all three read the same self-describing catalog:

| Method | Path | Notes |
|---|---|---|
| GET | `/api/settings` | `{groups: [...]}`, self-describing. Render generically; do not hardcode a settings list. |
| PUT | `/api/settings/{key}` | Inline save. |
| DELETE | `/api/settings/{key}` | Reset to default. |

**Web filter** — the underlying API is **per-host**, but the prototype places a fleet-level
section in Admin. Resolution: Admin → Web filter is a fleet-level roster (which hosts have
filtering on, their categories and schedule, pending bypass requests) that links into the
host page's Web filter modal for editing. Editing stays where the API is.

**Backup**

| Method | Path |
|---|---|
| GET / POST | `/api/backups` |
| GET | `/api/backups/{name}/download?source=local` (plain link, not a fetch) |
| POST | `/api/backups/{name}/verify`, `/restore` |
| DELETE | `/api/backups/{name}` |
| POST / PUT / DELETE | `/api/backup-targets`, `/api/backup-targets/{id}`, `/{id}/test` |

Restore is the most destructive action in the product. Gate it like an approval, not like a button.

**Updates**

| Method | Path |
|---|---|
| GET | `/api/updates` |
| POST | `/api/updates/check` |
| POST | `/api/updates/campaigns` — the prototype's "Approve rollout · pin 0.10.3" |
| POST | `/api/updates/campaigns/{id}/apply-now`, `/revoke` |

**Discord & Tickets**

| Method | Path |
|---|---|
| GET | `/api/discord/status`, `/identities`, `/claims`, `/members` |
| POST | `/api/discord/identities`, `/api/discord/claims/{code}` |
| DELETE | `/api/discord/identities/{did}` |

**Auto-ticket rules**

| Method | Path |
|---|---|
| GET | `/api/ticket-rules/vocabulary`, `/api/ticket-rules` |
| POST | `/api/ticket-rules` — response `{warnings}` renders inline when non-empty |
| DELETE | `/api/ticket-rules/{id}` — confirm first when fleet-wide |

**Users** (superuser only — the whole section is hidden otherwise)

| Method | Path |
|---|---|
| GET | `/api/users`, `/api/users/{id}`, `/api/avatars`, `/api/tool-classes`, `/api/fleet` |
| POST | `/api/users`, `/api/users/{id}/password`, `/api/users/{id}/pats` |
| PATCH | `/api/users/{id}` |
| PUT | `/api/users/{id}/profile`, `/api/users/{id}/hosts` |
| DELETE | `/api/users/{id}`, `/api/users/{id}/totp`, `/api/users/{id}/pats/{pid}` |

`hosts` is sent only when the role is `user`; operator+ are unscoped by definition.

---

## Profile — `#/profile`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/me`, `/api/avatars`, `/api/me/pats` | Avatars and PATs are skipped entirely for a shared-token identity. |
| PATCH | `/api/me` | Email, avatar. |
| POST | `/api/me/password` | Requires the current password. |
| POST / PUT / DELETE | `/api/me/totp` | Enable (returns `{secret, uri}`), verify, disable (requires password). |
| POST / DELETE | `/api/me/pats`, `/api/me/pats/{id}` | Creation returns `{token}` — shown **exactly once**, in a read-only copy field, with a plain statement that it will not be shown again. Never persist or re-display it. |
| PUT | `/api/me/theme` | **New.** Persists `light`/`dark` per user. |

A legacy shared-token identity has no editable account: profile, PATs and 2FA are all hidden.

---

## Ask kenny drawer — global overlay, ⌘K

| Method | Path | Notes |
|---|---|---|
| POST | `/api/chat/stream` | SSE. `agent_id` is **always sent, even as `''`** — omitting it leaves the server session pointed at the last host, and the drawer's scope chip would then be lying. |
| POST | `/api/chat/confirm/stream` | SSE. Resumes after a gate. |
| GET | `/api/chat/history`, `/api/chat/history/{id}` | History list and replay. |
| DELETE | `/api/chat/history/{id}` | Delete a conversation. The old code called this with a bare `fetch` and no 401 handling — that is a bug, not a behaviour to port. Use the standard client. |

---

## Deliberately dropped

- `GET /api/audit`, `GET /api/events` as separate views — merged into `/api/log`.
- The Flagged view had no endpoint of its own; it derived from cached fleet data. It is
  absorbed into Inbox.
- `reliability-modal.png` documents a modal already orphaned in the current docs.

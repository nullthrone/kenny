# kenny webui — actual API contract (reverse-engineered from `index.html`)

Source: `kenny-server/kenny_server/webui/index.html` (6757 lines, inline CSS/JS, hash
router, template strings). Read in full via chunked `Read`/`Grep`. This document is the
de-facto wire contract the React rewrite must replicate. Every path/body/field below is
quoted or paraphrased directly from the source at the line numbers given (as of the
version read; re-grep before relying on line numbers after further edits).

Two thin wrappers carry every request:

```js
async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  if (r.status === 401) { location = "/login"; throw new Error("unauthorized"); }
  if (!r.ok) {
    let msg = url + " -> " + r.status;
    try { const j = await r.json(); if (j && j.error) msg = j.error; } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}

async function api(url, method = "GET", body) {
  const opts = { method };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  if (r.status === 401) { location = "/login"; throw new Error("unauthorized"); }
  let data = null;
  try { data = await r.json(); } catch (e) {}
  if (!r.ok) {
    const msg = (data && (data.detail || data.error)) || (url + " -> " + r.status);
    throw new Error(msg);
  }
  return data;
}
```

`getJSON` is used both bare (GET) and with a raw `fetch`-style `opts` object (method +
manual headers/body) for non-GET calls in some sections — both patterns appear in the
file; I've normalized method/body per call-site below regardless of which wrapper was
used. `api(url, method, body)` reads FastAPI-style `detail`/`error` on failure; `getJSON`
reads `error` only. Both redirect the whole page to `/login` on a 401 — there is no
in-SPA re-auth flow.

---

## 1. Every HTTP call the frontend makes

Grouped by feature area. "Fires" = the user action or lifecycle event that triggers it.
Response fields are the ones the code actually destructures/reads, not full schemas —
telemetry payloads (`snapshot`, `health`, `governance`) are large, server-defined, mostly
opaque blobs that the UI walks generically (see §6), so I list only the top-level keys
the JS branches on, not every nested field.

### Bootstrap / identity

| Method | Path | Fires | Body | Fields read |
|---|---|---|---|---|
| GET | `/api/me` | app boot (`loadMe`, before first `router()`), profile modal open/refresh, after profile edits | — | `role`, `id`, `username`, `email`, `avatar`, `is_shared_token`, `totp_enabled` |
| GET | `/api/users/directory` | boot, only `if (canOperate())` | — | `{ users: [{id, username, role}] }` — powers `userLabel()`/`actorLabel()` |
| GET | `/api/about` | About modal open; also fired once non-blocking at boot (`getJSON("/api/about").then(a => { if (a && a.repo) serverRepo = a.repo; })`, line 6754) | — | `server_version`, `protocol_version`, `repo` |
| GET | `/api/agent-binary` | About modal, Fleet tab render, "retry GitHub fetch" banner | — | `available`, `by_os.{windows,linux}`, `version`, `message`, `github_configured` |
| GET | `/api/changelog` | About modal open | — | `{ releases: [{version, name, published_at, body}] }` |
| POST | `/api/agent-binary/fetch` | "retry GitHub fetch" button | — | `ok`, `version`, `message` |

### Fleet tab (`#/fleet`)

| Method | Path | Fires | Body | Fields read |
|---|---|---|---|---|
| GET | `/api/fleet` | `renderFleet()`, and as a cache-miss fallback from Overview/Activity/Tickets/Settings/Flagged (`state.fleet ? Promise.resolve(state.fleet) : getJSON("/api/fleet")`) | — | `{ agents: [...] }` — each agent has `agent_id`, `overall`, `warn_sections`, `crit_sections`, plus fields consumed by `fleetMetricsHtml`/`fleetListHtml` |
| GET | `/api/agent/{id}` | `selectAgent(id)` (clicking a host in the fleet list) | — | `agent_id`, `health.{overall,sections}`, `meta.{hostname,version,os}`, `os`, `snapshot` (raw per-section telemetry), `governance` |
| POST | `/api/agent/{id}/refresh` | "refresh now" action (`refreshNow`) | — | (re-triggers a `selectAgent`-style reload) |
| POST | `/api/agent/{id}/remotehelp` | remote-help action | — | result surfaced via `notify()` |
| POST | `/api/agent/{id}/screenshot` | "capture screenshot" button | — | triggers, then the `<img>` is loaded separately (below) |
| GET | `/api/agent/{id}/screenshot?t={Date.now()}` | `<img src>` on the screenshot card, cache-busted with `t=` on every render | — | raw image bytes; `onerror` hides the `<img>` and shows "none yet" |
| PUT | `/api/agent/{id}/channel` | update-channel selector | `{ channel }` | — |
| POST | `/api/agents/{id}/share-link` | "share installer" (Windows/default) | — | `{ url, expires_in }` |
| POST | `/api/agents/{id}/share-link?os=linux&arch={arch}` | "share installer" (Linux) | — | `{ oneliner \|\| url, expires_in }` |
| GET | `/api/agents/{id}/installer?arch={arch}` | "download installer" — **not fetched**, `window.location = ...` navigates the browser directly to it (line 6073-6075) | — | n/a (browser-handled download) |
| POST | `/api/agents/{id}/update` | "push update" | — | `{ version }` (used in the success toast) |
| DELETE | `/api/agent/{id}` | "remove from inventory" | — | — |

### Overview tab (`#/overview`, default landing route)

| Method | Path | Fires | Body | Fields read |
|---|---|---|---|---|
| GET | `/api/fleet/overview` | `renderOverview()` | — | consumed by `paintOverview()` (ECharts pies/bars/sankey/KPIs) |
| GET | `/api/fleet/trend?days={state.trendDays}` | `renderOverview()`, trend-window change | — | `{ days: [...] }` |
| GET | `/api/fleet` | same call as Fleet tab, reused/cached | — | see above |

**Cache-first behavior** (important for the rewrite, see §6): Overview repaints instantly
from `state.overview`/`state.trend`/`state.fleet` if present, then only re-fetches if
`Date.now() - state.overviewAt >= 15000` (a 15s staleness window). A failed fetch with no
cache shows an error; a failed fetch *with* a warm cache silently keeps showing stale
data (no error surfaced).

### Activity tab (`#/activity/audit`, `#/activity/events`)

| Method | Path | Fires | Body | Fields read |
|---|---|---|---|---|
| GET | `/api/fleet` | if `!state.fleet` | — | — |
| GET | `/api/audit` | every `renderActivity()` entry (both sub-views load both) | — | `{ entries: [...] }` — each entry has `tool`, `ok`, `state_changing`, timestamp fields used by `auditRow()` |
| GET | `/api/events` | every `renderActivity()` entry | — | `{ entries: [...] }` — `kind` ("audit" or other), `level`, `agent_id`, `target`, `message`, `ok`, `tool`, `error` |

Both audit and events are fetched **once per tab entry** and then filtered/paged
**entirely client-side** (`onAuditSearch`/`onEventsSearch` just set `state.auditQuery` +
`state.auditPage` and re-render from the already-fetched array; `AUDIT_PAGE_SIZE = 25`,
`EVENTS_PAGE_SIZE = 25`). No server-side search or pagination params exist for these
two endpoints in this UI.

### Flagged view (`#/flagged/warn`, `#/flagged/crit`)

Reuses the cached `/api/fleet` (or fetches it if missing) and derives its list purely
client-side from each agent's `warn_sections`/`crit_sections` arrays — no dedicated
flagged-view endpoint exists.

### Tickets tab (`#/tickets`, `#/tickets/{id}`)

| Method | Path | Fires | Body | Fields read |
|---|---|---|---|---|
| GET | `/api/tickets/vocabulary` | first access, cached in `state.ticketVocab` for the session | — | `{ states, blocked_reasons, priorities, categories }` — server-authoritative lifecycle vocabulary; comment: *"no client-side legality logic survives"* — `allowed_transitions`/`allowed_blocks`/`can_unblock` on the ticket itself drive what buttons render |
| GET | `/api/tickets/summary` | ticket list render, and every `renderHeaderRight()` (see polling note below) | — | `needs_you` (badge count), plus bucket counts used by `TICKET_GROUPS` tabs |
| GET | `/api/tickets?limit=200` | `renderTickets()` — **hardcoded limit=200, no further pagination** | — | `{ tickets: [...] }` |
| POST | `/api/tickets` | "create ticket" modal submit | `{ title, summary, agent_id }` (`agent_id` may be `null` for unassigned/triage) | returns created ticket; UI navigates to `#/tickets/{t.id}` |
| GET | `/api/tickets/{id}` | `renderTicketDetail(id)` | — | full ticket object (`state`, `blocked_on`, `blocked_since`, `requester_user_id`, `assistant_available`, `agent_id`, `discord_thread`, `allowed_transitions`, etc.) |
| GET | `/api/tickets/{id}/events` | `renderTicketDetail(id)` (parallel with the ticket fetch) | — | `{ events: [...] }` — the timeline, including approval rows |
| POST | `/api/tickets/{id}/transition` | state-change buttons, and bulk-apply | `{ to, reason }` (bulk sets `reason: "bulk action from dashboard"`) | — |
| POST | `/api/tickets/{id}/reassign` | reassign-host action | `{ agent_id }` | — |
| POST | `/api/tickets/{id}/close` | "close" action | `{}` | — |
| POST | `/api/tickets/{id}/unblock` | "unblock" action | `{}` | — |
| POST | `/api/tickets/{id}/assign` | assign-to-operator action | `{ assignee_user_id }` | — |
| PATCH | `/api/tickets/{id}` | inline field edit (title/priority/category etc.) | `{ [field]: value \|\| null }` — single-field patch | — |
| POST | `/api/tickets/{id}/note` | "add a note" composer mode | `{ summary }` | — |
| POST | `/api/tickets/{id}/chat/stream` | "Ask kenny" composer mode (SSE) | `{ message, mirror_to_discord }` | see §2 |
| POST | `/api/approvals/{id}` | inline gate row in ticket timeline, and the header approvals-badge modal | `{ approve: bool }` | `{ resumed: bool }` — `resumed === false` shows *"recorded — kenny could not continue this ticket automatically"* instead of a plain success toast |
| GET | `/api/approvals` | approvals-badge modal open/refresh, and every `renderHeaderRight()` | — | `{ approvals: [{id, tool, tool_class, args, ticket_id, agent_id}] }` |

Bulk transition (`applyBulkTicketState`) loops the `/transition` call per selected id
sequentially in a `for...of`, not a batch endpoint — no batch/bulk API exists server-side
for this.

### Ticket rules (Settings → auto-ticket rules, superuser)

| Method | Path | Fires | Body |
|---|---|---|---|
| GET | `/api/ticket-rules/vocabulary` | section render | — |
| GET | `/api/ticket-rules` | section render | — |
| POST | `/api/ticket-rules` | "add" | `{ event_type, section, agent_id, decision, note }` — response `{ warnings: [...] }` shown inline if non-empty |
| DELETE | `/api/ticket-rules/{id}` | "remove" (confirms first if fleet-wide, i.e. no `agent_id`) | — |

### Reliability suppressions (agent detail panel, operator+)

| Method | Path | Fires | Body |
|---|---|---|---|
| GET | `/api/reliability/suppressions` | panel mount | — |
| POST | `/api/reliability/suppressions` | one-click suppress from a reliability row, or the add-form | `{ event_id, source, note?, agent_id? }` (`event_id` required int; empty `agent_id` = fleet-wide) |
| DELETE | `/api/reliability/suppressions/{id}` | "remove" (confirms if fleet-wide) | — |

### Local accounts (agent detail → Accounts panel, host-governance)

| Method | Path | Fires | Body |
|---|---|---|---|
| POST | `/api/agent/{id}/accounts/{tool}` | every account-governance action, `tool` ∈ `account_set_enabled`, `account_set_admin`, `account_set_logon_rights`, `account_session_action`, `account_delete` | tool-specific args object, e.g. `{ principal, enabled }`, `{ principal, admin }`, `{ principal, deny: [...] }`, `{ principal, action, warn_seconds }`, `{ principal, remove_profile: true }` |

Response is checked for `r.ok === false`, in which case `r.error === "disabled"` renders
*"refused: remote control is switched off at that machine"*, else `r.message`/`r.error`.
On success it calls `refreshNow(agentId)` to re-pull telemetry rather than trusting the
optimistic UI state — see §6.

### Web filter / parental controls (agent detail panel)

| Method | Path | Fires | Body |
|---|---|---|---|
| GET | `/api/agent/{id}/webfilter` | panel mount, and after every mutation below | — |
| PUT | `/api/agent/{id}/webfilter/config` | toggle changes (e.g. DoH policy) | `{ [key]: value }` — single-key patch built as `const body = {}; body[key] = value;` |
| POST | `/api/agent/{id}/webfilter/domains` | "add domain" | `{ domain, action }` (`action` from a select, default `"block"`) |
| DELETE | `/api/agent/{id}/webfilter/domains/{domain}` | remove-domain row action | — |
| POST | `/api/agent/{id}/webfilter/apply` | "apply" button | — | `r.ok === false && r.error === "disabled"` → *"remote control is switched off at the PC — new rules were not applied; monitoring continues."* |

### Settings (superuser catalog)

| Method | Path | Fires | Body |
|---|---|---|---|
| GET | `/api/settings` | Settings tab entry (superuser only) | — returns `{ groups: [...] }`, a self-describing catalog (label, key, value, `is_set`, `sensitive`, numeric-ness) — the UI renders generically from this, no hardcoded settings list |
| PUT | `/api/settings/{key}` | inline save | `{ value }` |
| DELETE | `/api/settings/{key}` | "reset to default" | — |

### Backup (Settings → Backup, superuser)

| Method | Path | Fires | Body |
|---|---|---|---|
| GET | `/api/backups` | section render | — |
| POST | `/api/backups` | "create backup now" | — |
| GET | `/api/backups/{name}/download?source=local` | "download" — plain `<a href>`, **not fetched** | — |
| POST | `/api/backups/{name}/verify` | "verify" | `{ source: "local" }` |
| DELETE | `/api/backups/{name}` | "delete" | — |
| POST | `/api/backups/{name}/restore` | "restore" | `{ source: "local" }` |
| POST | `/api/backup-targets` | add remote target (http/scp/ftp) | `{ kind, label, config }` — `config` shape depends on `kind` (http: `{url, token}`; scp: `{host, port, username, password, private_key, remote_dir}`; ftp: `{host, port, username, password, remote_dir, use_tls}`) |
| PUT | `/api/backup-targets/{id}` | edit target | `{ label, config }` |
| POST | `/api/backup-targets/{id}/test` | "test connection" | — |
| DELETE | `/api/backup-targets/{id}` | "remove" | — |

### Updates (Settings → Updates, superuser)

| Method | Path | Fires | Body |
|---|---|---|---|
| GET | `/api/updates` | section render | — |
| POST | `/api/updates/check` | "check now" | — |
| POST | `/api/updates/campaigns` | "roll out" | `{ version, channel }` |
| POST | `/api/updates/campaigns/{id}/apply-now` | force-apply | — | `r.version` |
| POST | `/api/updates/campaigns/{id}/revoke` | "revoke" | — |

### Discord (Settings → Discord & Tickets, superuser)

| Method | Path | Fires |
|---|---|---|
| GET | `/api/discord/status` | section render |
| GET | `/api/discord/identities` | section render → `{ identities }` |
| GET | `/api/discord/claims` | section render → `{ claims }` |
| GET | `/api/users` | to populate the link-to-user picker → `{ users }` |
| DELETE | `/api/discord/identities/{did}` | "unlink" |
| POST | `/api/discord/claims/{code}` | "link account" | `{ user_id }` |
| GET | `/api/discord/members` | section render → `{ members }` |
| POST | `/api/discord/identities` | manual link | body built from form fields (not fully captured; see call site ~line 3469) |

### Profile / account (user menu → Profile modal)

| Method | Path | Fires | Body |
|---|---|---|---|
| GET | `/api/me` | modal open/refresh | — |
| GET | `/api/avatars` | modal open (skipped for shared-token identity) | `{ avatars }` |
| GET | `/api/me/pats` | modal open (skipped for shared-token identity) | `{ pats: [{id, label, created_at, last_used, revoked}] }` |
| PATCH | `/api/me` | "save profile" | `{ email, avatar }` |
| POST | `/api/me/password` | "change password" | `{ current_password, new_password }` |
| POST | `/api/me/totp` | "enable two-factor" (step 1) | — | `{ secret, uri }` — shown for the user to scan/paste |
| PUT | `/api/me/totp` | "verify & enable" (step 2) | `{ secret, code }` |
| DELETE | `/api/me/totp` | "disable two-factor" | `{ password }` |
| POST | `/api/me/pats` | "new token" | `{ label }` if provided, else `{}` | `{ token }` — shown exactly once in `tokenOnceModal()` |
| DELETE | `/api/me/pats/{id}` | "revoke" | — |

### Users admin (user menu → Users modal, superuser only — `openUsersModal` early-returns if `!isSuperuser()`)

| Method | Path | Fires | Body |
|---|---|---|---|
| GET | `/api/users` | modal open | `{ users: [{id, username, role, email, totp_enabled, disabled, avatar}] }` |
| GET | `/api/avatars` | create/detail modals | — |
| GET | `/api/fleet` | create/detail modals (host-scope checkboxes) | — |
| POST | `/api/users` | "create user" | `{ username, password, role }`, plus `hosts` appended only `if (role === "user")` |
| GET | `/api/users/{id}` | detail modal open | full user record |
| GET | `/api/tool-classes` | detail modal (capability-profile picker) | `{ profiles: {...} }` |
| PUT | `/api/users/{id}/profile` | "save capability profile" | `{ capability_profile }` |
| PATCH | `/api/users/{id}` | "save" (identity fields) | `{ username, email, role, avatar, disabled }` |
| POST | `/api/users/{id}/password` | "reset password" | `{ new_password }` |
| PUT | `/api/users/{id}/hosts` | "save host scope" | `{ hosts }` (array of agent ids) |
| DELETE | `/api/users/{id}/totp` | "reset two-factor" | — |
| POST | `/api/users/{id}/pats` | "new token" | `{ label }` if provided, else `{}` | `{ token }` |
| DELETE | `/api/users/{id}/pats/{id}` | "revoke" | — |
| DELETE | `/api/users/{id}` | "delete user" | — |

### Chat history (copilot rail)

| Method | Path | Fires |
|---|---|---|
| GET | `/api/chat/history` | "history" button → `{ conversations: [{id, title, updated_at, agent_id}] }` |
| GET | `/api/chat/history/{id}` | click a history row → `{ id, agent_id, transcript: [...] }` (transcript is replayed through `handleChatEvent`) |
| DELETE | `/api/chat/history/{id}` | delete row's `x` — uses **bare `fetch`**, not `getJSON`/`api` (line 6593): `await fetch(\`/api/chat/history/${id}\`, { method: "DELETE" })` — inconsistent with the rest of the app; no 401 handling on this one call |

---

## 2. SSE / streaming path

`streamSSE()` is the one streaming primitive, shared by all five streamed endpoints:

```js
async function* streamSSE(url, body, opts) {
  const r = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    signal: opts && opts.signal,
  });
  if (r.status === 401) { location = "/login"; throw new Error("unauthorized"); }
  if (!r.ok) {
    let msg = url + " -> " + r.status;
    try { const j = await r.json(); if (j && j.error) msg = j.error; } catch (e) {}
    throw new Error(msg);
  }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
      const line = frame.split("\n").find(l => l.startsWith("data:"));
      if (line) yield JSON.parse(line.slice(5).trim());
    }
  }
}
```

It is always a **POST**, always sends a JSON body, always parses raw `text/event-stream`
by hand (double-newline frame split, first `data:` line per frame, `JSON.parse`d) — it
ignores SSE `event:`/`id:` lines entirely, so every event's type comes from a `type`
field inside the JSON payload, not the SSE `event:` line. A pre-stream error (400/404/409
etc.) is expected as a normal JSON error body, exactly like `getJSON`; mid-stream errors
are just another event with `type: "error"`.

Five call sites, all consuming the **same event vocabulary**:

1. `POST /api/chat/stream` — `{ session_id, message, agent_id }` — the Fleet-rail copilot.
   `agent_id` is **always sent, even as `""`**, deliberately: *"omitting the key entirely
   would leave the server-side session pointing at whatever agent was last selected."*
2. `POST /api/chat/confirm/stream` — `{ session_id, approve }` — resumes after a gate.
3. `POST /api/tickets/{id}/chat/stream` — `{ message, mirror_to_discord }` — ticket "Ask
   kenny" composer; reuses the same event handling via `handleTicketChatEvent`.
4. `POST /api/forecast/stream` — `{ agent_id }` — near-term forecast card, text-only.
5. `POST /api/recommendation/stream` — `{ agent_id, section }` — AI recommendation popup;
   adds one extra event type, `remediation` (see below).

### Event types (from `handleChatEvent`, `handleTicketChatEvent`, and the two narrower
consumers)

- `user_text` — `{ type, text }` — **only emitted during history replay** (`loadConversation`); a live turn draws its own user bubble immediately client-side in `startChatTurn`/`bubble("user", message)`, so a live stream never actually sends this.
- `text_delta` — `{ type, text }` — incremental assistant text. The client does **not** append HTML incrementally; it accumulates the raw text in a buffer and re-renders the whole thing through `renderMarkdown()` on every delta:
  > "a delta can land mid-token (e.g. split a `**bold` marker), which incremental innerHTML append can't recover from. The buffer is short enough per turn for this to be cheap."
- `tool_result` — `{ type, tool, ok, image_b64?, format? }` — **an auto-run (read-only) tool call that has already executed.** Rendered as a single-line "tool ran" chip (`toolRun`), never a confirmation dialog. If `image_b64` is present, a screenshot card is appended (`chatShot`).
- `denied` — `{ type, tool, message? }` — a gated call the operator declined; rendered like a failed `tool_result` (`toolRun("denied " + tool, false)`).
- `pending` — `{ type, tool, args, agent_id, tool_class? }` — **this is the confirm-gate: the distinguishing signal for a state-changing call that needs human approval before it runs.** It has *not* run yet. Handling differs by surface:
  - Fleet copilot (`renderPending`): inserts an inline `.k-gate` card into the transcript showing the tool+args (`gateCall()`), **locks the composer** (`setComposerEnabled(false)`) so nothing else can be sent until this resolves, and opens a non-dismissible modal (`openGateModal`, no "close without deciding" — no click-outside/Escape/close-cross) whose only exits are "confirm & run" → `confirmChat(true)` or "cancel" → `confirmChat(false)`, each of which re-POSTs to `/api/chat/confirm/stream`.
  - Ticket composer (`handleTicketChatEvent`): the approval row is already durable server-side by the time this event arrives (comment: *"TicketAssistant.on_hold has already durably written the approval row and blocked the ticket before that event is yielded"*) — the client just locks the composer and calls `ticketPendingGate()`, which re-fetches the ticket+events to find the real approval id and opens a **dismissible** gate modal ("Decide later" only closes the dialog; the durable Approve/Deny row in the timeline, or the header approvals badge, remains the real path back to it — dismissing must never be confusable with denying).
- `done` — `{ type, session_id? }` — end of turn; `chatSessionId` is captured from it. Ticket chat treats `done` specially in its own loop (does a full `renderTicketDetail()` reload here, not in the generic handler) so the ticket's timeline/composer state is authoritative rather than provisional.
- `error` — `{ type, error, session_id? }` — surfaced as a failed tool-run chip (copilot) or a toast (`notify`, ticket chat).
- `remediation` (recommendation stream only) — `{ type, available, prompt }` — if `available && prompt`, renders an "Auto-Remediate" button that hands `prompt` to the copilot as a new chat turn (`autoRemediate`).

**Read-only vs. state-changing distinction, precisely**: there is no `tool_call`
"about to run" event for auto-run tools at all — a read-only tool simply appears
post-hoc as `tool_result`. A state-changing tool never appears as `tool_result` on first
attempt; it appears as `pending` and the identical call only becomes a `tool_result` (or
`denied`) after a separate `/confirm/stream` round-trip. The client has **no local
classification logic** for which tools are safe — it is entirely reactive to which event
type the server chose to emit. (`tool_class`, when present on a `pending` payload, is
carried through to the approvals list/modal display but is not used to make any
client-side decision.)

Composer lock/unlock, Stop button (`AbortController`), and "never overlap turns" guards
(`if (chatAbort) return;`) are all in-memory only — none of this state survives a reload;
a reload loses an in-flight turn's UI state entirely (the server-side session/approval
state is what's durable, per the tickets comment above; the fleet copilot's pending gate
is explicitly *not* said to be durable across reload in the same way — worth confirming
server-side before assuming it is).

---

## 3. Session / auth behaviour

**Important scoping note**: `index.html` contains **no login form and no login POST call
at all**. `/login` is a separate, fully server-rendered HTML page/form (`auth.py`,
`build_auth_routes` → `Route("/login", login, methods=["GET", "POST"])`), a plain
`<form method="post" action="/login">` submit — not a `fetch`/JSON call, not part of the
SPA's hash-routed UI. The rewrite needs to decide whether to keep a server-rendered login
page or bring login into the SPA; either way it is **not** part of `index.html`'s
behavior today, so nothing here should be treated as "the SPA already does this."

What the SPA *does* do, entirely reactively:

- **401 handling**: every request wrapper (`getJSON`, `api`, `streamSSE`) checks
  `r.status === 401` and does `location = "/login"; throw new Error("unauthorized")`.
  This is a **full page navigation**, not a client-side route change — it deliberately
  drops all SPA state. There is no token-refresh attempt, no retry, no "session expiring
  soon" warning.
- **Logout**: a plain link, not a fetch call — `<a class="kc-usermenu__item" href="/logout">Log out</a>`` (line 4216). Browser-navigates to a server route.
- **Cookie handling**: entirely implicit. Comment at line 1053-1054: *"401 sends the operator to /login (cookie auth is sent automatically by fetch)."* No `credentials: "include"` is ever set explicitly (same-origin default suffices), no cookie is read or written by JS.
- **CSRF**: no CSRF token appears anywhere in `index.html` — no hidden field, no header, no meta tag read. (Server-side, `auth.py`'s cookie is `samesite="lax"`, which is the app's actual CSRF mitigation; the SPA does nothing extra.)
- **PAT (personal access tokens)**: fully self-service, no admin approval flow.
  - A user manages their own via Profile modal → `/api/me/pats` (list/create/revoke).
  - A superuser can additionally create/revoke tokens **for another user** via `/api/users/{id}/pats` in the Users admin modal.
  - Both creation paths funnel through the same `tokenOnceModal(title, token, reopen)` — the raw secret is shown **exactly once**, in a `readonly` copy-field, with the text *"Copy this now — for security it will not be shown again."* After that only metadata (`label`, `created_at`, `last_used`, revoke button) is ever shown again — the token value itself is never persisted or re-displayed client-side.
  - The token labeled "Personal access token" in the UI is presumably the same credential type usable as an MCP bearer token (per `kenny-server/CLAUDE.md`'s description of PAT-as-Bearer auth), **but the UI itself never says so** — no copy mentions `/mcp`, "Bearer", or MCP anywhere in `index.html`. This is an explicit gap: if the React rewrite is expected to explain PAT→MCP usage to the user, that copy does not exist today and must be authored new, not ported.
  - "Legacy shared token" identity (`me.is_shared_token === true`) is a distinct auth mode: profile/PATs/2FA are all hidden for it (*"You are signed in with a legacy shared token, which has no editable account."*).

---

## 4. Client-side state and preferences

### The `state` object (module-level `let`, line ~1348)

```js
let state = { tab: "overview", activityView: "audit", settingsSection: null,
  fleet: null, bin: null, selected: null, detail: null,
  overview: null, trend: null, overviewAt: 0, topMetric: "disk", sankeyByOs: false, trendDays: 30,
  audit: [], auditQuery: "", auditPage: 1,
  events: [], eventsQuery: "", eventsPage: 1,
  tickets: [], ticketsStateFilter: "", ticketDetail: null, approvalsCount: 0,
  ticketsNeedsYouCount: 0 };
```

Fields added dynamically elsewhere (not in the initializer, but assigned at runtime):
`state.me`, `state.users`, `state.ticketVocab`, `state.ticketsSummary`,
`state.ticketsSelected` (a `Set`), `state.ticketsGroup`, `state.flaggedSev`,
`state.ticketRuleVocab`. This is a **single global mutable object**, no immutability, no
subscription model — every render function reads/writes it directly and re-renders by
replacing `app.innerHTML` wholesale. There is no diffing; every navigation or mutation
that "refreshes" a view does so by regenerating a big HTML string and swapping it in.

### `navToken` — stale-render guard

```js
let navToken = 0;
```

Incremented once at the very top of `router()` on every navigation. Every async render
function captures it on entry (`const nav = navToken;`) and checks `if (nav !== navToken)
return;` after each `await` before writing to the DOM — repeated after *every* awaited
fetch in a multi-step loader, not just once. Purpose (from the source comment, line
1357-1360): *"Bumped by router() on every navigation. An async render captures it on
entry and drops its DOM writes if it no longer matches — a response from a tab the
operator has already left must never clobber the tab they are looking at now."* This
exists because there is no request cancellation for plain `fetch`/`getJSON` calls (only
the SSE streams use `AbortController`) — a slow response from a tab the user already
navigated away from would otherwise land and overwrite whatever is now on screen. A React
rewrite gets this for free from key-based unmounting / query-library cancellation, but
must not silently regress to "last response wins" if it hand-rolls any fetch logic
outside such a library.

### localStorage keys (all wrapped in `try {} catch(e) {}` — must tolerate storage being
unavailable, e.g. private browsing)

| Key | Values | Set by | Read by |
|---|---|---|---|
| `kenny-theme` | `"dark"` \| `"light"` | `toggleTheme()` | Inline script in `<head>` **before** the app script even loads: `document.documentElement.setAttribute('data-theme', localStorage.getItem('kenny-theme') \|\| 'dark')` (line 10) — this is what prevents a flash-of-wrong-theme. Default is **dark**, not "system"/`prefers-color-scheme`. |
| `kenny-enter-send` | `"on"` \| `"off"` | `setEnterToSend(on)` | `enterToSend()` — `localStorage.getItem(...) === "on"`; **default is off** (Enter inserts a newline; opt-in required to send-on-Enter) |
| `kenny-copilot` | `"on"` \| `"off"` | `toggleAskRail()` (desktop only — the tablet drawer's open/closed state is explicitly *not* persisted, `copilotDrawerOpen` is a plain variable) | `askRailOn()` — `localStorage.getItem(...) !== "off"`; **default is on** |

No other keys. No `sessionStorage` usage anywhere. Comment worth preserving verbatim for
the migration since it explains why the key name itself is load-bearing (line 4708-4710):
> `"kenny-copilot" stays the storage key on purpose: renaming it would silently discard every operator's saved rail-open/closed preference.`

If the React rewrite renames these keys, every returning user silently reverts to
defaults (theme flips to dark, Enter-to-send resets off, copilot rail resets open) unless
a migration step reads the old keys once.

---

## 5. Hash routes

From `parseHash()` (line 1367) — this is the **complete, exhaustive route table**; any
hash not matched here falls through to Fleet... actually falls through to **Overview**
(the final `return` is `{ tab: "overview", ... }` — re-verify against source, the comment
at the top says "falls back to the Fleet tab" but the code's actual fallback branch
returns `tab: "overview"`; **this is a genuine discrepancy between the code comment and
the code** worth flagging rather than silently resolving one way).

| Hash | Resolves to |
|---|---|
| `#/overview` or `` (empty) or anything unrecognized | Overview tab (see caveat above re: the stale comment) |
| `#/fleet` | Fleet tab |
| `#/activity/audit` | Activity tab, audit sub-view |
| `#/activity/events` | Activity tab, events sub-view |
| `#/activity` (no sub-route) | Activity tab, defaults to audit (`parts[1] === "events" ? "events" : "audit"`) |
| `#/flagged/warn` | Flagged tab, warn severity |
| `#/flagged/crit` | Flagged tab, crit severity |
| `#/flagged` (no sub-route) | Flagged tab, defaults to warn |
| `#/tickets` | Tickets list |
| `#/tickets/{id}` | Ticket detail for `{id}` — the landing target for every "see the dashboard" link posted into Discord, so must work from a cold load |
| `#/settings` | Settings, first available section (superuser-gated; a non-operator hitting this is bounced to `#/overview` inside `router()`, not inside `parseHash()`) |
| `#/settings/{slug}` | Settings, specific section — `{slug}` values come from the server's dynamic settings catalog (`/api/settings`) plus the client-injected `ticket-rules` and (superuser-only) `backup`/`updates`/`discord-tickets` slugs; not a fixed enum |
| `#/backup` | **Alias** → resolves to `#/settings` with `settingsSection: "backup"` (kept "for old bookmarks and the Discord 'see the dashboard' links") |
| `#/updates` | **Alias** → resolves to `#/settings` with `settingsSection: "updates"` |

Navigation is driven by real `<a href="#/...">` links (so back/forward and bookmarking
work) plus some `location.hash = "#/..."` assignments for programmatic nav (e.g.
`openTicket(id)`, ticket creation success). `router()` also self-corrects the URL bar with
`history.replaceState` in two places without firing a second `hashchange`: normalizing an
empty hash to `#/overview` on boot, and normalizing a resolved settings hash to its
canonical `#/settings/{active.slug}` (e.g. after `#/settings` picks a default section, or
after `#/backup` resolves).

---

## 6. Things a naive rewrite would silently break

- **No polling anywhere.** There is no `setInterval` in the entire file (grepped and
  confirmed — the only `setTimeout` is a 600ms toast-removal fallback, line 1141, unrelated
  to data). All "freshness" comes from: (a) re-fetching on every tab navigation
  (`router()`), (b) the Overview's 15s cache-staleness window (§1), and (c) explicit
  refresh-after-mutation calls. **If a React rewrite adds polling where none existed, or
  removes the refresh-after-mutation calls without replacing them with something
  equivalent, both directions are a behavior change** — under-refresh (stale badges until
  next nav) is the *current* behavior and may be intentional (cheap, no background
  network chatter), not an oversight to "fix" silently.
- **Refresh-after-mutation, not optimistic updates.** Every write (`api()`/`getJSON()`
  POST/PUT/PATCH/DELETE call) is followed by a full re-fetch-and-rerender of the
  affected view (e.g. `acctCall` → `refreshNow(agentId)`; ticket actions →
  `renderTicketDetail(id)`; settings save → `refreshSettingsSection()`). There is **no
  optimistic UI** anywhere in this codebase — the account-governance comment makes the
  reasoning explicit: *"Run one governance tool, then re-collect telemetry so the panel
  reflects the machine rather than what we hoped would happen."* A React rewrite that
  adds optimistic updates for snappiness must handle the real failure mode this code
  deliberately avoids: a remote action can be silently refused by the target machine
  (`r.ok === false, r.error === "disabled"` when "remote control is switched off at that
  machine") — an optimistic UI would show success and then have to visibly revert.
- **Header badges (`approvalsCount`, `ticketsNeedsYouCount`) refetch on every
  `renderHeaderRight()` call**, which fires on essentially every tab render — this reads
  as "near-live" in practice purely because tab switches are frequent, not because of any
  actual polling. A SPA with client-side routing that doesn't force a full header
  re-render per navigation (e.g. a persistent layout shell in React Router) will need an
  explicit replacement mechanism or the badges will go stale.
- **The confirm-gate composer lock is a single global, not per-session.** `chatAbort`,
  `chatSessionId`, `tkGateShownFor` are module-level `let`s, not keyed by ticket/agent —
  the code comments assert "at most one gate is ever open" and "never overlap turns" as
  invariants enforced purely by these globals plus early-`return` guards
  (`if (chatAbort) return;`). A React version with concurrent chat surfaces (e.g. two
  browser tabs, or a future multi-session UI) would need this made explicit rather than
  ambient.
- **`kenny-copilot`/`kenny-theme`/`kenny-enter-send` localStorage keys must be preserved
  verbatim** (§4) or existing users lose their preferences silently on first load of the
  new app.
- **Role/host-scope gating is pervasive and multi-layered**, not a single route guard:
  `canOperate()` (`superuser`|`operator`) and `isSuperuser()` gate dozens of individual UI
  elements (buttons, whole modals, whole settings sections, the tickets badge, bulk
  actions, the note-vs-chat composer toggle for a ticket) — see the full grep list in the
  investigation, roughly 25 distinct call sites. All of this is **UI-only convenience
  hiding**; the actual enforcement is server-side (`webui/authz.py` per the project's own
  `CLAUDE.md`). The rewrite must not treat any of these client-side checks as security
  boundaries, but it does need to replicate all of them for UX parity — a `user`-role
  account currently sees a materially different, reduced UI (no Settings tab at all, no
  Users/Discord/Backup/Updates, ticket composer is chat-only with no note mode, no
  approvals badge, no directory-based actor names outside their own tickets).
- **`renderPending`'s fleet-rail gate is explicitly non-dismissible** (no Escape, no
  click-outside, no close cross) while the ticket-detail gate is explicitly dismissible
  ("Decide later"). This asymmetry is deliberate (comments at both call sites explain the
  reasoning — a fleet chat session has nothing else to do until decided; a ticket may
  legitimately wait hours for a different operator) and must be preserved, not
  unified into one modal behavior.
- **The `#/overview` vs. "Fleet" fallback discrepancy** noted in §5 — the code comment and
  the code itself disagree about what an unrecognized/bare hash resolves to. Worth
  resolving deliberately in the rewrite rather than copying whichever one "looks right."
- **Empty/error states are hand-authored per view**, not a generic empty-state component:
  e.g. tickets list shows *"could not load tickets: {message}"*; Overview shows a plain
  error only when there's no cache to fall back to; account-governance/webfilter panels
  distinguish a generic failure from the specific "remote control is switched off"
  refusal with different copy. A generic error boundary in React will lose these
  hand-tuned messages unless each is ported deliberately.
- **`/api/agent/{id}/screenshot`'s `<img>` is cache-busted with `?t=${Date.now()}` on every
  render**, not just after a capture — re-rendering the detail panel for any reason
  re-fetches the image. A React component that only re-fetches on explicit "recapture"
  clicks would show a different (more efficient, but behavior-different — could show a
  stale cached image the browser silently kept) caching behavior.
- **Client-side search/filter/pagination for Audit and Events is 100% local** — fetched
  once per tab entry, then sliced/filtered/paged in memory (`AUDIT_PAGE_SIZE = 25`,
  `EVENTS_PAGE_SIZE = 25`). There is no server-side search API for these two lists in
  this UI at all — if the underlying data volume has grown since this was written, a
  naive port that keeps "fetch everything, filter client-side" could become a real
  performance problem the original author didn't have to think about, or (if a future
  API adds a `limit`) could silently start filtering an incomplete window.
- **`downloadInstaller`/`shareLinuxInstaller`, and the backup `download` link, are plain
  browser navigations** (`window.location = ...` or a bare `<a href>`), not `fetch()`
  calls — a React port that tries to `fetch()` + blob-download these will hit any
  auth/redirect/streaming behavior the server built assuming a real browser navigation
  (e.g. `Content-Disposition` headers), and won't get automatic cookie-based auth the
  same way a plain navigation does.
- **`/api/chat/history/{id}` DELETE uses a bare, un-wrapped `fetch()`** (line 6593) instead
  of `getJSON`/`api` — it has no 401-redirect handling, unlike literally every other
  network call in the file. Almost certainly an inconsistency rather than a deliberate
  choice; flagging it so the rewrite doesn't "faithfully" reproduce a bug as if it were a
  feature.

---

## Open questions / things I could not determine from `index.html` alone

- Whether a fleet-rail confirm-gate (`pending` on `/api/chat/stream`) is durable across a
  page reload the way a ticket's approval row explicitly is — the source comments only
  make the durability claim for tickets. Would need to read the server-side session store
  (out of scope for this file-only pass) to confirm.
- The exact request body `addDiscordIdentity` sends to `POST /api/discord/identities`
  (line ~3469) — the call site builds the body from several form fields I did not fully
  trace field-by-field; worth re-reading that ~30-line block directly before implementing
  the Discord-link form.
- Whether PATs are actually accepted as `/mcp` Bearer tokens is asserted by
  `kenny-server/CLAUDE.md` but **never stated anywhere in the UI text itself** — the
  rewrite's copy for this will need to be authored fresh, not ported, since there's
  nothing to port.

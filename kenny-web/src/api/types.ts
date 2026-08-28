/**
 * The frozen contract between `kenny-web` and the server's `/api` surface.
 *
 * This file is the single statement of every response shape the console depends
 * on. It is authored ahead of both sides so the frontend and the endpoint work
 * can proceed in parallel, and it is what the seam test checks: if a handler
 * stops returning one of these fields, the build or the seam test fails rather
 * than a panel silently rendering empty (root `CLAUDE.md`, "every seam two
 * places must agree on gets a test that fails when they diverge").
 *
 * Field names here are the server's, verbatim. Where a shape already exists in
 * `kenny_server`, the producing function is named in the comment; treat those as
 * observations of current behaviour, not as an invitation to reshape them.
 * Additive is the rule: new fields are added, existing ones are not renamed,
 * because the MCP surface and the agent read some of the same producers.
 */

/* ── Core primitives ─────────────────────────────────────────────────────── */

/** Rolled-up health, worst-of across sections. `health_rules.worst` owns the ordering. */
export type Severity = 'ok' | 'warn' | 'crit' | 'unknown'

/** Role hierarchy is superuser > operator > user (`security.py`). */
export type Role = 'superuser' | 'operator' | 'user'

/**
 * Where a config value came from, as the server states it.
 *
 * `db` is an operator override stored in the database. The console renders it
 * with the word "custom", which is the design's label and the wording the legacy
 * dashboard already used — but the wire value is `db`, and renaming it
 * server-side would break `test_config.py` and the bundled dashboard for no gain.
 * The vocabulary has one owner (the server) and the translation happens once, at
 * the render boundary, in `views/admin/settingsMap.ts`.
 *
 * Env-derived keys are read-only: the server rejects those writes with 403, so
 * the console must not offer a control for them.
 */
export type ConfigSource = 'default' | 'env' | 'db'

/**
 * A host reference carried inside aggregate rows.
 * Produced by `fleet_stats._member`.
 */
export interface Member {
  agent_id: string
  value: string | number
  detail: string
}

/**
 * `GET /api/about` — static server identity, produced by `webui/__init__.py::api_about`.
 * Sources the sidebar's version line and the About dialog it opens.
 */
export interface About {
  server_version: string
  protocol_version: string
  repo: string | null
}

/**
 * One release in `GET /api/changelog`, produced by `changelog._to_public`, which
 * strips a leading `v`/`V` from `tag_name` for `version` and falls back to the tag
 * for `name`. Drafts and (by default) prereleases never reach the client.
 *
 * `tag` — not `version` — is the unique key: `version` is the tag with its `v`
 * stripped, so `v1.0` and `1.0` would collide.
 */
export interface ChangelogRelease {
  version: string
  tag: string
  name: string
  /** ISO-8601, or null for a release GitHub never published. */
  published_at: string | null
  body: string
  html_url: string | null
  prerelease: boolean
}

/**
 * `GET /api/changelog` — GitHub Releases, proxied server-side and cached for five
 * minutes. `changelog.fetch_releases` is best-effort and never raises, so an empty
 * `releases` means "GitHub had nothing to say", not "the server broke".
 */
export interface ChangelogResponse {
  repo: string
  releases: ChangelogRelease[]
}

/**
 * The slice of `GET /api/agent-binary` the About dialog reads. The endpoint
 * (`distribution.agent_binary_status`) also returns `ok`, `source`, `sha256`,
 * `by_os`, `targets` and `github_configured` for Admin's distribution section;
 * only what is consumed is typed here. Widen it when a caller needs more rather
 * than transcribing the whole shape speculatively.
 */
export interface AgentBinaryStatus {
  /** The staged binary's version, or null when nothing is cached. */
  version: string | null
  available: boolean
}

/** `GET /api/me` — identity, role and host scope for the signed-in principal. */
export interface Me {
  user_id: string
  username: string
  role: Role
  /** Empty for operator+ (unrestricted). Populated only for a scoped `user`. */
  hosts: string[]
  /**
   * Legacy shared-token identity. Has no editable account: profile, PATs and 2FA
   * are hidden entirely when this is true.
   */
  is_shared_token: boolean
}

/* ── Fleet ───────────────────────────────────────────────────────────────── */

/**
 * One host in `GET /api/fleet`. Produced by `webui/__init__.py::_overview`.
 *
 * `summary` already exists (`_fleet_summary`): the worst flagged section's reason,
 * with a `+N more` suffix, or `all green`, or `no telemetry yet`.
 * `severity_label` is NEW — the console renders it as the card's caps label
 * (e.g. `CRITICAL · DISK`). It is derived from the same section data as `summary`,
 * so it must be computed next to it and never from a threshold restated elsewhere:
 * thresholds live only in `health_rules.py` (`kenny-server/CLAUDE.md`).
 */
export interface FleetAgent {
  agent_id: string
  online: boolean
  overall: Severity
  summary: string
  /** NEW. `HEALTHY` when nothing is flagged. */
  severity_label: string
  os: string
  agent_version: string
  collected_at: string | null
}

export interface FleetResponse {
  overall: Severity
  agents: FleetAgent[]
}

/**
 * One telemetry section on a host.
 *
 * `attention` is NEW and is the whole point of item 5: the console splits problem
 * cards from the healthy checklist without carrying any threshold knowledge of its
 * own. It is computed in `health_rules.py` alongside `status`, nowhere else.
 */
export interface HostSection {
  name: string
  status: Severity
  attention: boolean
  reason?: string
  summary?: string
}

/* ── Today ───────────────────────────────────────────────────────────────── */

/** What a Today row wants the operator to do. `target` is the route to open. */
export interface TodayItem {
  severity: 'crit' | 'warn' | 'held'
  host: string | null
  title: string
  detail: string
  /** Caps label on the row's affordance, e.g. `FREE UP SPACE`. */
  action: string
  /** A console route, e.g. `#/fleet/oma-pc` or `#/inbox/ticket/ae73db26ad3e4c078c93050f63395873`. */
  target: string
}

export interface DonutSegment {
  key: Severity
  label: string
  value: number
  members: Member[]
}

/** One day of fleet health. Shape is `fleet_stats.aggregate_trend`'s existing bucket. */
export interface TrendDay {
  day: string
  ok: number
  warn: number
  crit: number
  unknown: number
  members: Member[]
}

/** A single actionable total. Shape is `fleet_stats._kpis`'s existing row. */
export interface Kpi {
  key: string
  label: string
  value: number
  severity: Severity
  members: Member[]
}

/**
 * `GET /api/today` — the landing aggregate. NEW.
 *
 * Composed from `fleet_stats.aggregate_overview` + `aggregate_trend` + the ticket
 * and approval stores; it is a re-packaging of data the server already computes,
 * not a new computation.
 *
 * `items` is ranked crit sections > warn sections > held approvals > stale tickets
 * and capped at three. The cap is the design's governing rule — consequence over
 * completeness — and belongs on the server so every client agrees on the ranking.
 * An empty `items` array is a first-class state ("all quiet"), not an error.
 *
 * `verdict_sentence` is server-generated prose, e.g. "Two machines need attention.
 * The other four are quiet." There is no fleet-wide equivalent today
 * (`forecast.py` is per-agent), so this is new prose logic.
 */
export interface TodayResponse {
  generated_at: string
  verdict_sentence: string
  items: TodayItem[]
  donut: { segments: DonutSegment[] }
  trend_30d: { days: TrendDay[] }
  kpis: Kpi[]
}

/* ── Inbox ───────────────────────────────────────────────────────────────── */

/**
 * Who the item waits on. The five groups already exist server-side for tickets
 * (`TicketStore.counts`) and carry that module's rule:
 *   needs_you = blocked on approval/operator, or a `new` alert-origin ticket
 *   waiting   = blocked on the requesting user
 *   working   = in progress, unblocked
 *   new       = not started, has a requester
 *   done      = resolved/closed/cancelled, collapsed
 * The merge extends the vocabulary to flagged sections and held approvals.
 */
export type InboxGroup = 'needs_you' | 'waiting' | 'working' | 'new' | 'done'

/** Which source a row came from. Drives the row's caps chip. */
export type InboxKind = 'approval' | 'ticket' | 'section' | 'alert'

/**
 * A held state-changing tool call, rendered inline in the row.
 *
 * `args` are frozen at hold time (`toolloop.PendingCall`) and the resolved target
 * is fixed before the gate runs, so a later host switch cannot retarget the call
 * (ADR-0038). The console renders these arguments verbatim: they are the operator's
 * only evidence of what approving will actually execute. Never reformat, truncate
 * or re-serialise them for display.
 */
export interface InboxGate {
  approval_id: string
  ticket_id: string
  tool: string
  args: Record<string, unknown>
  agent_id: string
  tool_class: string
  held_since: string
}

export interface InboxItem {
  id: string
  kind: InboxKind
  /** `''` when nothing is blocking, mirroring `Ticket.blocked_on`. */
  waits_on: 'operator' | 'user' | 'approval' | 'attention' | ''
  /**
   * Health severity, for rows that have one. A flagged section renders its own
   * severity as the row chip (`CRITICAL`, `WARNING`) rather than the generic kind,
   * which is how the design distinguishes a critical host from a routine ticket at
   * a glance. Null for kinds that carry no health status.
   */
  severity: Severity | null
  title: string
  meta: string
  host: string | null
  age_seconds: number
  /** Present only on `kind: 'approval'`. */
  gate: InboxGate | null
  /** Console route for the row's title link. */
  target: string
}

export interface InboxResponse {
  group: InboxGroup
  counts: Record<InboxGroup, number>
  items: InboxItem[]
}

/**
 * `GET /api/tickets/summary` — bucket counts, already served today.
 *
 * This is the cheap call behind the INBOX nav badge; the full inbox list is not
 * fetched just to render a number. Narrowed to the caller's own tickets for a
 * scoped `user`, mirroring the list endpoint's scoping.
 */
export type TicketSummary = Record<InboxGroup, number>

/* ── Log ─────────────────────────────────────────────────────────────────── */

export type LogKind = 'tools' | 'alerts' | 'events'

/**
 * One row of the unified stream. The underlying merged `events` table already
 * exists (`EventStore`); this is the common envelope over it.
 */
export interface LogRow {
  ts: string
  kind: LogKind
  /** Short caps tag rendered in the row's second column, e.g. `TOOL`, `ALERT`, `PUSH`. */
  tag: string
  host: string | null
  actor: string | null
  /** The subject — a tool name, an alert key. Rendered in mono. */
  what: string
  message: string
  meta: Record<string, unknown>
}

/** `GET /api/log?kind=&q=&cursor=` — cursor pagination; `next_cursor` null at the end. */
export interface LogResponse {
  rows: LogRow[]
  next_cursor: string | null
}

/* ── Admin ───────────────────────────────────────────────────────────────── */

/**
 * One config row. `config.py::Settings.describe()` already returns this shape.
 *
 * `source` drives the badge next to the value. `editable` is false for env-derived
 * keys — the server already rejects those writes with 403, and the console must not
 * offer a control that is guaranteed to fail.
 */
export interface SettingRow {
  key: string
  label: string
  help: string
  value: string | number | boolean | null
  source: ConfigSource
  editable: boolean
}

export interface AdminSection {
  key: string
  label: string
  rows: SettingRow[]
}

/**
 * Admin's section navigation is SERVER-DERIVED, not a hardcoded list.
 *
 * `GET /api/settings` returns `{groups: [{name, slug, settings}]}`; the slug is
 * derived from the group's display name and pinned by `test_config.py`, so it is
 * stable and a rename breaks loudly instead of silently. Today's groups are:
 *
 *   alerting-digest · web-filter · chat-ai · logging · network-process ·
 *   operator-agent-auth · telemetry-limits · agent-distribution · backup ·
 *   updates · discord-tickets
 *
 * That is eleven, where the prototype drew nine. The five the prototype does not
 * show (logging, network-process, operator-agent-auth, telemetry-limits,
 * agent-distribution) are real configuration and must not be dropped — the section
 * nav scrolls, which is what the prototype's own mobile treatment already does.
 *
 * Three further sections are synthetic — they are not config groups and have their
 * own endpoints:
 *   `auto-ticket-rules` → /api/ticket-rules
 *   `users`             → /api/users (superuser only; hidden entirely otherwise)
 *   `environment`       → a read-only view across ALL groups filtered to
 *                         `source === 'env'`. The prototype shows this as its own
 *                         section; the server has no such group, so the console
 *                         composes it.
 *
 * `#/admin` with no section resolves to the first server-provided group. Do not
 * invent a placeholder slug.
 */
export type AdminSectionKey = string

/* ── Chat ────────────────────────────────────────────────────────────────── */

/**
 * Streamed chat events, POSTed and parsed as raw `text/event-stream`.
 *
 * The event type lives in a `type` field inside the JSON payload, not in the SSE
 * `event:` line — the existing parser ignores `event:`/`id:` lines entirely.
 *
 * These names are deliberately unchanged. The brief asks for `auto_run` versus
 * `needs_confirmation`; renaming `tool_result`/`pending` would break the ticket
 * chat surface, which documents that it reuses this vocabulary exactly. The
 * distinction is instead made explicit by an additive `auto_run` flag.
 */
export type ChatEvent =
  /** History replay only. A live turn draws the user's own bubble client-side. */
  | { type: 'user_text'; text: string }
  /**
   * Incremental assistant text. A delta can split a markdown token, so the client
   * accumulates raw text and re-renders the whole buffer rather than appending.
   */
  | { type: 'text_delta'; text: string }
  /**
   * A tool call that HAS ALREADY RUN. Read-only calls reach the client only in
   * this form — there is no "about to run" event for them.
   * `auto_run` is NEW and additive: true when the tier was read-only.
   */
  | { type: 'tool_result'; tool: string; ok: boolean; auto_run: boolean; image_b64?: string; format?: string }
  /**
   * A state-changing call that has NOT run and needs a decision. This is the gate.
   * Resolving it is a separate POST to the confirm stream.
   */
  | { type: 'pending'; tool: string; args: Record<string, unknown>; agent_id: string; tool_class?: string }
  | { type: 'denied'; tool: string; message?: string }
  | { type: 'done'; session_id?: string }
  | { type: 'error'; error: string; session_id?: string }
  /**
   * Recommendation stream only. Carries a ready-made prompt for the fix the
   * recommendation just described; it is what a section modal's "Fix via Ask
   * kenny" hands to the drawer. The chat and forecast streams never emit it, but
   * all four streams share one parser and one event vocabulary, so it belongs in
   * the union rather than in a per-caller superset.
   */
  | { type: 'remediation'; available: boolean; prompt: string }

/**
 * `POST /api/chat/stream`.
 *
 * `agent_id` is always sent, including as `''`. Omitting the key leaves the
 * server-side session pointed at whatever host was last selected — the console's
 * scope chip would then lie about what the model can see.
 *
 * `scope` is derived, not stored: `'host'` when `agent_id` is non-empty, else
 * `'fleet'`. It exists so the drawer can label itself and so the system context
 * can say which it is; it must never change how a tool is classified. The tier is
 * a property of the tool, the gate is a property of the calling surface (ADR-0045).
 */
export interface ChatStreamRequest {
  session_id: string | null
  message: string
  agent_id: string
  scope: 'host' | 'fleet'
}

/* ── Onboarding ──────────────────────────────────────────────────────────── */

/**
 * `POST /api/agents/share-link` — hands an installer to someone who is not signed in.
 *
 * Single-use and time-limited: the nonce is burned on first fetch and the agent
 * token is minted lazily at that moment, so an unused link never creates a
 * credential. `expires_at` is 24h out.
 */
export interface ShareLinkResponse {
  url: string
  expires_at: string
  os: 'windows' | 'linux'
  name: string
}

/* ── Preferences ─────────────────────────────────────────────────────────── */

/**
 * `PUT /api/me/theme` — persists the operator's theme server-side so it follows
 * them between browsers. NEW.
 *
 * localStorage stays the fast path and the offline fallback; the server value wins
 * on load when the two disagree. A shared-token identity has no account to store it
 * against, so for that principal this call is skipped and localStorage is the only
 * store.
 */
export interface ThemePreference {
  theme: 'light' | 'dark'
}

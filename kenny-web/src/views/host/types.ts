import type { HostSection, Severity } from '../../api/types'

/**
 * `GET /api/agent/{id}` — not yet in the frozen contract (types.ts only
 * carries the standalone `HostSection` shape it documents as this
 * endpoint's needed addition). Modeled directly off the live handler,
 * `kenny-server/kenny_server/webui/__init__.py::api_agent`, whose response
 * fields notes/api-contract-actual.md §1 also lists:
 * `agent_id, health.{overall,sections}, meta.{hostname,version,os}, os,
 * snapshot, governance`.
 *
 * `health.sections` is normalized to the frozen `HostSection[]` shape by
 * `normalizeSections` below rather than trusted as-is: today's live handler
 * returns a dict keyed by name (`health_rules.evaluate_snapshot`), while
 * `HostSection` (frozen, with its own `name` field) implies an array — and
 * `attention` itself is the in-flight addition types.ts documents as NEW.
 * Both shapes are accepted so this view doesn't break depending on which
 * side of that in-flight change lands first.
 */
export interface AgentMeta {
  hostname?: string
  os?: string
  version?: string
  arch?: string
  channel?: string
  [key: string]: unknown
}

/** A raw per-section telemetry payload — `status`/`summary` always present
 * (docs/protocol.md, "Telemetry sections"), everything else is section-specific
 * and walked generically. */
export interface RawSection {
  status: Severity
  summary?: string
  reason?: string
  [key: string]: unknown
}

export interface AgentDetail {
  agent_id: string
  online: boolean
  os: string
  meta: AgentMeta
  collected_at: string | null
  snapshot: Record<string, RawSection> | null
  health: { overall: Severity; sections: HostSection[] }
  governance: { supported: boolean }
  ai_enabled: boolean
  history: { collected_at: string; overall: Severity }[]
}

/** Accepts either the dict-keyed-by-name shape the live handler returns today
 * or the frozen `HostSection[]` array shape, and always returns the array
 * shape this view renders from. Pure reshaping, not health logic — the
 * `status`/`attention` values themselves are never touched or re-derived. */
export function normalizeSections(raw: unknown): HostSection[] {
  if (Array.isArray(raw)) return raw as HostSection[]
  if (raw && typeof raw === 'object') {
    return Object.entries(raw as Record<string, Record<string, unknown>>).map(([name, s]) => ({
      name,
      status: (s.status as Severity) ?? 'unknown',
      attention: Boolean(s.attention ?? (s.status === 'warn' || s.status === 'crit')),
      reason: s.reason as string | undefined,
      summary: s.summary as string | undefined,
    }))
  }
  return []
}

/* ── Local accounts (snapshot.local_accounts — docs/protocol.md "local_accounts") ── */

export interface LocalAccount {
  name: string
  display?: string
  kind: 'local' | 'microsoft' | 'entra' | 'unknown'
  enabled: boolean
  is_admin: boolean
  password_required?: boolean
  password_last_set?: string | null
  last_logon?: string | null
  builtin_admin?: boolean
  builtin_guest?: boolean
  deny_logon?: string[]
  unsupported?: Record<string, string>
}

export interface LocalAccountsSection extends RawSection {
  accounts: LocalAccount[]
  admins: string[]
  count: number
  password_policy?: {
    applies_to: string
    min_length?: number
    max_age_days?: number
    lockout_threshold?: number
    unsupported?: Record<string, string>
  }
}

export type AccountActionResult =
  | { ok: true; result: unknown }
  | { ok: false; error: 'disabled' | 'blocked' | 'unsupported' | string; message?: string }

/* ── Disk (snapshot.disk / snapshot.disk_smart) ── */

export interface DiskVolume {
  mount: string
  total_bytes: number
  free_bytes: number
  percent_used: number
}

export interface DiskTopDir {
  path: string
  bytes: number
}

export interface DiskSection extends RawSection {
  volumes: DiskVolume[]
  top_dirs: DiskTopDir[]
}

/* ── Web filter (GET/PUT/POST/DELETE /api/agent/{id}/webfilter*) ── */

export interface WebfilterConfig {
  agent_id: string
  enabled: boolean
  block_mode: boolean
  use_external_adult: boolean
  use_bypass_protection: boolean
  doh_policy: 'disable' | 'leave'
  updated_at: string | null
  applied_hash: string | null
  applied_at: string | null
  applied_ok: boolean | null
}

export type WebfilterDomainAction = 'watch' | 'block' | 'allow'

export interface WebfilterDomain {
  domain: string
  action: WebfilterDomainAction
  note: string | null
  added_at: string
}

export interface WebfilterOverview {
  agent_id: string
  config: WebfilterConfig
  custom: WebfilterDomain[]
  seed_count: number
  external: {
    adult: { enabled: boolean; [key: string]: unknown }
    bypass: { enabled: boolean; [key: string]: unknown }
  }
  applied: { hash: string | null; at: string | null; ok: boolean | null }
  current_hash: string
  drift: boolean
}

export type WebfilterActionResult = { ok: true; [key: string]: unknown } | { ok: false; error: string }

/* ── Reliability suppressions (/api/reliability/suppressions) ── */

export interface SuppressionRule {
  id: string
  agent_id: string
  source: string
  event_id: number
  note: string
  created_by: string
  created_at: string
}

export interface ReliabilityEvent {
  source: string
  event_id: number
  level: string
  count: number
  sample: string
  last_seen: string
  by_day: Record<string, number>
  suppressed?: boolean
  suppressed_by?: { id: string; scope: 'host' | 'fleet'; source: string; event_id: number; note: string }
  category?: string
  severity?: 'benign' | 'notable' | 'serious' | 'unknown'
  suspected_cause?: string
}

export interface ReliabilitySection extends RawSection {
  stability_index: number | null
  recent_crashes: number
  window_days: number
  events: ReliabilityEvent[]
  truncated: boolean
}

/* ── Recommendation stream — extends the frozen ChatEvent vocabulary ──
 *
 * `remediation` (`{type, available, prompt}`) isn't in types.ts's `ChatEvent`
 * union — it's the recommendation stream's one addition
 * (notes/api-contract-actual.md §2, `recommend.py::_parse_remediation`).
 * `streamChatEvents` is typed to yield `ChatEvent`; events from the
 * recommendation stream are cast through this wider type at the point of
 * use rather than left silently mistyped. */
export type RecommendationEvent =
  | { type: 'text_delta'; text: string }
  | { type: 'remediation'; available: boolean; prompt: string }
  | { type: 'done'; session_id?: string }
  | { type: 'error'; error: string; session_id?: string }

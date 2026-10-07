import type { HostSection, Severity, ShareLinkResponse } from '../../api/types'

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
      tier: (s.tier as HostSection['tier']) ?? (s.status === 'posture' ? 'posture' : s.status === 'warn' || s.status === 'crit' ? 'incident' : 'none'),
      reason: s.reason as string | undefined,
      summary: s.summary as string | undefined,
      since: (s.since as string | null | undefined) ?? null,
      age_seconds: (s.age_seconds as number | null | undefined) ?? null,
      details: s.details as Record<string, unknown> | undefined,
    }))
  }
  return []
}

/* ── Availability (GET /api/agent/{id}/availability?days=N) ──
 *
 * `online` — the host's agent held its tunnel to the kenny server;
 * `offline` — the server was running but the host was not connected;
 * `unknown` — the server itself was down, or the host was not enrolled yet.
 * All instants are ISO-8601 UTC. */

export type AvailabilityState = 'online' | 'offline' | 'unknown'

export interface AvailabilitySegment {
  start: string
  end: string
  state: AvailabilityState
  /** Reconstructed from telemetry arrival times (~15-min resolution) rather
   * than recorded exactly — the stretch before `ledger_since`. */
  approx: boolean
}

export interface AgentAvailability {
  agent_id: string
  /** Current tunnel state. */
  online: boolean
  window: { start: string; end: string }
  /** Oldest first, contiguous, covering `window`. */
  segments: AvailabilitySegment[]
  /** Reboot instants inside the window, oldest first. */
  boots: string[]
  /** 0..100, one decimal, over known time only; null when none is known. */
  online_pct: number | null
  totals: { online_secs: number; offline_secs: number; unknown_secs: number }
  /** Before this instant segments are reconstructed (`approx: true`). */
  ledger_since: string | null
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

/* ── Physical-disk health (snapshot.disk_smart — docs/protocol.md "disk_smart") ──
 *
 * One row per physical disk. Every counter is lifetime and `null`/absent means
 * "the drive or its driver does not report it", which is unknown, never zero.
 * The 0.22 fields are additive, so every one is optional: an older agent's row
 * carries only the first group. */

/** NVMe SMART / health log (page 0x02). 128-bit counters saturate at 2^64-1,
 * so values above 2^53 lose precision here. */
export interface DiskNvmeHealth {
  /** Bitfield: 0 spare below threshold, 1 temperature, 2 reliability, 3 read-only, 4 volatile backup. */
  critical_warning?: number | null
  available_spare?: number | null
  available_spare_threshold?: number | null
  /** May exceed 100. */
  percentage_used?: number | null
  media_errors?: number | null
  unsafe_shutdowns?: number | null
  error_log_entries?: number | null
  data_units_written?: number | null
  power_on_hours?: number | null
  temperature_c?: number | null
}

export interface DiskSmartRow {
  model?: string | null
  health_status?: string | null
  /** The drive's own SMART predict-failure flag; null is unknown, not false. */
  predictive_failure?: boolean | null
  wear?: number | null
  temperature_c?: number | null
  power_on_hours?: number | null
  read_errors_total?: number | null
  read_errors_uncorrected?: number | null
  write_errors_uncorrected?: number | null
  device_number?: number | null
  serial?: string | null
  bus_type?: string | null
  media_type?: string | null
  size_bytes?: number | null
  removable?: boolean | null
  temperature_max_c?: number | null
  /** ATA attribute id (decimal string) → raw value; only ids the drive reports. */
  smart_attributes?: Record<string, number | null> | null
  nvme?: DiskNvmeHealth | null
  /** Why `nvme` is null on an NVMe disk. Never to be read as healthy. */
  nvme_error?: string | null
  /** Raw-device reads were skipped while anti-cheat coexistence is active. */
  paused?: boolean | null
}

export interface DiskSmartSection extends RawSection {
  disks?: DiskSmartRow[]
}

/* ── Hardware errors (snapshot.hardware_errors) ── */

export interface HardwareErrorGroup {
  source: string
  event_id: number
  level: string
  count: number
  last_seen?: string | null
  by_day?: Record<string, number> | null
  sample?: string | null
  /** key → { value: count }; counts cover only the newest ~10 events of the group. */
  details?: Record<string, Record<string, number>> | null
}

export interface HardwareAppCrashes {
  total?: number | null
  distinct_apps?: number | null
  distinct_modules?: number | null
  exception_codes?: Record<string, number> | null
  by_day?: Record<string, number> | null
}

/** One memory controller (Linux). */
export interface HardwareEdacEntry {
  controller: string
  ce_count?: number | null
  ue_count?: number | null
}

/** One PCIe device with a non-zero AER total (Linux). */
export interface HardwareAerEntry {
  device: string
  correctable?: number | null
  nonfatal?: number | null
  fatal?: number | null
}

export interface HardwareErrorsSection extends RawSection {
  window_days?: number | null
  /** How far back the queried log really reaches; shorter than `window_days` when the log wrapped. */
  effective_window_days?: number | null
  oldest_event_utc?: string | null
  sources?: string[]
  groups?: HardwareErrorGroup[]
  truncated?: boolean
  truncated_count?: number
  app_crashes?: HardwareAppCrashes | null
  edac?: HardwareEdacEntry[]
  aer?: HardwareAerEntry[]
  errors?: string[]
}

/* ── GPU (snapshot.gpu) ── */

export interface GpuPcie {
  gen_current?: number | null
  gen_max?: number | null
  width_current?: number | null
  width_max?: number | null
}

export interface GpuThrottle {
  hw_slowdown?: boolean | null
  hw_thermal_slowdown?: boolean | null
  hw_power_brake_slowdown?: boolean | null
  sw_thermal_slowdown?: boolean | null
}

export interface GpuEcc {
  uncorrected_volatile?: number | null
  retired_pages_pending?: boolean | null
  remapped_rows?: {
    correctable?: number | null
    uncorrectable?: number | null
    pending?: boolean | null
    failure?: boolean | null
  } | null
}

export interface GpuCard {
  name: string
  vendor?: 'nvidia' | 'amd' | 'intel' | 'unknown' | string
  pci_id?: string | null
  bus_id?: string | null
  uuid?: string | null
  driver_version?: string | null
  sources?: string[]
  temperature_c?: number | null
  utilization_percent?: number | null
  power_draw_w?: number | null
  power_limit_w?: number | null
  /** The driver's *target* fan speed, not a measured one. */
  fan_target_percent?: number | null
  pcie?: GpuPcie | null
  throttle?: GpuThrottle | null
  ecc?: GpuEcc | null
  /** amdgpu RAS block → error counts. */
  ras?: Record<string, { ue?: number | null; ce?: number | null }> | null
}

export interface GpuSection extends RawSection {
  gpus?: GpuCard[]
  truncated?: boolean
  errors?: string[]
}

/* ── Fans (snapshot.fans) ── */

export interface FanReading {
  key: string
  label?: string | null
  source?: 'hwmon' | 'lhm' | 'nvml' | string
  /** A burst of samples, in order. */
  rpm_samples?: number[] | null
  duty_percent?: number | null
  mode?: 'pwm' | 'dc' | 'auto' | 'manual' | 'unknown' | string | null
  /** Every sample 0 RPM and no duty readable: an unused channel, not a stall. */
  idle_or_absent?: boolean
}

export interface FansSection extends RawSection {
  sources_tried?: string[]
  sample_interval_ms?: number | null
  fans?: FanReading[]
  truncated?: boolean
  errors?: string[]
}

/* ── Web filter (GET/PUT/POST/DELETE /api/agent/{id}/webfilter*) ──
 *
 * Categories, schedule and bypass requests (ADR-0055,
 * `kenny_server/webfilter.py::CATEGORY_CATALOG`/`schedule_state`,
 * `webui/__init__.py::_webfilter_overview`/`api_webfilter_requests`).
 * `use_external_adult`/`use_bypass_protection` are still on the wire (the
 * two legacy toggle columns the server merges into `categories`) but are
 * deliberately not surfaced as separate controls — `adult` and `bypass`
 * render as ordinary rows in the category list instead.
 */

/** `describe_categories()` — the catalog, not a per-host setting. */
export interface WebfilterCategory {
  key: string
  label: string
  /** External: fetched upstream (`ExternalListCache`). Local: gathers only the
   * per-host custom entries tagged with this category. */
  external: boolean
  /** False only for `bypass` — deliberately uncapped so it can't be silently thinned. */
  capped: boolean
}

/** One entry of `_webfilter_overview`'s `external` map, keyed by category key. */
export interface WebfilterExternalStat {
  count: number
  last_fetch: string | null
  enabled: boolean
}

/** What the filter does on a host (ADR-0069): nothing, record and alarm on
 * matches, or also block them. */
export type WebfilterEnforcement = 'off' | 'log_only' | 'protect'

/** What the server keeps of a host's observed domains (ADR-0069): only the
 * matches against the filter, or every domain (a browsing profile). */
export type WebfilterHistory = 'violations' | 'full'

export interface WebfilterConfig {
  agent_id: string
  enforcement: WebfilterEnforcement
  history: WebfilterHistory
  /** Whether the host is told to collect web activity at all — derived
   * server-side from the two settings above; never re-derive it here. */
  collecting: boolean
  /** Legacy views of `enforcement`, kept for older API callers. */
  enabled: boolean
  block_mode: boolean
  use_external_adult: boolean
  use_bypass_protection: boolean
  /** Canonical merged set — the two legacy toggles above are already folded
   * in (`WebFilterStore._merge_categories`). This is the one list to read or
   * write; a category is enabled iff its key is in here. */
  categories: string[]
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
  /** Null for an entry that always applies (the pre-category shape). Tagged
   * only applies while its category is on — by the host's own toggles or by
   * an open schedule window. */
  category: string | null
  added_at: string
}

/** `ScheduleWindow.as_dict()` — one recurring per-host window. */
export interface WebfilterScheduleWindow {
  id: string
  agent_id: string
  label: string
  days: number[]
  day_keys: string[]
  start: string
  end: string
  wraps_midnight: boolean
  categories: string[]
  timezone: string
  enabled: boolean
  created_at: string
}

/**
 * `schedule_state()` — the observable answer to the two questions an
 * operator has about a host's schedule: is the stricter list in force right
 * now, and when does it revert. Instants are UTC (`*_at`) with a
 * zone-localized twin (`*_local`) for display; never re-derive one from the
 * other client-side.
 */
export interface WebfilterScheduleState {
  now: string
  timezone: string
  local_now: string
  base_categories: string[]
  extra_categories: string[]
  effective_categories: string[]
  active_windows: WebfilterScheduleWindow[]
  stricter: boolean
  next_change_at: string | null
  next_change_local: string | null
  /** Set only while `stricter` — the instant the extra categories drop off. */
  reverts_at: string | null
  windows: WebfilterScheduleWindow[]
}

/** `ListTooLargeError` surfaced as state, not an error banner — the overview
 * still reads so the operator can see which category to turn off. */
export interface WebfilterOversize {
  count: number
  cap: number
  over_by: number
}

export interface WebfilterOverview {
  agent_id: string
  config: WebfilterConfig
  custom: WebfilterDomain[]
  seed_count: number
  external: Record<string, WebfilterExternalStat>
  categories: WebfilterCategory[]
  schedule: WebfilterScheduleState
  applied: { hash: string | null; at: string | null; ok: boolean | null }
  /** Null when the effective list is over the cap (see `oversize`). */
  current_hash: string | null
  oversize: WebfilterOversize | null
  drift: boolean
}

export type WebfilterActionResult =
  | { ok: true; [key: string]: unknown }
  | { ok: false; error: string; message?: string; count?: number; cap?: number }

/** `api_webfilter_requests` — pending bypass-request tickets for one host.
 * `ticket` is the ticket row verbatim (`Ticket.as_dict()`); this view only
 * ever reads `id`/`number`/`title`/`summary`/`created_at` from it and links
 * the rest to the Inbox rather than modeling the whole ticket shape. */
export interface WebfilterBypassRequest {
  ticket: {
    id: string
    number: number
    title: string
    summary: string
    state: string
    created_at: string
    [key: string]: unknown
  }
  requested_domains: string[]
}

export interface WebfilterRequestsResponse {
  agent_id: string
  requests: WebfilterBypassRequest[]
}

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
  /** What a person at the machine would have noticed -- the only field health is scored on. */
  user_impact?: UserImpact
  /** That impact in one plain sentence, empty when there is none. */
  symptom?: string
  /** Why this group does or does not carry a verdict. */
  classification_state?: 'classified' | 'pending' | 'unavailable'
}

/**
 * Least to most consequential. `unknown` is the classifier saying it cannot
 * tell, which is deliberately not the same claim as `none`.
 */
export type UserImpact = 'none' | 'degraded' | 'crashed' | 'data_at_risk' | 'unknown'

/**
 * One reliability pattern's activity record, as `health_rules.reliability_patterns`
 * derives it from the group's `by_day`/`last_seen` and the categoriser's verdict
 * (ADR-0058). Carried on the section's `HostSection.details.patterns`; the three
 * booleans are the server's, the console only labels them (`activityLabel`).
 */
export interface ReliabilityPattern {
  source: string | null
  event_id: number | null
  level: string | null
  count: number
  severity: 'benign' | 'notable' | 'serious' | 'unknown'
  category: string | null
  cause: string | null
  user_impact: UserImpact
  symptom: string
  classification_state: 'classified' | 'pending' | 'unavailable' | 'unclassified'
  suppressed: boolean
  /** Distinct days in the window with at least one event. */
  active_days: number
  first_day: string | null
  last_day: string | null
  last_seen_age_hours: number | null
  /** Still happening: seen within the last two days, or on most days of the window. */
  active: boolean
  /** Seen on more than one day -- not a one-off. */
  recurring: boolean
  /** One day holds most of the count and it has gone quiet since -- a storm, not a drip. */
  burst: boolean
  /** Whether this pattern is a finding at all: an active, unsuppressed impact. */
  scores: boolean
}

export interface ReliabilityDetails {
  patterns: ReliabilityPattern[]
  window_days: number
  /** How many times the host booted inside the window. */
  boot_sessions?: number
}

export interface ReliabilitySection extends RawSection {
  /* All raw fields are optional: a collector whose probe failed reports only
   * `status` + `summary` ("reliability unavailable"), so that a failed reading
   * is not mistaken for a reading of zero. See docs/protocol.md. */
  stability_index?: number | null
  recent_crashes?: number
  window_days?: number
  events?: ReliabilityEvent[]
  truncated?: boolean
  /** How many groups the agent's cap dropped, so the total can be reconciled. */
  truncated_count?: number
  /** UTC boot instants inside the window. */
  boot_sessions?: string[]
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

/* ── Re-share (host action row, `POST /api/agents/share-link`) ──
 *
 * One endpoint, one type. `oneliner` now lives on `ShareLinkResponse` itself
 * (api/types.ts), because both callers of this route need it: the host action
 * row's re-share AND the Add-a-PC wizard. While the field was declared only
 * here, the wizard used the narrower shared type and silently dropped the Linux
 * install command — so re-sharing a host handed you more than creating it did.
 * This alias is kept for the existing import sites. */
export type ShareLinkResult = ShareLinkResponse

import type { AdminSection, ConfigSource, SettingRow } from '../../api/types'

/* ── Settings catalog (raw wire shape) ──────────────────────────────────────
 *
 * `AdminSection`/`SettingRow` (types.ts) describe the CONCEPT the console
 * renders (key/label/rows, editable). The wire response
 * (`config.py::Settings.describe`) uses different field names — `name`/
 * `slug`/`settings`, and each row carries `lifecycle`/raw `source` (`db` for
 * an override, not `custom`) plus type metadata the frozen `SettingRow`
 * doesn't carry. `settingsMap.ts` translates one into the other; these
 * interfaces describe the wire side of that translation only.
 */

export type RawSettingType = 'bool' | 'int' | 'float' | 'str' | 'enum' | 'secret'
export type RawSettingSource = 'db' | 'env' | 'default'

export interface RawSettingRow {
  key: string
  group: string
  type: RawSettingType
  label: string
  help: string
  lifecycle: 'live' | 'restart' | 'env_only'
  source: RawSettingSource
  editable: boolean
  choices: string[] | null
  min: number | null
  max: number | null
  sensitive: boolean
  /** A `restart` setting whose stored value differs from the one the server is running with. */
  pending_restart: boolean
  value: string | number | boolean | null
  is_set?: boolean
  default: string | number | boolean | null
}

export interface RawSettingGroup {
  name: string
  slug: string
  settings: RawSettingRow[]
}

export interface RawSettingsResponse {
  groups: RawSettingGroup[]
}

/**
 * `AdminRow` extends the frozen `SettingRow` with the raw type metadata
 * needed to render the right editor (a toggle for `bool`, a `<select>` for
 * `enum`, min/max on a number input) and to reconstruct the "not set"
 * distinction a masked secret's `value: null` alone can't carry.
 */
export interface AdminRow extends SettingRow {
  type: RawSettingType
  choices: string[] | null
  min: number | null
  max: number | null
  isSet: boolean
  lifecycle: RawSettingRow['lifecycle']
  pendingRestart: boolean
}

export interface MappedAdminSection extends AdminSection {
  rows: AdminRow[]
}

export const CONFIG_SOURCE_COLOR: Record<ConfigSource, string> = {
  default: 'var(--text-faint)',
  env: 'var(--text-muted)',
  db: 'var(--brass-600)',
}

/**
 * The server's vocabulary is the wire value; these are the words the operator
 * reads. `db` — an override stored in the database — is shown as "custom",
 * which is what the design calls it and what the previous dashboard displayed.
 * The translation lives here and nowhere else.
 */
export const CONFIG_SOURCE_LABEL: Record<ConfigSource, string> = {
  default: 'DEFAULT',
  env: 'ENV',
  db: 'CUSTOM',
}

/* ── Backup ──────────────────────────────────────────────────────────────── */

export interface BackupPushStatus {
  target: string
  ok: boolean
  error?: string
}

export interface BackupEntry {
  name: string
  created_at: string
  size: number
  sha256: string
  integrity: string
  trigger: string
  targets: { target: string }[]
  push_status?: BackupPushStatus[]
}

export type BackupTargetKind = 'http' | 'scp' | 'ftp'

export interface BackupTarget {
  id: string
  kind: BackupTargetKind
  label: string
  enabled?: boolean
  config: Record<string, unknown>
}

export interface BackupConfig {
  interval_secs: number | null
  retention: number | null
  backup_dir: string | null
}

export interface BackupsResponse {
  backups: BackupEntry[]
  config: BackupConfig
  targets: BackupTarget[]
}

export interface BackupVerifyResult {
  ok: boolean
  integrity?: string
  error?: string
}

/* ── Updates ─────────────────────────────────────────────────────────────── */

export interface UpdateAvailability {
  component: string
  version: string | null
  url: string | null
  sha256: string | null
  digest: string | null
  ok: boolean
  message: string | null
  checked_at: string | null
}

export interface UpdateCampaign {
  id: string
  channel: 'stable' | 'dev'
  version: string
  on_connect: boolean
  status: 'active' | 'suspended' | 'revoked' | 'expired' | 'completed'
  expires_at: string | null
  created_at: string
}

export interface UpdateAgentRow {
  agent_id: string
  online: boolean
  os: string
  arch: string
  channel: string
  desired_channel: string
  current_version: string | null
  eligible: boolean
  attempts: number
  held: boolean
  updated: boolean
}

export interface UpdatesResponse {
  /** Keyed by `"agent"`/`"server"` (stable) or `"agent:dev"`/`"server:dev"` (`store._availability_key`). */
  available: Record<string, UpdateAvailability>
  active_campaign: UpdateCampaign | null
  campaigns: UpdateCampaign[]
  agents: UpdateAgentRow[]
  active_campaign_dev: UpdateCampaign | null
  campaigns_dev: UpdateCampaign[]
  agents_dev: UpdateAgentRow[]
  server_apply: { tag: string; digest?: string; command: string | null } | null
  config: {
    check_interval_secs: number | null
    rollout_on_connect: boolean | null
    server_image_ref: string | null
  }
}

/* ── Discord ─────────────────────────────────────────────────────────────── */

export interface DiscordStatus {
  configured: boolean
  connected: boolean
  guilds?: string[]
  support_channel_id?: string | null
  operator_channel_id?: string | null
  missing_message_content?: boolean
  startup_error?: string | null
  model?: string | null
}

export interface DiscordIdentity {
  discord_user_id: string
  user_id: number
  guild_id: string
  linked_at: string
  linked_by: number | null
  linked_via: string
  disabled: boolean
}

export interface DiscordClaim {
  code: string
  discord_user_id: string
  display_hint: string
  guild_id: string
  created_at: string
  expires_at: string
  consumed_at: string | null
  consumed_by: number | null
}

export interface DiscordMember {
  user_id: string
  display_hint: string
}

/* ── Auto-ticket rules ───────────────────────────────────────────────────── */

export type TicketRuleEventType = 'health' | 'offline' | 'disk_forecast' | 'hardware_forecast' | 'change'
export type TicketRuleDecision = 'open_all' | 'open_crit' | 'never'

export interface TicketRule {
  id: string
  agent_id: string
  event_type: TicketRuleEventType
  section: string
  decision: TicketRuleDecision
  note: string
  created_by: string
  created_at: string
}

export interface TicketRuleVocabulary {
  event_types: TicketRuleEventType[]
  decisions: TicketRuleDecision[]
  sections: Record<string, string[]>
}

/* ── Users ───────────────────────────────────────────────────────────────── */

export interface AdminUser {
  id: number
  username: string
  email: string | null
  role: 'superuser' | 'operator' | 'user'
  avatar: string | null
  disabled: boolean
  totp_enabled: boolean
  capability_profile: string | null
  created_at: string
  updated_at: string
  hosts?: string[]
  pats?: { id: number; label: string | null; created_at: string; last_used: string | null; revoked: boolean }[]
}

export interface ToolClassesResponse {
  profiles: Record<string, string[]>
  classes: Record<string, string>
}

/**
 * One policy rule, in the shape the wire contract uses for both the shared deny
 * catalog and an allow rule (`docs/protocol.md` § `policy`).
 */
export interface PolicyRule {
  id: string
  applies_to: string
  pattern: string
  reason: string
}

/** `GET /api/policy/rules` — the compiled-in catalog plus the operator's additions. */
export interface PolicyRulesResponse {
  builtin: PolicyRule[]
  operator: PolicyRule[]
}

/**
 * `GET /api/policy/shell-allow` — the fleet shell execution mode, its allow rules, and the
 * rules the server ships (what a new install starts with and a reset restores).
 */
export interface ShellAllowResponse {
  mode: string
  allow: PolicyRule[]
  defaults: PolicyRule[]
}

/* ── Specialized agents (ADR-0071, ADR-0072) ─────────────────────────────── */

export type AgentMode = 'off' | 'shadow' | 'act'
export type ToolTier = 'read_only' | 'standard_change' | 'normal_change'
export type AgentRunStatus = 'running' | 'completed' | 'failed' | 'skipped'
export type AuthorizationStatus = 'live' | 'revoked' | 'voided' | 'expired'

export interface AgentTrigger {
  kind: 'event' | 'schedule' | 'on_demand'
  event: string | null
}

/**
 * One argument of one change-tier tool (`agents/spec.py::ArgConstraint.to_dict`).
 * `allowed` is a literal set the spec fixes; `param` names the agent parameter that
 * feeds the values at run start. `evidence` is the third kind (ADR-0072 rule 6:
 * values computed at run start from the server's own records); the server does not
 * send it yet, so the view renders it when present and treats a constraint with
 * neither `allowed` nor `param` nor `evidence` as an empty literal set.
 */
export interface AgentConstraint {
  tool: string
  arg: string
  allowed?: string[]
  param?: string
  evidence?: string
}

export interface AgentTimeout {
  tool: string
  max_s: number
}

/** `window` as `validate_params` stores it: weekday keys (`mon`..`sun`), `HH:MM`, an IANA zone. */
export interface AgentWindow {
  days: string[]
  start: string
  end: string
  tz: string
}

/** The parameters an agent takes, by name. A name the spec does not declare never appears. */
export interface AgentParamValues {
  window?: AgentWindow
  hosts?: string[]
  packages?: string[]
  require_idle?: boolean
  [name: string]: unknown
}

/** An entry of a run's `actions` or `recommendations` — one tool call (`agents/policy.py::_record`). */
export interface AgentCall {
  tool: string
  args: Record<string, unknown>
  agent_id: string | null
  tool_class: ToolTier
  authorization_id?: string
  ok?: boolean
  code?: string | null
}

/** `agents/store.py::AgentRun.to_public`. */
export interface AgentRun {
  id: string
  agent_id: string
  spec_hash: string
  trigger: string
  subject: string | null
  host_id: string | null
  mode: AgentMode
  status: AgentRunStatus
  verdict: string | null
  summary: string | null
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_creation_tokens: number
  ticket_id: string | null
  error: string | null
  actions: AgentCall[]
  recommendations: AgentCall[]
  started_at: string
  finished_at: string | null
  params: AgentParamValues | null
  effective_hash: string | null
}

/** One entry of `GET /api/specialized-agents` (`agents/runner.py::overview`). */
export interface SpecializedAgent {
  id: string
  title: string
  description: string
  trigger: AgentTrigger
  tools: string[]
  tool_classes: Record<string, ToolTier>
  /**
   * Where each tool runs: on a PC (`'host'`) or on the server (`'server'`). Absent on a
   * server that predates it; a consumer then offers both.
   */
  tool_targets?: Record<string, 'host' | 'server'>
  verdict_tool: string | null
  budget: { max_iterations: number }
  constraints: AgentConstraint[]
  timeouts: AgentTimeout[]
  sensitive_ok: boolean
  default_mode: AgentMode
  version: number
  spec_hash: string
  mode: AgentMode
  /** The values on this install (the overview replaces the spec's list of names with them). */
  params: AgentParamValues
  effective_hash: string | null
  /** `null` for triage, whose `act` is its settings rather than a binding to a hash. */
  act_bound: boolean | null
  latest_run: AgentRun | null
}

export interface SpecializedAgentsResponse {
  enabled: boolean
  agents: SpecializedAgent[]
}

/** `GET /api/specialized-agents/{id}/params` — `declared` is what the spec takes. */
export interface AgentParamsResponse {
  agent_id: string
  declared: string[]
  params: AgentParamValues
  effective_hash: string | null
}

/** `PUT /api/specialized-agents/{id}/params`. */
export interface AgentParamsSaved {
  agent_id: string
  params: AgentParamValues
  effective_hash: string
  mode: AgentMode
  voided: number
}

export interface AgentAuthorization {
  id: string
  agent_id: string
  effective_hash: string
  tool: string
  /** Host ids, or the sentinel `"server"`. */
  scope: string[] | string
  max_attempts_per_day: number
  expires_at: string
  granted_by: string
  granted_at: string
  revoked_at: string | null
  revoked_by: string | null
  voided_at: string | null
  voided_by: string | null
  note: string
  status: AuthorizationStatus
  /** Attempts spent in the last 24 hours, per host (`server` for a change that names none). */
  attempts_last_24h: Record<string, number>
}

export interface AgentAuthorizationsResponse {
  agent_id: string
  effective_hash: string | null
  authorizations: AgentAuthorization[]
}

/** `PUT /api/specialized-agents/{id}/mode` — `mode` is the one now in force, which for triage can differ from `requested`. */
export interface AgentModeSet {
  agent_id: string
  mode: AgentMode
  requested: AgentMode
}

/** `POST /api/specialized-agents/{id}/runs` (preview, always shadow) answers 202. */
export interface AgentPreviewStarted {
  run_id: string
}

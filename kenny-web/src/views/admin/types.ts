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

export type TicketRuleEventType = 'health' | 'offline' | 'disk_forecast' | 'change'
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

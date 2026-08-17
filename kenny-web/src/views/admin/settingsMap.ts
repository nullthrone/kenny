import type { ConfigSource } from '../../api/types'
import type { AdminRow, MappedAdminSection, RawSettingGroup, RawSettingRow, RawSettingsResponse } from './types'

function mapSource(raw: RawSettingRow['source']): ConfigSource {
  // The wire format calls a persisted override "db"; the console's vocabulary is "custom".
  return raw === 'db' ? 'custom' : raw
}

function displayValue(row: RawSettingRow): string | number | boolean | null {
  if (row.sensitive) return row.is_set ? 'set' : 'not set'
  return row.value
}

function mapRow(raw: RawSettingRow): AdminRow {
  const source = mapSource(raw.source)
  return {
    key: raw.key,
    label: raw.label,
    help: raw.help,
    value: displayValue(raw),
    source,
    // The server rejects a write to an env-sourced key with 403 regardless of
    // lifecycle — never render a control that is guaranteed to fail.
    editable: source !== 'env',
    type: raw.type,
    choices: raw.choices,
    min: raw.min,
    max: raw.max,
    isSet: raw.sensitive ? Boolean(raw.is_set) : raw.value !== null && raw.value !== '',
  }
}

function mapGroup(group: RawSettingGroup): MappedAdminSection {
  return { key: group.slug, label: group.name, rows: group.settings.map(mapRow) }
}

/** The eleven real config groups from `GET /api/settings`, mapped to the console's `AdminSection` shape. */
export function mapSettingsGroups(raw: RawSettingsResponse): MappedAdminSection[] {
  return raw.groups.map(mapGroup)
}

/**
 * The synthetic `environment` section: every row across every group whose
 * source is `env`, read-only, composed client-side because the server has
 * no such group of its own (types.ts's `AdminSectionKey` doc comment).
 */
export function buildEnvironmentSection(groups: MappedAdminSection[]): MappedAdminSection {
  const rows = groups.flatMap((g) => g.rows.filter((r) => r.source === 'env'))
  return { key: 'environment', label: 'Environment', rows }
}

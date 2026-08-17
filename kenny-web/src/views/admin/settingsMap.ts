import type { ConfigSource } from '../../api/types'
import type { AdminRow, MappedAdminSection, RawSettingGroup, RawSettingRow, RawSettingsResponse } from './types'

function displayValue(row: RawSettingRow): string | number | boolean | null {
  if (row.sensitive) return row.is_set ? 'set' : 'not set'
  return row.value
}

function mapRow(raw: RawSettingRow): AdminRow {
  const source: ConfigSource = raw.source
  return {
    key: raw.key,
    label: raw.label,
    help: raw.help,
    value: displayValue(raw),
    source,
    editable: raw.editable,
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

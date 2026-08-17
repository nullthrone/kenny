import { Link } from 'react-router'
import { useQueries, useQuery } from '@tanstack/react-query'
import { api } from '../../../api/client'
import type { FleetResponse } from '../../../api/types'
import type { AdminRow } from '../types'
import GenericSettingsSection from './GenericSettingsSection'
import shared from '../shared.module.css'

export interface WebFilterSectionProps {
  rows: AdminRow[]
}

interface HostWebfilterToggleConfig {
  enabled: boolean
  block_mode: boolean
  use_external_adult: boolean
  use_bypass_protection: boolean
  doh_policy: string | null
}

/** `GET /api/agent/{id}/webfilter` — `webui/__init__.py::_webfilter_overview`. */
interface HostWebfilterOverview {
  agent_id: string
  config: HostWebfilterToggleConfig
}

/**
 * Admin → Web filter. The design shows a fleet-level roster (which hosts
 * have filtering on); the underlying config is per-host
 * (`GET /api/agent/{id}/webfilter`, owned by the Host page — editing stays
 * there). This section renders the roster read-only plus the group's own
 * fleet-wide tunables (external list refresh, source URLs, max block
 * domains) via the generic renderer, and links each host to its own page.
 */
export default function WebFilterSection({ rows }: WebFilterSectionProps) {
  const fleet = useQuery({ queryKey: ['fleet'], queryFn: () => api.get<FleetResponse>('/api/fleet') })
  const agentIds = fleet.data?.agents.map((a) => a.agent_id) ?? []

  const configs = useQueries({
    queries: agentIds.map((id) => ({
      queryKey: ['admin', 'webfilter-roster', id],
      queryFn: (): Promise<HostWebfilterOverview | null> =>
        api.get<HostWebfilterOverview>(`/api/agent/${id}/webfilter`).catch(() => null),
    })),
  })

  return (
    <div>
      <div className={shared.cardTitle}>HOSTS</div>
      {fleet.isLoading ? (
        <div className={shared.loading}>Loading…</div>
      ) : agentIds.length === 0 ? (
        <p className={shared.help}>No hosts in the fleet yet.</p>
      ) : (
        <div className={shared.table} style={{ marginBottom: 24 }}>
          {agentIds.map((id, i) => {
            const c = configs[i]?.data?.config
            return (
              <div key={id} className={shared.tableRow}>
                <div className={shared.tableMeta}>
                  <div className={shared.tableLabel}>{id}</div>
                  <div className={shared.tableSub}>
                    {c ? (c.enabled ? `filtering on · ${c.block_mode ? 'block' : 'monitor'} mode` : 'filtering off') : '…'}
                  </div>
                </div>
                <Link to={`/fleet/${id}`} className={shared.btnSmall} style={{ textDecoration: 'none', textAlign: 'center' }}>
                  OPEN HOST
                </Link>
              </div>
            )
          })}
        </div>
      )}

      <div className={shared.cardTitle}>FLEET-WIDE SETTINGS</div>
      <GenericSettingsSection rows={rows} />
    </div>
  )
}

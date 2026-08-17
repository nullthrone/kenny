import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../../api/client'
import EmptyState from '../../../components/EmptyState/EmptyState'
import type { UpdateAgentRow, UpdateCampaign, UpdatesResponse } from '../types'
import shared from '../shared.module.css'
import styles from './UpdatesSection.module.css'

type Channel = 'stable' | 'dev'

function statusChipColor(status: UpdateCampaign['status']): string {
  if (status === 'active') return 'var(--brass-600)'
  if (status === 'suspended') return 'var(--warn)'
  return 'var(--text-faint)'
}

function hostStatus(row: UpdateAgentRow, campaignVersion: string): { label: string; pct: number; color: string } {
  if (row.updated) return { label: campaignVersion, pct: 100, color: 'var(--ok)' }
  if (row.held) return { label: 'HELD', pct: 40, color: 'var(--danger)' }
  if (!row.eligible) return { label: row.online ? 'NOT ELIGIBLE' : 'ON CONNECT', pct: 0, color: 'var(--text-faint)' }
  if (!row.online) return { label: 'ON CONNECT', pct: 0, color: 'var(--text-faint)' }
  if (row.attempts > 0) return { label: 'UPDATING', pct: 60, color: 'var(--brass-600)' }
  return { label: 'QUEUED', pct: 0, color: 'var(--text-faint)' }
}

/**
 * Admin → Updates. The rollout card from the design, wired to
 * `/api/updates/campaigns`. The campaign lifecycle
 * (`update_manager.py`) is `active` → `suspended` → `active` again, or
 * → `revoked`/`expired`/`completed` (terminal). SUSPEND stops both the
 * on-connect push and `apply-now` without discarding pinned artifacts or
 * per-agent attempt history; RESUME reactivates the same campaign exactly
 * where it left off. REVOKE is the terminal, non-reversible stop.
 *
 * A suspended campaign is no longer `active_campaign` (the server only
 * ever reports one *active* campaign there) — it drops into the history
 * list with `status: "suspended"`, so RESUME renders on its history row,
 * not on the rollout card.
 */
export default function UpdatesSection() {
  const queryClient = useQueryClient()
  const [channel, setChannel] = useState<Channel>('stable')

  const query = useQuery({ queryKey: ['admin', 'updates'], queryFn: () => api.get<UpdatesResponse>('/api/updates') })

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ['admin', 'updates'] })
  }

  const check = useMutation({
    mutationFn: () => api.post<{ ok: boolean }>('/api/updates/check'),
    onSuccess: invalidate,
  })

  const approve = useMutation({
    mutationFn: (onConnect: boolean) => api.post<{ ok: boolean; campaign: UpdateCampaign }>('/api/updates/campaigns', { channel, on_connect: onConnect }),
    onSuccess: invalidate,
  })

  const applyNow = useMutation({
    mutationFn: (campaignId: string) => api.post<{ ok: boolean; attempted: string[] }>(`/api/updates/campaigns/${campaignId}/apply-now`),
    onSuccess: invalidate,
  })

  const revoke = useMutation({
    mutationFn: (campaignId: string) => api.post<{ ok: boolean }>(`/api/updates/campaigns/${campaignId}/revoke`),
    onSuccess: invalidate,
  })

  const suspend = useMutation({
    mutationFn: (campaignId: string) => api.post<{ ok: boolean }>(`/api/updates/campaigns/${campaignId}/suspend`),
    onSuccess: invalidate,
  })

  const resume = useMutation({
    mutationFn: (campaignId: string) => api.post<{ ok: boolean }>(`/api/updates/campaigns/${campaignId}/resume`),
    onSuccess: invalidate,
  })

  if (query.isLoading) return <div className={shared.loading}>Loading…</div>
  if (query.isError) return <EmptyState title="Could not load update status" message="Something went wrong. Reload to try again." />
  if (!query.data) return null

  const data = query.data
  const availKey = channel === 'stable' ? 'agent' : 'agent:dev'
  const availability = data.available[availKey]
  const activeCampaign = channel === 'stable' ? data.active_campaign : data.active_campaign_dev
  const campaigns = channel === 'stable' ? data.campaigns : data.campaigns_dev
  const agents = channel === 'stable' ? data.agents : data.agents_dev

  const stateLabel = activeCampaign
    ? activeCampaign.status === 'active'
      ? 'ROLLING OUT'
      : activeCampaign.status.toUpperCase()
    : availability?.version
      ? `${availability.version} AVAILABLE`
      : 'NO KNOWN VERSION'
  const stateColor = activeCampaign?.status === 'active' ? 'var(--brass-600)' : availability?.version ? 'var(--warn)' : 'var(--text-faint)'

  const doneCount = agents.filter((a) => a.updated).length

  return (
    <div>
      <div className={styles.channelTabs}>
        {(['stable', 'dev'] as const).map((c) => (
          <button
            key={c}
            type="button"
            className={`${styles.channelTab}${channel === c ? ` ${styles.active}` : ''}`}
            onClick={() => setChannel(c)}
          >
            {c.toUpperCase()}
          </button>
        ))}
      </div>

      {(check.isError || approve.isError || applyNow.isError || revoke.isError || suspend.isError || resume.isError) && (
        <div className={shared.errorBox}>
          {[check, approve, applyNow, revoke, suspend, resume]
            .map((m) => (m.error instanceof ApiError ? m.error.message : null))
            .find((m) => m) ?? 'Something went wrong. Try again.'}
        </div>
      )}

      <div className={shared.card}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12, flexWrap: 'wrap', marginBottom: 6 }}>
          <span className={shared.cardTitle} style={{ marginBottom: 0 }}>
            AGENT ROLLOUT
          </span>
          <span className={shared.tag} style={{ color: stateColor }}>
            {stateLabel}
          </span>
        </div>
        <div className={styles.line}>
          {agents.length} agent{agents.length === 1 ? '' : 's'} on {channel}
          {activeCampaign ? ` · pinned ${activeCampaign.version} · ${doneCount} of ${agents.length} done` : ''}
        </div>

        {activeCampaign && agents.length > 0 && (
          <div className={styles.hostList}>
            {agents.map((a) => {
              const s = hostStatus(a, activeCampaign.version)
              return (
                <div key={a.agent_id} className={styles.hostRow}>
                  <span className={styles.hostName}>{a.agent_id}</span>
                  <span className={styles.bar}>
                    <span className={styles.barFill} style={{ width: `${s.pct}%`, background: s.color }} />
                  </span>
                  <span className={styles.hostStatus} style={{ color: s.color }}>
                    {s.label}
                  </span>
                </div>
              )
            })}
          </div>
        )}

        <div className={shared.actions} style={{ marginTop: 0 }}>
          {!activeCampaign && availability?.version && (
            <button type="button" className={shared.btnPrimary} onClick={() => approve.mutate(true)} disabled={approve.isPending}>
              {approve.isPending ? 'APPROVING…' : `APPROVE ROLLOUT · PIN ${availability.version}`}
            </button>
          )}
          {activeCampaign && activeCampaign.status === 'active' && (
            <>
              <button type="button" className={shared.btn} onClick={() => applyNow.mutate(activeCampaign.id)} disabled={applyNow.isPending}>
                {applyNow.isPending ? 'APPLYING…' : 'APPLY NOW'}
              </button>
              <button type="button" className={shared.btn} onClick={() => suspend.mutate(activeCampaign.id)} disabled={suspend.isPending}>
                {suspend.isPending ? 'SUSPENDING…' : 'SUSPEND'}
              </button>
              <button type="button" className={shared.btnDanger} onClick={() => revoke.mutate(activeCampaign.id)} disabled={revoke.isPending}>
                {revoke.isPending ? 'REVOKING…' : 'REVOKE ROLLOUT'}
              </button>
            </>
          )}
          <button type="button" className={shared.btn} onClick={() => check.mutate()} disabled={check.isPending}>
            {check.isPending ? 'CHECKING…' : 'CHECK NOW'}
          </button>
        </div>

        {data.server_apply && (
          <p className={styles.serverLine}>
            Server update available ({data.server_apply.tag}) — apply with{' '}
            <code className={shared.mono}>{data.server_apply.command ?? 'docker pull && docker compose up -d'}</code>
          </p>
        )}
      </div>

      {campaigns.length > 0 && (
        <div className={styles.history}>
          <div className={styles.historyHeading}>CAMPAIGN HISTORY — {channel.toUpperCase()}</div>
          <div className={shared.table}>
            {campaigns.map((c) => (
              <div key={c.id} className={shared.tableRow}>
                <div className={shared.tableMeta}>
                  <div className={`${shared.tableLabel} ${shared.mono}`}>{c.version}</div>
                  <div className={shared.tableSub}>{new Date(c.created_at).toLocaleString()}</div>
                </div>
                <span className={shared.tag} style={{ color: statusChipColor(c.status) }}>
                  {c.status.toUpperCase()}
                </span>
                {c.status === 'suspended' && (
                  <button type="button" className={shared.btnSmall} onClick={() => resume.mutate(c.id)} disabled={resume.isPending}>
                    {resume.isPending ? 'RESUMING…' : 'RESUME'}
                  </button>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

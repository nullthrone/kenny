import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../../api/client'
import EmptyState from '../../../components/EmptyState/EmptyState'
import { formatRelativeTime } from '../../host/format'
import type { UpdateAgentRow, UpdateCampaign, UpdatesResponse } from '../types'
import shared from '../shared.module.css'
import styles from './UpdatesSection.module.css'

type Channel = 'stable' | 'dev'

function statusChipColor(status: UpdateCampaign['status']): string {
  if (status === 'active') return 'var(--brass-600)'
  if (status === 'suspended') return 'var(--warn)'
  return 'var(--text-faint)'
}

/**
 * Per-agent rollout state. `rolloutOnConnect` is the global
 * `KENNY_AGENT_ROLLOUT_ON_CONNECT` setting, not the campaign's own flag: an
 * offline agent is only really queued for connect when both gates are open
 * (`update_manager.on_agent_connect`), so with the global switch off — its
 * default — an offline host is reported as plain OFFLINE rather than being
 * promised a delivery that will never fire.
 */
function hostStatus(
  row: UpdateAgentRow,
  campaignVersion: string,
  rolloutOnConnect: boolean,
): { label: string; pct: number; color: string } {
  if (row.updated) return { label: campaignVersion, pct: 100, color: 'var(--ok)' }
  if (row.held) return { label: 'HELD', pct: 40, color: 'var(--danger)' }
  // Not eligible is an arch/channel mismatch, never a connection state — the
  // agent is skipped without penalty (`update_manager._apply_to_agent`).
  if (!row.eligible) return { label: 'NOT ELIGIBLE', pct: 0, color: 'var(--text-faint)' }
  if (!row.online) {
    return rolloutOnConnect
      ? { label: 'ON CONNECT', pct: 0, color: 'var(--text-faint)' }
      : { label: 'OFFLINE', pct: 0, color: 'var(--text-faint)' }
  }
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
 *
 * The per-agent table only renders under an active campaign because
 * `update_manager._agents_for_campaign` returns no rows without one — the
 * rows are eligibility against a campaign, not a fleet listing.
 */
export default function UpdatesSection() {
  const queryClient = useQueryClient()
  const [channel, setChannel] = useState<Channel>('stable')
  // Mirrors the campaign's `on_connect` at approval time. Defaults to on,
  // which is what this card has always sent; the checkbox makes the choice
  // visible rather than leaving it a hardcoded argument.
  const [approveOnConnect, setApproveOnConnect] = useState(true)

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

  // Soll/ist channel split (ADR-0048): campaign eligibility is checked against
  // the operator-set desired channel, never the channel the connected binary
  // reports about itself, so an agent just flipped to dev is eligible for the
  // dev campaign that will bring it there.
  const setDesiredChannel = useMutation({
    mutationFn: ({ agentId, desired }: { agentId: string; desired: Channel }) =>
      api.put<{ ok: boolean; agent_id: string; desired_channel: string }>(`/api/agent/${agentId}/channel`, { channel: desired }),
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
  const rolloutOnConnect = data.config.rollout_on_connect === true

  const stateLabel = activeCampaign
    ? activeCampaign.status === 'active'
      ? 'ROLLING OUT'
      : activeCampaign.status.toUpperCase()
    : availability?.version
      ? `${availability.version} AVAILABLE`
      : 'NO KNOWN VERSION'
  const stateColor = activeCampaign?.status === 'active' ? 'var(--brass-600)' : availability?.version ? 'var(--warn)' : 'var(--text-faint)'

  const doneCount = agents.filter((a) => a.updated).length

  const mutations = [check, approve, applyNow, revoke, suspend, resume, setDesiredChannel]

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

      {mutations.some((m) => m.isError) && (
        <div className={shared.errorBox}>
          {mutations.map((m) => (m.error instanceof ApiError ? m.error.message : null)).find((m) => m) ?? 'Something went wrong. Try again.'}
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

        {/* Detection state. A `checked` that is days old is the clearest sign
            the update check has stalled — without it a stale availability
            version reads as the newest release there is. */}
        <dl className={styles.stateList}>
          <dt>latest known</dt>
          <dd className={shared.mono}>
            {availability?.version || '—'}
            {availability?.checked_at ? (
              <span className={styles.stateNote}> · checked {formatRelativeTime(availability.checked_at)}</span>
            ) : null}
          </dd>
          <dt>auto-apply on connect</dt>
          <dd>
            {rolloutOnConnect ? 'on' : 'off'}
            {!rolloutOnConnect ? <span className={styles.stateNote}> · offline agents wait for APPLY NOW</span> : null}
          </dd>
          {activeCampaign ? (
            <>
              <dt>campaign on-connect</dt>
              <dd>{activeCampaign.on_connect ? 'on' : 'off (apply now only)'}</dd>
              <dt>expires</dt>
              <dd>{activeCampaign.expires_at ? formatRelativeTime(activeCampaign.expires_at) : 'never'}</dd>
            </>
          ) : null}
        </dl>

        {activeCampaign && agents.length > 0 && (
          <div className={styles.hostList}>
            <div className={`${styles.hostRow} ${styles.hostHead}`}>
              <span>agent</span>
              <span>os / arch</span>
              <span>current</span>
              <span />
              <span>channel (built / desired)</span>
              <span className={styles.hostStatus}>status</span>
            </div>
            {agents.map((a) => {
              const s = hostStatus(a, activeCampaign.version, rolloutOnConnect)
              return (
                <div key={a.agent_id} className={styles.hostRow}>
                  <span className={styles.hostName}>
                    <span
                      className={styles.dot}
                      style={{ background: a.online ? 'var(--ok)' : 'var(--text-faint)' }}
                      title={a.online ? 'online' : 'offline'}
                    />
                    {a.agent_id}
                  </span>
                  <span className={styles.hostMeta}>
                    {a.os}/{a.arch}
                  </span>
                  <span className={`${styles.hostMeta} ${shared.mono}`}>{a.current_version || '?'}</span>
                  <span className={styles.bar}>
                    <span className={styles.barFill} style={{ width: `${s.pct}%`, background: s.color }} />
                  </span>
                  <span className={styles.channelCell}>
                    <span className={styles.builtBadge}>{a.channel || 'stable'}</span>
                    <select
                      className={styles.channelSelect}
                      aria-label={`desired channel for ${a.agent_id}`}
                      value={a.desired_channel === 'dev' ? 'dev' : 'stable'}
                      disabled={setDesiredChannel.isPending}
                      onChange={(e) =>
                        setDesiredChannel.mutate({ agentId: a.agent_id, desired: e.target.value as Channel })
                      }
                    >
                      <option value="stable">desired: stable</option>
                      <option value="dev">desired: dev</option>
                    </select>
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
            <>
              <button type="button" className={shared.btnPrimary} onClick={() => approve.mutate(approveOnConnect)} disabled={approve.isPending}>
                {approve.isPending ? 'APPROVING…' : `APPROVE ROLLOUT · PIN ${availability.version}`}
              </button>
              <label className={styles.approveOpt}>
                <input
                  type="checkbox"
                  checked={approveOnConnect}
                  onChange={(e) => setApproveOnConnect(e.target.checked)}
                />
                apply on connect
              </label>
            </>
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

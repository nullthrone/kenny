import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../../api/client'
import EmptyState from '../../../components/EmptyState/EmptyState'
import type { DiscordClaim, DiscordIdentity, DiscordMember, DiscordStatus } from '../types'
import shared from '../shared.module.css'

interface DirectoryUser {
  id: number
  username: string
  role: string
}

function usernameOf(users: DirectoryUser[] | undefined, id: number): string {
  return users?.find((u) => u.id === id)?.username ?? `user #${id}`
}

/** Admin → Discord & Tickets. Status, pending `/link` claims, linked identities, manual link. */
export default function DiscordSection() {
  const queryClient = useQueryClient()
  const [manualDiscordId, setManualDiscordId] = useState('')
  const [manualUserId, setManualUserId] = useState('')
  const [claimUserIds, setClaimUserIds] = useState<Record<string, string>>({})
  const [membersLoaded, setMembersLoaded] = useState(false)

  const status = useQuery({ queryKey: ['admin', 'discord', 'status'], queryFn: () => api.get<DiscordStatus>('/api/discord/status') })
  const identities = useQuery({
    queryKey: ['admin', 'discord', 'identities'],
    queryFn: () => api.get<{ identities: DiscordIdentity[] }>('/api/discord/identities'),
    enabled: status.data?.configured,
  })
  const claims = useQuery({
    queryKey: ['admin', 'discord', 'claims'],
    queryFn: () => api.get<{ claims: DiscordClaim[] }>('/api/discord/claims'),
    enabled: status.data?.configured,
  })
  const users = useQuery({
    queryKey: ['users', 'directory'],
    queryFn: () => api.get<{ users: DirectoryUser[] }>('/api/users/directory'),
  })
  const members = useQuery({
    queryKey: ['admin', 'discord', 'members'],
    queryFn: () => api.get<{ members: DiscordMember[] }>('/api/discord/members'),
    enabled: membersLoaded,
  })

  function invalidateLinks() {
    queryClient.invalidateQueries({ queryKey: ['admin', 'discord', 'identities'] })
    queryClient.invalidateQueries({ queryKey: ['admin', 'discord', 'claims'] })
  }

  const confirmClaim = useMutation({
    mutationFn: ({ code, userId }: { code: string; userId: number }) => api.post(`/api/discord/claims/${code}`, { user_id: userId }),
    onSuccess: invalidateLinks,
  })

  const unlink = useMutation({
    mutationFn: (did: string) => api.delete<{ ok: boolean }>(`/api/discord/identities/${did}`),
    onSuccess: invalidateLinks,
  })

  const manualLink = useMutation({
    mutationFn: () => api.post('/api/discord/identities', { discord_user_id: manualDiscordId.trim(), user_id: Number(manualUserId) }),
    onSuccess: () => {
      setManualDiscordId('')
      setManualUserId('')
      invalidateLinks()
    },
  })

  if (status.isLoading) return <div className={shared.loading}>Loading…</div>
  if (status.isError) return <EmptyState title="Could not load Discord status" message="Something went wrong. Reload to try again." />
  if (!status.data) return null

  if (!status.data.configured) {
    return <EmptyState title="Discord is not configured" message="No bot token is set for this deployment — ticket linking and mentions are unavailable." />
  }

  const actionError = [confirmClaim, unlink].map((m) => (m.error instanceof ApiError ? m.error.message : null)).find((m) => m)

  return (
    <div>
      {actionError && <div className={shared.errorBox}>{actionError}</div>}
      <div className={shared.card}>
        <div className={shared.cardTitle}>STATUS</div>
        <div className={shared.help}>
          {status.data.connected ? 'connected' : 'not connected'}
          {status.data.model ? ` · model ${status.data.model}` : ''}
          {status.data.guilds?.length ? ` · ${status.data.guilds.length} guild(s)` : ''}
        </div>
        {status.data.startup_error && <div className={shared.errorBox} style={{ marginTop: 8 }}>{status.data.startup_error}</div>}
        {status.data.missing_message_content && (
          <div className={shared.warnBox} style={{ marginTop: 8 }}>
            The Message Content privileged intent is disabled — kenny cannot read requests sent as plain mentions.
          </div>
        )}
      </div>

      <div className={shared.cardTitle}>PENDING CLAIMS</div>
      {(claims.data?.claims.length ?? 0) === 0 ? (
        <p className={shared.help}>No one has run <code className={shared.mono}>/link</code> waiting on confirmation.</p>
      ) : (
        <div className={shared.table} style={{ marginBottom: 24 }}>
          {claims.data!.claims.map((c) => (
            <div key={c.code} className={shared.tableRow}>
              <div className={shared.tableMeta}>
                <div className={shared.tableLabel}>{c.display_hint}</div>
                <div className={shared.tableSub}>
                  code {c.code} · expires {new Date(c.expires_at).toLocaleString()}
                </div>
              </div>
              <select
                className={shared.input}
                style={{ width: 160 }}
                value={claimUserIds[c.code] ?? ''}
                onChange={(e) => setClaimUserIds((m) => ({ ...m, [c.code]: e.target.value }))}
              >
                <option value="">link to…</option>
                {users.data?.users.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.username}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className={shared.btnSmall}
                disabled={!claimUserIds[c.code] || confirmClaim.isPending}
                onClick={() => confirmClaim.mutate({ code: c.code, userId: Number(claimUserIds[c.code]) })}
              >
                CONFIRM
              </button>
            </div>
          ))}
        </div>
      )}

      <div className={shared.cardTitle}>LINKED IDENTITIES</div>
      {(identities.data?.identities.length ?? 0) === 0 ? (
        <p className={shared.help}>No Discord accounts linked yet.</p>
      ) : (
        <div className={shared.table} style={{ marginBottom: 24 }}>
          {identities.data!.identities.map((id) => (
            <div key={id.discord_user_id} className={shared.tableRow}>
              <div className={shared.tableMeta}>
                <div className={shared.tableLabel}>{usernameOf(users.data?.users, id.user_id)}</div>
                <div className={shared.tableSub}>
                  discord {id.discord_user_id} · linked {new Date(id.linked_at).toLocaleDateString()} via {id.linked_via}
                </div>
              </div>
              <button type="button" className={shared.btnDanger} onClick={() => unlink.mutate(id.discord_user_id)} disabled={unlink.isPending}>
                UNLINK
              </button>
            </div>
          ))}
        </div>
      )}

      <div className={shared.cardTitle}>LINK MANUALLY</div>
      {!membersLoaded && (
        <div className={shared.actions} style={{ marginTop: 0, marginBottom: 8 }}>
          <button type="button" className={shared.btnSmall} onClick={() => setMembersLoaded(true)}>
            LOAD GUILD MEMBERS
          </button>
        </div>
      )}
      {membersLoaded && members.data && (
        <select className={shared.input} style={{ marginBottom: 8 }} value={manualDiscordId} onChange={(e) => setManualDiscordId(e.target.value)}>
          <option value="">pick a guild member…</option>
          {members.data.members.map((m) => (
            <option key={m.user_id} value={m.user_id}>
              {m.display_hint}
            </option>
          ))}
        </select>
      )}
      <form
        className={shared.actions}
        style={{ marginTop: 0 }}
        onSubmit={(e) => {
          e.preventDefault()
          manualLink.mutate()
        }}
      >
        <input
          type="text"
          className={shared.input}
          style={{ width: 200 }}
          placeholder="Discord user id"
          value={manualDiscordId}
          onChange={(e) => setManualDiscordId(e.target.value)}
        />
        <select className={shared.input} style={{ width: 160 }} value={manualUserId} onChange={(e) => setManualUserId(e.target.value)}>
          <option value="">kenny account…</option>
          {users.data?.users.map((u) => (
            <option key={u.id} value={u.id}>
              {u.username}
            </option>
          ))}
        </select>
        <button type="submit" className={shared.btnSmall} disabled={!manualDiscordId.trim() || !manualUserId || manualLink.isPending}>
          {manualLink.isPending ? 'LINKING…' : 'LINK'}
        </button>
      </form>
      {manualLink.isError && (
        <div className={shared.errorBox} style={{ marginTop: 8 }}>
          {manualLink.error instanceof ApiError ? manualLink.error.message : 'Could not link that account.'}
        </div>
      )}
    </div>
  )
}

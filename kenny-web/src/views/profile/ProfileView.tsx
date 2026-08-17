import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import KeyValueRow from '../../components/KeyValueRow/KeyValueRow'
import EmptyState from '../../components/EmptyState/EmptyState'
import { initialsOf, roleLabel } from '../../components/format'
import type { DiscordMeStatus, Pat, ProfileMe, SessionRow } from './types'
import PasswordModal from './PasswordModal'
import TotpModal from './TotpModal'
import PatModal from './PatModal'
import EditProfileModal from './EditProfileModal'
import SessionsModal from './SessionsModal'
import DiscordModal from './DiscordModal'
import styles from './ProfileView.module.css'

function scopeLabel(me: ProfileMe): string {
  return me.role === 'user' ? (me.hosts.length === 1 ? me.hosts[0] : `${me.hosts.length} hosts`) : 'full fleet'
}

function sinceLabel(createdAt: string | null): string | null {
  if (!createdAt) return null
  const d = new Date(createdAt)
  if (Number.isNaN(d.getTime())) return null
  return `since ${d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })}`
}

type OpenModal = 'edit' | 'password' | 'totp' | 'pats' | 'sessions' | 'discord' | null

/**
 * `#/profile` — the signed-in account, separate from fleet administration.
 * A legacy shared-token identity (`me.is_shared_token`) has no editable
 * account: profile edits, PATs and 2FA are hidden entirely.
 */
export default function ProfileView() {
  const [openModal, setOpenModal] = useState<OpenModal>(null)

  const me = useQuery({ queryKey: ['me'], queryFn: () => api.get<ProfileMe>('/api/me') })

  const pats = useQuery({
    queryKey: ['me', 'pats'],
    queryFn: () => api.get<{ pats: Pat[] }>('/api/me/pats'),
    enabled: !!me.data && !me.data.is_shared_token,
  })

  const sessions = useQuery({
    queryKey: ['me', 'sessions'],
    queryFn: () => api.get<{ sessions: SessionRow[] }>('/api/me/sessions'),
    enabled: !!me.data && !me.data.is_shared_token,
  })

  const discord = useQuery({
    queryKey: ['me', 'discord'],
    queryFn: () => api.get<DiscordMeStatus>('/api/me/discord'),
    enabled: !!me.data && !me.data.is_shared_token,
  })

  return (
    <div className={`kc-content kc-view ${styles.root}`}>
      <h1 className="kc-h1" style={{ fontFamily: 'var(--font-display)', fontWeight: 500, fontSize: 'var(--display-md)', margin: '0 0 6px' }}>
        Profile
      </h1>
      <p className={styles.lead}>Your account — separate from fleet administration.</p>

      {me.isLoading && <div className={styles.loading}>Loading…</div>}

      {me.isError && (
        <EmptyState title="Could not load your profile" message="Something went wrong. Reload to try again." />
      )}

      {me.data && (
        <>
          <div className={styles.identity}>
            <div className={styles.avatar}>
              {me.data.avatar ? <img src={`/assets/${me.data.avatar}.png`} alt="" /> : initialsOf(me.data.username)}
            </div>
            <div className={styles.identityText}>
              <div className={styles.username}>{me.data.username}</div>
              <div className={styles.meta}>
                {roleLabel(me.data.role)} · {scopeLabel(me.data)}
                {sinceLabel(me.data.created_at) ? ` · ${sinceLabel(me.data.created_at)?.toUpperCase()}` : ''}
              </div>
            </div>
            {!me.data.is_shared_token && (
              <button type="button" className={styles.editBtn} onClick={() => setOpenModal('edit')}>
                EDIT PROFILE
              </button>
            )}
          </div>

          {me.data.is_shared_token ? (
            <div className={styles.sharedNotice}>
              You are signed in with a legacy shared token, which has no editable account. Password, two-factor and personal
              access tokens are managed per user account, not for this identity.
            </div>
          ) : (
            <div className={styles.rows}>
              <KeyValueRow
                label="Password"
                help="Change your password"
                value={<span style={{ color: 'var(--text-muted)' }}>••••••••</span>}
                action={{ label: 'CHANGE', onClick: () => setOpenModal('password') }}
              />
              <KeyValueRow
                label="Two-factor auth"
                help="TOTP app · adds a second step to sign-in"
                value={
                  <span style={{ color: me.data.totp_enabled ? 'var(--ok)' : 'var(--text-muted)' }}>
                    {me.data.totp_enabled ? 'enabled' : 'disabled'}
                  </span>
                }
                action={{ label: 'MANAGE', onClick: () => setOpenModal('totp') }}
              />
              <KeyValueRow
                label="MCP token"
                help="Personal access tokens — usable as a bearer token by an MCP client or the API"
                value={
                  <span style={{ color: 'var(--text-muted)' }}>
                    {pats.data ? `${pats.data.pats.filter((p) => !p.revoked).length} active` : '…'}
                  </span>
                }
                action={{ label: 'MANAGE', onClick: () => setOpenModal('pats') }}
              />
              <KeyValueRow
                label="Sessions"
                help="Where this account is signed in — this browser, and any others"
                value={
                  <span style={{ color: 'var(--text-muted)' }}>
                    {sessions.data ? `${sessions.data.sessions.length} active` : '…'}
                  </span>
                }
                action={{ label: 'MANAGE', onClick: () => setOpenModal('sessions') }}
              />
              <KeyValueRow
                label="Discord"
                help="Linked Discord account — used for support requests in chat"
                value={
                  <span style={{ color: discord.data?.linked ? 'var(--ok)' : 'var(--text-muted)' }}>
                    {discord.data ? (discord.data.linked ? 'linked' : 'not linked') : '…'}
                  </span>
                }
                action={{ label: 'MANAGE', onClick: () => setOpenModal('discord') }}
              />
            </div>
          )}
        </>
      )}

      {me.data && !me.data.is_shared_token && (
        <>
          <EditProfileModal open={openModal === 'edit'} onClose={() => setOpenModal(null)} me={me.data} />
          <PasswordModal open={openModal === 'password'} onClose={() => setOpenModal(null)} />
          <TotpModal open={openModal === 'totp'} onClose={() => setOpenModal(null)} enabled={me.data.totp_enabled} />
          <PatModal open={openModal === 'pats'} onClose={() => setOpenModal(null)} />
          <SessionsModal open={openModal === 'sessions'} onClose={() => setOpenModal(null)} />
          <DiscordModal open={openModal === 'discord'} onClose={() => setOpenModal(null)} />
        </>
      )}
    </div>
  )
}

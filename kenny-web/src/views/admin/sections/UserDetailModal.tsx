import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../../api/client'
import Modal from '../../../components/Modal/Modal'
import EmptyState from '../../../components/EmptyState/EmptyState'
import { X, Trash2, ICON_STROKE_WIDTH } from '../../../components/icons'
import type { AvatarsResponse } from '../../profile/types'
import type { FleetResponse, Role } from '../../../api/types'
import type { AdminUser, ToolClassesResponse } from '../types'
import shared from '../shared.module.css'

export interface UserDetailModalProps {
  userId: number | null
  onClose: () => void
  /** The signed-in principal's own id — a superuser cannot delete/demote themselves into lockout server-side, but the UI hides the delete action on its own row too. */
  ownId: number | null
}

const ROLES: Role[] = ['user', 'operator', 'superuser']

function formatDate(iso: string | null): string {
  if (!iso) return 'never'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

/** Admin → Users → a row's MANAGE. Every capability from the Users endpoint table in one modal. */
export default function UserDetailModal({ userId, onClose, ownId }: UserDetailModalProps) {
  const queryClient = useQueryClient()
  const open = userId !== null

  const detail = useQuery({
    queryKey: ['admin', 'users', userId],
    queryFn: () => api.get<AdminUser>(`/api/users/${userId}`),
    enabled: open,
  })
  const avatars = useQuery({ queryKey: ['avatars'], queryFn: () => api.get<AvatarsResponse>('/api/avatars'), enabled: open, staleTime: Infinity })
  const toolClasses = useQuery({ queryKey: ['tool-classes'], queryFn: () => api.get<ToolClassesResponse>('/api/tool-classes'), enabled: open, staleTime: Infinity })
  const fleet = useQuery({ queryKey: ['fleet'], queryFn: () => api.get<FleetResponse>('/api/fleet'), enabled: open })

  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<Role>('user')
  const [avatar, setAvatar] = useState<string | null>(null)
  const [disabled, setDisabled] = useState(false)
  const [profile, setProfile] = useState('')
  const [hosts, setHosts] = useState<string[]>([])
  const [newPassword, setNewPassword] = useState('')
  const [patLabel, setPatLabel] = useState('')
  const [justCreatedToken, setJustCreatedToken] = useState<string | null>(null)
  const [deleteArmed, setDeleteArmed] = useState(false)

  useEffect(() => {
    if (detail.data) {
      setUsername(detail.data.username)
      setEmail(detail.data.email ?? '')
      setRole(detail.data.role)
      setAvatar(detail.data.avatar)
      setDisabled(detail.data.disabled)
      setProfile(detail.data.capability_profile ?? '')
      setHosts(detail.data.hosts ?? [])
    }
  }, [detail.data])

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ['admin', 'users'] })
    queryClient.invalidateQueries({ queryKey: ['admin', 'users', userId] })
  }

  const saveIdentity = useMutation({
    mutationFn: () => api.patch<AdminUser>(`/api/users/${userId}`, { username: username.trim(), email: email.trim() || null, role, avatar, disabled }),
    onSuccess: invalidate,
  })
  const saveProfile = useMutation({
    mutationFn: () => api.put(`/api/users/${userId}/profile`, { capability_profile: profile || null }),
    onSuccess: invalidate,
  })
  const saveHosts = useMutation({
    mutationFn: () => api.put(`/api/users/${userId}/hosts`, { hosts }),
    onSuccess: invalidate,
  })
  const resetPassword = useMutation({
    mutationFn: () => api.post<{ ok: boolean }>(`/api/users/${userId}/password`, { new_password: newPassword }),
    onSuccess: () => setNewPassword(''),
  })
  const resetTotp = useMutation({
    mutationFn: () => api.delete<{ ok: boolean }>(`/api/users/${userId}/totp`),
    onSuccess: invalidate,
  })
  const createPat = useMutation({
    mutationFn: () => api.post<{ token: string }>(`/api/users/${userId}/pats`, patLabel.trim() ? { label: patLabel.trim() } : {}),
    onSuccess: (res) => {
      setJustCreatedToken(res.token)
      setPatLabel('')
      invalidate()
    },
  })
  const revokePat = useMutation({
    mutationFn: (pid: number) => api.delete<{ ok: boolean }>(`/api/users/${userId}/pats/${pid}`),
    onSuccess: invalidate,
  })
  const deleteUser = useMutation({
    mutationFn: () => api.delete<{ ok: boolean }>(`/api/users/${userId}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'users'] })
      handleClose()
    },
  })

  function handleClose() {
    setJustCreatedToken(null)
    setDeleteArmed(false)
    onClose()
  }

  function toggleHost(id: string) {
    setHosts((h) => (h.includes(id) ? h.filter((x) => x !== id) : [...h, id]))
  }

  return (
    <Modal open={open} onClose={handleClose} labelledBy="user-detail-title" width={560}>
      <div id="user-detail-title" className={shared.cardTitle} style={{ padding: '20px 22px 0' }}>
        {detail.data?.username ?? 'User'}
        <button
          type="button"
          onClick={handleClose}
          style={{ float: 'right', background: 'transparent', border: 'none', color: 'var(--text-muted)' }}
          aria-label="Close"
        >
          <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} />
        </button>
      </div>

      {detail.isLoading && <div className={shared.loading}>Loading…</div>}
      {detail.isError && <EmptyState title="Could not load this user" message="Something went wrong. Reload to try again." />}

      {detail.data && (
        <div style={{ padding: '16px 22px', display: 'flex', flexDirection: 'column', gap: 20, maxHeight: '68vh', overflowY: 'auto' }}>
          {/* Identity */}
          <div>
            <div className={shared.fieldLabel} style={{ marginBottom: 8 }}>
              IDENTITY
            </div>
            {saveIdentity.isError && (
              <div className={shared.errorBox}>{saveIdentity.error instanceof ApiError ? saveIdentity.error.message : 'Could not save.'}</div>
            )}
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              <label className={shared.field} style={{ flex: 1, minWidth: 140 }}>
                <span className={shared.fieldLabel}>USERNAME</span>
                <input type="text" className={shared.input} value={username} onChange={(e) => setUsername(e.target.value)} />
              </label>
              <label className={shared.field} style={{ flex: 1, minWidth: 140 }}>
                <span className={shared.fieldLabel}>EMAIL</span>
                <input type="email" className={shared.input} value={email} onChange={(e) => setEmail(e.target.value)} />
              </label>
            </div>
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 10 }}>
              <label className={shared.field} style={{ minWidth: 140 }}>
                <span className={shared.fieldLabel}>ROLE</span>
                <select className={shared.input} value={role} onChange={(e) => setRole(e.target.value as Role)}>
                  {ROLES.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              </label>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 'var(--text-sm)', marginTop: 20 }}>
                <input type="checkbox" checked={disabled} onChange={(e) => setDisabled(e.target.checked)} />
                Disabled
              </label>
            </div>
            {avatars.data && (
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 10 }}>
                {avatars.data.avatars.map((a) => (
                  <button
                    key={a}
                    type="button"
                    onClick={() => setAvatar(a)}
                    style={{
                      width: 36,
                      height: 36,
                      padding: 0,
                      border: `2px solid ${avatar === a ? 'var(--brass-500)' : 'var(--border-line)'}`,
                      background: 'var(--surface-card)',
                      overflow: 'hidden',
                    }}
                    aria-pressed={avatar === a}
                  >
                    <img src={`/assets/${a}.png`} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                  </button>
                ))}
              </div>
            )}
            <div className={shared.actions} style={{ marginTop: 10 }}>
              <button type="button" className={shared.btnSmall} onClick={() => saveIdentity.mutate()} disabled={saveIdentity.isPending}>
                {saveIdentity.isPending ? 'SAVING…' : 'SAVE IDENTITY'}
              </button>
            </div>
          </div>

          {/* Capability profile */}
          {toolClasses.data && (
            <div>
              <div className={shared.fieldLabel} style={{ marginBottom: 8 }}>
                CAPABILITY PROFILE
              </div>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                <select className={shared.input} style={{ width: 220 }} value={profile} onChange={(e) => setProfile(e.target.value)}>
                  <option value="">default</option>
                  {Object.keys(toolClasses.data.profiles).map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
                <button type="button" className={shared.btnSmall} onClick={() => saveProfile.mutate()} disabled={saveProfile.isPending}>
                  {saveProfile.isPending ? 'SAVING…' : 'SAVE'}
                </button>
              </div>
            </div>
          )}

          {/* Host scope */}
          {role === 'user' && (
            <div>
              <div className={shared.fieldLabel} style={{ marginBottom: 8 }}>
                HOST SCOPE
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {fleet.data?.agents.map((a) => (
                  <label key={a.agent_id} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 'var(--text-sm)' }}>
                    <input type="checkbox" checked={hosts.includes(a.agent_id)} onChange={() => toggleHost(a.agent_id)} />
                    {a.agent_id}
                  </label>
                ))}
              </div>
              <div className={shared.actions} style={{ marginTop: 8 }}>
                <button type="button" className={shared.btnSmall} onClick={() => saveHosts.mutate()} disabled={saveHosts.isPending}>
                  {saveHosts.isPending ? 'SAVING…' : 'SAVE HOST SCOPE'}
                </button>
              </div>
            </div>
          )}

          {/* Password + 2FA */}
          <div>
            <div className={shared.fieldLabel} style={{ marginBottom: 8 }}>
              PASSWORD & TWO-FACTOR
            </div>
            {resetPassword.isSuccess && <div className={shared.okBox}>Password reset.</div>}
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <input
                type="password"
                className={shared.input}
                style={{ width: 200 }}
                placeholder="new password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
              />
              <button type="button" className={shared.btnSmall} onClick={() => resetPassword.mutate()} disabled={!newPassword || resetPassword.isPending}>
                {resetPassword.isPending ? 'RESETTING…' : 'RESET PASSWORD'}
              </button>
              {detail.data.totp_enabled && (
                <button type="button" className={shared.btnSmall} onClick={() => resetTotp.mutate()} disabled={resetTotp.isPending}>
                  {resetTotp.isPending ? 'RESETTING…' : 'RESET TWO-FACTOR'}
                </button>
              )}
            </div>
            <p className={shared.help} style={{ marginTop: 6 }}>
              Two-factor: {detail.data.totp_enabled ? 'enabled' : 'disabled'}
            </p>
          </div>

          {/* PATs */}
          <div>
            <div className={shared.fieldLabel} style={{ marginBottom: 8 }}>
              PERSONAL ACCESS TOKENS
            </div>
            {justCreatedToken ? (
              <>
                <div className={shared.okBox}>This token will not be shown again. Copy it now.</div>
                <input type="text" readOnly className={shared.input} value={justCreatedToken} onFocus={(e) => e.currentTarget.select()} />
                <div className={shared.actions} style={{ marginTop: 8 }}>
                  <button type="button" className={shared.btnSmall} onClick={() => setJustCreatedToken(null)}>
                    DONE
                  </button>
                </div>
              </>
            ) : (
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <input
                  type="text"
                  className={shared.input}
                  style={{ width: 200 }}
                  placeholder="label (optional)"
                  value={patLabel}
                  onChange={(e) => setPatLabel(e.target.value)}
                />
                <button type="button" className={shared.btnSmall} onClick={() => createPat.mutate()} disabled={createPat.isPending}>
                  {createPat.isPending ? 'CREATING…' : 'NEW TOKEN'}
                </button>
              </div>
            )}
            {(detail.data.pats?.length ?? 0) > 0 && (
              <div className={shared.table} style={{ marginTop: 8 }}>
                {detail.data.pats!.map((p) => (
                  <div key={p.id} className={shared.tableRow}>
                    <div className={shared.tableMeta}>
                      <div className={shared.tableLabel}>{p.label || `token #${p.id}`}</div>
                      <div className={shared.tableSub}>
                        created {formatDate(p.created_at)} · last used {formatDate(p.last_used)}
                      </div>
                    </div>
                    {p.revoked ? (
                      <span className={shared.tag} style={{ color: 'var(--text-faint)' }}>
                        REVOKED
                      </span>
                    ) : (
                      <button type="button" className={shared.btnDanger} onClick={() => revokePat.mutate(p.id)} disabled={revokePat.isPending}>
                        <Trash2 width={14} height={14} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                        &nbsp;REVOKE
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Delete */}
          {userId !== ownId && (
            <div>
              <div className={shared.fieldLabel} style={{ marginBottom: 8, color: 'var(--danger)' }}>
                DANGER
              </div>
              {deleteUser.isError && (
                <div className={shared.errorBox}>{deleteUser.error instanceof ApiError ? deleteUser.error.message : 'Could not delete this user.'}</div>
              )}
              {deleteArmed ? (
                <div className={shared.actions} style={{ marginTop: 0 }}>
                  <button type="button" className={shared.btnDanger} onClick={() => deleteUser.mutate()} disabled={deleteUser.isPending}>
                    {deleteUser.isPending ? 'DELETING…' : `CONFIRM DELETE ${detail.data.username}`}
                  </button>
                  <button type="button" className={shared.btn} onClick={() => setDeleteArmed(false)}>
                    CANCEL
                  </button>
                </div>
              ) : (
                <button type="button" className={shared.btnDanger} onClick={() => setDeleteArmed(true)}>
                  DELETE USER
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </Modal>
  )
}

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../api/client'
import Modal from '../../components/Modal/Modal'
import { X, ICON_STROKE_WIDTH } from '../../components/icons'
import type { RevokeOthersResponse, SessionRow } from './types'
import shared from './shared.module.css'

export interface SessionsModalProps {
  open: boolean
  onClose: () => void
}

function formatDateTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

/**
 * Profile → Sessions → MANAGE. Real rows from `GET /api/me/sessions`
 * (`created_at`/`expires_at`/`ip`/`user_agent`/`current`) — not a fabricated
 * device list. "Sign out other sessions" ends every other browser session
 * (and OAuth grant) via `POST /api/me/sessions/revoke-others`, keeping this
 * browser signed in. It does NOT touch personal access tokens — those are a
 * separate credential (how Claude Desktop and other MCP clients reach
 * `/mcp`), managed from the MCP token row on the profile page, right next to
 * this one.
 */
export default function SessionsModal({ open, onClose }: SessionsModalProps) {
  const queryClient = useQueryClient()
  const [justRevoked, setJustRevoked] = useState<number | null>(null)

  const sessions = useQuery({
    queryKey: ['me', 'sessions'],
    queryFn: () => api.get<{ sessions: SessionRow[] }>('/api/me/sessions'),
    enabled: open,
  })

  const revokeOthers = useMutation({
    mutationFn: () => api.post<RevokeOthersResponse>('/api/me/sessions/revoke-others'),
    onSuccess: (res) => {
      setJustRevoked(res.revoked)
      queryClient.invalidateQueries({ queryKey: ['me', 'sessions'] })
    },
  })

  function handleClose() {
    setJustRevoked(null)
    revokeOthers.reset()
    onClose()
  }

  function handleRevokeOthers() {
    const count = others.length
    if (!window.confirm(`Sign out ${count} other session${count === 1 ? '' : 's'}? This browser stays signed in.`)) return
    setJustRevoked(null)
    revokeOthers.mutate()
  }

  const rows = sessions.data?.sessions ?? []
  const others = rows.filter((s) => !s.current)

  return (
    <Modal open={open} onClose={handleClose} labelledBy="sessions-modal-title" width={540}>
      <div id="sessions-modal-title" className={shared.header}>
        Sessions
        <button type="button" className={shared.closeBtn} onClick={handleClose} aria-label="Close">
          <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} />
        </button>
      </div>
      <div className={shared.body}>
        {sessions.isLoading && <p className={shared.help}>Loading sessions…</p>}
        {sessions.isError && <div className={shared.errorBox}>Could not load sessions. Reload to try again.</div>}

        {rows.length > 0 && (
          <div className={shared.list}>
            {rows.map((s, i) => (
              <div key={i} className={shared.listRow}>
                <div className={shared.listMeta}>
                  <div className={shared.listLabel}>
                    {s.user_agent || 'unknown device'}
                    {s.current && <span style={{ color: 'var(--ok)' }}> · this session</span>}
                  </div>
                  <div className={shared.listSub}>
                    {s.ip || 'unknown ip'} · signed in {formatDateTime(s.created_at)} · expires {formatDateTime(s.expires_at)}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}

        <p className={shared.help}>
          Signs out every other session and revokes OAuth grants. Personal access tokens are not touched — that&rsquo;s how
          Claude Desktop and other MCP clients reach kenny, so revoke those separately from the MCP token row above.
        </p>

        {revokeOthers.isError && (
          <div className={shared.errorBox}>
            {revokeOthers.error instanceof ApiError ? revokeOthers.error.message : 'Could not sign out other sessions. Try again.'}
          </div>
        )}
        {justRevoked !== null && !revokeOthers.isError && (
          <div className={shared.okBox}>
            Signed out {justRevoked} other session{justRevoked === 1 ? '' : 's'}.
          </div>
        )}

        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button
            type="button"
            className={shared.btnDanger}
            onClick={handleRevokeOthers}
            disabled={others.length === 0 || revokeOthers.isPending}
          >
            {revokeOthers.isPending ? 'SIGNING OUT…' : 'SIGN OUT OTHER SESSIONS'}
          </button>
        </div>
      </div>
      <div className={shared.footer}>
        <button type="button" className={shared.btn} onClick={handleClose}>
          CLOSE
        </button>
      </div>
    </Modal>
  )
}

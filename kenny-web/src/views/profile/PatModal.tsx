import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../api/client'
import Modal from '../../components/Modal/Modal'
import { X, Trash2, ICON_STROKE_WIDTH } from '../../components/icons'
import type { Pat, PatCreateResponse } from './types'
import shared from './shared.module.css'

export interface PatModalProps {
  open: boolean
  onClose: () => void
}

function formatDate(iso: string | null): string {
  if (!iso) return 'never'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

/**
 * Profile → MCP token → MANAGE. Personal access tokens — the bearer
 * credential used by an MCP client (`claude-desktop`) as well as the API
 * directly (`kenny-server/CLAUDE.md`: "a per-user PAT (Bearer)").
 *
 * A newly created token is shown exactly once, in a read-only field, and is
 * never persisted or re-fetched — `justCreated` lives only in this
 * component's state and is dropped the moment the modal closes.
 */
export default function PatModal({ open, onClose }: PatModalProps) {
  const queryClient = useQueryClient()
  const [label, setLabel] = useState('')
  const [justCreated, setJustCreated] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  const pats = useQuery({
    queryKey: ['me', 'pats'],
    queryFn: () => api.get<{ pats: Pat[] }>('/api/me/pats'),
    enabled: open,
  })

  const create = useMutation({
    mutationFn: () => api.post<PatCreateResponse>('/api/me/pats', label.trim() ? { label: label.trim() } : {}),
    onSuccess: (res) => {
      setJustCreated(res.token)
      setLabel('')
      setCopied(false)
      queryClient.invalidateQueries({ queryKey: ['me', 'pats'] })
    },
  })

  const revoke = useMutation({
    mutationFn: (id: number) => api.delete<{ ok: boolean }>(`/api/me/pats/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['me', 'pats'] }),
  })

  function handleClose() {
    setJustCreated(null)
    setLabel('')
    setCopied(false)
    create.reset()
    onClose()
  }

  async function handleCopy() {
    if (!justCreated) return
    try {
      await navigator.clipboard.writeText(justCreated)
      setCopied(true)
    } catch {
      // Clipboard access can be denied; the field stays selectable for a manual copy.
    }
  }

  const activePats = pats.data?.pats.filter((p) => !p.revoked) ?? []
  const revokedPats = pats.data?.pats.filter((p) => p.revoked) ?? []

  return (
    <Modal open={open} onClose={handleClose} labelledBy="pat-modal-title" width={520}>
      <div id="pat-modal-title" className={shared.header}>
        MCP tokens
        <button type="button" className={shared.closeBtn} onClick={handleClose} aria-label="Close">
          <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} />
        </button>
      </div>
      <div className={shared.body}>
        {justCreated ? (
          <>
            <div className={shared.okBox}>
              This token will not be shown again. Copy it now and store it wherever your MCP client (e.g. claude-desktop) reads its
              bearer token from.
            </div>
            <div className={shared.copyRow}>
              <input type="text" readOnly className={shared.input} value={justCreated} onFocus={(e) => e.currentTarget.select()} />
              <button type="button" className={shared.btn} onClick={handleCopy}>
                {copied ? 'COPIED' : 'COPY'}
              </button>
            </div>
            <button type="button" className={shared.btn} onClick={() => setJustCreated(null)}>
              DONE
            </button>
          </>
        ) : (
          <>
            <p className={shared.help}>A personal access token authenticates as you — over the API directly, or as an MCP client's bearer token.</p>
            <form
              onSubmit={(e) => {
                e.preventDefault()
                create.mutate()
              }}
              className={shared.copyRow}
            >
              <input
                type="text"
                className={shared.input}
                placeholder="Label (optional) — e.g. claude-desktop"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
              />
              <button type="submit" className={shared.btnPrimary} disabled={create.isPending}>
                {create.isPending ? 'CREATING…' : 'NEW TOKEN'}
              </button>
            </form>
            {create.isError && (
              <div className={shared.errorBox}>
                {create.error instanceof ApiError ? create.error.message : 'Could not create a token. Try again.'}
              </div>
            )}
          </>
        )}

        {pats.isLoading && <p className={shared.help}>Loading tokens…</p>}
        {!pats.isLoading && (activePats.length > 0 || revokedPats.length > 0) && (
          <div className={shared.list}>
            {[...activePats, ...revokedPats].map((p) => (
              <div key={p.id} className={shared.listRow}>
                <div className={shared.listMeta}>
                  <div className={shared.listLabel}>{p.label || `token #${p.id}`}</div>
                  <div className={shared.listSub}>
                    created {formatDate(p.created_at)} · last used {formatDate(p.last_used)}
                  </div>
                </div>
                {p.revoked ? (
                  <span className={shared.revokedTag}>REVOKED</span>
                ) : (
                  <button
                    type="button"
                    className={shared.btnDanger}
                    onClick={() => revoke.mutate(p.id)}
                    disabled={revoke.isPending}
                    title="Revoke this token"
                  >
                    <Trash2 width={14} height={14} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                    &nbsp;REVOKE
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
        {!pats.isLoading && activePats.length === 0 && revokedPats.length === 0 && (
          <p className={shared.help}>No tokens yet.</p>
        )}
      </div>
      <div className={shared.footer}>
        <button type="button" className={shared.btn} onClick={handleClose}>
          CLOSE
        </button>
      </div>
    </Modal>
  )
}

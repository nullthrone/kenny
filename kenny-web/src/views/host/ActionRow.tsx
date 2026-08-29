import { useState } from 'react'
import { useNavigate } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { Me } from '../../api/types'
import Modal from '../../components/Modal/Modal'
import { RefreshCw, LifeBuoy, Package, Link as LinkIcon, ArrowUpCircle, Trash2, X, ICON_STROKE_WIDTH } from '../../components/icons'
import { useRefreshAgent, useRemoteHelp, useUpdateAgent, useRemoveAgent, useSetChannel, useShareLink } from './api'
import { toShareOs } from './format'
import type { ShareLinkResult } from './types'
import styles from './ActionRow.module.css'

export interface ActionRowProps {
  agentId: string
  /** Family string (`windows`/`linux`/`macos`) — narrowed to what the
   * installer/share-link endpoints accept via `toShareOs`. */
  os: string
  /** `meta.arch`, when telemetry has reported one (ADR-0036). Pinned onto
   * the reinstall link so the operator doesn't fall back to `uname -m`
   * auto-detection for a box that already told us its arch. */
  arch?: string
  channel?: string
}

/**
 * The host action button row: REFRESH, REMOTE HELP, REINSTALL, RE-SHARE,
 * UPDATE AGENT, REMOVE, and the stable/dev channel selector.
 *
 * Only REFRESH and REMOTE HELP floor at `user` server-side
 * (`/api/agent/{id}/refresh`, `/api/agent/{id}/remotehelp`, both `scoped`).
 * Everything else is operator+: `/api/agents/{id}/installer` and
 * `/api/agents/share-link` (`distribution.py`), `/api/agents/{id}/update` and
 * `/api/agent/{id}/channel` (`op_scoped`), and `DELETE /api/agent/{id}` (`op`).
 * So a scoped `user` sees the first two and nothing else — this list mirrors
 * those `min_role` values, and `ActionRow.test.tsx` fails if the two drift.
 * Hiding is a courtesy, not the boundary: the server refuses regardless. But a
 * REMOVE button that offers to purge a host and is guaranteed to 403 is worse
 * than no button at all.
 */
export default function ActionRow({ agentId, os, arch, channel }: ActionRowProps) {
  const navigate = useNavigate()
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null)
  const [confirmRemove, setConfirmRemove] = useState(false)
  const [shareOpen, setShareOpen] = useState(false)
  const [shareState, setShareState] = useState<
    | { status: 'loading' }
    | { status: 'error'; message: string }
    | { status: 'done'; link: ShareLinkResult }
  >({ status: 'loading' })
  const [copiedField, setCopiedField] = useState<'url' | 'oneliner' | null>(null)

  const me = useQuery({ queryKey: ['me'], queryFn: () => api.get<Me>('/api/me') })
  const isOperator = me.data ? me.data.role !== 'user' : false

  const refresh = useRefreshAgent(agentId)
  const remoteHelp = useRemoteHelp(agentId)
  const update = useUpdateAgent(agentId)
  const remove = useRemoveAgent()
  const setChannel = useSetChannel(agentId)
  const shareLink = useShareLink(agentId)

  function doRefresh() {
    setMessage(null)
    refresh.mutate(undefined, {
      onSuccess: (r) => setMessage({ text: r.warning ?? 'Refreshed.', error: Boolean(r.warning) }),
      onError: (e) => setMessage({ text: `Could not refresh: ${e instanceof Error ? e.message : 'unknown error'}`, error: true }),
    })
  }

  function doRemoteHelp() {
    setMessage(null)
    remoteHelp.mutate(undefined, {
      onSuccess: (r) =>
        setMessage({
          text: r.note || 'Quick Assist was launched on the desktop. A human helper still shares the code.',
          error: false,
        }),
      onError: (e) => setMessage({ text: `Could not start remote help: ${e instanceof Error ? e.message : 'unknown error'}`, error: true }),
    })
  }

  function doUpdate() {
    setMessage(null)
    update.mutate(undefined, {
      onSuccess: (r) => setMessage({ text: r.version ? `Update to ${r.version} pushed.` : 'Update pushed.', error: false }),
      onError: (e) => setMessage({ text: `Could not push an update: ${e instanceof Error ? e.message : 'unknown error'}`, error: true }),
    })
  }

  function doRemove() {
    remove.mutate(agentId, {
      onSuccess: () => navigate('/fleet'),
      onError: (e) => setMessage({ text: `Could not remove this host: ${e instanceof Error ? e.message : 'unknown error'}`, error: true }),
    })
  }

  /** Plain browser navigation, NOT a fetch — Content-Disposition and cookie
   * auth need a real navigation, same as every other installer download in
   * this app (Wizard's `startDownload`, notes/api-contract-actual.md §6). */
  function doReinstall() {
    const params = new URLSearchParams({ os: toShareOs(os) })
    if (arch) params.set('arch', arch)
    window.location.href = `/api/agents/${encodeURIComponent(agentId)}/installer?${params.toString()}`
  }

  function requestShare() {
    setCopiedField(null)
    setShareState({ status: 'loading' })
    setShareOpen(true)
    shareLink.mutate(toShareOs(os), {
      onSuccess: (link) => setShareState({ status: 'done', link }),
      onError: (e) => setShareState({ status: 'error', message: e instanceof Error ? e.message : 'unknown error' }),
    })
  }

  async function copyShareValue(field: 'url' | 'oneliner', value: string) {
    try {
      await navigator.clipboard.writeText(value)
      setCopiedField(field)
      setTimeout(() => setCopiedField(null), 2000)
    } catch {
      // Clipboard access can be denied by the browser — the value is still
      // right there in the readonly field to select and copy by hand.
    }
  }

  return (
    <div>
      <div className={`${styles.row} kc-hostactions`}>
        <button type="button" className={styles.button} onClick={doRefresh} disabled={refresh.isPending}>
          <RefreshCw width={13} height={13} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          {refresh.isPending ? 'REFRESHING…' : 'REFRESH'}
        </button>
        <button type="button" className={styles.button} onClick={doRemoteHelp} disabled={remoteHelp.isPending}>
          <LifeBuoy width={13} height={13} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          {remoteHelp.isPending ? 'STARTING…' : 'REMOTE HELP'}
        </button>
        {isOperator && (
          <button type="button" className={styles.button} onClick={doReinstall}>
            <Package width={13} height={13} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
            REINSTALL
          </button>
        )}
        {isOperator && (
          <button type="button" className={styles.button} onClick={requestShare}>
            <LinkIcon width={13} height={13} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
            RE-SHARE
          </button>
        )}
        {isOperator && (
          <button type="button" className={styles.button} onClick={doUpdate} disabled={update.isPending}>
            <ArrowUpCircle width={13} height={13} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
            {update.isPending ? 'UPDATING…' : 'UPDATE AGENT'}
          </button>
        )}
        {isOperator && (
          <button type="button" className={`${styles.button} ${styles.remove}`} onClick={() => setConfirmRemove(true)}>
            <Trash2 width={13} height={13} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
            REMOVE
          </button>
        )}
      </div>

      {message && <p className={`${styles.message}${message.error ? ` ${styles.messageError}` : ''}`}>{message.text}</p>}

      {isOperator && (
        <div className={styles.channelRow}>
          <span className={styles.channelLabel}>CHANNEL</span>
          {(['stable', 'dev'] as const).map((c) => (
            <button
              key={c}
              type="button"
              className={`${styles.channelButton}${channel === c ? ` ${styles.channelActive}` : ''}`}
              disabled={setChannel.isPending}
              onClick={() => setChannel.mutate(c)}
            >
              {c.toUpperCase()}
            </button>
          ))}
        </div>
      )}

      <Modal open={confirmRemove} onClose={() => setConfirmRemove(false)} labelledBy="remove-host-title" width={440}>
        <div className={styles.modalHeader}>
          <span id="remove-host-title" className={styles.modalTitle}>
            REMOVE HOST
          </span>
          <button type="button" className={styles.cancelButton} style={{ minHeight: 44, minWidth: 44, padding: 0, border: 'none' }} onClick={() => setConfirmRemove(false)}>
            <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          </button>
        </div>
        <div className={styles.confirmBody}>
          <p className={styles.confirmText}>
            This purges every record of <strong>{agentId}</strong> — telemetry, screenshots, and history. The agent
            itself keeps running and can be re-added later. This cannot be undone.
          </p>
          <div className={styles.confirmActions}>
            <button type="button" className={styles.cancelButton} onClick={() => setConfirmRemove(false)}>
              CANCEL
            </button>
            <button type="button" className={styles.confirmRemoveButton} onClick={doRemove} disabled={remove.isPending}>
              {remove.isPending ? 'REMOVING…' : 'REMOVE HOST'}
            </button>
          </div>
        </div>
      </Modal>

      <Modal open={shareOpen} onClose={() => setShareOpen(false)} labelledBy="reshare-title" width={480}>
        <div className={styles.modalHeader}>
          <span id="reshare-title" className={styles.modalTitle}>
            RE-SHARE LINK
          </span>
          <button type="button" className={styles.cancelButton} style={{ minHeight: 44, minWidth: 44, padding: 0, border: 'none' }} onClick={() => setShareOpen(false)}>
            <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          </button>
        </div>
        <div className={styles.shareBody}>
          {shareState.status === 'loading' && <p className={styles.shareMeta}>Minting a link…</p>}

          {shareState.status === 'error' && (
            <>
              <p className={styles.shareError}>Could not create the link: {shareState.message}.</p>
              <button type="button" className={styles.cancelButton} onClick={requestShare}>
                TRY AGAIN
              </button>
            </>
          )}

          {shareState.status === 'done' && (
            <>
              <div className={styles.linkRow}>
                <input
                  className={styles.linkInput}
                  readOnly
                  value={shareState.link.url}
                  onFocus={(e) => e.target.select()}
                  aria-label="Share link"
                />
                <button type="button" className={styles.copyButton} onClick={() => void copyShareValue('url', shareState.link.url)}>
                  {copiedField === 'url' ? 'COPIED' : 'COPY'}
                </button>
              </div>
              {shareState.link.oneliner && (
                <div className={styles.linkRow}>
                  <input
                    className={styles.linkInput}
                    readOnly
                    value={shareState.link.oneliner}
                    onFocus={(e) => e.target.select()}
                    aria-label="Install one-liner"
                  />
                  <button
                    type="button"
                    className={styles.copyButton}
                    onClick={() => void copyShareValue('oneliner', shareState.link.oneliner as string)}
                  >
                    {copiedField === 'oneliner' ? 'COPIED' : 'COPY'}
                  </button>
                </div>
              )}
              <p className={styles.shareMeta}>
                Works once — expires {new Date(shareState.link.expires_at).toLocaleString()}.
              </p>
            </>
          )}
        </div>
      </Modal>
    </div>
  )
}

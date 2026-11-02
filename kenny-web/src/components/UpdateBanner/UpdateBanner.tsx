import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { useOutdatedAgents, useUpdateOutdated } from '../../api/agentUpdates'
import type { Me, UpdateOutdatedResult } from '../../api/types'
import { ArrowUp, ICON_STROKE_WIDTH } from '../icons'
import styles from './UpdateBanner.module.css'

/**
 * Fleet's "these PCs run an older agent than the one staged" offer.
 *
 * Detection is the server's: an agent's reported version against the binary staged
 * for its own OS, architecture and channel. This only shows the answer and lets an
 * operator act on it — one PC, or every online PC that is behind. Pressing a button
 * is the consent; nothing here updates a machine on its own, and the agent's own
 * kill switch still decides whether it accepts.
 *
 * Offline PCs are listed but not offered a button: the update cannot reach them, and
 * the server announces them again when they reconnect. A scoped `user` sees nothing,
 * since updating is operator+ and a banner with no usable action is only noise.
 */
export default function UpdateBanner() {
  const me = useQuery({ queryKey: ['me'], queryFn: () => api.get<Me>('/api/me') })
  const isOperator = me.data ? me.data.role !== 'user' : false
  const outdated = useOutdatedAgents(isOperator)
  const update = useUpdateOutdated()

  const agents = outdated.data?.agents ?? []
  if (!isOperator || agents.length === 0) return null

  const online = agents.filter((a) => a.online)
  const staged = [...new Set(agents.map((a) => a.staged_version))].join(', ')
  const results = update.data?.results ?? []
  const failed = results.filter((r) => r.status === 'failed')
  const sentCount = update.data?.sent ?? 0

  function updateAll() {
    const names = online.map((a) => a.agent_id).join(', ')
    if (!window.confirm(`Update ${online.length} PC${online.length === 1 ? '' : 's'} to ${staged}? ${names}`)) return
    update.mutate(undefined)
  }

  return (
    <div className={styles.banner} role="status" aria-label="Agent updates available">
      <div className={styles.head}>
        <ArrowUp width={15} height={15} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" className={styles.icon} />
        <div className={styles.text}>
          <strong className={styles.title}>
            {agents.length === 1 ? '1 PC runs' : `${agents.length} PCs run`} an older agent than the staged {staged}.
          </strong>
          <div className={styles.sub}>Each PC downloads the staged build and restarts its agent.</div>
        </div>
        {online.length > 1 && (
          <button type="button" className={styles.btn} onClick={updateAll} disabled={update.isPending}>
            {update.isPending ? 'UPDATING…' : `UPDATE ALL ONLINE (${online.length})`}
          </button>
        )}
      </div>
      <ul className={styles.list}>
        {agents.map((a) => (
          <li key={a.agent_id} className={styles.row}>
            <span className={styles.name}>{a.agent_id}</span>
            <span className={styles.versions}>
              {a.current_version} → {a.staged_version}
            </span>
            {a.online ? (
              <button
                type="button"
                className={styles.btn}
                onClick={() => update.mutate([a.agent_id])}
                disabled={update.isPending}
              >
                UPDATE
              </button>
            ) : (
              <span className={styles.offline}>offline — offered again on reconnect</span>
            )}
          </li>
        ))}
      </ul>
      {update.isError && (
        <div className={`${styles.result} ${styles.failed}`}>
          {update.error instanceof Error ? update.error.message : 'The update request failed.'}
        </div>
      )}
      {update.isSuccess && (
        <div className={styles.result}>
          {sentCount > 0 ? `Sent to ${sentCount}. They reconnect on the new version.` : 'Nothing was sent.'}
          {failed.length > 0 && (
            <span className={styles.failed}> {failed.map((r) => describeFailure(r)).join('; ')}</span>
          )}
        </div>
      )}
    </div>
  )
}

function describeFailure(r: UpdateOutdatedResult): string {
  return `${r.agent_id}: ${r.error ?? 'failed'}`
}

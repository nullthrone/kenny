import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { useAgentBinary, useRetryAgentBinaryFetch } from '../../api/agentBinary'
import type { Me } from '../../api/types'
import { AlertTriangle, ICON_STROKE_WIDTH } from '../icons'
import styles from './BinaryBanner.module.css'

/**
 * Fleet's "there is nothing to install" notice.
 *
 * The server stages the agent binary itself, from GitHub when a token is
 * configured (ADR-0015) or from an operator-placed file. When it has none, every
 * onboarding path fails at the last step — and the installer download is a plain
 * browser navigation, so the failure arrives as a raw JSON 503 replacing the
 * page. Saying so up front, next to "Add a PC", is the difference between a
 * known state and a broken one.
 *
 * `retry GitHub fetch` is offered to any operator: the fetch reads GitHub
 * anonymously (ADR-0057), so there is no configuration that could make the
 * button useless in advance. Retrying is operator+ server-side, so a scoped
 * `user` gets the explanation without a button they cannot press.
 */
export default function BinaryBanner() {
  const binary = useAgentBinary()
  const me = useQuery({ queryKey: ['me'], queryFn: () => api.get<Me>('/api/me') })
  const retry = useRetryAgentBinaryFetch()

  const status = binary.data
  if (!status) return null

  // `by_os` is the authority when present; `available` is the older Windows-only
  // field and stands in for servers that predate it.
  const anyAvailable = status.by_os
    ? Object.values(status.by_os).some(Boolean)
    : status.available
  if (anyAvailable) return null

  const isOperator = me.data ? me.data.role !== 'user' : false
  const canRetry = isOperator
  const attempt = status.last_check?.message ?? status.last_fetch?.message ?? ''

  return (
    <div className={styles.banner} role="status">
      <AlertTriangle width={15} height={15} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" className={styles.icon} />
      <div className={styles.text}>
        <strong className={styles.title}>No agent installer is staged.</strong>{' '}
        {`kenny fetches it from ${status.repo ?? 'GitHub'} releases. `}
        {/*
          The durable row (`last_check`) leads: `last_fetch` lives on the server
          process, so after a restart it is null while a refresh has in fact
          been failing for weeks — and "no fetch has been attempted yet" then
          describes the wrong problem. Say that only when neither record exists.
        */}
        {attempt && <span className={styles.reason}>Last attempt: {attempt}</span>}
        {!attempt && <span className={styles.reason}>No fetch has been attempted yet.</span>}
        <div className={styles.consequence}>
          Adding a PC still mints a token and a link, but there is no binary to hand over yet.
        </div>
      </div>
      {canRetry && (
        <button type="button" className={styles.retry} onClick={() => retry.mutate()} disabled={retry.isPending}>
          {retry.isPending ? 'FETCHING…' : 'RETRY GITHUB FETCH'}
        </button>
      )}
    </div>
  )
}

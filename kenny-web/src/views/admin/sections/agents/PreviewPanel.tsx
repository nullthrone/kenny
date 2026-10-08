import { useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../../../api/client'
import type { AgentPreviewStarted, SpecializedAgent } from '../../types'
import { AGENTS_KEY, useAgentParams, useFleetHosts } from './queries'
import shared from '../../shared.module.css'
import styles from './agents.module.css'

export interface PreviewPanelProps {
  agent: SpecializedAgent
  /** Called with the id of the run a successful start created, so the runs list can open it. */
  onStarted: (runId: string) => void
}

/** A server that predates preview runs has no such route; both statuses mean "not offered here". */
function isUnsupported(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 404 || err.status === 405)
}

/**
 * "Run preview": one shadow run, started by hand (operator and up). A preview is always
 * shadow — it reads, decides and reports, and every change it would make is only
 * recorded as a recommendation — whatever mode the agent is in.
 */
export default function PreviewPanel({ agent, onStarted }: PreviewPanelProps) {
  const queryClient = useQueryClient()
  const params = useAgentParams(agent.id)
  const fleet = useFleetHosts()
  const [hostId, setHostId] = useState('')
  const [started, setStarted] = useState<string | null>(null)

  const startsFromTicket = agent.trigger.kind === 'event'
  const hostBound = params.data?.declared.includes('hosts') ?? false
  const configured = Array.isArray(agent.params.hosts) ? agent.params.hosts : []
  const fleetIds = fleet.data?.agents.map((a) => a.agent_id) ?? []
  const choices = configured.length > 0 ? configured : fleetIds

  const start = useMutation({
    mutationFn: () =>
      api.post<AgentPreviewStarted>(
        `/api/specialized-agents/${encodeURIComponent(agent.id)}/runs`,
        hostBound ? { host_id: hostId } : {},
      ),
    onMutate: () => setStarted(null),
    onSuccess: (res) => {
      setStarted(`Preview started${hostBound ? ` on ${hostId}` : ''}. It is open under Runs below.`)
      queryClient.invalidateQueries({ queryKey: AGENTS_KEY })
      onStarted(res.run_id)
    },
  })

  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    if (!hostBound || hostId) start.mutate()
  }

  const headingId = `preview-${agent.id}`

  return (
    <section className={shared.card} aria-labelledby={headingId}>
      <h3 className={styles.heading} id={headingId}>
        PREVIEW
      </h3>
      <p className={shared.help} style={{ marginBottom: 12 }}>
        A preview runs the agent once in <strong>shadow</strong>, whatever its mode: it reads, decides and reports, and
        every change it would make is only recorded as a recommendation. <strong>A preview never changes anything</strong>{' '}
        on any PC. It does spend model tokens.
      </p>
      {startsFromTicket ? (
        <p className={shared.help}>
          This agent starts from a new ticket, so there is nothing to preview by hand. Its shadow runs appear under Runs.
        </p>
      ) : (
        <form className={styles.row} onSubmit={submit} aria-label={`Preview ${agent.title}`}>
          {hostBound && (
            <label className={shared.field}>
              <span className={shared.fieldLabel}>PC</span>
              <select className={shared.input} value={hostId} onChange={(e) => setHostId(e.target.value)} required>
                <option value="">choose a PC…</option>
                {choices.map((id) => (
                  <option key={id} value={id}>
                    {id}
                  </option>
                ))}
              </select>
            </label>
          )}
          <button type="submit" className={shared.btnPrimary} disabled={start.isPending || params.isLoading || (hostBound && !hostId)}>
            {start.isPending ? 'STARTING…' : 'RUN PREVIEW'}
          </button>
        </form>
      )}
      <div aria-live="polite">
        {started && (
          <div className={shared.okBox} style={{ marginTop: 12 }}>
            {started}
          </div>
        )}
        {start.isError && (
          <div className={shared.errorBox} role="alert" style={{ marginTop: 12 }}>
            {isUnsupported(start.error)
              ? 'This server does not offer preview runs yet. Update the server to start one from here.'
              : start.error instanceof ApiError && start.error.message
                ? start.error.message
                : 'Could not start the preview.'}
          </div>
        )}
      </div>
    </section>
  )
}

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, ApiError } from '../../../api/client'
import EmptyState from '../../../components/EmptyState/EmptyState'
import type { FleetResponse } from '../../../api/types'
import type { SuppressionRule } from '../../host/types'
import { useAddSuppression, useRemoveSuppression, useSuppressions } from '../../host/api'
import shared from '../shared.module.css'

/**
 * Admin → Alarm rules → reliability suppressions (ADR-0041). Every rule, fleet-wide
 * and per host, in one list. A host's page offers "suppress on this host" from the
 * event itself; this is where a fleet-wide rule is added and where all of them are
 * reviewed.
 */
export default function SuppressionsSection() {
  const [eventId, setEventId] = useState('')
  const [source, setSource] = useState('')
  const [agentId, setAgentId] = useState('')
  const [note, setNote] = useState('')

  const rules = useSuppressions()
  const fleet = useQuery({ queryKey: ['fleet'], queryFn: () => api.get<FleetResponse>('/api/fleet') })
  const add = useAddSuppression()
  const remove = useRemoveSuppression()

  function handleRemove(rule: SuppressionRule) {
    if (!rule.agent_id && !window.confirm(`Remove the fleet-wide suppression of event ${rule.event_id}? This affects every host.`)) return
    remove.mutate(rule.id)
  }

  if (rules.isLoading) return <div className={shared.loading}>Loading…</div>
  if (rules.isError || !rules.data) return <EmptyState title="Could not load suppressions" message="Something went wrong. Reload to try again." />

  const error = [add, remove].map((m) => (m.error instanceof ApiError ? m.error.message : m.isError ? 'Something went wrong. Try again.' : null)).find((m) => m)

  return (
    <div>
      <p className={shared.help} style={{ marginBottom: 16 }}>
        Reliability events that never count against a host&apos;s health. A rule with no host applies fleet-wide.
      </p>

      {rules.data.rules.length === 0 ? (
        <EmptyState title="No suppressions" message="Every reliability event counts until a rule mutes it." />
      ) : (
        <div className={shared.table} style={{ marginBottom: 24 }}>
          {rules.data.rules.map((r) => (
            <div key={r.id} className={shared.tableRow}>
              <div className={shared.tableMeta}>
                <div className={shared.tableLabel}>
                  event {r.event_id}
                  {r.source ? ` · ${r.source}` : ''}
                </div>
                <div className={shared.tableSub}>
                  {r.agent_id || 'fleet-wide'}
                  {r.note ? ` · ${r.note}` : ''}
                  {r.created_by ? ` · by ${r.created_by}` : ''}
                </div>
              </div>
              <button type="button" className={shared.btnDanger} onClick={() => handleRemove(r)} disabled={remove.isPending}>
                REMOVE
              </button>
            </div>
          ))}
        </div>
      )}

      <div className={shared.cardTitle}>ADD SUPPRESSION</div>
      {error && <div className={shared.errorBox}>{error}</div>}
      <form
        onSubmit={(e) => {
          e.preventDefault()
          add.mutate(
            { event_id: Number(eventId), source: source.trim(), agent_id: agentId, note: note.trim() },
            {
              onSuccess: () => {
                setEventId('')
                setSource('')
                setNote('')
              },
            },
          )
        }}
        className={shared.actions}
        style={{ marginTop: 0, alignItems: 'flex-end' }}
      >
        <label className={shared.field}>
          <span className={shared.fieldLabel}>EVENT ID</span>
          <input type="number" min={0} step={1} className={shared.input} value={eventId} onChange={(e) => setEventId(e.target.value)} required />
        </label>
        <label className={shared.field}>
          <span className={shared.fieldLabel}>SOURCE (OPTIONAL)</span>
          <input type="text" className={shared.input} value={source} onChange={(e) => setSource(e.target.value)} placeholder="any source" />
        </label>
        <label className={shared.field}>
          <span className={shared.fieldLabel}>HOST (OPTIONAL)</span>
          <select className={shared.input} value={agentId} onChange={(e) => setAgentId(e.target.value)}>
            <option value="">fleet-wide</option>
            {fleet.data?.agents.map((a) => (
              <option key={a.agent_id} value={a.agent_id}>
                {a.agent_id}
              </option>
            ))}
          </select>
        </label>
        <label className={shared.field} style={{ minWidth: 160 }}>
          <span className={shared.fieldLabel}>NOTE (OPTIONAL)</span>
          <input type="text" className={shared.input} value={note} onChange={(e) => setNote(e.target.value)} />
        </label>
        <button type="submit" className={shared.btnPrimary} disabled={eventId.trim() === '' || add.isPending}>
          {add.isPending ? 'ADDING…' : 'ADD'}
        </button>
      </form>
    </div>
  )
}

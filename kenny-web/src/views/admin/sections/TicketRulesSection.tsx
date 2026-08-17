import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../../api/client'
import EmptyState from '../../../components/EmptyState/EmptyState'
import type { FleetResponse } from '../../../api/types'
import type { TicketRule, TicketRuleVocabulary } from '../types'
import shared from '../shared.module.css'

/** Admin → Auto-ticket rules. Which alerts open a ticket, fleet-wide or per host. */
export default function TicketRulesSection() {
  const queryClient = useQueryClient()
  const [eventType, setEventType] = useState('')
  const [section, setSection] = useState('')
  const [agentId, setAgentId] = useState('')
  const [decision, setDecision] = useState('')
  const [note, setNote] = useState('')
  const [warnings, setWarnings] = useState<string[]>([])

  const vocab = useQuery({ queryKey: ['admin', 'ticket-rules', 'vocabulary'], queryFn: () => api.get<TicketRuleVocabulary>('/api/ticket-rules/vocabulary') })
  const rules = useQuery({ queryKey: ['admin', 'ticket-rules'], queryFn: () => api.get<{ rules: TicketRule[] }>('/api/ticket-rules') })
  const fleet = useQuery({ queryKey: ['fleet'], queryFn: () => api.get<FleetResponse>('/api/fleet') })

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ['admin', 'ticket-rules'] })
  }

  const add = useMutation({
    mutationFn: () => api.post<{ rules: TicketRule[]; warnings: string[] }>('/api/ticket-rules', { event_type: eventType, section, agent_id: agentId, decision, note }),
    onSuccess: (res) => {
      setWarnings(res.warnings)
      setSection('')
      setNote('')
      invalidate()
    },
  })

  const remove = useMutation({
    mutationFn: (id: string) => api.delete<{ ok: boolean }>(`/api/ticket-rules/${id}`),
    onSuccess: invalidate,
  })

  function handleRemove(rule: TicketRule) {
    if (!rule.agent_id) {
      if (!window.confirm(`Remove the fleet-wide rule for ${rule.event_type}${rule.section ? ` · ${rule.section}` : ''}? This affects every host.`)) return
    }
    remove.mutate(rule.id)
  }

  if (vocab.isLoading || rules.isLoading) return <div className={shared.loading}>Loading…</div>
  if (vocab.isError || rules.isError) return <EmptyState title="Could not load ticket rules" message="Something went wrong. Reload to try again." />
  if (!vocab.data || !rules.data) return null

  const sectionsForType = eventType ? (vocab.data.sections[eventType] ?? []) : []

  return (
    <div>
      <p className={shared.help} style={{ marginBottom: 16 }}>
        Which alerts open a ticket automatically. A rule with no host applies fleet-wide.
      </p>

      {rules.data.rules.length === 0 ? (
        <EmptyState title="No rules yet" message="Every alert falls back to the default decision until a rule is added." />
      ) : (
        <div className={shared.table} style={{ marginBottom: 24 }}>
          {rules.data.rules.map((r) => (
            <div key={r.id} className={shared.tableRow}>
              <div className={shared.tableMeta}>
                <div className={shared.tableLabel}>
                  {r.event_type}
                  {r.section ? ` · ${r.section}` : ''}
                </div>
                <div className={shared.tableSub}>
                  {r.agent_id || 'fleet-wide'} · {r.decision}
                  {r.note ? ` · ${r.note}` : ''}
                </div>
              </div>
              <button type="button" className={shared.btnDanger} onClick={() => handleRemove(r)} disabled={remove.isPending}>
                REMOVE
              </button>
            </div>
          ))}
        </div>
      )}

      <div className={shared.cardTitle}>ADD RULE</div>
      {add.isError && (
        <div className={shared.errorBox}>{add.error instanceof ApiError ? add.error.message : 'Could not add the rule.'}</div>
      )}
      {warnings.length > 0 && <div className={shared.warnBox}>{warnings.join(' ')}</div>}
      <form
        onSubmit={(e) => {
          e.preventDefault()
          add.mutate()
        }}
        className={shared.actions}
        style={{ marginTop: 0, alignItems: 'flex-end' }}
      >
        <label className={shared.field}>
          <span className={shared.fieldLabel}>EVENT TYPE</span>
          <select className={shared.input} value={eventType} onChange={(e) => { setEventType(e.target.value); setSection('') }} required>
            <option value="">choose…</option>
            {vocab.data.event_types.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label className={shared.field}>
          <span className={shared.fieldLabel}>SECTION (OPTIONAL)</span>
          <select className={shared.input} value={section} onChange={(e) => setSection(e.target.value)}>
            <option value="">any</option>
            {sectionsForType.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
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
        <label className={shared.field}>
          <span className={shared.fieldLabel}>DECISION</span>
          <select className={shared.input} value={decision} onChange={(e) => setDecision(e.target.value)} required>
            <option value="">choose…</option>
            {vocab.data.decisions.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </label>
        <label className={shared.field} style={{ minWidth: 160 }}>
          <span className={shared.fieldLabel}>NOTE (OPTIONAL)</span>
          <input type="text" className={shared.input} value={note} onChange={(e) => setNote(e.target.value)} />
        </label>
        <button type="submit" className={shared.btnPrimary} disabled={!eventType || !decision || add.isPending}>
          {add.isPending ? 'ADDING…' : 'ADD'}
        </button>
      </form>
    </div>
  )
}

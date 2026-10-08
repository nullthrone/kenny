import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../../../../api/client'
import type { AgentMode, AgentModeSet, SpecializedAgent } from '../../types'
import { shortHash } from './badges'
import { AGENTS_KEY, consentErrorMessage, isHashChanged } from './queries'
import shared from '../../shared.module.css'
import styles from './agents.module.css'

const MODES: { mode: AgentMode; label: string; help: string }[] = [
  { mode: 'off', label: 'OFF', help: 'Never started.' },
  { mode: 'shadow', label: 'SHADOW', help: 'Runs the whole job; every change is refused and kept as a recommendation.' },
  { mode: 'act', label: 'ACT', help: 'Makes the changes its constraints and standing authorizations allow, with nobody asked.' },
]

export interface ModePanelProps {
  agent: SpecializedAgent
  canManage: boolean
}

/**
 * The mode switch. Choosing `act` is a person's consent to exactly what is on this page,
 * so the request carries the effective hash that is displayed here (ADR-0072 rule 2): if
 * the agent has changed since it was loaded the server answers 409 and nothing is bound.
 */
export default function ModePanel({ agent, canManage }: ModePanelProps) {
  const queryClient = useQueryClient()
  const [confirmingAct, setConfirmingAct] = useState(false)
  const [outcome, setOutcome] = useState<{ tone: 'ok' | 'warn'; text: string } | null>(null)

  const refresh = () => queryClient.invalidateQueries({ queryKey: AGENTS_KEY })

  const change = useMutation({
    mutationFn: (mode: AgentMode) =>
      api.put<AgentModeSet>(
        `/api/specialized-agents/${encodeURIComponent(agent.id)}/mode`,
        mode === 'act' ? { mode, effective_hash: agent.effective_hash } : { mode },
      ),
    onMutate: () => setOutcome(null),
    onSuccess: (res) => {
      setConfirmingAct(false)
      if (res.mode !== res.requested) {
        setOutcome({
          tone: 'warn',
          text: `You chose ${res.requested}, but the agent is running as ${res.mode}. Triage stays off while no AI key or gateway is configured.`,
        })
      } else {
        setOutcome({ tone: 'ok', text: `Mode is now ${res.mode}.` })
      }
      refresh()
    },
  })

  const headingId = `mode-${agent.id}`

  return (
    <section className={shared.card} aria-labelledby={headingId}>
      <h3 className={styles.heading} id={headingId}>
        MODE
      </h3>
      <div role="group" aria-labelledby={headingId} className={styles.modeButtons}>
        {MODES.map(({ mode, label, help }) => (
          <button
            key={mode}
            type="button"
            className={styles.modeButton}
            aria-pressed={agent.mode === mode}
            title={help}
            disabled={
              !canManage ||
              change.isPending ||
              (agent.mode === mode && !(mode === 'act' && agent.act_bound === false)) ||
              (mode === 'act' && !agent.effective_hash)
            }
            onClick={() => {
              if (mode === 'act') setConfirmingAct(true)
              else {
                setConfirmingAct(false)
                change.mutate(mode)
              }
            }}
          >
            {label}
          </button>
        ))}
      </div>
      <p className={shared.help}>{MODES.find((m) => m.mode === agent.mode)?.help}</p>

      {!canManage && (
        <p className={shared.help} style={{ marginTop: 8 }}>
          Choosing a mode is a superuser&apos;s decision, made signed in to the dashboard.
        </p>
      )}

      {canManage && confirmingAct && (
        <div className={shared.warnBox} role="group" aria-label="Confirm act" style={{ marginTop: 12 }}>
          <p style={{ margin: '0 0 8px' }}>
            In <strong>act</strong> this agent makes the changes its limits allow, without asking each time. It is bound to
            the version you are looking at (hash <span className={styles.hash}>{shortHash(agent.effective_hash)}</span>):
            any later change to its spec or parameters drops it back to shadow. A normal change still needs a standing
            authorization below.
          </p>
          <div className={shared.actions} style={{ marginTop: 0 }}>
            <button type="button" className={shared.btnPrimary} onClick={() => change.mutate('act')} disabled={change.isPending}>
              {change.isPending ? 'SETTING…' : 'CONFIRM ACT'}
            </button>
            <button type="button" className={shared.btn} onClick={() => setConfirmingAct(false)} disabled={change.isPending}>
              CANCEL
            </button>
          </div>
        </div>
      )}

      <div aria-live="polite">
        {outcome && (
          <div className={outcome.tone === 'ok' ? shared.okBox : shared.warnBox} style={{ marginTop: 12 }}>
            {outcome.text}
          </div>
        )}
        {change.isError && (
          <div className={shared.errorBox} role="alert" style={{ marginTop: 12 }}>
            {consentErrorMessage(change.error, 'Could not change the mode.')}
            {isHashChanged(change.error) && (
              <>
                {' '}
                <button type="button" className={styles.linkButton} onClick={() => { change.reset(); setConfirmingAct(false); refresh() }}>
                  RELOAD
                </button>
              </>
            )}
          </div>
        )}
      </div>
    </section>
  )
}

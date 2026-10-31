import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../../../../api/client'
import type { StewardMode, StewardModeSet, Steward } from '../../types'
import { shortHash } from './badges'
import { STEWARDS_KEY, consentErrorMessage, isHashChanged } from './queries'
import shared from '../../shared.module.css'
import styles from './stewards.module.css'

const MODES: { mode: StewardMode; label: string; help: string }[] = [
  { mode: 'off', label: 'OFF', help: 'Never started.' },
  { mode: 'shadow', label: 'SHADOW', help: 'Runs the whole job; every change is refused and kept as a recommendation.' },
  { mode: 'act', label: 'ACT', help: 'Makes the changes its constraints and standing authorizations allow, with nobody asked.' },
]

export interface ModePanelProps {
  steward: Steward
  canManage: boolean
}

/**
 * The mode switch. Choosing `act` is a person's consent to exactly what is on this page,
 * so the request carries the effective hash that is displayed here (ADR-0072 rule 2): if
 * the steward has changed since it was loaded the server answers 409 and nothing is bound.
 */
export default function ModePanel({ steward, canManage }: ModePanelProps) {
  const queryClient = useQueryClient()
  const [confirmingAct, setConfirmingAct] = useState(false)
  const [outcome, setOutcome] = useState<{ tone: 'ok' | 'warn'; text: string } | null>(null)

  const refresh = () => queryClient.invalidateQueries({ queryKey: STEWARDS_KEY })

  const change = useMutation({
    mutationFn: (mode: StewardMode) =>
      api.put<StewardModeSet>(
        `/api/stewards/${encodeURIComponent(steward.id)}/mode`,
        mode === 'act' ? { mode, effective_hash: steward.effective_hash } : { mode },
      ),
    onMutate: () => setOutcome(null),
    onSuccess: (res) => {
      setConfirmingAct(false)
      if (res.mode !== res.requested) {
        setOutcome({
          tone: 'warn',
          text: `You chose ${res.requested}, but the steward is running as ${res.mode}. Triage stays off while no AI key or gateway is configured.`,
        })
      } else {
        setOutcome({ tone: 'ok', text: `Mode is now ${res.mode}.` })
      }
      refresh()
    },
  })

  const headingId = `mode-${steward.id}`

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
            aria-pressed={steward.mode === mode}
            title={help}
            disabled={
              !canManage ||
              change.isPending ||
              (steward.mode === mode && !(mode === 'act' && steward.act_bound === false)) ||
              (mode === 'act' && !steward.effective_hash)
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
      <p className={shared.help}>{MODES.find((m) => m.mode === steward.mode)?.help}</p>

      {!canManage && (
        <p className={shared.help} style={{ marginTop: 8 }}>
          Choosing a mode is a superuser&apos;s decision, made signed in to the dashboard.
        </p>
      )}

      {canManage && confirmingAct && (
        <div className={shared.warnBox} role="group" aria-label="Confirm act" style={{ marginTop: 12 }}>
          <p style={{ margin: '0 0 8px' }}>
            In <strong>act</strong> this steward makes the changes its limits allow, without asking each time. It is bound to
            the version you are looking at (hash <span className={styles.hash}>{shortHash(steward.effective_hash)}</span>):
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

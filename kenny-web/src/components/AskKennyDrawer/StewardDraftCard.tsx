import { useState } from 'react'
import { STEWARD_DRAFT_ROUTE, STEWARD_DRAFT_STORAGE_KEY, type StewardDraft } from '../../api/types'
import styles from './TicketDraftCard.module.css'
import own from './StewardRunProposalCard.module.css'
import draftStyles from './StewardDraftCard.module.css'

export interface StewardDraftCardProps {
  draft: StewardDraft
  /** What the copilot said when it drafted this; shown as its suggestion, never as the server's word. */
  reason: string
  /** The server's tier of each tool the draft names. */
  toolClasses: Record<string, string>
  /** The server's list of the draft's tools that read private data. */
  sensitiveTools?: string[]
}

const TIER_ORDER = ['read_only', 'standard_change', 'normal_change']
const TIER_LABEL: Record<string, string> = {
  read_only: 'read-only',
  standard_change: 'standard change',
  normal_change: 'normal change',
}

function constraintText(c: StewardDraft['constraints'][number]): string {
  const bound = c.param !== undefined ? `the parameter ${c.param}` : (c.allowed ?? []).join(', ') || 'nothing'
  return `${c.tool}.${c.arg} may only be ${bound}`
}

function startText(trigger: StewardDraft['trigger']): string {
  if (trigger.kind === 'event') return `when ${trigger.event ?? 'an event'} is raised`
  const every = trigger.min_interval_days
  return every ? `on its schedule, at most every ${every} days` : 'on its schedule, in every window'
}

/**
 * A steward kenny drafted, as a card to read and, if it looks right, take to the editor.
 *
 * Nothing is saved when this renders or when its button is pressed (ADR-0074: the
 * model drafts, never saves). **Open in editor** stores the draft in `sessionStorage`
 * under {@link STEWARD_DRAFT_STORAGE_KEY} and navigates to {@link STEWARD_DRAFT_ROUTE};
 * the steward exists only if a superuser saves it there.
 *
 * Every string on the card -- the prompt included -- came from the model, so it is
 * rendered as escaped text and nothing in it is acted on.
 */
export default function StewardDraftCard({ draft, reason, toolClasses, sensitiveTools = [] }: StewardDraftCardProps) {
  const [error, setError] = useState<string | null>(null)

  const byTier = new Map<string, string[]>()
  for (const tool of draft.tools) {
    const tier = toolClasses[tool] ?? 'unknown'
    byTier.set(tier, [...(byTier.get(tier) ?? []), tool])
  }
  const tiers = [...byTier.keys()].sort((a, b) => {
    const ia = TIER_ORDER.indexOf(a)
    const ib = TIER_ORDER.indexOf(b)
    return (ia === -1 ? TIER_ORDER.length : ia) - (ib === -1 ? TIER_ORDER.length : ib)
  })
  const sensitive = draft.tools.filter((t) => sensitiveTools.includes(t))
  const allowed = draft.triggers_allowed
  const ceiling = [
    allowed.schedule ? 'schedule' : null,
    allowed.events.length > 0 ? `events: ${allowed.events.join(', ')}` : null,
    allowed.command ? 'command runs' : null,
  ].filter((p): p is string => p !== null)

  function openInEditor() {
    setError(null)
    try {
      window.sessionStorage.setItem(STEWARD_DRAFT_STORAGE_KEY, JSON.stringify(draft))
    } catch {
      setError('Could not hand the draft to the editor: this browser would not store it.')
      return
    }
    window.location.hash = STEWARD_DRAFT_ROUTE
  }

  return (
    <div className={styles.card} data-testid="steward-draft-card">
      <p className={styles.heading}>DRAFT STEWARD — nothing is saved until a superuser saves it in the editor</p>
      <dl className={own.facts}>
        <dt>TITLE</dt>
        <dd>{draft.title}</dd>
        <dt>ID</dt>
        <dd>
          <code>{draft.id}</code>
        </dd>
        {draft.description && (
          <>
            <dt>ABOUT</dt>
            <dd>{draft.description}</dd>
          </>
        )}
        <dt>TOOLS</dt>
        <dd>
          <ul className={draftStyles.list}>
            {tiers.map((tier) => (
              <li key={tier}>
                {TIER_LABEL[tier] ?? tier}: {(byTier.get(tier) ?? []).map((t) => (
                  <span key={t}>
                    <code>{t}</code>
                    {sensitiveTools.includes(t) && <em className={draftStyles.sensitive}> (reads private data)</em>}{' '}
                  </span>
                ))}
              </li>
            ))}
          </ul>
        </dd>
        <dt>CONSTRAINTS</dt>
        <dd>
          {draft.constraints.length === 0 ? (
            'none'
          ) : (
            <ul className={draftStyles.list}>
              {draft.constraints.map((c, i) => (
                <li key={`${c.tool}.${c.arg}.${i}`}>{constraintText(c)}</li>
              ))}
            </ul>
          )}
        </dd>
        {(sensitive.length > 0 || draft.sensitive_ok) && (
          <>
            <dt>PRIVATE DATA</dt>
            <dd>
              {sensitive.length > 0 ? `reads private data: ${sensitive.join(', ')}. ` : 'asks to read private data. '}
              The editor leaves <code>sensitive_ok</code> unticked; the consent is yours to give there.
            </dd>
          </>
        )}
        <dt>STARTS</dt>
        <dd>{startText(draft.trigger)}</dd>
        <dt>CAN BE STARTED BY</dt>
        <dd>{ceiling.length > 0 ? ceiling.join(' · ') : 'nothing yet — no trigger is allowed'}</dd>
        <dt>BUDGET</dt>
        <dd>
          {draft.budget.max_iterations} model turns
          {draft.timeouts.length > 0 && <>; timeouts: {draft.timeouts.map((t) => `${t.tool} ${t.max_s} s`).join(', ')}</>}
        </dd>
        <dt>PARAMETERS</dt>
        <dd>{draft.params.length > 0 ? draft.params.join(', ') : 'none'}</dd>
        <dt>PRECONDITIONS</dt>
        <dd>{draft.preconditions.length > 0 ? draft.preconditions.join(', ') : 'none'}</dd>
        {reason && (
          <>
            <dt>KENNY SUGGESTS</dt>
            <dd>
              <q className={own.suggested}>{reason}</q>
            </dd>
          </>
        )}
      </dl>
      <details className={draftStyles.prompt}>
        <summary>PROMPT — what the steward is told to do</summary>
        <pre className={draftStyles.promptText}>{draft.prompt}</pre>
      </details>
      <p className={own.note}>
        Kenny wrote this draft. Read the prompt and the limits above before saving: a saved steward starts in shadow
        mode, and nothing in the draft runs until a superuser saves it and it is started.
      </p>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      <div className={`${styles.footer} kc-actions`}>
        <button type="button" className={`${styles.create} kc-btn`} onClick={openInEditor}>
          OPEN IN EDITOR
        </button>
      </div>
    </div>
  )
}

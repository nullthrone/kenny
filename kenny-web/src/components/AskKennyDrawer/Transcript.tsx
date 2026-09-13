import { useEffect, useRef, useState } from 'react'
import type { TranscriptItem } from '../../chat/types'
import type { TicketDraftCardProps } from './TicketDraftCard'
import { Check, X, ICON_STROKE_WIDTH } from '../icons'
import Markdown from '../Markdown/Markdown'
import TicketDraftCard from './TicketDraftCard'
import styles from './Transcript.module.css'

/**
 * The spinner and elapsed-seconds counter on a call that is still running.
 *
 * A tool runs until it is done or until its own `timeout_s` — a PowerShell
 * sweep of a whole disk is minutes — and for that entire span the server has
 * nothing further to say. A spinner alone answers "is anything happening";
 * the counter answers "how long has it been", which is the question an
 * operator actually has by the second minute.
 *
 * It counts from its own mount rather than from a timestamp in state, which
 * is the same moment: the row is created by the `tool_started` that mounts it,
 * and keeping the clock here leaves the reducer pure.
 */
function Running() {
  const [seconds, setSeconds] = useState(0)

  useEffect(() => {
    const started = Date.now()
    const timer = window.setInterval(
      () => setSeconds(Math.floor((Date.now() - started) / 1000)),
      1000,
    )
    return () => window.clearInterval(timer)
  }, [])

  return (
    <>
      <span className={styles.spinner} aria-hidden="true" />
      running…{seconds > 0 ? ` ${seconds}s` : ''}
    </>
  )
}

export interface TranscriptProps {
  items: TranscriptItem[]
  /** The reasoning block still being written, if any — it is the only one whose
   * label says kenny is still at it. Absent on a replayed conversation, which
   * carries no reasoning at all. */
  openThinkingId?: string | null
  /** The gate row whose card is being rendered below this transcript. Skipped
   * here: an undecided gate is shown once, on the card that can decide it. */
  pendingGateItemId?: string | null
  /** The row of the call that is running right now, if any — the only row that
   * spins. A row left mid-flight by a Stop or a dropped stream is not this one
   * any more, and says its outcome is unknown instead of spinning forever. */
  openToolItemId?: string | null
  /** Opens the ticket a draft row is proposing, with the operator's edits. */
  onCreateDraft?: TicketDraftCardProps['onCreate']
  /** Puts a draft away unfiled. */
  onDismissDraft?: TicketDraftCardProps['onDismiss']
}

/**
 * Renders the folded event stream. Read-only tool calls are collapsed
 * one-line "auto-run" chips (never a decision UI — they already ran). A
 * `gate` row is a trace of what the confirm gate did, not the gate itself:
 * the actual CONFIRM & RUN / CANCEL decision only ever happens in
 * `PendingGateModal`, so this never renders a second set of action buttons.
 *
 * Reasoning is rendered as a closed disclosure. It is shown because a reader
 * who disagrees with an answer wants to see how it was reached; it is closed
 * because the answer is what they came for.
 */
export default function Transcript({
  items,
  openThinkingId = null,
  pendingGateItemId = null,
  openToolItemId = null,
  onCreateDraft,
  onDismissDraft,
}: TranscriptProps) {
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [items])

  if (items.length === 0) {
    return (
      <div className={styles.root} ref={scrollRef} data-shot="ask-kenny-transcript">
        <p className={styles.empty}>Ask kenny anything about this fleet — read-only checks run right away; anything that changes a machine waits for your confirmation.</p>
      </div>
    )
  }

  return (
    <div className={styles.root} ref={scrollRef} data-shot="ask-kenny-transcript">
      {items.map((item) => {
        switch (item.kind) {
          case 'user':
            return (
              <div key={item.id} className={styles.userBubble}>
                {item.text}
              </div>
            )

          case 'assistant':
            return (
              <Markdown key={item.id} className={styles.assistant} text={item.text} />
            )

          case 'thinking':
            return (
              // Folded, and folded by default: reasoning is context for the
              // answer, not the answer. `<details>` and nothing else — the
              // browser already owns open/closed state, keyboard access and
              // the disclosure affordance, and a reader who opens one while
              // the next delta lands must not have it snapped shut again.
              <details key={item.id} className={styles.thinking}>
                <summary className={styles.thinkingSummary}>
                  {item.id === openThinkingId ? 'Kenny is thinking…' : 'Kenny thought about this'}
                </summary>
                <div className={styles.thinkingBody}>{item.text}</div>
              </details>
            )

          case 'auto_run': {
            // Three states, and the third is not a failure: a call whose
            // result never arrived (the turn was stopped, the stream dropped)
            // is unknown, and saying so beats a red cross for something that
            // may well have run.
            const live = item.id === openToolItemId
            const unresolved = item.ok === undefined
            return (
              <div key={item.id}>
                <div className={styles.chip}>
                  {unresolved ? null : item.ok ? (
                    <Check width={11} height={11} strokeWidth={ICON_STROKE_WIDTH} className={styles.chipOk} aria-hidden="true" />
                  ) : (
                    <X width={11} height={11} strokeWidth={ICON_STROKE_WIDTH} className={styles.chipFail} aria-hidden="true" />
                  )}
                  {item.tool} ·{' '}
                  {live ? <Running /> : unresolved ? 'no result' : 'auto-run'}
                </div>
                {item.imageB64 && (
                  <img
                    className={styles.screenshot}
                    src={`data:image/${item.format ?? 'png'};base64,${item.imageB64}`}
                    alt={`${item.tool} screenshot`}
                  />
                )}
              </div>
            )
          }

          case 'gate': {
            // The undecided gate is on the card below, which is the only thing
            // that can answer it. Once decided this row comes back, as the
            // trace of what was decided and how it went.
            if (item.id === pendingGateItemId && item.resolution === 'pending') return null
            const cls =
              item.resolution === 'approved'
                ? `${styles.gateTrace} ${styles.gateTraceApproved}`
                : item.resolution === 'denied'
                  ? `${styles.gateTrace} ${styles.gateTraceDenied}`
                  : styles.gateTrace
            // 'running' is the answer to the operator's first question after
            // clicking CONFIRM & RUN — the decision took effect and the call is
            // under way. Without it the row still read "awaiting your decision"
            // for as long as the tool ran.
            const running = item.resolution === 'running'
            const label =
              item.resolution === 'pending'
                ? `${item.tool} · awaiting your decision`
                : item.resolution === 'approved'
                  ? `${item.tool} · confirmed & ${item.ok === false ? 'failed' : 'ran'}`
                  : running
                    ? `${item.tool} · confirmed · `
                    : `${item.tool} · denied`
            return (
              <div key={item.id} className={cls}>
                {item.resolution === 'approved' && item.ok !== false && (
                  <Check width={11} height={11} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                )}
                {(item.resolution === 'denied' || item.ok === false) && (
                  <X width={11} height={11} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                )}
                {label}
                {running && (item.id === openToolItemId ? <Running /> : 'no result')}
              </div>
            )
          }

          case 'draft':
            // Unlike a gate, this card belongs *in* the transcript: it is not
            // holding a turn open, and the conversation it came out of is the
            // context the operator is editing it against.
            return (
              <TicketDraftCard
                key={item.id}
                itemId={item.id}
                title={item.title}
                summary={item.summary}
                agentId={item.agentId}
                resolution={item.resolution}
                ticketId={item.ticketId}
                ticketNumber={item.ticketNumber}
                onCreate={onCreateDraft ?? (async () => undefined)}
                onDismiss={onDismissDraft ?? (() => undefined)}
              />
            )

          case 'denied':
            return (
              <div key={item.id} className={styles.chip}>
                <X width={11} height={11} strokeWidth={ICON_STROKE_WIDTH} className={styles.chipFail} aria-hidden="true" />
                denied {item.tool}
                {item.message ? ` · ${item.message}` : ''}
              </div>
            )

          case 'error':
            return (
              <div key={item.id} className={styles.errorRow}>
                {item.error}
              </div>
            )

          default:
            return null
        }
      })}
    </div>
  )
}

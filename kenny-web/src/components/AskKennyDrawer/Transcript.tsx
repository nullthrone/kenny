import { useEffect, useRef } from 'react'
import type { TranscriptItem } from '../../chat/types'
import { Check, X, ICON_STROKE_WIDTH } from '../icons'
import Markdown from '../Markdown/Markdown'
import styles from './Transcript.module.css'

export interface TranscriptProps {
  items: TranscriptItem[]
}

/**
 * Renders the folded event stream. Read-only tool calls are collapsed
 * one-line "auto-run" chips (never a decision UI — they already ran). A
 * `gate` row is a trace of what the confirm gate did, not the gate itself:
 * the actual CONFIRM & RUN / CANCEL decision only ever happens in
 * `PendingGateModal`, so this never renders a second set of action buttons.
 */
export default function Transcript({ items }: TranscriptProps) {
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

          case 'auto_run':
            return (
              <div key={item.id}>
                <div className={styles.chip}>
                  {item.ok ? (
                    <Check width={11} height={11} strokeWidth={ICON_STROKE_WIDTH} className={styles.chipOk} aria-hidden="true" />
                  ) : (
                    <X width={11} height={11} strokeWidth={ICON_STROKE_WIDTH} className={styles.chipFail} aria-hidden="true" />
                  )}
                  {item.tool} · auto-run
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

          case 'gate': {
            const cls =
              item.resolution === 'approved'
                ? `${styles.gateTrace} ${styles.gateTraceApproved}`
                : item.resolution === 'denied'
                  ? `${styles.gateTrace} ${styles.gateTraceDenied}`
                  : styles.gateTrace
            const label =
              item.resolution === 'pending'
                ? `${item.tool} · awaiting your decision`
                : item.resolution === 'approved'
                  ? `${item.tool} · confirmed & ${item.ok === false ? 'failed' : 'ran'}`
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
              </div>
            )
          }

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

import { Fragment, useEffect, useState } from 'react'
import { streamChatEvents } from '../../api/sse'
import type { RecommendationEvent } from './types'
import styles from './RecommendationBlock.module.css'

export interface RecommendationBlockProps {
  agentId: string
  sectionName: string
  /** True only when an Anthropic API key is configured (`AgentDetail.ai_enabled`) —
   * skips the fetch entirely rather than round-tripping to a route that will
   * answer 503. */
  aiEnabled: boolean
  onRemediate: (prompt: string) => void
}

/** The prose arrives as plain text (the model is instructed "no markdown"),
 * one labelled line per fact. Bolding the recognized `Diagnosis:`/`Action:`/
 * `Urgency:` lead only — never a markdown parser for server-authored prose. */
function renderProse(text: string) {
  return text.split('\n').map((line, i) => {
    const m = line.match(/^(Diagnosis|Action|Urgency):\s*(.*)$/i)
    return (
      <span key={i} className={styles.line}>
        {m ? (
          <Fragment>
            <strong>{m[1]}:</strong> {m[2]}
          </Fragment>
        ) : (
          line
        )}
      </span>
    )
  })
}

/**
 * `POST /api/recommendation/stream` — fills the section modal's
 * Recommendation block. Also the only place `remediation` (an extra event
 * type the frozen `ChatEvent` union doesn't carry — see `RecommendationEvent`'s
 * doc comment) matters: it's what "FIX VIA ASK KENNY" hands to the chat drawer.
 */
export default function RecommendationBlock({ agentId, sectionName, aiEnabled, onRemediate }: RecommendationBlockProps) {
  const [text, setText] = useState('')
  const [remediation, setRemediation] = useState<{ available: boolean; prompt: string } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)

  useEffect(() => {
    if (!aiEnabled) return
    const controller = new AbortController()
    setText('')
    setRemediation(null)
    setError(null)
    setDone(false)

    async function run() {
      try {
        for await (const raw of streamChatEvents(
          '/api/recommendation/stream',
          { agent_id: agentId, section: sectionName },
          { signal: controller.signal },
        )) {
          const ev = raw as unknown as RecommendationEvent
          if (ev.type === 'text_delta') {
            setText((t) => t + ev.text)
          } else if (ev.type === 'remediation') {
            setRemediation({ available: ev.available, prompt: ev.prompt })
          } else if (ev.type === 'done') {
            setDone(true)
          } else if (ev.type === 'error') {
            setError(ev.error)
          }
        }
      } catch (err) {
        if (controller.signal.aborted) return
        setError(err instanceof Error ? err.message : 'Could not load a recommendation.')
      }
    }

    run()
    return () => controller.abort()
  }, [agentId, sectionName, aiEnabled])

  if (!aiEnabled) {
    return (
      <div className={styles.box}>
        <div className={styles.eyebrow}>RECOMMENDATION</div>
        <div className={styles.prose}>AI recommendations are not configured on this server.</div>
      </div>
    )
  }

  return (
    <div className={styles.box}>
      <div className={styles.eyebrow}>RECOMMENDATION</div>
      <div className={styles.prose}>
        {error
          ? `Could not generate a recommendation: ${error}`
          : text
            ? renderProse(text)
            : done
              ? 'No recommendation was returned.'
              : 'Thinking…'}
      </div>
      {remediation?.available && (
        <button type="button" className={styles.fixButton} onClick={() => onRemediate(remediation.prompt)}>
          FIX VIA ASK KENNY
        </button>
      )}
    </div>
  )
}

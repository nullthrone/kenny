import { useEffect, useState } from 'react'
import { streamChatEvents } from '../../api/sse'
import styles from './ForecastPanel.module.css'

export interface ForecastPanelProps {
  agentId: string
}

/**
 * The inverse-ink Forecast panel — `POST /api/forecast/stream`, text-only
 * (the route only ever emits `text_delta` then `done`, or `error`;
 * `kenny_server/webui/__init__.py::api_forecast_stream`). Unlike the
 * recommendation stream this route always answers 200 (a deterministic
 * prose summary substitutes when no AI key is configured), so there's no
 * "not configured" branch to special-case here.
 */
export default function ForecastPanel({ agentId }: ForecastPanelProps) {
  const [text, setText] = useState('')
  const [generatedAt, setGeneratedAt] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    setText('')
    setGeneratedAt(null)
    setError(null)
    setDone(false)

    async function run() {
      try {
        for await (const ev of streamChatEvents('/api/forecast/stream', { agent_id: agentId }, { signal: controller.signal })) {
          if (ev.type === 'text_delta') {
            setText((t) => t + ev.text)
          } else if (ev.type === 'done') {
            setDone(true)
            setGeneratedAt(new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }))
          } else if (ev.type === 'error') {
            setError(ev.error)
          }
        }
      } catch (err) {
        if (controller.signal.aborted) return
        setError(err instanceof Error ? err.message : 'Could not load the forecast.')
      }
    }

    run()
    return () => controller.abort()
  }, [agentId])

  return (
    <div className={styles.panel}>
      <div className={styles.head}>
        <span className={styles.eyebrow}>FORECAST</span>
        {generatedAt && <span className={styles.meta}>generated {generatedAt}</span>}
      </div>
      <p className={styles.text}>
        {error
          ? `Could not generate a forecast: ${error}`
          : text || (done ? 'No forecast text was returned.' : 'Reading the last 30 days…')}
      </p>
    </div>
  )
}

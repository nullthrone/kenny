import { useState } from 'react'
import { useCaptureScreenshot } from './api'
import styles from './ScreenshotCard.module.css'

export interface ScreenshotCardProps {
  agentId: string
}

/**
 * `GET /api/agent/{id}/screenshot?t=` — cache-busted on every mount AND on
 * every recapture (notes/api-contract-actual.md §6: re-rendering for any
 * reason re-fetches the image in the old dashboard; kept here since nothing
 * signals "this is definitely still current" between pushes).
 * `onError` collapses to the "none yet" placeholder rather than a broken
 * image icon — the agent may never have captured one.
 */
export default function ScreenshotCard({ agentId }: ScreenshotCardProps) {
  const [bust, setBust] = useState(() => Date.now())
  const [broken, setBroken] = useState(false)
  const capture = useCaptureScreenshot(agentId)

  function recapture() {
    capture.mutate(undefined, {
      onSuccess: () => {
        setBroken(false)
        setBust(Date.now())
      },
    })
  }

  return (
    <div>
      <div className={styles.head}>
        <span className={styles.eyebrow}>LAST SCREENSHOT</span>
        <button type="button" className={styles.recapture} onClick={recapture} disabled={capture.isPending}>
          {capture.isPending ? 'CAPTURING…' : 'RECAPTURE'}
        </button>
      </div>
      <div className={styles.frame}>
        {broken ? (
          <span>none yet</span>
        ) : (
          <img
            key={bust}
            src={`/api/agent/${encodeURIComponent(agentId)}/screenshot?t=${bust}`}
            alt=""
            className={styles.image}
            onError={() => setBroken(true)}
          />
        )}
      </div>
      {capture.isError && (
        <p style={{ color: 'var(--danger)', fontSize: 'var(--text-xs)', marginTop: 6 }}>
          Could not capture a screenshot: {capture.error instanceof Error ? capture.error.message : 'Unknown error.'}
        </p>
      )}
    </div>
  )
}

import { useState } from 'react'
import Modal from '../../components/Modal/Modal'
import { X, ICON_STROKE_WIDTH } from '../../components/icons'
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
 *
 * The thumbnail crops to 16:9 (`object-fit: cover`); clicking it opens the
 * uncropped image in a modal.
 */
export default function ScreenshotCard({ agentId }: ScreenshotCardProps) {
  const [bust, setBust] = useState(() => Date.now())
  const [broken, setBroken] = useState(false)
  const [enlarged, setEnlarged] = useState(false)
  const capture = useCaptureScreenshot(agentId)
  const src = `/api/agent/${encodeURIComponent(agentId)}/screenshot?t=${bust}`

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
          <button type="button" className={styles.zoom} onClick={() => setEnlarged(true)} aria-label="Enlarge screenshot">
            <img key={bust} src={src} alt="" className={styles.image} onError={() => setBroken(true)} />
          </button>
        )}
      </div>
      {capture.isError && (
        <p style={{ color: 'var(--danger)', fontSize: 'var(--text-xs)', marginTop: 6 }}>
          Could not capture a screenshot: {capture.error instanceof Error ? capture.error.message : 'Unknown error.'}
        </p>
      )}
      <Modal open={enlarged && !broken} onClose={() => setEnlarged(false)} labelledBy="screenshot-modal-title" width={1280}>
        <div className={styles.modalHeader}>
          <span id="screenshot-modal-title" className={styles.modalTitle}>
            SCREENSHOT · {agentId.toUpperCase()}
          </span>
          <button type="button" className={styles.close} onClick={() => setEnlarged(false)} aria-label="Close">
            <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          </button>
        </div>
        <img src={src} alt={`Screenshot of ${agentId}`} className={styles.full} />
      </Modal>
    </div>
  )
}

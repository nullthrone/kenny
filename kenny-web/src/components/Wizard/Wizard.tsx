import { useState } from 'react'
import { api } from '../../api/client'
import type { ShareLinkResponse } from '../../api/types'
import Modal from '../Modal/Modal'
import { AppWindow, Download, Link, Server, X, ICON_STROKE_WIDTH } from '../icons'
import styles from './Wizard.module.css'

export interface WizardProps {
  open: boolean
  onClose: () => void
  /** Called once a new agent has actually been provisioned (a share link was minted,
   * or the operator downloaded the installer) so the caller can refresh the fleet list. */
  onProvisioned?: (name: string) => void
}

type Os = 'windows' | 'linux'
type Handover = 'download' | 'share'

const STEP_LABELS = ['NAME THE MACHINE', 'OPERATING SYSTEM', 'HAND IT OVER']
const NAME_PATTERN = /^[a-z0-9]+(-[a-z0-9]+)*$/

/**
 * `POST /api/agents/share-link` is body-based (view-endpoint-map's Fleet
 * section, "Changed"): unlike every other per-agent endpoint, there is no
 * `{id}` in the path, because at this point in the wizard the agent doesn't
 * exist in the fleet yet — naming it here is what creates it.
 */
interface ShareLinkRequest {
  name: string
  os: Os
}

/**
 * The 3-step Add-a-PC modal: name → operating system → hand-over. Exported
 * for the Fleet view to mount (`src/views/Fleet.tsx`, owned by another
 * agent) — it owns its own open/close trigger; this component only needs
 * `open`/`onClose`.
 *
 * Unlike the Ask Kenny confirm gate, this modal is an ordinary, fully
 * dismissible one (Escape, backdrop click, the ✕) — nothing here runs on a
 * real machine merely by being open.
 */
export default function Wizard({ open, onClose, onProvisioned }: WizardProps) {
  const [step, setStep] = useState(0)
  const [name, setName] = useState('')
  const [os, setOs] = useState<Os>('windows')
  const [handover, setHandover] = useState<Handover | null>(null)
  const [shareState, setShareState] = useState<
    { status: 'idle' } | { status: 'loading' } | { status: 'error'; message: string } | { status: 'done'; link: ShareLinkResponse }
  >({ status: 'idle' })
  const [copied, setCopied] = useState(false)

  const nameValid = NAME_PATTERN.test(name)

  function reset() {
    setStep(0)
    setName('')
    setOs('windows')
    setHandover(null)
    setShareState({ status: 'idle' })
    setCopied(false)
  }

  function handleClose() {
    onClose()
    // Deferred so the closing animation doesn't visibly reset mid-flight.
    setTimeout(reset, 200)
  }

  async function requestShareLink() {
    setShareState({ status: 'loading' })
    try {
      const body: ShareLinkRequest = { name, os }
      const link = await api.post<ShareLinkResponse>('/api/agents/share-link', body)
      setShareState({ status: 'done', link })
      onProvisioned?.(name)
    } catch (err) {
      setShareState({ status: 'error', message: err instanceof Error ? err.message : String(err) })
    }
  }

  function startDownload() {
    // Browser-handled navigation, not a fetch — same pattern as every other
    // installer download in the app (notes/api-contract-actual.md §6):
    // Content-Disposition and cookie auth need a real navigation.
    window.location.href = `/api/agents/${encodeURIComponent(name)}/installer?os=${encodeURIComponent(os)}`
    onProvisioned?.(name)
  }

  async function copyLink(url: string) {
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // Clipboard access can be denied by the browser — the URL is still
      // right there in the readonly field to select and copy by hand.
    }
  }

  const canAdvance = step === 0 ? nameValid : true
  const nextLabel = step === 2 ? 'DONE' : 'NEXT'

  function handleNext() {
    if (step < 2) {
      setStep((s) => s + 1)
    } else {
      handleClose()
    }
  }

  return (
    <Modal open={open} onClose={handleClose} width={520}>
      <div className={styles.header}>
        <span className={styles.title}>ADD A PC</span>
        <button type="button" className={styles.close} onClick={handleClose} aria-label="Close">
          <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
        </button>
      </div>

      <div className={styles.steps}>
        {STEP_LABELS.map((label, i) => (
          <div key={label} className={`${styles.step}${i === step ? ` ${styles.stepActive}` : i < step ? ` ${styles.stepDone}` : ''}`}>
            {label}
          </div>
        ))}
      </div>

      <div className={styles.body}>
        {step === 0 && (
          <>
            <label className={styles.label} htmlFor="wizard-name">
              Name the machine
            </label>
            <input
              id="wizard-name"
              className={styles.nameInput}
              placeholder="e.g. tante-laptop"
              value={name}
              onChange={(e) => setName(e.target.value.toLowerCase())}
              autoFocus
            />
            {name.length > 0 && !nameValid ? (
              <p className={styles.nameError}>Lowercase letters, numbers and hyphens only — no spaces.</p>
            ) : (
              <p className={styles.hint}>The agent id — lowercase, no spaces. It appears everywhere this PC is shown.</p>
            )}
          </>
        )}

        {step === 1 && (
          <>
            <label className={styles.label}>Operating system</label>
            <div className={styles.osGrid}>
              <button
                type="button"
                className={`${styles.osOption}${os === 'windows' ? ` ${styles.osOptionActive}` : ''}`}
                onClick={() => setOs('windows')}
                aria-pressed={os === 'windows'}
              >
                <AppWindow width={20} height={20} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                WINDOWS
              </button>
              <button
                type="button"
                className={`${styles.osOption}${os === 'linux' ? ` ${styles.osOptionActive}` : ''}`}
                onClick={() => setOs('linux')}
                aria-pressed={os === 'linux'}
              >
                <Server width={20} height={20} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                LINUX
              </button>
            </div>
          </>
        )}

        {step === 2 && (
          <>
            <label className={styles.label}>Hand it over</label>
            <div className={styles.handoverList}>
              <button
                type="button"
                className={`${styles.handoverOption}${handover === 'download' ? ` ${styles.handoverOptionActive}` : ''}`}
                onClick={() => {
                  setHandover('download')
                  startDownload()
                }}
              >
                <Download width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                <span>
                  <span className={styles.handoverTitle}>Download installer</span>
                  <span className={styles.handoverDetail}>ZIP with agent + setup.bat + a fresh token — you run it on the PC.</span>
                </span>
              </button>
              <button
                type="button"
                className={`${styles.handoverOption}${handover === 'share' ? ` ${styles.handoverOptionActive}` : ''}`}
                onClick={() => {
                  setHandover('share')
                  void requestShareLink()
                }}
                disabled={shareState.status === 'loading'}
              >
                <Link width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                <span>
                  <span className={styles.handoverTitle}>Share a one-time link</span>
                  <span className={styles.handoverDetail}>Expires after one use — the person at the PC installs it without your login.</span>
                </span>
              </button>
            </div>

            {handover === 'download' && (
              <p className={styles.handoverMeta}>Download started for {name} ({os}). If nothing happened, check your browser's download prompt.</p>
            )}

            {handover === 'share' && shareState.status === 'loading' && <p className={styles.handoverMeta}>Generating link…</p>}
            {handover === 'share' && shareState.status === 'error' && (
              <p className={styles.handoverError}>Could not create the link: {shareState.message}. Try again, or use the download instead.</p>
            )}
            {handover === 'share' && shareState.status === 'done' && (
              <div className={styles.handoverResult}>
                <div className={styles.linkRow}>
                  <input className={styles.linkInput} readOnly value={shareState.link.url} onFocus={(e) => e.target.select()} />
                  <button type="button" className={styles.copyButton} onClick={() => void copyLink(shareState.link.url)}>
                    {copied ? 'COPIED' : 'COPY'}
                  </button>
                </div>
                <span className={styles.handoverMeta}>Expires {new Date(shareState.link.expires_at).toLocaleString()}</span>
              </div>
            )}
          </>
        )}

        <div className={styles.footer}>
          <button
            type="button"
            className={styles.back}
            onClick={() => setStep((s) => Math.max(0, s - 1))}
            style={{ visibility: step === 0 ? 'hidden' : 'visible' }}
          >
            ← BACK
          </button>
          <button type="button" className={styles.next} onClick={handleNext} disabled={!canAdvance}>
            {nextLabel}
          </button>
        </div>
      </div>
    </Modal>
  )
}

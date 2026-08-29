import { useMemo, useState } from 'react'
import { useAgentBinary } from '../../api/agentBinary'
import { api } from '../../api/client'
import type { AgentBinaryTarget, ShareLinkResponse } from '../../api/types'
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
 *
 * `arch` is optional and pins the CPU architecture (ADR-0036). Sent when the
 * operator picked one, omitted when there is only one published target for the
 * chosen OS — in which case the Linux install script keeps its `uname -m`
 * auto-detection rather than being pinned to a guess.
 */
interface ShareLinkRequest {
  name: string
  os: Os
  arch?: string
}

/** The targets the server publishes for one OS, in the order it lists them (`agent_release.SUPPORTED_TARGETS`). */
function targetsForOs(targets: AgentBinaryTarget[] | undefined, os: Os): AgentBinaryTarget[] {
  return (targets ?? []).filter((t) => t.os === os)
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
  const [arch, setArch] = useState<string | null>(null)
  const [handover, setHandover] = useState<Handover | null>(null)
  const [shareState, setShareState] = useState<
    { status: 'idle' } | { status: 'loading' } | { status: 'error'; message: string } | { status: 'done'; link: ShareLinkResponse }
  >({ status: 'idle' })
  const [copied, setCopied] = useState<'url' | 'oneliner' | null>(null)

  // Only read while the wizard is open: it is the one place the answer changes
  // what the operator is offered, and Fleet's banner already fetched it if the
  // fleet page is what they opened this from.
  const binary = useAgentBinary(open)

  const osTargets = useMemo(() => targetsForOs(binary.data?.targets, os), [binary.data, os])
  const availableTargets = useMemo(() => osTargets.filter((t) => t.available), [osTargets])
  /**
   * `targets` is the authority when the server sends it (ADR-0036). Without it —
   * an older server — fall back to `by_os`, then to the Windows-only `available`,
   * and finally to "assume yes": never block provisioning because the status read
   * failed, only because it came back and said no.
   */
  const osAvailable = binary.data
    ? binary.data.targets
      ? availableTargets.length > 0
      : (binary.data.by_os?.[os] ?? (os === 'windows' ? binary.data.available : true))
    : true
  // Offering a choice of one is noise; the server resolves the single target itself.
  const archChoices = availableTargets.length > 1 ? availableTargets : []
  const effectiveArch = arch && archChoices.some((t) => t.arch === arch) ? arch : null

  const nameValid = NAME_PATTERN.test(name)

  function reset() {
    setStep(0)
    setName('')
    setOs('windows')
    setArch(null)
    setHandover(null)
    setShareState({ status: 'idle' })
    setCopied(null)
  }

  function handleClose() {
    onClose()
    // Deferred so the closing animation doesn't visibly reset mid-flight.
    setTimeout(reset, 200)
  }

  async function requestShareLink() {
    setShareState({ status: 'loading' })
    try {
      const body: ShareLinkRequest = { name, os, ...(effectiveArch ? { arch: effectiveArch } : {}) }
      const link = await api.post<ShareLinkResponse>('/api/agents/share-link', body)
      setShareState({ status: 'done', link })
      onProvisioned?.(name)
    } catch (err) {
      setShareState({ status: 'error', message: err instanceof Error ? err.message : String(err) })
    }
  }

  function startDownload() {
    // Browser-handled navigation, not a fetch — same pattern as every other
    // installer download in the app (`ActionRow`'s reinstall): Content-Disposition
    // and cookie auth need a real navigation. Which is also why the availability
    // check above the button matters: a 503 from this route does not surface as a
    // caught error, it replaces the page with a JSON body.
    const params = new URLSearchParams({ os })
    if (effectiveArch) params.set('arch', effectiveArch)
    window.location.href = `/api/agents/${encodeURIComponent(name)}/installer?${params.toString()}`
    onProvisioned?.(name)
  }

  async function copyValue(field: 'url' | 'oneliner', value: string) {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(field)
      setTimeout(() => setCopied(null), 2000)
    } catch {
      // Clipboard access can be denied by the browser — the value is still
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
                onClick={() => {
                  setOs('windows')
                  setArch(null)
                }}
                aria-pressed={os === 'windows'}
              >
                <AppWindow width={20} height={20} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                WINDOWS
              </button>
              <button
                type="button"
                className={`${styles.osOption}${os === 'linux' ? ` ${styles.osOptionActive}` : ''}`}
                onClick={() => {
                  setOs('linux')
                  setArch(null)
                }}
                aria-pressed={os === 'linux'}
              >
                <Server width={20} height={20} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                LINUX
              </button>
            </div>

            {/* Only when the server has published more than one target for this OS
                (ADR-0036). Leaving it unset keeps the Linux script's `uname -m`
                auto-detection, which is the right answer when nobody knows better —
                so this is an override, not a required step. */}
            {archChoices.length > 0 && (
              <>
                <label className={styles.label} htmlFor="wizard-arch" style={{ marginTop: 18 }}>
                  Processor architecture
                </label>
                <select
                  id="wizard-arch"
                  className={styles.archSelect}
                  value={arch ?? ''}
                  onChange={(e) => setArch(e.target.value || null)}
                >
                  <option value="">Detect on the machine</option>
                  {archChoices.map((t) => (
                    <option key={t.arch} value={t.arch}>
                      {t.arch}
                    </option>
                  ))}
                </select>
                <p className={styles.hint}>Only architectures kenny has a binary staged for are listed.</p>
              </>
            )}

            {!osAvailable && (
              <p className={styles.unavailable}>
                No {os === 'windows' ? 'Windows' : 'Linux'} agent binary is staged on the server, so there is nothing
                to hand this machine yet. Fleet explains why, and offers a retry when kenny can fetch one itself.
              </p>
            )}
          </>
        )}

        {step === 2 && (
          <>
            <label className={styles.label}>Hand it over</label>
            <div className={styles.handoverList}>
              {/* Disabled rather than hidden, with the reason: the operator needs to
                  know this path exists and why it is unavailable right now. The
                  download is a page navigation, so letting it through would replace
                  the wizard with the route's raw 503 body. */}
              <button
                type="button"
                className={`${styles.handoverOption}${handover === 'download' ? ` ${styles.handoverOptionActive}` : ''}`}
                disabled={!osAvailable}
                onClick={() => {
                  setHandover('download')
                  startDownload()
                }}
              >
                <Download width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                <span>
                  <span className={styles.handoverTitle}>Download installer</span>
                  <span className={styles.handoverDetail}>
                    {osAvailable
                      ? os === 'linux'
                        ? 'A pre-filled install script carrying a fresh token — you run it as root on the machine.'
                        : 'ZIP with agent + setup.bat + a fresh token — you run it on the PC.'
                      : 'Unavailable — no agent binary is staged for this operating system.'}
                  </span>
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
                  <input
                    className={styles.linkInput}
                    readOnly
                    value={shareState.link.url}
                    onFocus={(e) => e.target.select()}
                    aria-label="Share link"
                  />
                  <button type="button" className={styles.copyButton} onClick={() => void copyValue('url', shareState.link.url)}>
                    {copied === 'url' ? 'COPIED' : 'COPY'}
                  </button>
                </div>
                {/* Linux only. The URL on its own is not usable — the person at the
                    machine has to know it must be piped to a root shell — so the
                    command is what actually gets handed over. */}
                {shareState.link.oneliner && (
                  <div className={styles.linkRow}>
                    <input
                      className={styles.linkInput}
                      readOnly
                      value={shareState.link.oneliner}
                      onFocus={(e) => e.target.select()}
                      aria-label="Install one-liner"
                    />
                    <button
                      type="button"
                      className={styles.copyButton}
                      onClick={() => void copyValue('oneliner', shareState.link.oneliner as string)}
                    >
                      {copied === 'oneliner' ? 'COPIED' : 'COPY'}
                    </button>
                  </div>
                )}
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

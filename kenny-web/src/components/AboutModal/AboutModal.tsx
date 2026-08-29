import { useState } from 'react'
import Modal from '../Modal/Modal'
import KeyValueRow from '../KeyValueRow/KeyValueRow'
import Markdown from '../Markdown/Markdown'
import EmptyState from '../EmptyState/EmptyState'
import { Info, Link as LinkIcon, X, ICON_STROKE_WIDTH } from '../icons'
import { useAgentBinary } from '../../api/agentBinary'
import { useAbout, useChangelog } from './api'
import styles from './AboutModal.module.css'

/**
 * About kenny — the server's identity box, opened from the sidebar's version
 * line (Shell). Restores the dialog the legacy dashboard hung off a header user
 * menu that the current shell does not have; the four rows and the filterable
 * changelog are the same ones it showed.
 *
 * Only `/api/about` is load-bearing. The staged agent version and the changelog
 * are best-effort reads that degrade to a rendered dialog, never a broken one —
 * see `./api.ts`.
 */
export interface AboutModalProps {
  open: boolean
  onClose: () => void
}

/** Matches the server's own fallback (`agent_release.DEFAULT_REPO`). */
const DEFAULT_REPO = 't11z/kenny'

function formatPublished(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString()
}

export default function AboutModal({ open, onClose }: AboutModalProps) {
  const about = useAbout()
  const binary = useAgentBinary(open)
  const changelog = useChangelog(open)

  /**
   * `null` means "the operator has not chosen", so the derived default applies
   * — and applies the moment the releases land, with no effect and no flash.
   * `''` is a real choice ("all versions") and must stay chosen. A single
   * `useState('')` cannot tell those apart, and would snap the selection back
   * to the running version on the next render.
   */
  const [filter, setFilter] = useState<string | null>(null)

  const releases = changelog.data?.releases ?? []
  const running = about.data?.server_version ?? ''
  const repo = about.data?.repo || DEFAULT_REPO
  const repoUrl = `https://github.com/${repo}`

  // The legacy default: preselect the running version only if a release matches
  // it exactly, otherwise show every version.
  const defaultVersion = running && releases.some((r) => r.version === running) ? running : ''
  const selected = filter ?? defaultVersion
  const shown = selected ? releases.filter((r) => r.version === selected) : releases

  function handleClose() {
    // Reopening returns to the running-version default rather than whatever was
    // last filtered to.
    setFilter(null)
    onClose()
  }

  return (
    <Modal open={open} onClose={handleClose} labelledBy="about-modal-title" width={560}>
      <div className={styles.header}>
        <Info width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
        <span id="about-modal-title" className={styles.title}>
          ABOUT KENNY
        </span>
        <button type="button" className={styles.close} onClick={handleClose} aria-label="Close">
          <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
        </button>
      </div>

      <div className={styles.body}>
        {about.isError && (
          <div className={styles.errorBox}>Could not load server identity. Reload to try again.</div>
        )}

        <KeyValueRow label="server version" value={about.data?.server_version ?? 'unknown'} />
        <KeyValueRow label="protocol version" value={about.data?.protocol_version ?? 'unknown'} />
        <KeyValueRow
          label="staged agent version"
          value={binary.data?.version ?? 'unknown'}
          help={binary.isError ? 'binary status unavailable' : undefined}
        />
        <KeyValueRow
          label="repository"
          value={
            <a className={styles.link} href={repoUrl} target="_blank" rel="noopener noreferrer">
              <LinkIcon width={13} height={13} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
              {repo}
            </a>
          }
        />

        <div className={styles.changelogBar}>
          <div className={styles.groupLabel}>CHANGELOG</div>
          {releases.length > 0 && (
            <select
              className={styles.select}
              value={selected}
              onChange={(e) => setFilter(e.target.value)}
              aria-label="Filter release notes by version"
            >
              <option value="">all versions</option>
              {releases.map((r) => (
                <option key={r.tag} value={r.version}>
                  {r.version}
                  {r.version === running ? ' (running)' : ''}
                </option>
              ))}
            </select>
          )}
        </div>

        <div className={styles.changelog}>
          {changelog.isPending && <p className={styles.fallback}>Loading release notes…</p>}
          {changelog.isError && (
            <p className={styles.fallback}>Could not reach GitHub for release notes.</p>
          )}
          {changelog.isSuccess && shown.length === 0 && (
            <EmptyState title="No release notes" message={`No releases published on GitHub for ${repo} yet.`} />
          )}
          {shown.map((r) => (
            <div key={r.tag} className={styles.entry}>
              <div className={styles.entryHead}>
                <span className={styles.entryVersion}>{r.name || r.version}</span>
                <span className={styles.entryDate}>{formatPublished(r.published_at)}</span>
              </div>
              {r.body ? (
                <Markdown text={r.body} className={styles.entryBody} />
              ) : (
                <p className={styles.fallback}>(no release notes)</p>
              )}
            </div>
          ))}
        </div>
      </div>

      <div className={styles.footer}>
        <a className={styles.link} href={`${repoUrl}/releases`} target="_blank" rel="noopener noreferrer">
          <LinkIcon width={13} height={13} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          view full changelog on GitHub
        </a>
      </div>
    </Modal>
  )
}

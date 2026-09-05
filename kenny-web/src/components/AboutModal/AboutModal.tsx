import { useState } from 'react'
import Modal from '../Modal/Modal'
import KeyValueRow from '../KeyValueRow/KeyValueRow'
import Markdown from '../Markdown/Markdown'
import EmptyState from '../EmptyState/EmptyState'
import { Info, Link as LinkIcon, X, ICON_STROKE_WIDTH } from '../icons'
import { useAgentBinary, useRetryAgentBinaryFetch } from '../../api/agentBinary'
import { api } from '../../api/client'
import { useQuery } from '@tanstack/react-query'
import type { Me } from '../../api/types'
import { formatRelativeTime } from '../../views/host/format'
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
 *
 * Degrading has to stay distinguishable from succeeding, though. This dialog
 * once rendered a failed GitHub read as "no releases published for <repo> yet"
 * and a months-stale staged binary as a bare version number, so the one surface
 * an operator opens to check kenny's identity was also the one that hid a dead
 * token. Every best-effort read here now says which of the two it is.
 */
export interface AboutModalProps {
  open: boolean
  onClose: () => void
}

/**
 * Matches the server's own fallback (`agent_release.DEFAULT_REPO`).
 * `kenny-server/tests/test_ownership_defaults.py` fails if the two drift.
 */
const DEFAULT_REPO = 'nullthrone/kenny'

function formatPublished(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString()
}

/**
 * A clean `X.Y.Z`, i.e. a stable release build.
 *
 * The guard for the mismatch hint below: a dev-channel server (`0.0.0-dev`) or
 * a dev agent (`X.Y.Z-dev.N`) legitimately differs from its counterpart, so
 * nothing is claimed unless both sides are stable releases. Same discipline as
 * `server_release.is_newer`: on ambiguity, say nothing.
 */
function isReleaseVersion(v: string): boolean {
  return /^\d+\.\d+\.\d+$/.test(v)
}

export default function AboutModal({ open, onClose }: AboutModalProps) {
  const about = useAbout()
  const binary = useAgentBinary(open)
  const changelog = useChangelog(open)
  const retry = useRetryAgentBinaryFetch()
  const me = useQuery({ queryKey: ['me'], queryFn: () => api.get<Me>('/api/me'), enabled: open })

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

  // A server older than this bundle sends no `ok`; absent means it succeeded.
  const changelogOk = changelog.data?.ok !== false
  const changelogError = changelog.data?.error ?? null

  const staged = binary.data?.version ?? ''
  // The durable row first: `last_fetch` is per-process and reads as "never
  // attempted" after a restart, which is exactly when the operator is looking.
  const lastCheck = binary.data?.last_check ?? null
  const lastFetch = binary.data?.last_fetch ?? null
  const refreshFailed = lastCheck ? !lastCheck.ok : lastFetch ? !lastFetch.ok : false
  const refreshMessage = lastCheck?.message ?? lastFetch?.message ?? ''
  /**
   * CI stamps one git tag into both `KENNY_SERVER_VERSION` and
   * `KENNY_AGENT_VERSION` (`.github/workflows/_release-artifacts.yml`), so for
   * two stable builds a difference is not a preference — it means the staged
   * binary has not been refreshed since an older release.
   */
  const stagedIsBehind =
    isReleaseVersion(running) && isReleaseVersion(staged) && staged !== running

  function stagedHelp(): string | undefined {
    if (binary.isError) return 'binary status unavailable'
    if (refreshFailed) return `last refresh failed — ${refreshMessage}`
    if (stagedIsBehind) return `expected ${running} — this binary is from an older release`
    if (lastCheck?.ok) return `refreshed ${formatRelativeTime(lastCheck.checked_at)}`
    return undefined
  }

  // Retrying reaches out to GitHub and writes the cache, so it is operator+
  // server-side; a scoped `user` gets the explanation without a dead button.
  const canRetry = (me.data ? me.data.role !== 'user' : false) && !!binary.data

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
          help={stagedHelp()}
          action={
            canRetry
              ? {
                  label: retry.isPending ? 'FETCHING…' : 'FETCH NOW',
                  onClick: () => retry.mutate(),
                }
              : undefined
          }
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
          {/*
            Three states that used to collapse into one. The empty state is now
            reserved for the only case that actually justifies it: GitHub
            answered, and there was nothing to report.
          */}
          {changelog.isSuccess && !changelogOk && releases.length === 0 && (
            <EmptyState
              title="Release notes unavailable"
              message={changelogError ?? 'kenny could not read the releases from GitHub.'}
            />
          )}
          {changelog.isSuccess && !changelogOk && releases.length > 0 && (
            <p className={styles.fallback}>
              Showing cached notes
              {changelog.data?.fetched_at
                ? ` from ${formatRelativeTime(changelog.data.fetched_at)}`
                : ''}
              . {changelogError}
            </p>
          )}
          {changelog.isSuccess && changelogOk && shown.length === 0 && (
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

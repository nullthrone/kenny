import { useMemo, useState, type FormEvent } from 'react'
import type { WebfilterDomainAction, WebfilterOverview } from '../types'
import { describeActionError } from '../errors'
import { formatRelativeTime } from '../format'
import { useAddWebfilterDomain, useApplyWebfilter, useRemoveWebfilterDomain, useSetWebfilterConfig } from '../api'
import WebFilterStateBanner, { type OversizeCandidate } from './WebFilterStateBanner'
import WebFilterCategories from './WebFilterCategories'
import WebFilterSchedule from './WebFilterSchedule'
import WebFilterRequests from './WebFilterRequests'
import styles from './WebFilterBody.module.css'

export interface WebFilterBodyProps {
  agentId: string
  overview: WebfilterOverview
}

const ACTION_COLOR: Record<WebfilterDomainAction, string> = {
  block: 'var(--danger)',
  allow: 'var(--ok)',
  watch: 'var(--text-muted)',
}

/** The largest currently-enabled external category, by cached list size —
 * the concrete "turn this off" answer the over-cap banner and the category
 * list both point at. `null` when nothing is over the cap or nothing
 * external is enabled (an all-custom-entries list can still be oversize). */
function findOversizeCandidate(overview: WebfilterOverview): OversizeCandidate | null {
  if (!overview.oversize) return null
  const enabled = new Set(overview.config.categories)
  let best: OversizeCandidate | null = null
  for (const cat of overview.categories) {
    if (!cat.external || !enabled.has(cat.key)) continue
    const count = overview.external[cat.key]?.count ?? 0
    if (!best || count > best.count) best = { key: cat.key, label: cat.label, count }
  }
  return best
}

/** Full-edit web filter section modal body — the state banner, category
 * toggles, schedule, bypass requests, the custom domain list, and Apply.
 * `GET/PUT/POST/DELETE /api/agent/{id}/webfilter*` (ADR-0024, ADR-0055).
 * Every mutation invalidates and re-pulls this overview rather than
 * patching it optimistically. */
export default function WebFilterBody({ agentId, overview }: WebFilterBodyProps) {
  const [domain, setDomain] = useState('')
  const [action, setAction] = useState<WebfilterDomainAction>('block')
  const [category, setCategory] = useState('')
  const [applyResult, setApplyResult] = useState<{ ok: boolean; text: string } | null>(null)

  const setConfig = useSetWebfilterConfig(agentId)
  const addDomain = useAddWebfilterDomain(agentId)
  const removeDomain = useRemoveWebfilterDomain(agentId)
  const apply = useApplyWebfilter(agentId)

  const { config, custom, schedule, oversize, categories, external } = overview
  const oversizeCandidate = useMemo(() => findOversizeCandidate(overview), [overview])

  function onAddDomain(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const trimmed = domain.trim()
    if (!trimmed) return
    addDomain.mutate(
      { domain: trimmed, action, category: category || null },
      { onSuccess: () => setDomain('') },
    )
  }

  function onRequestDomain(requested: string) {
    setDomain(requested)
    setAction('allow')
  }

  function onApply() {
    setApplyResult(null)
    apply.mutate(undefined, {
      onSuccess: (r) => {
        if (r.ok) setApplyResult({ ok: true, text: 'Rules applied.' })
        else setApplyResult({ ok: false, text: describeActionError(String(r.error), r.message, r.count, r.cap) })
      },
      onError: (e) =>
        setApplyResult({ ok: false, text: e instanceof Error ? describeActionError(e.message) : 'Could not apply.' }),
    })
  }

  return (
    <div>
      <WebFilterStateBanner
        filteringEnabled={config.enabled}
        schedule={schedule}
        oversize={oversize}
        oversizeCandidate={oversizeCandidate}
      />

      <div className={styles.section}>
        <div className={styles.eyebrow}>CONFIGURATION</div>
        <label className={styles.toggleRow}>
          <input
            type="checkbox"
            checked={config.enabled}
            onChange={(e) => setConfig.mutate({ enabled: e.target.checked })}
            disabled={setConfig.isPending}
          />
          Filtering enabled
        </label>
        <label className={styles.toggleRow}>
          <input
            type="checkbox"
            checked={config.block_mode}
            onChange={(e) => setConfig.mutate({ block_mode: e.target.checked })}
            disabled={setConfig.isPending}
          />
          Block mode <span className={styles.help}>— off logs matches without blocking them</span>
        </label>
        <div className={styles.dohRow}>
          <span>DNS-over-HTTPS</span>
          <select
            className={styles.select}
            value={config.doh_policy}
            onChange={(e) => setConfig.mutate({ doh_policy: e.target.value as 'disable' | 'leave' })}
            disabled={setConfig.isPending}
          >
            <option value="disable">Disable (recommended — DoH bypasses filtering)</option>
            <option value="leave">Leave as-is</option>
          </select>
        </div>
        {setConfig.isError && (
          <p className={`${styles.status} ${styles.statusError}`}>
            {setConfig.error instanceof Error ? setConfig.error.message : 'Could not save that setting.'}
          </p>
        )}
      </div>

      <div className={styles.section}>
        <div className={styles.eyebrow}>CATEGORIES</div>
        <WebFilterCategories
          agentId={agentId}
          config={config}
          categories={categories}
          external={external}
          oversizeCandidate={oversizeCandidate}
        />
      </div>

      <div className={styles.section}>
        <div className={styles.eyebrow}>SCHEDULE</div>
        <WebFilterSchedule agentId={agentId} schedule={schedule} categories={categories} />
      </div>

      <div className={styles.section}>
        <div className={styles.eyebrow}>BYPASS REQUESTS</div>
        <WebFilterRequests agentId={agentId} onRequestDomain={onRequestDomain} />
      </div>

      <div className={styles.section}>
        <div className={styles.eyebrow}>CUSTOM DOMAINS · {custom.length}</div>
        <form className={styles.domainForm} onSubmit={onAddDomain}>
          <input
            className={styles.domainInput}
            placeholder="example.com"
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
          />
          <select
            className={styles.select}
            value={action}
            onChange={(e) => setAction(e.target.value as WebfilterDomainAction)}
          >
            <option value="block">block</option>
            <option value="allow">allow</option>
            <option value="watch">watch</option>
          </select>
          <select className={styles.select} value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">no category — always applies</option>
            {categories.map((c) => (
              <option key={c.key} value={c.key}>
                {c.label}
              </option>
            ))}
          </select>
          <button type="submit" className={styles.addButton} disabled={addDomain.isPending || !domain.trim()}>
            ADD DOMAIN
          </button>
        </form>
        {addDomain.isError && (
          <p className={`${styles.status} ${styles.statusError}`}>
            {addDomain.error instanceof Error ? addDomain.error.message : 'Could not add that domain.'}
          </p>
        )}

        {custom.length === 0 ? (
          <p className={styles.empty}>No custom domain rules yet — the shipped block list still applies.</p>
        ) : (
          <div className={styles.domainList}>
            {custom.map((d) => (
              <div key={d.domain} className={styles.domainRow}>
                <span className={styles.domainName}>{d.domain}</span>
                <span className={styles.actionChip} style={{ color: ACTION_COLOR[d.action] }}>
                  {d.action.toUpperCase()}
                </span>
                {d.category && <span className={styles.categoryChip}>{d.category}</span>}
                <span className={styles.added}>{formatRelativeTime(d.added_at)}</span>
                <button
                  type="button"
                  className={styles.removeButton}
                  onClick={() => removeDomain.mutate(d.domain)}
                  disabled={removeDomain.isPending}
                >
                  REMOVE
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className={styles.section}>
        <div className={styles.applyRow}>
          <button type="button" className={styles.applyButton} onClick={onApply} disabled={apply.isPending}>
            {apply.isPending ? 'APPLYING…' : 'APPLY RULES'}
          </button>
          <span className={`${styles.status}${applyResult && !applyResult.ok ? ` ${styles.statusError}` : ''}`}>
            {applyResult
              ? applyResult.text
              : overview.applied.at
                ? `Last applied ${formatRelativeTime(overview.applied.at)}${overview.applied.ok === false ? ' (failed)' : ''}`
                : 'Never applied.'}
          </span>
          {overview.drift && !applyResult && <span className={styles.status}>Rules changed since the last apply.</span>}
        </div>
      </div>
    </div>
  )
}

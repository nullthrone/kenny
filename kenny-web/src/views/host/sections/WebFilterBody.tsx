import { useState, type FormEvent } from 'react'
import type { WebfilterDomainAction, WebfilterOverview } from '../types'
import { describeActionError } from '../errors'
import { formatRelativeTime } from '../format'
import { useAddWebfilterDomain, useApplyWebfilter, useRemoveWebfilterDomain, useSetWebfilterConfig } from '../api'
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

/** Full-edit web filter section modal body — config toggles, the custom
 * domain list, and Apply. `GET/PUT/POST/DELETE /api/agent/{id}/webfilter*`
 * (notes/view-endpoint-map.md, Host). Every mutation invalidates and re-pulls
 * this overview rather than patching it optimistically. */
export default function WebFilterBody({ agentId, overview }: WebFilterBodyProps) {
  const [domain, setDomain] = useState('')
  const [action, setAction] = useState<WebfilterDomainAction>('block')
  const [applyResult, setApplyResult] = useState<{ ok: boolean; text: string } | null>(null)

  const setConfig = useSetWebfilterConfig(agentId)
  const addDomain = useAddWebfilterDomain(agentId)
  const removeDomain = useRemoveWebfilterDomain(agentId)
  const apply = useApplyWebfilter(agentId)

  const { config, custom } = overview

  function onAddDomain(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const trimmed = domain.trim()
    if (!trimmed) return
    addDomain.mutate({ domain: trimmed, action }, { onSuccess: () => setDomain('') })
  }

  function onApply() {
    setApplyResult(null)
    apply.mutate(undefined, {
      onSuccess: (r) => {
        if (r.ok) setApplyResult({ ok: true, text: 'Rules applied.' })
        else setApplyResult({ ok: false, text: describeActionError(String(r.error)) })
      },
      onError: (e) => setApplyResult({ ok: false, text: e instanceof Error ? e.message : 'Could not apply.' }),
    })
  }

  return (
    <div>
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
        <label className={styles.toggleRow}>
          <input
            type="checkbox"
            checked={config.use_external_adult}
            onChange={(e) => setConfig.mutate({ use_external_adult: e.target.checked })}
            disabled={setConfig.isPending}
          />
          Use external adult-content list{' '}
          <span className={styles.help}>({overview.external.adult.enabled ? 'active' : 'off'})</span>
        </label>
        <label className={styles.toggleRow}>
          <input
            type="checkbox"
            checked={config.use_bypass_protection}
            onChange={(e) => setConfig.mutate({ use_bypass_protection: e.target.checked })}
            disabled={setConfig.isPending}
          />
          Block VPN/proxy bypass domains
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

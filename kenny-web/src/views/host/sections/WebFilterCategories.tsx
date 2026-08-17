import type { WebfilterCategory, WebfilterConfig, WebfilterExternalStat } from '../types'
import { formatRelativeTime } from '../format'
import { useSetWebfilterConfig } from '../api'
import type { OversizeCandidate } from './WebFilterStateBanner'
import styles from './WebFilterCategories.module.css'

export interface WebFilterCategoriesProps {
  agentId: string
  config: WebfilterConfig
  categories: WebfilterCategory[]
  external: Record<string, WebfilterExternalStat>
  oversizeCandidate: OversizeCandidate | null
}

/**
 * The category catalog as per-host toggles. `adult` and `bypass` render
 * exactly like every other row — no parallel `use_external_adult` /
 * `use_bypass_protection` controls — because the server already merges both
 * into `config.categories` and treats them as ordinary catalog entries
 * (ADR-0055). Toggling here sends the whole set, not a delta.
 *
 * When the effective list is over the cap, rows are re-ordered largest
 * enabled external category first, so the one worth turning off is the one
 * on top rather than a fact the operator has to hunt for.
 */
export default function WebFilterCategories({ agentId, config, categories, external, oversizeCandidate }: WebFilterCategoriesProps) {
  const setConfig = useSetWebfilterConfig(agentId)
  const enabled = new Set(config.categories)

  const rows = oversizeCandidate
    ? [...categories].sort((a, b) => {
        if (a.external !== b.external) return a.external ? -1 : 1
        if (a.external && b.external) return (external[b.key]?.count ?? 0) - (external[a.key]?.count ?? 0)
        return 0
      })
    : categories

  function toggle(key: string, checked: boolean) {
    const next = new Set(config.categories)
    if (checked) next.add(key)
    else next.delete(key)
    setConfig.mutate({ categories: Array.from(next) })
  }

  return (
    <div className={styles.list} data-testid="webfilter-categories">
      {rows.map((cat) => {
        const stat = external[cat.key]
        const isOn = enabled.has(cat.key)
        const isCandidate = oversizeCandidate?.key === cat.key
        return (
          <label key={cat.key} className={styles.row}>
            <input
              type="checkbox"
              checked={isOn}
              onChange={(e) => toggle(cat.key, e.target.checked)}
              disabled={setConfig.isPending}
            />
            <span className={styles.label}>{cat.label}</span>
            <span className={styles.tag}>{cat.external ? 'EXTERNAL' : 'LOCAL'}</span>
            {isCandidate && <span className={`${styles.tag} ${styles.candidateTag}`}>LARGEST ENABLED</span>}
            <span className={styles.meta}>
              {cat.external
                ? `${(stat?.count ?? 0).toLocaleString()} domains · ${
                    stat?.last_fetch ? `fetched ${formatRelativeTime(stat.last_fetch)}` : 'not fetched yet'
                  }`
                : 'from tagged custom entries'}
            </span>
          </label>
        )
      })}
      {setConfig.isError && (
        <p className={styles.error}>{setConfig.error instanceof Error ? setConfig.error.message : 'Could not save that category.'}</p>
      )}
    </div>
  )
}

import { Link, useParams } from 'react-router'
import { ApiError } from '../../../api/client'
import EmptyState from '../../../components/EmptyState/EmptyState'
import { formatRelativeTime } from '../../host/format'
import type { InvalidSteward, Steward } from '../types'
import { NewStewardPage } from './stewards/BuilderEntry'
import InvalidStewardDetail from './stewards/InvalidStewardDetail'
import StewardDetail from './stewards/StewardDetail'
import { ModeBadge, RunStatusTag, shortHash, triggerLabel } from './stewards/badges'
import { useStewards } from './stewards/queries'
import shared from '../shared.module.css'
import styles from './stewards/stewards.module.css'

/** `#/admin/stewards/new`: the builder form. No built steward can be called this: ids start with `custom_`. */
const NEW_ID = 'new'

export interface StewardsSectionProps {
  /**
   * Whether this account may change a steward: choose its mode, edit its parameters,
   * grant or revoke an authorization. Superuser only (ADR-0072 rule 2). The server
   * enforces it again, and additionally refuses anything but a person signed in at
   * a browser — this only decides which controls are drawn.
   */
  canManage: boolean
}

function InvalidRow({ steward }: { steward: InvalidSteward }) {
  return (
    <li className={styles.stewardRow}>
      <div>
        <Link to={`/admin/stewards/${encodeURIComponent(steward.id)}`} className={styles.stewardTitle}>
          {steward.title}
        </Link>
        <p className={styles.desc}>{steward.description}</p>
        <p className={styles.desc} style={{ color: 'var(--danger)' }}>
          It no longer validates and does not run: {steward.errors.length} problem
          {steward.errors.length === 1 ? '' : 's'}. {steward.errors[0] ? `${steward.errors[0].field}: ${steward.errors[0].error}` : ''}
        </p>
      </div>
      <div className={styles.rowSide}>
        <span className={styles.badgeRow}>
          <span className={`${styles.tag} ${styles.toneDanger}`}>INVALID</span>
          <span className={`${styles.tag} ${styles.toneMuted}`}>BUILT</span>
        </span>
      </div>
    </li>
  )
}

function StewardRow({ steward }: { steward: Steward }) {
  const latest = steward.latest_run
  return (
    <li className={styles.stewardRow}>
      <div>
        <Link to={`/admin/stewards/${encodeURIComponent(steward.id)}`} className={styles.stewardTitle}>
          {steward.title}
        </Link>
        <p className={styles.desc}>{steward.description}</p>
        <div className={styles.facts}>
          <span>trigger: {triggerLabel(steward)}</span>
          <span>
            hash <span className={styles.hash}>{shortHash(steward.effective_hash)}</span>
          </span>
          <span>
            {latest ? (
              <>
                last run {formatRelativeTime(latest.started_at)}
                {latest.verdict ? ` · ${latest.verdict}` : ''}
              </>
            ) : (
              'no runs yet'
            )}
          </span>
        </div>
      </div>
      <div className={styles.rowSide}>
        <span className={styles.badgeRow}>
          {steward.builtin && <span className={`${styles.tag} ${styles.toneMuted}`}>BUILT</span>}
          <ModeBadge steward={steward} />
        </span>
        {latest && <RunStatusTag status={latest.status} />}
      </div>
    </li>
  )
}

/**
 * Admin → Stewards (ADR-0071, ADR-0072). Operators and up read the catalog,
 * every steward's mode, parameters, authorizations and runs, and may start a preview;
 * a superuser also changes them. `#/admin/stewards` is the list, `#/admin/stewards/{id}`
 * one steward.
 */
export default function StewardsSection({ canManage }: StewardsSectionProps) {
  const { detail } = useParams<{ detail?: string }>()
  const query = useStewards()

  if (query.isLoading) return <div className={shared.loading}>Loading…</div>
  if (query.isError || !query.data) {
    const unavailable = query.error instanceof ApiError && query.error.status === 503
    return (
      <EmptyState
        title={unavailable ? 'Stewards are not configured' : 'Could not load the stewards'}
        message={unavailable ? 'This server was started without them.' : 'Something went wrong. Reload to try again.'}
      />
    )
  }

  const { enabled, stewards } = query.data

  const banner = !enabled && (
    <div className={`${shared.warnBox} ${styles.banner}`} role="status">
      Stewards are switched off for the whole install, so none of them starts, whatever its mode says.
      A superuser turns them back on under Admin → AI.
    </div>
  )

  if (detail === NEW_ID) {
    return (
      <div>
        {banner}
        <NewStewardPage canManage={canManage} />
      </div>
    )
  }

  if (detail) {
    const steward = stewards.find((a) => a.id === detail)
    return (
      <div>
        {banner}
        {steward && steward.invalid === true ? (
          <InvalidStewardDetail steward={steward} canManage={canManage} />
        ) : steward ? (
          <StewardDetail steward={steward} canManage={canManage} />
        ) : (
          <>
            <Link to="/admin/stewards" className={styles.back}>
              ← ALL STEWARDS
            </Link>
            <EmptyState title="Unknown steward" message={`There is no steward called ${detail}.`} />
          </>
        )}
      </div>
    )
  }

  return (
    <div>
      {banner}
      <p className={shared.help} style={{ marginBottom: 12 }}>
        Stewards are tasks kenny runs on its own, each with a closed set of tools. In <strong>shadow</strong> an
        steward does the whole job but every change is only recorded as a recommendation; in <strong>act</strong>{' '}
        it makes the changes its limits allow.
      </p>
      {canManage && (
        <div className={shared.actions} style={{ marginTop: 0, marginBottom: 16 }}>
          <Link to={`/admin/stewards/${NEW_ID}`} className={shared.btnPrimary} style={{ textDecoration: 'none' }}>
            NEW STEWARD
          </Link>
        </div>
      )}
      {stewards.length === 0 ? (
        <EmptyState title="No stewards" message="This server ships none." />
      ) : (
        <ul className={shared.table} style={{ listStyle: 'none', margin: 0, padding: 0 }} aria-label="Stewards">
          {stewards.map((a) =>
            a.invalid === true ? <InvalidRow key={a.id} steward={a} /> : <StewardRow key={a.id} steward={a} />,
          )}
        </ul>
      )}
    </div>
  )
}

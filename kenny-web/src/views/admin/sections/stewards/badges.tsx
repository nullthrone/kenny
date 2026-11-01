import type { StewardMode, StewardRunStatus, AuthorizationStatus, Steward, ToolTier } from '../../types'
import styles from './stewards.module.css'

/** The first characters of a hash: enough to compare two by eye, never the whole fingerprint. */
export function shortHash(hash: string | null | undefined): string {
  return hash ? hash.slice(0, 8) : '—'
}

/**
 * Stand-ins for the server's `NEVER_AUTHORIZED` and `MAX_EXPIRY_DAYS`, used only when
 * `GET /api/stewards/builder/catalog` cannot be read (`useAuthorizationLimits`). The server
 * enforces its own values at grant and at match; the view never prefers these to its answer.
 */
export const FALLBACK_NEVER_AUTHORIZED: readonly string[] = ['shell_exec', 'powershell_exec', 'agent_update']
export const FALLBACK_MAX_EXPIRY_DAYS = 180

const MODE_CLASS: Record<StewardMode, string> = {
  off: styles.toneMuted,
  shadow: styles.toneBrass,
  act: styles.toneOk,
}

const MODE_HELP: Record<StewardMode, string> = {
  off: 'Never started.',
  shadow: 'Runs fully; every change is refused and kept as a recommendation.',
  act: 'Makes the changes its constraints and authorizations allow.',
}

/**
 * The mode as text first, colour second ("status is never colour-only"). `act` whose
 * binding no longer matches the effective hash reads `ACT UNBOUND`: the choice was
 * made for something the steward no longer is, so it is not acting.
 */
export function ModeBadge({ steward }: { steward: Pick<Steward, 'mode' | 'act_bound'> }) {
  const unbound = steward.mode === 'act' && steward.act_bound === false
  return (
    <span className={styles.badgeRow}>
      <span className={`${styles.tag} ${MODE_CLASS[steward.mode]}`} title={MODE_HELP[steward.mode]}>
        {steward.mode.toUpperCase()}
      </span>
      {unbound && (
        <span
          className={`${styles.tag} ${styles.toneWarn}`}
          title="act was chosen for a version of this steward that no longer exists. Choose act again to bind it to what you see now."
        >
          ACT UNBOUND
        </span>
      )}
    </span>
  )
}

const TIER_LABEL: Record<ToolTier, string> = {
  read_only: 'READ-ONLY',
  standard_change: 'STANDARD CHANGE',
  normal_change: 'NORMAL CHANGE',
}

const TIER_CLASS: Record<ToolTier, string> = {
  read_only: styles.toneMuted,
  standard_change: styles.toneBrass,
  normal_change: styles.toneWarn,
}

export function TierBadge({ tier }: { tier: ToolTier }) {
  return <span className={`${styles.tag} ${TIER_CLASS[tier] ?? styles.toneMuted}`}>{TIER_LABEL[tier] ?? String(tier).toUpperCase()}</span>
}

const RUN_CLASS: Record<StewardRunStatus, string> = {
  running: styles.toneBrass,
  completed: styles.toneOk,
  failed: styles.toneDanger,
  skipped: styles.toneMuted,
}

export function RunStatusTag({ status }: { status: StewardRunStatus }) {
  return <span className={`${styles.tag} ${RUN_CLASS[status] ?? styles.toneMuted}`}>{status.toUpperCase()}</span>
}

const AUTH_CLASS: Record<AuthorizationStatus, string> = {
  live: styles.toneOk,
  revoked: styles.toneDanger,
  voided: styles.toneWarn,
  expired: styles.toneMuted,
}

export function AuthorizationStateTag({ status }: { status: AuthorizationStatus }) {
  return <span className={`${styles.tag} ${AUTH_CLASS[status] ?? styles.toneMuted}`}>{status.toUpperCase()}</span>
}

export function triggerLabel(steward: Pick<Steward, 'trigger'>): string {
  const { kind, event } = steward.trigger
  if (kind === 'event') return event === 'ticket_created' ? 'a new ticket' : `event ${event ?? ''}`.trim()
  return kind === 'schedule' ? 'on a schedule' : kind
}

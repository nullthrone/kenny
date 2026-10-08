import type { AgentMode, AgentRunStatus, AuthorizationStatus, SpecializedAgent, ToolTier } from '../../types'
import styles from './agents.module.css'

/** The first characters of a hash: enough to compare two by eye, never the whole fingerprint. */
export function shortHash(hash: string | null | undefined): string {
  return hash ? hash.slice(0, 8) : '—'
}

/** `kenny_server/agents/authorizations.py::NEVER_AUTHORIZED` — refused at grant and at match. */
export const NEVER_AUTHORIZED: readonly string[] = ['shell_exec', 'powershell_exec', 'agent_update']

/** A hard ceiling the server enforces too (`MAX_EXPIRY_DAYS`). */
export const MAX_EXPIRY_DAYS = 180

const MODE_CLASS: Record<AgentMode, string> = {
  off: styles.toneMuted,
  shadow: styles.toneBrass,
  act: styles.toneOk,
}

const MODE_HELP: Record<AgentMode, string> = {
  off: 'Never started.',
  shadow: 'Runs fully; every change is refused and kept as a recommendation.',
  act: 'Makes the changes its constraints and authorizations allow.',
}

/**
 * The mode as text first, colour second ("status is never colour-only"). `act` whose
 * binding no longer matches the effective hash reads `ACT UNBOUND`: the choice was
 * made for something the agent no longer is, so it is not acting.
 */
export function ModeBadge({ agent }: { agent: Pick<SpecializedAgent, 'mode' | 'act_bound'> }) {
  const unbound = agent.mode === 'act' && agent.act_bound === false
  return (
    <span className={styles.badgeRow}>
      <span className={`${styles.tag} ${MODE_CLASS[agent.mode]}`} title={MODE_HELP[agent.mode]}>
        {agent.mode.toUpperCase()}
      </span>
      {unbound && (
        <span
          className={`${styles.tag} ${styles.toneWarn}`}
          title="act was chosen for a version of this agent that no longer exists. Choose act again to bind it to what you see now."
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

const RUN_CLASS: Record<AgentRunStatus, string> = {
  running: styles.toneBrass,
  completed: styles.toneOk,
  failed: styles.toneDanger,
  skipped: styles.toneMuted,
}

export function RunStatusTag({ status }: { status: AgentRunStatus }) {
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

export function triggerLabel(agent: Pick<SpecializedAgent, 'trigger'>): string {
  const { kind, event } = agent.trigger
  if (kind === 'event') return event === 'ticket_created' ? 'a new ticket' : `event ${event ?? ''}`.trim()
  if (kind === 'schedule') return 'on a schedule'
  return 'on demand'
}

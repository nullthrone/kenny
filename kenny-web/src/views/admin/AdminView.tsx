import { useMemo } from 'react'
import { Navigate, useParams } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import EmptyState from '../../components/EmptyState/EmptyState'
import type { ProfileMe } from '../profile/types'
import type { RawSettingsResponse } from './types'
import { buildEnvironmentSection, mapSettingsGroups } from './settingsMap'
import AdminNav, { type AdminNavItem } from './AdminNav'
import GenericSettingsSection from './sections/GenericSettingsSection'
import EnvironmentSection from './sections/EnvironmentSection'
import WebFilterSection from './sections/WebFilterSection'
import BackupSection from './sections/BackupSection'
import UpdatesSection from './sections/UpdatesSection'
import DiscordSection from './sections/DiscordSection'
import TicketRulesSection from './sections/TicketRulesSection'
import UsersSection from './sections/UsersSection'
import styles from './AdminView.module.css'

const SYNTHETIC_LABELS: Record<string, string> = {
  updates: 'Updates',
  'auto-ticket-rules': 'Auto-ticket rules',
  users: 'Users',
  environment: 'Environment',
}

/**
 * Section slugs that moved, mapped to where they live now. `#/settings/:section`
 * redirects into `#/admin/:section` keeping the slug (`router/routes.tsx`), so a
 * bookmark minted before the redesign arrives here verbatim — and the Auto-ticket
 * section is the one whose slug did change (`ticket-rules` in the legacy
 * dashboard). Resolving the alias here covers the redirect and a hand-typed URL
 * in one place.
 */
const SLUG_ALIASES: Record<string, string> = {
  'ticket-rules': 'auto-ticket-rules',
}

/**
 * `#/admin/:section` — 220px section nav + a row list.
 *
 * What the nav holds depends on the role, because `GET /api/settings` is
 * superuser-only (`webui/__init__.py`). A superuser gets every catalog group
 * (`groups[].slug`, eleven today) plus three synthetic sections with no server
 * group of their own: `auto-ticket-rules`, `users`, `environment` (composed
 * client-side from the env-sourced rows, see `settingsMap.ts`).
 *
 * An operator gets **Updates** and **Auto-ticket rules** — the two sections whose
 * own routes floor at `operator` (`/api/updates`, `/api/ticket-rules`). Neither
 * has a settings-catalog group behind it, so both render without `/api/settings`
 * ever being requested: asking for it would 403 and take the whole page down with
 * it, which is exactly what used to happen. `environment` is deliberately not in
 * that list — it is derived from the catalog an operator cannot read.
 *
 * The design's prototype only drew nine sections; the five it omits (Logging,
 * Network & Process, Operator & Agent Auth, Telemetry limits, Agent distribution)
 * are real configuration and render generically here — dropping them would be a
 * silent capability loss the brief explicitly rules out.
 */
export default function AdminView() {
  const { section: rawSection } = useParams<{ section?: string }>()
  const section = rawSection ? (SLUG_ALIASES[rawSection] ?? rawSection) : rawSection

  const me = useQuery({ queryKey: ['me'], queryFn: () => api.get<ProfileMe>('/api/me') })
  const isSuperuser = me.data?.role === 'superuser'
  const isOperator = me.data ? me.data.role !== 'user' : false

  const settings = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.get<RawSettingsResponse>('/api/settings'),
    enabled: isSuperuser,
  })

  const groups = useMemo(() => (settings.data ? mapSettingsGroups(settings.data) : []), [settings.data])
  const environmentSection = useMemo(() => buildEnvironmentSection(groups), [groups])

  const navItems: AdminNavItem[] = useMemo(() => {
    if (!isOperator) return []
    if (!isSuperuser) {
      return [
        { key: 'updates', label: SYNTHETIC_LABELS.updates },
        { key: 'auto-ticket-rules', label: SYNTHETIC_LABELS['auto-ticket-rules'] },
      ]
    }
    const real = groups.map((g) => ({ key: g.key, label: g.label }))
    return [
      ...real,
      { key: 'auto-ticket-rules', label: SYNTHETIC_LABELS['auto-ticket-rules'] },
      { key: 'users', label: SYNTHETIC_LABELS.users },
      { key: 'environment', label: SYNTHETIC_LABELS.environment },
    ]
  }, [groups, isOperator, isSuperuser])

  if (me.isLoading || (isSuperuser && settings.isLoading)) {
    return (
      <div className={`kc-content kc-view ${styles.root}`}>
        <h1 className="kc-h1" style={{ fontFamily: 'var(--font-display)', fontWeight: 500, fontSize: 'var(--display-md)', margin: '0 0 24px' }}>
          Admin
        </h1>
        <div className={styles.loading}>Loading…</div>
      </div>
    )
  }

  // An unreadable identity is not the same as an insufficient one: falling
  // through to the message below would tell an operator their role is too low
  // when the truth is we never learned what it is.
  if (me.isError || !me.data) {
    return (
      <div className={`kc-content kc-view ${styles.root}`}>
        <h1 className="kc-h1" style={{ fontFamily: 'var(--font-display)', fontWeight: 500, fontSize: 'var(--display-md)', margin: '0 0 24px' }}>
          Admin
        </h1>
        <EmptyState title="Could not read your account" message="Something went wrong. Reload to try again." />
      </div>
    )
  }

  // A scoped `user` has no section here at all: every admin route floors at
  // `operator`. The nav item is hidden for them, so this is the hand-typed-URL
  // path — say so, rather than drawing a nav of sections that all 403.
  if (!isOperator) {
    return (
      <div className={`kc-content kc-view ${styles.root}`}>
        <h1 className="kc-h1" style={{ fontFamily: 'var(--font-display)', fontWeight: 500, fontSize: 'var(--display-md)', margin: '0 0 24px' }}>
          Admin
        </h1>
        <EmptyState
          title="Admin is for operators"
          message="Your account works its own tickets and the PCs assigned to it. Fleet administration needs an operator."
        />
      </div>
    )
  }

  if (isSuperuser && (settings.isError || !settings.data)) {
    return (
      <div className={`kc-content kc-view ${styles.root}`}>
        <h1 className="kc-h1" style={{ fontFamily: 'var(--font-display)', fontWeight: 500, fontSize: 'var(--display-md)', margin: '0 0 24px' }}>
          Admin
        </h1>
        <EmptyState title="Could not load the settings catalog" message="Something went wrong. Reload to try again." />
      </div>
    )
  }

  // Bare #/admin resolves to the first section this role can see — never an
  // invented placeholder slug. An aliased slug is rewritten to its canonical one
  // so the address bar and the nav highlight agree.
  if (!section) {
    const first = navItems[0]?.key ?? 'environment'
    return <Navigate to={`/admin/${first}`} replace />
  }
  if (rawSection && section !== rawSection) {
    return <Navigate to={`/admin/${section}`} replace />
  }

  // A deep link into a section this role cannot see (a `users` link without a
  // superuser session, or any catalog group as an operator) resolves to the first
  // section it can — the section is hidden entirely, not just its nav entry.
  if (!navItems.some((item) => item.key === section)) {
    const first = navItems[0]?.key
    if (first) return <Navigate to={`/admin/${first}`} replace />
  }

  const activeGroup = groups.find((g) => g.key === section)
  const title = activeGroup?.label ?? SYNTHETIC_LABELS[section] ?? section

  return (
    <div className={`kc-content kc-view ${styles.root}`}>
      <h1 className="kc-h1" style={{ fontFamily: 'var(--font-display)', fontWeight: 500, fontSize: 'var(--display-md)', margin: '0 0 24px' }}>
        Admin
      </h1>
      <div className={`kc-adminwrap ${styles.wrap}`}>
        <AdminNav items={navItems} />
        <div>
          <div className={styles.sectionTitle}>{title.toUpperCase()}</div>
          {section === 'backup' ? (
            <BackupSection />
          ) : section === 'updates' ? (
            <UpdatesSection />
          ) : section === 'discord-tickets' ? (
            <DiscordSection />
          ) : section === 'web-filter' ? (
            <WebFilterSection rows={activeGroup?.rows ?? []} />
          ) : section === 'auto-ticket-rules' ? (
            <TicketRulesSection />
          ) : section === 'users' ? (
            <UsersSection />
          ) : section === 'environment' ? (
            <EnvironmentSection rows={environmentSection.rows} />
          ) : activeGroup ? (
            <GenericSettingsSection rows={activeGroup.rows} />
          ) : (
            <EmptyState title="Unknown section" message="This section does not exist. Pick one from the list on the left." />
          )}
        </div>
      </div>
    </div>
  )
}

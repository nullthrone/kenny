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
  'auto-ticket-rules': 'Auto-ticket rules',
  users: 'Users',
  environment: 'Environment',
}

/**
 * `#/admin/:section` — 220px section nav + a row list. The nav is built
 * from `GET /api/settings`'s `groups[].slug` (eleven today), plus three
 * synthetic sections with no server group of their own (`AdminSectionKey`'s
 * doc comment in `api/types.ts`): `auto-ticket-rules`, `users` (superuser
 * only), `environment` (composed client-side, see `settingsMap.ts`).
 *
 * The design's prototype only drew nine sections; the five it omits
 * (Logging, Network & Process, Operator & Agent Auth, Telemetry limits,
 * Agent distribution) are real configuration and render generically here —
 * dropping them would be a silent capability loss the brief explicitly
 * rules out.
 */
export default function AdminView() {
  const { section } = useParams<{ section?: string }>()

  const me = useQuery({ queryKey: ['me'], queryFn: () => api.get<ProfileMe>('/api/me') })
  const settings = useQuery({ queryKey: ['settings'], queryFn: () => api.get<RawSettingsResponse>('/api/settings') })

  const groups = useMemo(() => (settings.data ? mapSettingsGroups(settings.data) : []), [settings.data])
  const environmentSection = useMemo(() => buildEnvironmentSection(groups), [groups])

  const isSuperuser = me.data?.role === 'superuser'

  const navItems: AdminNavItem[] = useMemo(() => {
    const real = groups.map((g) => ({ key: g.key, label: g.label }))
    const synthetic: AdminNavItem[] = [
      { key: 'auto-ticket-rules', label: SYNTHETIC_LABELS['auto-ticket-rules'] },
      ...(isSuperuser ? [{ key: 'users', label: SYNTHETIC_LABELS.users }] : []),
      { key: 'environment', label: SYNTHETIC_LABELS.environment },
    ]
    return [...real, ...synthetic]
  }, [groups, isSuperuser])

  if (settings.isLoading || me.isLoading) {
    return (
      <div className={`kc-content kc-view ${styles.root}`}>
        <h1 className="kc-h1" style={{ fontFamily: 'var(--font-display)', fontWeight: 500, fontSize: 'var(--display-md)', margin: '0 0 24px' }}>
          Admin
        </h1>
        <div className={styles.loading}>Loading…</div>
      </div>
    )
  }

  if (settings.isError || !settings.data) {
    return (
      <div className={`kc-content kc-view ${styles.root}`}>
        <h1 className="kc-h1" style={{ fontFamily: 'var(--font-display)', fontWeight: 500, fontSize: 'var(--display-md)', margin: '0 0 24px' }}>
          Admin
        </h1>
        <EmptyState title="Could not load the settings catalog" message="Something went wrong. Reload to try again." />
      </div>
    )
  }

  // Bare #/admin resolves to the first server-provided group — never an invented placeholder slug.
  if (!section) {
    const first = navItems[0]?.key ?? 'environment'
    return <Navigate to={`/admin/${first}`} replace />
  }

  // A `users` deep link with a non-superuser session: the section is hidden entirely, not just its nav entry.
  if (section === 'users' && !isSuperuser) {
    return <Navigate to={`/admin/${navItems[0]?.key ?? 'environment'}`} replace />
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

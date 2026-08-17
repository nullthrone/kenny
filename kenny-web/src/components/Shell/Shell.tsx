import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, NavLink, Outlet, useLocation } from 'react-router'
import { api } from '../../api/client'
import type { FleetResponse, Me } from '../../api/types'
import { useTheme } from '../../theme/ThemeProvider'
import Monogram from '../Monogram/Monogram'
import MobileTabBar from '../MobileTabBar/MobileTabBar'
import { NAV_ITEMS, activeNavKey } from '../navItems'
import { Sun, Moon, Terminal, LogOut, X, ICON_STROKE_WIDTH } from '../icons'
import { initialsOf, roleLabel } from '../format'
import { deriveCrumb } from './crumb'
import styles from './Shell.module.css'

/**
 * The app shell: 232px ink sidebar (desktop) / MobileTabBar (below 760px,
 * rendered as a sibling here), 64px header, content area rendered through
 * `<Outlet/>`. Used as the element of the wrapping layout Route
 * (src/router/routes.tsx) — every view renders inside it.
 *
 * Self-sufficient for its own chrome data: fetches `/api/me` (user block)
 * and `/api/fleet` (online count) itself, the same way the old dashboard's
 * header re-derives these on every render rather than a view passing them
 * down. Per-nav badge counts (e.g. Inbox's "needs you" count) are not
 * wired — there is no documented endpoint for a lightweight global badge
 * count in the frozen contract, only full `/api/inbox` list responses.
 * Wire `navBadges` once that's decided; it renders correctly already.
 */
export interface ShellProps {
  navBadges?: Partial<Record<(typeof NAV_ITEMS)[number]['key'], string>>
}

export default function Shell({ navBadges }: ShellProps) {
  const location = useLocation()
  const { theme, toggleTheme } = useTheme()
  const [chatOpen, setChatOpen] = useState(false)

  const me = useQuery({ queryKey: ['me'], queryFn: () => api.get<Me>('/api/me') })
  const fleet = useQuery({ queryKey: ['fleet'], queryFn: () => api.get<FleetResponse>('/api/fleet') })

  const fleetTotal = fleet.data?.agents.length ?? null
  const online = fleet.data ? fleet.data.agents.filter((a) => a.online).length : null

  const { crumb, mobileTitle } = deriveCrumb(location.pathname, fleetTotal)
  const active = activeNavKey(location.pathname)

  useEffect(() => {
    function onKeydown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault()
        setChatOpen((v) => !v)
      }
      if (e.key === 'Escape') setChatOpen(false)
    }
    window.addEventListener('keydown', onKeydown)
    return () => window.removeEventListener('keydown', onKeydown)
  }, [])

  return (
    <div className={styles.root}>
      <aside className={`${styles.sidebar} kc-sidebar`}>
        <div className={styles.logoRow}>
          <Monogram variant="full" width={30} height={29} color="var(--brass-400)" />
          <div>
            <div className={styles.wordmark}>KENNY</div>
            <div className={styles.tagline}>FLEET CONSOLE</div>
          </div>
        </div>
        <nav className={styles.nav}>
          {NAV_ITEMS.map((item) => {
            const isActive = active === item.key
            const Icon = item.icon
            return (
              <NavLink
                key={item.key}
                to={item.href}
                className={`${styles.navItem} kc-btn`}
                style={{
                  color: isActive ? '#F4F2EC' : 'var(--ink-300)',
                  borderLeftColor: isActive ? 'var(--brass-400)' : 'transparent',
                  background: isActive ? 'rgba(255,255,255,0.06)' : 'transparent',
                }}
              >
                <Icon width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                <span className={styles.navLabel}>{item.label}</span>
                {navBadges?.[item.key] && <span className={styles.navBadge}>{navBadges[item.key]}</span>}
              </NavLink>
            )
          })}
        </nav>
        <div className={styles.spacer} />
        <div className={styles.userBlock}>
          <div className={styles.userRow}>
            <Link to="/profile" className={styles.userLink}>
              <div className={styles.avatar}>{me.data ? initialsOf(me.data.username) : '··'}</div>
              <div className={styles.userText}>
                <div className={styles.username}>{me.data?.username ?? '—'}</div>
                <div className={styles.roleLabel}>{me.data ? roleLabel(me.data.role) : ''}</div>
              </div>
            </Link>
            {/* Plain link, not a fetch call — a full browser navigation to the
                server's /logout route, exactly like the old dashboard
                (notes/api-contract-actual.md §3). */}
            <a href="/logout" title="Log out" className={styles.logoutLink}>
              <LogOut width={14} height={14} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
            </a>
          </div>
          {/* The prototype's line is "v0.10 · 6 agents · all reporting" — the
              version segment is dropped here: there is no version field in
              the frozen contract (types.ts) to source it from honestly. */}
          <div className={styles.versionLine}>
            {fleetTotal !== null && online !== null
              ? `${fleetTotal} agent${fleetTotal === 1 ? '' : 's'} · ${
                  online === fleetTotal ? 'all reporting' : `${fleetTotal - online} offline`
                }`
              : '— agents'}
          </div>
        </div>
      </aside>

      <main className={styles.main}>
        <header className={`${styles.header} kc-header`}>
          <div className={styles.headerLeft}>
            <Monogram variant="mark" width={22} height={21} color="var(--ink-950)" className="kc-mobilebar" />
            <div className="kc-crumb" style={{ fontFamily: 'var(--font-display)', fontSize: 11, letterSpacing: 'var(--track-caps-wide)', color: 'var(--text-muted)' }}>
              {crumb}
            </div>
            <div className={`${styles.mobileTitle} kc-mobilebar`}>{mobileTitle}</div>
          </div>
          <div className={styles.headerRight}>
            <div className="kc-online" style={{ display: 'flex', alignItems: 'center', gap: 8, fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--text-muted)' }}>
              <span className={styles.onlineDot} />
              {online !== null && fleetTotal !== null ? `${online}/${fleetTotal} ONLINE` : '—/— ONLINE'}
            </div>
            <button
              type="button"
              onClick={toggleTheme}
              title={theme === 'dark' ? 'Switch to light' : 'Switch to dark'}
              className={`${styles.themeToggle} kc-btn`}
            >
              {theme === 'dark' ? (
                <Sun width={15} height={15} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
              ) : (
                <Moon width={15} height={15} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
              )}
            </button>
            <button type="button" onClick={() => setChatOpen((v) => !v)} className={`${styles.askButton} kc-btn`}>
              <Terminal width={14} height={14} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
              <span className="kc-askword">ASK KENNY</span>
              <span className={styles.askHint}>⌘K</span>
            </button>
          </div>
        </header>

        <Outlet />
      </main>

      <MobileTabBar navBadges={navBadges} />

      {chatOpen && (
        <>
          <div className={`${styles.backdrop} kc-backdrop`} onClick={() => setChatOpen(false)} />
          <div className={`${styles.chatPanel} kc-chat`}>
            <div className={styles.chatHeader}>
              <Terminal width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} color="var(--brass-400)" aria-hidden="true" />
              <span className={styles.chatTitle}>ASK KENNY</span>
              <button type="button" onClick={() => setChatOpen(false)} className={`${styles.chatClose} kc-btn`}>
                <X width={16} height={16} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
              </button>
            </div>
            <div className={styles.chatBody}>
              The Ask Kenny transcript and composer are built by the next wave on top
              of GateCard, Modal and the SSE client in src/api/sse.ts.
            </div>
          </div>
        </>
      )}
    </div>
  )
}

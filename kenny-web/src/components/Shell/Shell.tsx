import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, NavLink, Outlet, useLocation } from 'react-router'
import { api } from '../../api/client'
import type { FleetResponse, Me, TicketSummary } from '../../api/types'
import { useTheme } from '../../theme/ThemeProvider'
import Monogram from '../Monogram/Monogram'
import MobileTabBar from '../MobileTabBar/MobileTabBar'
import { activeNavKey, navItemsFor, type NavKey } from '../navItems'
import { Sun, Moon, Terminal, LogOut, X, ICON_STROKE_WIDTH } from '../icons'
import { initialsOf, roleLabel } from '../format'
import { deriveCrumb } from './crumb'
import AskKennyDrawer from '../AskKennyDrawer/AskKennyDrawer'
import AboutModal from '../AboutModal/AboutModal'
import { useAbout } from '../AboutModal/api'
import styles from './Shell.module.css'

/**
 * The app shell: 232px ink sidebar (desktop) / MobileTabBar (below 760px,
 * rendered as a sibling here), header, content area rendered through
 * `<Outlet/>`. Used as the element of the wrapping layout Route
 * (src/router/routes.tsx) — every view renders inside it.
 *
 * The header and the sidebar's logo block are the same height
 * (`--kc-header-h`, set on `.root` in Shell.module.css), so the rule under
 * each is one continuous line across the full width.
 *
 * Self-sufficient for its own chrome data, the same way the old dashboard's
 * header re-derived these on every render rather than a view passing them
 * down: `/api/me` (user block and which destinations the role may reach),
 * `/api/fleet` (online count), `/api/about` (the version segment of the
 * sidebar's fleet line) and `/api/tickets/summary` (the Inbox badge).
 * `/api/about` is a process constant and is cached accordingly — see
 * `AboutModal/api.ts`, which the About dialog shares the entry with.
 *
 * The Inbox badge mirrors the queue's NEEDS YOU count. It rides
 * `/api/tickets/summary` rather than `/api/inbox` because that endpoint
 * exists precisely to be the cheap count (`TicketSummary` in api/types.ts)
 * — a badge must never pull a full list response. It floors at `user` and
 * narrows to the caller's own tickets, so every role gets its own number.
 * The badge is informational: the nav item still opens the Inbox unfiltered.
 */
export default function Shell() {
  const location = useLocation()
  const { theme, toggleTheme, adoptTheme } = useTheme()
  const [chatOpen, setChatOpen] = useState(false)
  const [aboutOpen, setAboutOpen] = useState(false)

  const me = useQuery({ queryKey: ['me'], queryFn: () => api.get<Me>('/api/me') })
  const fleet = useQuery({ queryKey: ['fleet'], queryFn: () => api.get<FleetResponse>('/api/fleet') })
  const about = useAbout()
  const summary = useQuery({
    queryKey: ['tickets', 'summary'],
    queryFn: () => api.get<TicketSummary>('/api/tickets/summary'),
  })

  const fleetTotal = fleet.data?.agents.length ?? null
  const online = fleet.data ? fleet.data.agents.filter((a) => a.online).length : null

  const role = me.data?.role ?? null
  const navItems = navItemsFor(role)
  // Nothing needing you is the ordinary state, and an unread "0" would read
  // as a thing to clear — so the badge is absent rather than zero.
  const needsYou = summary.data?.needs_you ?? 0
  const navBadges: Partial<Record<NavKey, string>> =
    needsYou > 0 ? { inbox: needsYou > 99 ? '99+' : String(needsYou) } : {}

  const { crumb, mobileTitle } = deriveCrumb(location.pathname, fleetTotal)
  const active = activeNavKey(location.pathname)

  /**
   * The account's stored theme wins over this browser's copy on load.
   *
   * The inline boot script in index.html has already painted from localStorage
   * — that is what keeps the first frame from flashing the wrong theme — so this
   * only ever corrects a browser the operator has not set a theme on before.
   * `adoptTheme` deliberately does not write back: this is the server's value
   * arriving, not a choice being made. A shared-token identity reports
   * `theme: null` and keeps the browser copy.
   */
  const accountTheme = me.data?.theme ?? null
  useEffect(() => {
    if (accountTheme && accountTheme !== theme) adoptTheme(accountTheme)
  }, [accountTheme, theme, adoptTheme])

  useEffect(() => {
    function onKeydown(e: KeyboardEvent) {
      // Not while About is up: the drawer would open behind the dialog.
      if ((e.metaKey || e.ctrlKey) && e.key === 'k' && !aboutOpen) {
        e.preventDefault()
        setChatOpen((v) => !v)
      }
      if (e.key === 'Escape') setChatOpen(false)
    }
    window.addEventListener('keydown', onKeydown)
    return () => window.removeEventListener('keydown', onKeydown)
  }, [aboutOpen])

  /**
   * "Fix via Ask kenny" on a host's section modal starts a real chat turn through
   * the chat store, but the drawer's visible state lives here. Without this the
   * turn would run unseen until the operator happened to open the drawer — the
   * one case where kenny appears to have done nothing while actually working.
   */
  useEffect(() => {
    function onOpenRequest() {
      setChatOpen(true)
    }
    window.addEventListener('kenny:ask-kenny-open', onOpenRequest)
    return () => window.removeEventListener('kenny:ask-kenny-open', onOpenRequest)
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
        {/* The tab bar below renders the same destinations for narrow viewports and
            is only hidden by CSS, so both navs carry a name — two unlabelled
            navigation landmarks with identical contents are indistinguishable to a
            screen reader. */}
        <nav className={styles.nav} aria-label="Sections">
          {navItems.map((item) => {
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
          {/* The prototype's line: "v0.10 · 6 agents · all reporting". It is also
              the only way into the About dialog — the legacy dashboard opened
              About from a header user menu this shell does not have. The
              version segment is omitted entirely until /api/about resolves, so
              a slow or failed read degrades to the fleet half rather than
              rendering "vundefined".

              The visible text describes fleet state, not the action, so the
              button carries an accessible name of its own. */}
          <button
            type="button"
            className={styles.versionLine}
            onClick={() => setAboutOpen(true)}
            aria-haspopup="dialog"
            title="About kenny"
            aria-label={about.data ? `About kenny — server version ${about.data.server_version}` : 'About kenny'}
          >
            {about.data ? `v${about.data.server_version} · ` : ''}
            {fleetTotal !== null && online !== null
              ? `${fleetTotal} agent${fleetTotal === 1 ? '' : 's'} · ${
                  online === fleetTotal ? 'all reporting' : `${fleetTotal - online} offline`
                }`
              : '— agents'}
          </button>
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

      <MobileTabBar role={role} navBadges={navBadges} />

      <AboutModal open={aboutOpen} onClose={() => setAboutOpen(false)} />

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
              <AskKennyDrawer />
            </div>
          </div>
        </>
      )}
    </div>
  )
}

import { describe, expect, it } from 'vitest'
import { NAV_ITEMS, activeNavKey, navItemsFor } from './navItems'

/**
 * The nav is a seam: these hrefs must land on a route `router/routes.tsx`
 * defines, and — for Admin — on a section the server's settings catalog can
 * actually produce.
 *
 * The regression this pins: ADMIN pointed at `/admin/general` for the whole of
 * 2.2.0. `general` is not a slug `config.py`'s `GROUP_ORDER` -> `group_slug`
 * yields, nor one of `AdminView`'s synthetic keys, so the primary way into Admin
 * rendered the "Unknown section" empty state. Nothing failed, because nothing
 * checked that the two sides agreed.
 */

/** Every path `AppRoutes` matches, transcribed from `router/routes.tsx`. */
const DEFINED_ROUTES = [
  '/today',
  '/fleet',
  '/fleet/:host',
  '/inbox',
  '/inbox/:group',
  '/inbox/ticket/:id',
  '/log',
  '/admin',
  '/admin/:section',
  '/profile',
]

describe('NAV_ITEMS hrefs', () => {
  it('every href is a route the router defines', () => {
    for (const item of NAV_ITEMS) {
      expect(DEFINED_ROUTES, `${item.key} -> ${item.href}`).toContain(item.href)
    }
  })

  it('no href invents a path segment the router would have to guess at', () => {
    // A concrete-looking second segment (`/admin/general`, `/inbox/whatever`) can
    // only be a slug this file made up: real ones come from the server. Bare
    // destinations are what the views resolve from.
    for (const item of NAV_ITEMS) {
      expect(item.href.split('/').filter(Boolean), `${item.key} -> ${item.href}`).toHaveLength(1)
    }
  })
})

describe('navItemsFor', () => {
  it('hides ADMIN from a scoped user, whose every admin route would 403', () => {
    const keys = navItemsFor('user').map((i) => i.key)
    expect(keys).not.toContain('admin')
    // LOG stays: /api/log floors at `user` and narrows to their own hosts.
    expect(keys).toContain('log')
  })

  it('shows ADMIN to an operator and a superuser', () => {
    expect(navItemsFor('operator').map((i) => i.key)).toContain('admin')
    expect(navItemsFor('superuser').map((i) => i.key)).toContain('admin')
  })

  it('shows the unprivileged set while the identity is still loading', () => {
    expect(navItemsFor(null).map((i) => i.key)).not.toContain('admin')
  })
})

describe('activeNavKey', () => {
  it.each([
    ['/today', 'today'],
    ['/fleet', 'fleet'],
    ['/fleet/oma-pc', 'fleet'],
    ['/inbox', 'inbox'],
    ['/inbox/ticket/42', 'inbox'],
    ['/log', 'log'],
    ['/admin', 'admin'],
    ['/admin/backup', 'admin'],
  ])('%s lights %s', (path, key) => {
    expect(activeNavKey(path)).toBe(key)
  })

  it('lights the destination an old route redirects into, so the redirect never flashes an unlit nav', () => {
    expect(activeNavKey('/settings/backup')).toBe('admin')
    expect(activeNavKey('/activity/audit')).toBe('log')
  })

  it('returns null for a destination outside the nav', () => {
    expect(activeNavKey('/profile')).toBeNull()
  })
})

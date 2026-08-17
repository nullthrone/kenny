export interface CrumbInfo {
  crumb: string
  mobileTitle: string
}

/**
 * Header crumb + mobile title text, derived from the current path.
 * Mirrors the prototype's `crumbs` lookup (Kenny Console.dc.html line 622)
 * — same copy, keyed by route shape instead of a `view` state string.
 *
 * `fleetTotal` fills in the "N MACHINES" count on the Fleet crumb when
 * known; it reads "FLEET" alone while the fleet query is still loading.
 */
export function deriveCrumb(pathname: string, fleetTotal: number | null): CrumbInfo {
  const segments = pathname.split('/').filter(Boolean)

  if (segments[0] === 'fleet' && segments[1]) {
    return { crumb: `FLEET · ${segments[1].toUpperCase()}`, mobileTitle: 'FLEET' }
  }
  if (segments[0] === 'fleet') {
    const suffix = fleetTotal !== null ? `${fleetTotal} MACHINE${fleetTotal === 1 ? '' : 'S'}` : 'THE FLEET'
    return { crumb: `FLEET · ${suffix}`, mobileTitle: 'FLEET' }
  }
  if (segments[0] === 'inbox' && segments[1] === 'ticket' && segments[2]) {
    return { crumb: `INBOX · TICKET #${segments[2]}`, mobileTitle: 'INBOX' }
  }
  if (segments[0] === 'inbox') {
    return { crumb: 'INBOX · WHAT WAITS ON WHOM', mobileTitle: 'INBOX' }
  }
  if (segments[0] === 'log') {
    return { crumb: 'LOG · AUDIT & EVENTS', mobileTitle: 'LOG' }
  }
  if (segments[0] === 'admin') {
    return { crumb: 'ADMIN · SETTINGS, USERS, UPDATES', mobileTitle: 'ADMIN' }
  }
  if (segments[0] === 'profile') {
    return { crumb: 'PROFILE · YOUR ACCOUNT', mobileTitle: 'PROFILE' }
  }
  return { crumb: 'TODAY · THE FLEET IN ONE SENTENCE', mobileTitle: 'TODAY' }
}

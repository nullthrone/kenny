import { Navigate, Route, Routes, useParams } from 'react-router'
import Shell from '../components/Shell/Shell'
import Today from '../views/Today'
import Fleet from '../views/Fleet'
import FleetHost from '../views/FleetHost'
import Inbox from '../views/Inbox'
import InboxTicket from '../views/InboxTicket'
import Log from '../views/Log'
import Admin from '../views/Admin'
import Profile from '../views/Profile'

/**
 * Redirects a captured param onto a new path shape. Used for the two old
 * routes whose id/slug needs to survive the rename
 * (`#/tickets/:id` → `#/inbox/ticket/:id`, `#/settings/:section` →
 * `#/admin/:section`).
 */
function ParamRedirect({ build }: { build: (params: Readonly<Record<string, string | undefined>>) => string }) {
  const params = useParams()
  return <Navigate to={build(params)} replace />
}

/**
 * The complete route table, per the brief:
 *
 *   #/today  #/fleet  #/fleet/:host  #/inbox  #/inbox/:group
 *   #/inbox/ticket/:id  #/log  #/admin/:section  #/profile
 *
 * plus redirects for every old route published as a bookmarkable deep link
 * (notes/api-contract-actual.md §5), so none of them 404:
 *
 *   #/overview          → #/today
 *   #/fleet              → #/fleet         (already canonical, no-op)
 *   #/activity/audit     → #/log
 *   #/activity/events    → #/log
 *   #/tickets             → #/inbox
 *   #/tickets/:id          → #/inbox/ticket/:id
 *   #/flagged/warn         → #/inbox
 *   #/flagged/crit         → #/inbox
 *   #/settings/:section    → #/admin/:section
 *
 * and the old app's bare aliases, which resolve to the section they used to open
 * rather than dumping the visitor on the landing page:
 *
 *   #/activity          → #/log
 *   #/flagged           → #/inbox
 *   #/settings          → #/admin
 *   #/backup            → #/admin/backup
 *   #/updates           → #/admin/updates
 *
 * `#/settings/:section` keeps its slug on the way to `#/admin/:section`: both sides
 * derive it from `config.group_slug`, which `test_config.py` pins, so an old
 * `#/settings/updates` bookmark lands on the same content it always did.
 *
 * An unrecognised hash (including the bare empty hash) goes to #/today.
 */
export default function AppRoutes() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Navigate to="/today" replace />} />
        <Route path="today" element={<Today />} />

        <Route path="fleet" element={<Fleet />} />
        <Route path="fleet/:host" element={<FleetHost />} />

        <Route path="inbox" element={<Inbox />} />
        <Route path="inbox/:group" element={<Inbox />} />
        <Route path="inbox/ticket/:id" element={<InboxTicket />} />

        <Route path="log" element={<Log />} />
        <Route path="admin/:section" element={<Admin />} />
        {/* Bare #/admin renders the section nav and resolves to the first group the
            server returns. The section list is server-derived (config.GROUP_ORDER →
            group_slug) — there is no hardcoded slug to redirect to. */}
        <Route path="admin" element={<Admin />} />
        <Route path="profile" element={<Profile />} />

        {/* ── Redirects from old routes ── */}
        <Route path="overview" element={<Navigate to="/today" replace />} />
        <Route path="activity/audit" element={<Navigate to="/log" replace />} />
        <Route path="activity/events" element={<Navigate to="/log" replace />} />
        <Route path="tickets" element={<Navigate to="/inbox" replace />} />
        <Route path="tickets/:id" element={<ParamRedirect build={(p) => `/inbox/ticket/${p.id}`} />} />
        <Route path="flagged/warn" element={<Navigate to="/inbox" replace />} />
        <Route path="flagged/crit" element={<Navigate to="/inbox" replace />} />
        <Route path="settings/:section" element={<ParamRedirect build={(p) => `/admin/${p.section}`} />} />
        <Route path="activity" element={<Navigate to="/log" replace />} />
        <Route path="flagged" element={<Navigate to="/inbox" replace />} />
        <Route path="settings" element={<Navigate to="/admin" replace />} />
        <Route path="backup" element={<Navigate to="/admin/backup" replace />} />
        <Route path="updates" element={<Navigate to="/admin/updates" replace />} />

        {/* Unrecognised hash → #/today. */}
        <Route path="*" element={<Navigate to="/today" replace />} />
      </Route>
    </Routes>
  )
}

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }))
vi.mock('../../api/client', () => ({
  api: { get: apiGetMock, post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  ApiError: class ApiError extends Error {},
}))

const { default: AdminView } = await import('./AdminView')

/**
 * `config.py`'s groups, in `GROUP_ORDER`, with their real slugs and one editable
 * row each. The row label is what the "every group renders its rows" test looks
 * for — a section that draws its own UI and forgets the rows is exactly how 22
 * settings once had no control anywhere.
 */
const GROUP_SLUGS: [string, string][] = [
  ['alerts-notifications', 'Alerts & notifications'],
  ['ai', 'AI'],
  ['tickets', 'Tickets'],
  ['discord', 'Discord'],
  ['backup', 'Backup'],
  ['updates', 'Updates'],
  ['web-filter', 'Web filter'],
  ['shell-policy', 'Shell policy'],
  ['system', 'System'],
]

function settingRow(slug: string) {
  return {
    key: `KENNY_TEST_${slug.toUpperCase().replace(/-/g, '_')}`,
    group: slug,
    type: 'int',
    label: `row of ${slug}`,
    help: '',
    lifecycle: 'live',
    source: 'default',
    editable: true,
    choices: null,
    min: null,
    max: null,
    sensitive: false,
    pending_restart: false,
    value: 1,
    default: 1,
  }
}

const SETTINGS = {
  groups: GROUP_SLUGS.map(([slug, name]) => ({ slug, name, settings: [settingRow(slug)] })),
}

const UPDATES = {
  available: {},
  active_campaign: null,
  campaigns: [],
  agents: [],
  active_campaign_dev: null,
  campaigns_dev: [],
  agents_dev: [],
  server_apply: null,
  config: { check_interval_secs: 86400, rollout_on_connect: false, server_image_ref: null },
}

function mockApi(role: 'superuser' | 'operator' | 'user', over: Record<string, unknown> = {}) {
  const routes: Record<string, unknown> = {
    '/api/me': { user_id: '1', username: 'thomas', role, hosts: [], theme: null, is_shared_token: false },
    '/api/settings': SETTINGS,
    // The sections themselves fetch too; enough for them to render empty.
    '/api/ticket-rules': { rules: [] },
    '/api/ticket-rules/vocabulary': { event_types: [], sections: {}, decisions: [] },
    '/api/reliability/suppressions': { rules: [] },
    '/api/fleet': { agents: [] },
    '/api/updates': UPDATES,
    '/api/users': { users: [] },
    '/api/users/directory': { users: [] },
    '/api/backups': { backups: [], config: {}, targets: [] },
    '/api/discord/status': { configured: false },
    '/api/policy/rules': { builtin: [], operator: [] },
    '/api/policy/shell-allow': { mode: 'unrestricted', allow: [] },
    ...over,
  }
  apiGetMock.mockImplementation((path: string) => {
    const hit = routes[path.split('?')[0]]
    if (hit === undefined) return Promise.resolve({})
    return hit instanceof Error ? Promise.reject(hit) : Promise.resolve(hit)
  })
}

function renderAdmin(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/admin" element={<AdminView />} />
          <Route path="/admin/:section" element={<AdminView />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  apiGetMock.mockReset()
})

/**
 * `GET /api/settings` is superuser-only (`webui/__init__.py`), but Updates and
 * Auto-ticket rules both floor at `operator` (`/api/updates`,
 * `/api/ticket-rules`) and `docs/dashboard.md` names them as the operator's two
 * sections. Building the whole nav from the settings catalog meant an operator's
 * 403 took the entire page down and put their own rollout console out of reach.
 * This is the seam between the client's nav and the server's `min_role` values.
 */
describe('AdminView — what each role can reach', () => {
  it('gives an operator Updates and Alarm rules without asking for the catalog', async () => {
    mockApi('operator')

    renderAdmin('/admin')

    expect(await screen.findByRole('link', { name: 'UPDATES' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'ALARM RULES' })).toBeInTheDocument()
    expect(screen.queryByText('Could not load the settings catalog')).not.toBeInTheDocument()
    // Never requested: it would 403, and the failure is what used to break the page.
    expect(apiGetMock).not.toHaveBeenCalledWith('/api/settings')
  })

  it('keeps the operator out of superuser-only sections', async () => {
    mockApi('operator')

    renderAdmin('/admin')

    await screen.findByRole('link', { name: 'UPDATES' })
    expect(screen.queryByRole('link', { name: 'USERS' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'BACKUP' })).not.toBeInTheDocument()
  })

  it('survives an operator deep-linking into a catalog section they cannot read', async () => {
    mockApi('operator')

    renderAdmin('/admin/backup')

    // Resolves to the first section they can see rather than "Unknown section".
    expect(await screen.findByRole('link', { name: 'UPDATES' })).toBeInTheDocument()
    expect(screen.queryByText('Unknown section')).not.toBeInTheDocument()
  })

  it('gives a superuser the catalog groups plus the synthetic sections', async () => {
    mockApi('superuser')

    renderAdmin('/admin')

    expect(await screen.findByRole('link', { name: 'ALERTS & NOTIFICATIONS' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'ALARM RULES' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'BACKUP' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'USERS' })).toBeInTheDocument()
    // Nothing in Admin is read-only: the environment is not a section.
    expect(screen.queryByRole('link', { name: 'ENVIRONMENT' })).not.toBeInTheDocument()
  })

  it('still reports a genuine catalog failure to the superuser it belongs to', async () => {
    mockApi('superuser', { '/api/settings': new Error('boom') })

    renderAdmin('/admin')

    expect(await screen.findByText('Could not load the settings catalog')).toBeInTheDocument()
  })
})

/**
 * `#/settings/:section` redirects into `#/admin/:section` keeping the slug, so an
 * old bookmark arrives verbatim. Slugs whose section was renamed resolve through
 * an alias; an unresolved one used to land on "Unknown section".
 */
describe('AdminView — legacy slugs', () => {
  it.each(['ticket-rules', 'auto-ticket-rules'])('resolves the old %s slug onto Alarm rules', async (slug) => {
    mockApi('superuser')

    renderAdmin(`/admin/${slug}`)

    await waitFor(() => expect(screen.queryByText('Unknown section')).not.toBeInTheDocument())
    // The section's own copy, not the heading — the nav entry carries that string too.
    expect(
      await screen.findByText('Which alerts open a ticket automatically. A rule with no host applies fleet-wide.'),
    ).toBeInTheDocument()
  })

  it.each([
    ['alerting-digest', 'alerts-notifications'],
    ['discord-tickets', 'discord'],
    ['chat-ai', 'ai'],
    ['logging', 'system'],
  ])('resolves the old %s slug onto %s', async (old, now) => {
    mockApi('superuser')

    renderAdmin(`/admin/${old}`)

    expect(await screen.findByText(`row of ${now}`)).toBeInTheDocument()
  })

  it('sends a removed read-only section to the first section', async () => {
    mockApi('superuser')

    renderAdmin('/admin/environment')

    expect(await screen.findByText('row of alerts-notifications')).toBeInTheDocument()
    expect(screen.queryByText('Unknown section')).not.toBeInTheDocument()
  })

  it('still reports a slug that is not an alias for anything', async () => {
    mockApi('superuser')

    renderAdmin('/admin/general')

    // Not a real group and not an alias: resolving it silently would hide the bug.
    // It redirects to the first section the role can see instead of a dead end.
    expect(await screen.findByRole('link', { name: 'ALERTS & NOTIFICATIONS' })).toBeInTheDocument()
    expect(screen.queryByText('Unknown section')).not.toBeInTheDocument()
  })
})

/**
 * The seam between the server's settings catalog and the sections that draw
 * it: every group `GET /api/settings` returns must put its rows on screen, as
 * editable controls. A section with a bespoke UI that forgot its rows is how a
 * whole group of settings once had no control anywhere.
 */
describe('AdminView — every settings group reaches the screen', () => {
  it.each(GROUP_SLUGS)('renders the rows of %s as editable', async (slug) => {
    mockApi('superuser')

    renderAdmin(`/admin/${slug}`)

    expect(await screen.findByText(`row of ${slug}`)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'EDIT' }).length).toBeGreaterThan(0)
  })
})

describe('AdminView — a scoped user', () => {
  it('says Admin is not theirs rather than drawing sections that all 403', async () => {
    mockApi('user')

    renderAdmin('/admin')

    expect(await screen.findByText('Admin is for operators')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'UPDATES' })).not.toBeInTheDocument()
    expect(apiGetMock).not.toHaveBeenCalledWith('/api/settings')
  })
})

describe('AdminView — an unreadable identity', () => {
  it('reports the failure instead of blaming the role it never learned', async () => {
    mockApi('superuser', { '/api/me': new Error('boom') })

    renderAdmin('/admin')

    expect(await screen.findByText('Could not read your account')).toBeInTheDocument()
    expect(screen.queryByText('Admin is for operators')).not.toBeInTheDocument()
  })
})

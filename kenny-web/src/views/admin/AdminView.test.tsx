import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }))
vi.mock('../../api/client', () => ({
  api: { get: apiGetMock, post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))

const { default: AdminView } = await import('./AdminView')

/** Two of `config.py`'s eleven groups, in `GROUP_ORDER`, with their real slugs. */
const SETTINGS = {
  groups: [
    { slug: 'alerting-digest', name: 'Alerting & Digest', settings: [] },
    { slug: 'backup', name: 'Backup', settings: [] },
  ],
}

function mockApi(role: 'superuser' | 'operator' | 'user', over: Record<string, unknown> = {}) {
  const routes: Record<string, unknown> = {
    '/api/me': { user_id: '1', username: 'thomas', role, hosts: [], theme: null, is_shared_token: false },
    '/api/settings': SETTINGS,
    // The sections themselves fetch too; enough for them to render empty.
    '/api/ticket-rules': { rules: [] },
    '/api/ticket-rules/vocabulary': { event_types: [], sections: [], decisions: [] },
    '/api/fleet': { agents: [] },
    '/api/updates': {},
    '/api/users': { users: [] },
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
  it('gives an operator Updates and Auto-ticket rules without asking for the catalog', async () => {
    mockApi('operator')

    renderAdmin('/admin')

    expect(await screen.findByRole('link', { name: 'UPDATES' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'AUTO-TICKET RULES' })).toBeInTheDocument()
    expect(screen.queryByText('Could not load the settings catalog')).not.toBeInTheDocument()
    // Never requested: it would 403, and the failure is what used to break the page.
    expect(apiGetMock).not.toHaveBeenCalledWith('/api/settings')
  })

  it('keeps the operator out of superuser-only sections', async () => {
    mockApi('operator')

    renderAdmin('/admin')

    await screen.findByRole('link', { name: 'UPDATES' })
    expect(screen.queryByRole('link', { name: 'USERS' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'ENVIRONMENT' })).not.toBeInTheDocument()
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

    expect(await screen.findByRole('link', { name: 'ALERTING & DIGEST' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'BACKUP' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'USERS' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'ENVIRONMENT' })).toBeInTheDocument()
  })

  it('still reports a genuine catalog failure to the superuser it belongs to', async () => {
    mockApi('superuser', { '/api/settings': new Error('boom') })

    renderAdmin('/admin')

    expect(await screen.findByText('Could not load the settings catalog')).toBeInTheDocument()
  })
})

/**
 * `#/settings/:section` redirects into `#/admin/:section` keeping the slug, so a
 * bookmark minted before the redesign arrives verbatim. Auto-ticket rules is the
 * one section whose slug moved — `ticket-rules` -> `auto-ticket-rules` — and an
 * unresolved alias landed on "Unknown section".
 */
describe('AdminView — legacy slugs', () => {
  it('resolves the old ticket-rules slug onto the section it became', async () => {
    mockApi('superuser')

    renderAdmin('/admin/ticket-rules')

    await waitFor(() => expect(screen.queryByText('Unknown section')).not.toBeInTheDocument())
    // The section's own copy, not the heading — the nav entry carries that string too.
    expect(
      await screen.findByText('Which alerts open a ticket automatically. A rule with no host applies fleet-wide.'),
    ).toBeInTheDocument()
  })

  it('still reports a slug that is not an alias for anything', async () => {
    mockApi('superuser')

    renderAdmin('/admin/general')

    // Not a real group and not an alias: resolving it silently would hide the bug.
    // It redirects to the first section the role can see instead of a dead end.
    expect(await screen.findByRole('link', { name: 'ALERTING & DIGEST' })).toBeInTheDocument()
    expect(screen.queryByText('Unknown section')).not.toBeInTheDocument()
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

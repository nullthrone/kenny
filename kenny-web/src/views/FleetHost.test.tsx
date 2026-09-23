import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Link, MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toRoutePath } from './inbox/age'

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }))
vi.mock('../api/client', () => ({
  api: { get: apiGetMock, post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
// The host page opens two SSE streams on mount (forecast, and — when AI is
// configured — the section recommendation). Neither is what this file is
// about, and jsdom has no EventSource.
vi.mock('../api/sse', () => ({ streamChatEvents: vi.fn(() => () => {}) }))

const { default: FleetHost } = await import('./FleetHost')

const AGENT_DETAIL = {
  agent_id: 'crit-pc',
  online: true,
  os: 'windows',
  meta: { hostname: 'crit-pc', version: '1.2.3' },
  collected_at: '2026-08-01T00:00:00Z',
  snapshot: {
    disk: { status: 'crit', summary: 'C: nearly full', volumes: [], top_dirs: [] },
    defender: { status: 'warn', summary: 'real-time protection off' },
  },
  health: {
    overall: 'crit',
    sections: {
      disk: { status: 'crit', reason: 'C: 97% full (>=95%)', summary: 'C: nearly full', attention: true },
      defender: { status: 'warn', reason: 'real-time protection off', summary: '', attention: true },
      cpu: { status: 'ok', summary: '', attention: false },
      encryption: {
        status: 'posture',
        tier: 'posture',
        reason: 'C: not BitLocker-protected',
        summary: '',
        attention: false,
        since: '2026-07-01T00:00:00Z',
        age_seconds: 31 * 86_400,
      },
    },
  },
  governance: { supported: true },
  // Keeps `RecommendationBlock` from opening a stream of its own.
  ai_enabled: false,
  history: [],
}

/**
 * The exact `target` the server puts on a flagged-section row on Today
 * (`section_target()` in `kenny_server/webui/__init__.py`; pinned from the
 * other side by `test_a_flagged_section_opens_the_section_not_the_machine`).
 * The point of the tests below is that following it lands on the *section*,
 * not on the machine — so it is written out here as the server writes it,
 * rather than assembled from the constant this view reads.
 *
 * Today is the only surface that emits it: the Inbox carries tickets only
 * (ADR-0059), so no queue row links here any more.
 */
const SECTION_TARGET = '#/fleet/crit-pc?section=disk'

const SECTION_TITLE = 'C: 97% full (>=95%)'

/** A Today row, reduced to the one thing these tests drive: its link. */
function sectionLink(target: string) {
  return (
    <Link to={toRoutePath(target)}>{SECTION_TITLE}</Link>
  )
}

function renderAt(initialEntry: string, element: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/today" element={element} />
          <Route path="/fleet/:host" element={<FleetHost />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  apiGetMock.mockReset()
  apiGetMock.mockImplementation((url: string) =>
    url.startsWith('/api/agent/') ? Promise.resolve(AGENT_DETAIL) : Promise.resolve({}),
  )
})

/**
 * Joined seam: the `target` the server puts on a flagged-section row and the
 * host page's reading of `?section=` (`FleetHost`) have to agree, or a click
 * on a flagged section lands the reader on the machine and leaves them to find
 * the finding again among every other section. Driven through the router the
 * way a click really is, so it fails if either half moves — the target
 * regressing to a bare `#/fleet/{host}`, or the page ignoring the param.
 */
describe('a flagged-section row opens the section, not the machine', () => {
  it('follows the row link straight to that section’s detail', async () => {
    renderAt('/today', sectionLink(SECTION_TARGET))

    const link = screen.getByRole('link', { name: SECTION_TITLE })
    expect(link).toHaveAttribute('href', expect.stringContaining(toRoutePath(SECTION_TARGET)))

    fireEvent.click(link)

    // The section's own detail — not merely the host page, which would render
    // the same section as one card among many.
    expect(await screen.findByText('DISK & SMART · CRIT-PC')).toBeInTheDocument()
  })

  it('closes back to the plain host page, leaving no section pinned in the URL', async () => {
    renderAt('/today', sectionLink(SECTION_TARGET))

    fireEvent.click(screen.getByRole('link', { name: SECTION_TITLE }))
    const title = await screen.findByText('DISK & SMART · CRIT-PC')

    fireEvent.click(title.closest('div')!.querySelector('button')!)

    await waitFor(() => expect(screen.queryByText('DISK & SMART · CRIT-PC')).not.toBeInTheDocument())
    // Still on the host page, with its section cards.
    expect(screen.getByText('Disk & SMART')).toBeInTheDocument()
  })

  it('opens no section when the route names none', async () => {
    renderAt('/fleet/crit-pc', null)

    expect(await screen.findByText('Disk & SMART')).toBeInTheDocument()
    expect(screen.queryByText('DISK & SMART · CRIT-PC')).not.toBeInTheDocument()
  })

  it('opens no section when the route names one this host does not report', async () => {
    renderAt('/fleet/crit-pc?section=not_a_section', null)

    expect(await screen.findByText('Disk & SMART')).toBeInTheDocument()
    expect(screen.queryByText(/· CRIT-PC$/)).not.toBeInTheDocument()
  })
})

describe('posture sections (ADR-0058)', () => {
  beforeEach(() => {
    apiGetMock.mockReset()
    apiGetMock.mockImplementation((path: string) => {
      if (path === '/api/agent/crit-pc') return Promise.resolve(AGENT_DETAIL)
      return Promise.resolve({})
    })
  })

  it('lists a posture section as a standing fact with its age, not as a problem card', async () => {
    renderAt('/fleet/crit-pc', <Route path="/fleet/:id" element={<FleetHost />} />)
    await screen.findByText('NEEDS ATTENTION · 2 SECTIONS')
    expect(screen.getByText('POSTURE · 1 STANDING FACT')).toBeInTheDocument()
    expect(screen.getByText('C: not BitLocker-protected')).toBeInTheDocument()
    expect(screen.getByText('since 31 d')).toBeInTheDocument()
    // Not counted as healthy either.
    expect(screen.getByText('HEALTHY · 1 SECTION')).toBeInTheDocument()
  })
})

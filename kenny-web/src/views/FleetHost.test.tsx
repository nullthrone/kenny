import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { InboxItem } from '../api/types'
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
const { default: InboxRow } = await import('./inbox/InboxRow')

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
    },
  },
  governance: { supported: true },
  // Keeps `RecommendationBlock` from opening a stream of its own.
  ai_enabled: false,
  history: [],
}

/**
 * The exact `target` the server puts on a flagged-section inbox row
 * (`section_target()` in `kenny_server/webui/__init__.py`; pinned from the
 * other side by `test_inbox_section_target_opens_the_section_not_the_machine`).
 * The point of the tests below is that following it lands on the *section*,
 * not on the machine — so it is written out here as the server writes it,
 * rather than assembled from the constant this view reads.
 */
const SECTION_TARGET = '#/fleet/crit-pc?section=disk'

function sectionItem(target: string): InboxItem {
  return {
    id: 'section:crit-pc:disk',
    kind: 'section',
    waits_on: 'attention',
    severity: 'crit',
    title: 'C: 97% full (>=95%)',
    meta: 'disk',
    host: 'crit-pc',
    age_seconds: 120,
    gate: null,
    target,
  }
}

function renderAt(initialEntry: string, element: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/inbox" element={element} />
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
 * Joined seam: an inbox row's link (`InboxRow`) and the host page's reading of
 * `?section=` (`FleetHost`) have to agree, or a click on a flagged section
 * lands the reader on the machine and leaves them to find the finding again
 * among every other section. Driven through the router the way a click really
 * is, so it fails if either half moves — the row linking to a bare
 * `#/fleet/{host}` again, or the page ignoring the param.
 */
describe('a flagged-section inbox row opens the section, not the machine', () => {
  it('follows the row link straight to that section’s detail', async () => {
    renderAt('/inbox', <InboxRow item={sectionItem(SECTION_TARGET)} onDecided={vi.fn()} />)

    const link = screen.getByRole('link', { name: 'C: 97% full (>=95%)' })
    expect(link).toHaveAttribute('href', expect.stringContaining(toRoutePath(SECTION_TARGET)))

    fireEvent.click(link)

    // The section's own detail — not merely the host page, which would render
    // the same section as one card among many.
    expect(await screen.findByText('DISK & SMART · CRIT-PC')).toBeInTheDocument()
  })

  it('closes back to the plain host page, leaving no section pinned in the URL', async () => {
    renderAt('/inbox', <InboxRow item={sectionItem(SECTION_TARGET)} onDecided={vi.fn()} />)

    fireEvent.click(screen.getByRole('link', { name: 'C: 97% full (>=95%)' }))
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

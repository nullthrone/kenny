import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { WebfilterOverview } from '../types'

const { apiGetMock, apiPostMock, apiPutMock, apiDeleteMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  apiPostMock: vi.fn(),
  apiPutMock: vi.fn(),
  apiDeleteMock: vi.fn(),
}))
vi.mock('../../../api/client', () => ({
  api: {
    get: apiGetMock,
    post: apiPostMock,
    put: apiPutMock,
    patch: vi.fn(),
    delete: apiDeleteMock,
  },
}))

const { default: WebFilterBody } = await import('./WebFilterBody')

const CATEGORIES: WebfilterOverview['categories'] = [
  { key: 'adult', label: 'Adult content', external: true, capped: true },
  { key: 'bypass', label: 'VPN / proxy / DoH bypass', external: true, capped: false },
  { key: 'gambling', label: 'Gambling', external: true, capped: true },
  { key: 'gaming', label: 'Gaming', external: false, capped: true },
]

function baseOverview(overrides: Partial<WebfilterOverview> = {}): WebfilterOverview {
  return {
    agent_id: 'oma-pc',
    config: {
      agent_id: 'oma-pc',
      enabled: true,
      block_mode: true,
      use_external_adult: true,
      use_bypass_protection: false,
      categories: ['adult'],
      doh_policy: 'disable',
      updated_at: null,
      applied_hash: 'abc123',
      applied_at: '2026-08-17T10:00:00Z',
      applied_ok: true,
    },
    custom: [],
    seed_count: 10,
    external: {
      adult: { count: 500, last_fetch: '2026-08-17T09:00:00Z', enabled: true },
      bypass: { count: 200, last_fetch: '2026-08-17T09:00:00Z', enabled: false },
      gambling: { count: 8000, last_fetch: '2026-08-17T09:00:00Z', enabled: false },
    },
    categories: CATEGORIES,
    schedule: {
      now: '2026-08-17T18:00:00Z',
      timezone: 'UTC',
      local_now: '2026-08-17T18:00:00+00:00',
      base_categories: ['adult'],
      extra_categories: [],
      effective_categories: ['adult'],
      active_windows: [],
      stricter: false,
      next_change_at: null,
      next_change_local: null,
      reverts_at: null,
      windows: [],
    },
    applied: { hash: 'abc123', at: '2026-08-17T10:00:00Z', ok: true },
    current_hash: 'abc123',
    oversize: null,
    drift: false,
    ...overrides,
  }
}

beforeEach(() => {
  apiGetMock.mockReset()
  apiPostMock.mockReset()
  apiPutMock.mockReset()
  apiDeleteMock.mockReset()
  apiGetMock.mockResolvedValue({ agent_id: 'oma-pc', requests: [] })
})

function renderBody(overview: WebfilterOverview) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <WebFilterBody agentId="oma-pc" overview={overview} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('WebFilterBody — state banner legibility', () => {
  it('shows the stricter-now state with a revert time and the extra categories, not just a window table', () => {
    renderBody(
      baseOverview({
        schedule: {
          ...baseOverview().schedule,
          stricter: true,
          extra_categories: ['gaming'],
          effective_categories: ['adult', 'gaming'],
          reverts_at: '2026-08-17T19:30:00Z',
          next_change_at: '2026-08-17T19:30:00Z',
          next_change_local: '2026-08-17T19:30:00+00:00',
        },
      }),
    )

    expect(screen.getByText('STRICTER LIST IN FORCE')).toBeInTheDocument()
    expect(screen.getByText(/Reverts at/)).toBeInTheDocument()
    expect(screen.getByText(/gaming/)).toBeInTheDocument()
  })

  it('shows the base state with no schedule configured, distinct from stricter', () => {
    renderBody(baseOverview())

    expect(screen.getByText('BASE LIST IN FORCE')).toBeInTheDocument()
    expect(screen.getByText(/No schedule configured/)).toBeInTheDocument()
    expect(screen.queryByText('STRICTER LIST IN FORCE')).not.toBeInTheDocument()
  })
})

describe('WebFilterBody — over-cap state', () => {
  it('names the count, the cap, that monitoring continues, and the category to turn off — not a generic error', () => {
    renderBody(
      baseOverview({
        current_hash: null,
        oversize: { count: 12000, cap: 10000, over_by: 2000 },
      }),
    )

    expect(screen.getByText('FILTER TOO LARGE TO ENFORCE')).toBeInTheDocument()
    expect(screen.getByText(/12,000 domains/)).toBeInTheDocument()
    expect(screen.getByText(/10,000-domain cap/)).toBeInTheDocument()
    expect(screen.getByText(/Monitoring continues/)).toBeInTheDocument()
    // "adult" is the only enabled external category in this fixture (500
    // cached domains) — gambling (8000) and bypass (200) are both off, so
    // the banner must name adult specifically, not the largest list overall.
    expect(screen.getByText(/"Adult content"/)).toBeInTheDocument()
  })
})

describe('WebFilterBody — categories', () => {
  it('renders adult and bypass as ordinary category rows, not as separate toggles', () => {
    renderBody(baseOverview())
    const list = within(screen.getByTestId('webfilter-categories'))

    // Exactly one control per category — no second "use external adult"/
    // "bypass protection" checkbox alongside the catalog row.
    expect(screen.queryByText(/use external adult/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/block vpn\/proxy bypass/i)).not.toBeInTheDocument()
    expect(list.getByText('Adult content')).toBeInTheDocument()
    expect(list.getByText('VPN / proxy / DoH bypass')).toBeInTheDocument()
  })

  it('turning on a category PUTs the whole merged category set, adult included', async () => {
    apiPutMock.mockResolvedValue({ config: baseOverview().config })
    renderBody(baseOverview())

    const gamblingRow = within(screen.getByTestId('webfilter-categories')).getByText('Gambling').closest('label')!
    const checkbox = gamblingRow.querySelector('input[type="checkbox"]')!
    fireEvent.click(checkbox)

    await waitFor(() =>
      expect(apiPutMock).toHaveBeenCalledWith(
        '/api/agent/oma-pc/webfilter/config',
        expect.objectContaining({ categories: expect.arrayContaining(['adult', 'gambling']) }),
      ),
    )
  })
})

describe('WebFilterBody — bypass requests', () => {
  it('links a request to its real ticket in the Inbox', async () => {
    apiGetMock.mockResolvedValue({
      agent_id: 'oma-pc',
      requests: [
        {
          ticket: {
            id: 'tk_9',
            number: 9,
            title: 'unblock discord for homework group',
            summary: 'need discord.com for a school project',
            state: 'new',
            created_at: '2026-08-17T12:00:00Z',
          },
          requested_domains: ['discord.com'],
        },
      ],
    })

    renderBody(baseOverview())

    const link = await screen.findByText('#9 unblock discord for homework group')
    expect(link.closest('a')).toHaveAttribute('href', '/inbox/ticket/tk_9')
  })

  it('clicking a requested domain prefills the existing allow-domain form rather than granting directly', async () => {
    apiGetMock.mockResolvedValue({
      agent_id: 'oma-pc',
      requests: [
        {
          ticket: {
            id: 'tk_9',
            number: 9,
            title: 'unblock discord',
            summary: '',
            state: 'new',
            created_at: '2026-08-17T12:00:00Z',
          },
          requested_domains: ['discord.com'],
        },
      ],
    })

    renderBody(baseOverview())

    fireEvent.click(await screen.findByText('discord.com'))

    const domainInput = screen.getByPlaceholderText('example.com') as HTMLInputElement
    expect(domainInput.value).toBe('discord.com')
    // No POST fired yet — the operator still has to submit the form.
    expect(apiPostMock).not.toHaveBeenCalled()
  })
})

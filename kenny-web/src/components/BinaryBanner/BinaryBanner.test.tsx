import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { apiGetMock, apiPostMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  apiPostMock: vi.fn(),
}))
vi.mock('../../api/client', () => ({
  api: { get: apiGetMock, post: apiPostMock, put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))

const { default: BinaryBanner } = await import('./BinaryBanner')

/** Nothing staged for either OS — the only state in which the banner renders. */
const NOTHING_STAGED = {
  version: null,
  available: false,
  by_os: { windows: false, linux: false },
  repo: 'nullthrone/kenny',
}

function mockApi(over: Record<string, unknown> = {}) {
  const routes: Record<string, unknown> = {
    '/api/agent-binary': NOTHING_STAGED,
    '/api/me': { user_id: 'u1', username: 'op', role: 'operator' },
    ...over,
  }
  apiGetMock.mockImplementation((path: string) => Promise.resolve(routes[path]))
}

function renderBanner() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <BinaryBanner />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  apiGetMock.mockReset()
  apiPostMock.mockReset()
})

describe('BinaryBanner', () => {
  it('stays out of the way when a binary is staged', async () => {
    mockApi({ '/api/agent-binary': { ...NOTHING_STAGED, by_os: { windows: true, linux: false } } })
    renderBanner()
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/agent-binary'))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  /**
   * The retry used to be gated on a configured token, on the reasoning that
   * without one the button could not do anything. Releases are read anonymously
   * now (ADR-0057), so there is no configuration left that could make it useless
   * in advance — an operator can always ask kenny to try again.
   */
  it('offers the retry to an operator with no token configured', async () => {
    mockApi()
    renderBanner()
    expect(await screen.findByRole('button', { name: 'RETRY GITHUB FETCH' })).toBeInTheDocument()
  })

  it('keeps the retry from a scoped user, who cannot run it server-side', async () => {
    mockApi({ '/api/me': { user_id: 'u2', username: 'kid', role: 'user' } })
    renderBanner()
    await waitFor(() => expect(screen.getByRole('status')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'RETRY GITHUB FETCH' })).not.toBeInTheDocument()
  })

  it('names the repo it fetches from rather than a setting to configure', async () => {
    mockApi()
    renderBanner()
    await waitFor(() => expect(screen.getByText(/nullthrone\/kenny/)).toBeInTheDocument())
    expect(screen.queryByText(/Set a GitHub token/)).not.toBeInTheDocument()
  })

  /**
   * `last_fetch` is per-process; `last_check` survives a restart. Preferring the
   * durable one is what stops the banner claiming nothing was ever tried while a
   * real failure stands.
   */
  it('prefers the durable last_check over the in-process last_fetch', async () => {
    mockApi({
      '/api/agent-binary': {
        ...NOTHING_STAGED,
        last_fetch: null,
        last_check: {
          ok: false,
          message: 'GitHub rate limit exhausted, resets at 2026-09-05 08:00 UTC',
          checked_at: '2026-09-05T06:00:00Z',
          version: '',
        },
      },
    })
    renderBanner()
    await waitFor(() => expect(screen.getByText(/rate limit exhausted/)).toBeInTheDocument())
    expect(screen.queryByText(/No fetch has been attempted yet/)).not.toBeInTheDocument()
  })
})

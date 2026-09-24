import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }))
vi.mock('../../api/client', () => ({
  api: { get: apiGetMock, post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('../../api/sse', () => ({
  async *streamChatEvents() {
    yield { type: 'text_delta', text: 'Disk C: fills in about 40 days.' }
    yield { type: 'done' }
  },
}))

const { default: ForecastPanel } = await import('./ForecastPanel')

function renderPanel(forecast: boolean) {
  apiGetMock.mockResolvedValue({ enabled: true, configured: true, source: 'db', features: { forecast } })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <ForecastPanel agentId="pc" />
    </QueryClientProvider>,
  )
}

beforeEach(() => apiGetMock.mockReset())

/**
 * The server streams the computed summary when AI prose is off, so the panel
 * stays. Only AI-written prose is marked; off leaves no trace of AI.
 */
describe('ForecastPanel', () => {
  it('marks AI-written prose', async () => {
    renderPanel(true)
    expect(await screen.findByText(/· AI$/)).toBeInTheDocument()
  })

  it('carries no AI mark on the computed summary', async () => {
    renderPanel(false)
    expect(await screen.findByText('Disk C: fills in about 40 days.')).toBeInTheDocument()
    expect(await screen.findByText(/^generated /)).toBeInTheDocument()
    expect(screen.queryByText(/AI/)).not.toBeInTheDocument()
  })
})

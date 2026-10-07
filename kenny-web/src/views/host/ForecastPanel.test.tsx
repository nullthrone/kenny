import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
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

const AI_STATUS = (forecast: boolean) => ({ enabled: true, configured: true, source: 'db', features: { forecast } })

const FAILS = Symbol('trends request fails')

/** `/trends` answers with `trends`, or rejects when it is `FAILS`. */
function renderPanel(forecast: boolean, trends: unknown = { agent_id: 'pc', disk: [], battery: null }) {
  apiGetMock.mockImplementation((url: string) => {
    if (url.includes('/trends')) return trends === FAILS ? Promise.reject(new Error('404')) : Promise.resolve(trends)
    return Promise.resolve(AI_STATUS(forecast))
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <ForecastPanel agentId="pc" />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  apiGetMock.mockReset()
})

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

  it('lists the hardware the server expects to fail, with its symptom and how far off', async () => {
    renderPanel(false, {
      agent_id: 'pc',
      hardware: {
        window_days: 180,
        devices: [],
        forecasts: [
          {
            device_key: 'disk:23012A800123',
            kind: 'disk',
            label: 'WD_BLACK SN850X 2000GB',
            reason: 'wear_out',
            symptom: 'The SSD will reach its rated write endurance.',
            days_until: 143.5,
          },
          {
            device_key: 'host:memory',
            kind: 'component',
            label: 'Memory',
            reason: 'first_error',
            symptom: 'The first memory error was just logged.',
            days_until: null,
          },
        ],
      },
    })
    expect(await screen.findByText('HARDWARE AT RISK')).toBeInTheDocument()
    expect(screen.getByText('WD_BLACK SN850X 2000GB')).toBeInTheDocument()
    expect(screen.getByText('The SSD will reach its rated write endurance.')).toBeInTheDocument()
    expect(screen.getByText('in ~144 days')).toBeInTheDocument()
    expect(screen.getByText('The first memory error was just logged.')).toBeInTheDocument()
    // A forecast without a date says nothing about timing.
    expect(screen.getAllByText(/^in ~/)).toHaveLength(1)
  })

  it.each([
    ['without a hardware key (older server)', { agent_id: 'pc', disk: [], battery: null }],
    ['with no forecasts', { agent_id: 'pc', hardware: { window_days: 180, devices: [], forecasts: [] } }],
    ['when the trends request fails', FAILS],
  ])('shows no hardware list %s', async (_name, trends) => {
    renderPanel(false, trends)
    expect(await screen.findByText('Disk C: fills in about 40 days.')).toBeInTheDocument()
    await waitFor(() => expect(apiGetMock.mock.calls.some(([url]) => String(url).includes('/trends'))).toBe(true))
    expect(screen.queryByText('HARDWARE AT RISK')).not.toBeInTheDocument()
  })

  it('fetches the trends once for the panel', async () => {
    renderPanel(false)
    await screen.findByText('Disk C: fills in about 40 days.')
    await waitFor(() => expect(apiGetMock.mock.calls.filter(([url]) => String(url).includes('/trends'))).toHaveLength(1))
    expect(apiGetMock).toHaveBeenCalledWith('/api/agent/pc/trends')
  })
})

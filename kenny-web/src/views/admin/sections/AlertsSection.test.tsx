import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const { apiPostMock } = vi.hoisted(() => ({ apiPostMock: vi.fn() }))
vi.mock('../../../api/client', () => ({
  api: { get: vi.fn(), post: apiPostMock, put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  ApiError: class ApiError extends Error {},
}))

const { default: AlertsSection } = await import('./AlertsSection')

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <AlertsSection rows={[]} />
    </QueryClientProvider>,
  )
}

/** `POST /api/notify/test` reports per channel; the operator sees which one failed and why. */
describe('AlertsSection — test notification', () => {
  it('shows each channel with its outcome', async () => {
    apiPostMock.mockResolvedValue({
      results: [
        { channel: 'ntfy', ok: true, error: null },
        { channel: 'webhook', ok: false, error: 'HTTP 500' },
      ],
    })
    renderSection()

    fireEvent.click(screen.getByRole('button', { name: 'SEND TEST NOTIFICATION' }))

    expect(await screen.findByText('delivered')).toBeInTheDocument()
    expect(screen.getByText('failed · HTTP 500')).toBeInTheDocument()
    expect(apiPostMock).toHaveBeenCalledWith('/api/notify/test')
  })

  it('says so when no channel is configured', async () => {
    apiPostMock.mockResolvedValue({ results: [] })
    renderSection()

    fireEvent.click(screen.getByRole('button', { name: 'SEND TEST NOTIFICATION' }))

    expect(await screen.findByText(/No channel is configured/)).toBeInTheDocument()
  })
})

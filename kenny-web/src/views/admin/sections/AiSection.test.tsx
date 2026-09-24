import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AdminRow } from '../types'

const { apiGetMock, apiPutMock } = vi.hoisted(() => ({ apiGetMock: vi.fn(), apiPutMock: vi.fn() }))
vi.mock('../../../api/client', () => ({
  api: { get: apiGetMock, post: vi.fn(), put: apiPutMock, patch: vi.fn(), delete: vi.fn() },
  ApiError: class ApiError extends Error {},
}))

const { default: AiSection } = await import('./AiSection')

function row(key: string, label: string, value: boolean): AdminRow {
  return {
    key,
    label,
    help: '',
    value,
    source: 'default',
    editable: true,
    type: 'bool',
    choices: null,
    min: null,
    max: null,
    isSet: true,
    lifecycle: 'live',
    pendingRestart: false,
  }
}

function renderSection(masterOn: boolean) {
  apiGetMock.mockResolvedValue({
    enabled: masterOn,
    configured: true,
    source: 'db',
    features: { ask: masterOn, recommend: masterOn },
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <AiSection rows={[row('KENNY_AI_ENABLED', 'AI features', masterOn), row('KENNY_AI_ASK_ENABLED', 'Ask kenny', true)]} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  apiGetMock.mockReset()
  apiPutMock.mockReset()
  apiPutMock.mockResolvedValue({})
})

/** One switch turns every AI feature off at once, and back on. */
describe('AiSection — the master switch', () => {
  it('switches all AI off with one click', async () => {
    renderSection(true)
    const toggle = screen.getByRole('switch', { name: 'AI features' })
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(toggle)
    await waitFor(() => expect(apiPutMock).toHaveBeenCalledWith('/api/settings/KENNY_AI_ENABLED', { value: false }))
  })

  it('switches it back on, and says nothing runs while it is off', async () => {
    renderSection(false)
    expect(await screen.findByText(/AI is switched off/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('switch', { name: 'AI features' }))
    await waitFor(() => expect(apiPutMock).toHaveBeenCalledWith('/api/settings/KENNY_AI_ENABLED', { value: true }))
  })

  it('is not listed a second time among the per-feature settings', () => {
    renderSection(true)
    expect(screen.getAllByText(/AI features/i)).toHaveLength(1)
    expect(screen.getByText('Ask kenny')).toBeInTheDocument()
  })
})

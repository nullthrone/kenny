import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const { streamMock } = vi.hoisted(() => ({ streamMock: vi.fn() }))
vi.mock('../../api/sse', () => ({ streamChatEvents: streamMock }))
vi.mock('../../api/client', () => ({
  api: { get: vi.fn(() => Promise.resolve({})), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))

const { default: RecommendationBlock } = await import('./RecommendationBlock')

/** A switched-off feature leaves nothing behind — no block saying what is missing. */
describe('RecommendationBlock', () => {
  it('renders nothing and opens no stream while recommendations are off', () => {
    const client = new QueryClient()
    const { container } = render(
      <QueryClientProvider client={client}>
        <RecommendationBlock agentId="pc" sectionName="disk" aiEnabled={false} onRemediate={() => {}} />
      </QueryClientProvider>,
    )
    expect(container).toBeEmptyDOMElement()
    expect(streamMock).not.toHaveBeenCalled()
  })
})

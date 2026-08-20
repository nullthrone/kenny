import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ChatEvent } from '../../api/types'
import { KENNY_REPLY } from '../../test/markdownSamples'

const { streamChatEvents } = vi.hoisted(() => ({ streamChatEvents: vi.fn() }))
vi.mock('../../api/sse', () => ({ streamChatEvents }))

const TicketChat = (await import('./TicketChat')).default

/** Deltas that split markdown tokens mid-way — the case the whole-buffer rule exists for. */
function deltas(text: string, size: number): ChatEvent[] {
  const out: ChatEvent[] = []
  for (let i = 0; i < text.length; i += size) {
    out.push({ type: 'text_delta', text: text.slice(i, i + size) } as ChatEvent)
  }
  return out
}

function renderChat(events: ChatEvent[]) {
  streamChatEvents.mockImplementation(async function* () {
    for (const event of events) yield event
  })
  return render(
    <TicketChat
      ticketId="t-42"
      discordThread={false}
      assistantAvailable
      blockedOnApproval={false}
      openApproval={undefined}
      onNeedsApprovalRefetch={() => {}}
      onTurnDone={() => {}}
      onDecided={() => {}}
    />,
  )
}

describe('TicketChat live stream', () => {
  it('renders the in-flight reply as markdown, even when deltas split tokens', async () => {
    // No `done`: the panel clears on `done`, and this asserts what the operator
    // sees while the turn is still streaming.
    const { container } = renderChat(deltas(KENNY_REPLY, 3))

    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'was tun?' } })
    fireEvent.click(screen.getByLabelText('Send'))

    await waitFor(() => {
      expect(container.querySelectorAll('ul li')).toHaveLength(2)
    })
    expect(container.querySelectorAll('ol li')).toHaveLength(2)
    expect(container.querySelector('strong')).not.toBeNull()
    expect(container.textContent).not.toContain('**')
  })
})

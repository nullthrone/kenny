import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api/client', () => ({
  api: { get: vi.fn(() => Promise.resolve({ agents: [{ agent_id: 'pc-kid' }] })) },
}))

const { default: TicketDraftCard } = await import('./TicketDraftCard')

afterEach(cleanup)

const DRAFT = {
  itemId: 'item-3',
  title: 'Windows updates fail',
  summary: 'Error 0x80070422 since Tuesday.',
  agentId: 'pc-kid',
}

describe('TicketDraftCard', () => {
  it('opens the ticket with the operator’s edits, not the wording kenny proposed', async () => {
    const onCreate = vi.fn(() => Promise.resolve())
    render(
      <TicketDraftCard
        {...DRAFT}
        resolution="pending"
        onCreate={onCreate}
        onDismiss={vi.fn()}
      />,
    )

    fireEvent.change(screen.getByLabelText('Title'), {
      target: { value: 'Update service disabled on pc-kid' },
    })
    fireEvent.change(screen.getByLabelText('What should kenny do?'), {
      target: { value: 'wuauserv is set to Disabled. Re-enable it and re-run updates.' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'OPEN TICKET' }))

    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1))
    expect(onCreate).toHaveBeenCalledWith('item-3', {
      title: 'Update service disabled on pc-kid',
      summary: 'wuauserv is set to Disabled. Re-enable it and re-run updates.',
      agentId: 'pc-kid',
      startImmediately: true,
    })
  })

  it('cannot open a ticket with an emptied title or description', () => {
    render(
      <TicketDraftCard {...DRAFT} resolution="pending" onCreate={vi.fn()} onDismiss={vi.fn()} />,
    )
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: '  ' } })
    expect(screen.getByRole('button', { name: 'OPEN TICKET' })).toBeDisabled()
  })

  it('discards without creating anything', () => {
    const onCreate = vi.fn()
    const onDismiss = vi.fn()
    render(
      <TicketDraftCard {...DRAFT} resolution="pending" onCreate={onCreate} onDismiss={onDismiss} />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'DISCARD' }))
    expect(onDismiss).toHaveBeenCalledWith('item-3')
    expect(onCreate).not.toHaveBeenCalled()
  })

  it('stops being a form once it has opened a ticket, and links to it', () => {
    render(
      <TicketDraftCard
        {...DRAFT}
        resolution="created"
        ticketId="tk-9"
        ticketNumber={82}
        onCreate={vi.fn()}
        onDismiss={vi.fn()}
      />,
    )
    expect(screen.queryByRole('button', { name: 'OPEN TICKET' })).toBeNull()
    expect(screen.getByRole('link', { name: 'ticket #82' })).toHaveAttribute(
      'href',
      '#/inbox/ticket/tk-9',
    )
  })
})

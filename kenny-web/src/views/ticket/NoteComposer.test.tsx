import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const { apiPostMock } = vi.hoisted(() => ({
  apiPostMock: vi.fn((_url: string, _body: unknown) => Promise.resolve({})),
}))
vi.mock('../../api/client', () => ({
  api: { post: (url: string, body: unknown) => apiPostMock(url, body) },
}))

const { default: NoteComposer } = await import('./NoteComposer')
const { writeEnterToSend } = await import('../../preferences')

function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <NoteComposer ticketId="t-42" onPosted={() => {}} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  apiPostMock.mockClear()
  writeEnterToSend(false)
})

describe('NoteComposer', () => {
  it('posts the note to the ticket', async () => {
    show()
    fireEvent.change(screen.getByLabelText('Add a note'), { target: { value: '  watched it overnight  ' } })
    fireEvent.click(screen.getByText('POST'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/tickets/t-42/note', {
        summary: 'watched it overnight',
      }),
    )
  })

  it('follows the same Enter preference as every other composer', async () => {
    // This was the one field with a rule of its own — plain Enter posted it
    // while the drawer inserted a newline. A note can be a paragraph, so the
    // shared decider (src/preferences.ts) is the correct one here too.
    show()
    const box = screen.getByLabelText('Add a note')
    fireEvent.change(box, { target: { value: 'line one' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(apiPostMock).not.toHaveBeenCalled()

    writeEnterToSend(true)
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(apiPostMock).toHaveBeenCalledTimes(1))
  })
})

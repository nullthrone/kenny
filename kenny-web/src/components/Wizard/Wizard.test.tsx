import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { apiPostMock } = vi.hoisted(() => ({ apiPostMock: vi.fn() }))
vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(),
    post: apiPostMock,
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}))

const { default: Wizard } = await import('./Wizard')

beforeEach(() => {
  apiPostMock.mockReset()
})

function goToStep2() {
  fireEvent.change(screen.getByPlaceholderText('e.g. tante-laptop'), { target: { value: 'tante-laptop' } })
  fireEvent.click(screen.getByText('NEXT'))
}

describe('Wizard', () => {
  it('keeps NEXT disabled until the machine name is a valid slug', () => {
    render(<Wizard open onClose={vi.fn()} />)
    expect(screen.getByText('NEXT')).toBeDisabled()

    fireEvent.change(screen.getByPlaceholderText('e.g. tante-laptop'), { target: { value: 'Tante Laptop' } })
    expect(screen.getByText('NEXT')).toBeDisabled()

    fireEvent.change(screen.getByPlaceholderText('e.g. tante-laptop'), { target: { value: 'tante-laptop' } })
    expect(screen.getByText('NEXT')).not.toBeDisabled()
  })

  it('walks name -> OS -> hand-over and defaults to Windows', () => {
    render(<Wizard open onClose={vi.fn()} />)
    goToStep2()
    expect(screen.getByText('WINDOWS')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('LINUX')).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(screen.getByText('LINUX'))
    expect(screen.getByText('LINUX')).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(screen.getByText('NEXT'))
    expect(screen.getByText('Hand it over')).toBeInTheDocument()
  })

  it('share-link posts {name, os} to /api/agents/share-link and shows the returned URL', async () => {
    apiPostMock.mockResolvedValue({ url: 'https://kenny.local/o/abc123', expires_at: '2026-08-18T00:00:00Z', os: 'windows', name: 'tante-laptop' })

    render(<Wizard open onClose={vi.fn()} />)
    goToStep2()
    fireEvent.click(screen.getByText('NEXT'))
    fireEvent.click(screen.getByText('Share a one-time link'))

    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith('/api/agents/share-link', { name: 'tante-laptop', os: 'windows' }))
    expect(await screen.findByDisplayValue('https://kenny.local/o/abc123')).toBeInTheDocument()
  })

  it('is an ordinary dismissible modal — closes on Escape, unlike the chat confirm gate', () => {
    const onClose = vi.fn()
    render(<Wizard open onClose={onClose} />)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  it('renders nothing when closed', () => {
    render(<Wizard open={false} onClose={vi.fn()} />)
    expect(screen.queryByText('ADD A PC')).not.toBeInTheDocument()
  })
})

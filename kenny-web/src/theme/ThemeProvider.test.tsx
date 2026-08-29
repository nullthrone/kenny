import { render, screen, waitFor } from '@testing-library/react'
import { act } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { apiPutMock } = vi.hoisted(() => ({ apiPutMock: vi.fn() }))
vi.mock('../api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: apiPutMock, patch: vi.fn(), delete: vi.fn() },
}))

const { ThemeProvider, useTheme } = await import('./ThemeProvider')
const { THEME_STORAGE_KEY } = await import('./theme')

function Probe() {
  const { theme, toggleTheme, setTheme, adoptTheme } = useTheme()
  return (
    <>
      <span data-testid="theme">{theme}</span>
      <button onClick={toggleTheme}>toggle</button>
      <button onClick={() => setTheme('dark')}>set dark</button>
      <button onClick={() => adoptTheme('dark')}>adopt dark</button>
    </>
  )
}

function renderProbe() {
  return render(
    <ThemeProvider>
      <Probe />
    </ThemeProvider>,
  )
}

beforeEach(() => {
  apiPutMock.mockReset()
  apiPutMock.mockResolvedValue({ theme: 'dark', stored: true })
  localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
})

/**
 * `PUT /api/me/theme` shipped with the redesign and had no caller for the whole
 * of 2.2.0, so the theme never followed an operator to another browser even
 * though the docs said it did. localStorage stays the fast path — the inline
 * boot script paints from it before React mounts — and this is the durable copy
 * on top.
 */
describe('ThemeProvider — persisting to the account', () => {
  it('persists a toggle to the account as well as the browser', async () => {
    renderProbe()

    act(() => screen.getByText('toggle').click())

    expect(screen.getByTestId('theme')).toHaveTextContent('dark')
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    await waitFor(() => expect(apiPutMock).toHaveBeenCalledWith('/api/me/theme', { theme: 'dark' }))
  })

  it('persists an explicit setTheme the same way', async () => {
    renderProbe()

    act(() => screen.getByText('set dark').click())

    await waitFor(() => expect(apiPutMock).toHaveBeenCalledWith('/api/me/theme', { theme: 'dark' }))
  })

  /**
   * The repaint has already happened by the time the request goes out. Failing it
   * costs the operator nothing this session — only that the choice will not follow
   * them — so it must never undo or block the switch they just made.
   */
  it('keeps the switch when the account write is rejected', async () => {
    apiPutMock.mockRejectedValue(new Error('403'))
    renderProbe()

    act(() => screen.getByText('toggle').click())

    await waitFor(() => expect(apiPutMock).toHaveBeenCalled())
    expect(screen.getByTestId('theme')).toHaveTextContent('dark')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
  })

  /**
   * `adoptTheme` is the server's value arriving, not a choice being made. Writing
   * it back would echo it round on every load, and would make a browser that
   * merely adopted a value indistinguishable from one where it was chosen.
   */
  it('does not write back a theme it adopted from the account', async () => {
    renderProbe()

    act(() => screen.getByText('adopt dark').click())

    expect(screen.getByTestId('theme')).toHaveTextContent('dark')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(apiPutMock).not.toHaveBeenCalled()
  })
})

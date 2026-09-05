import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { HOSTILE, KENNY_REPLY } from '../../test/markdownSamples'

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }))
vi.mock('../../api/client', () => ({
  api: { get: apiGetMock, post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))

const { default: AboutModal } = await import('./AboutModal')

const ABOUT = { server_version: '2.2.0', protocol_version: '0.17', repo: 'nullthrone/kenny' }
const BINARY = { version: '2.1.0', available: true }
const RELEASES = [
  { version: '2.2.0', tag: 'v2.2.0', name: 'kenny 2.2.0', published_at: '2026-08-20T10:00:00Z', body: 'Newest release.', html_url: null, prerelease: false },
  { version: '2.1.0', tag: 'v2.1.0', name: 'kenny 2.1.0', published_at: '2026-08-06T10:00:00Z', body: 'Older release.', html_url: null, prerelease: false },
]

/** Route table per test; a value that is an Error is served as a rejection. */
function mockApi(over: Record<string, unknown> = {}) {
  const routes: Record<string, unknown> = {
    '/api/about': ABOUT,
    '/api/agent-binary': BINARY,
    '/api/changelog': { repo: 'nullthrone/kenny', ok: true, releases: RELEASES },
    '/api/me': { user_id: 'u1', username: 'scoped', role: 'user' },
    ...over,
  }
  apiGetMock.mockImplementation((path: string) => {
    const hit = routes[path]
    return hit instanceof Error ? Promise.reject(hit) : Promise.resolve(hit)
  })
}

/**
 * A fresh QueryClient per test is mandatory, not hygiene: `useAbout` sets
 * `staleTime: Infinity`, so a shared client would leak the first test's server
 * version into every later one.
 */
function renderModal(open = true) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AboutModal open={open} onClose={vi.fn()} />
    </QueryClientProvider>,
  )
}

/**
 * The value cell beside a KeyValueRow label. Walks label → labelWrap → row,
 * which is where `.kc-value` is a sibling (see KeyValueRow).
 */
function valueOf(label: string): string {
  const row = screen.getByText(label).parentElement?.parentElement
  return row?.querySelector('.kc-value')?.textContent ?? ''
}

beforeEach(() => {
  apiGetMock.mockReset()
})

describe('AboutModal', () => {
  it('shows the four identity rows from the wire', async () => {
    mockApi()
    renderModal()
    await waitFor(() => expect(valueOf('server version')).toBe('2.2.0'))
    expect(valueOf('protocol version')).toBe('0.17')
    expect(valueOf('staged agent version')).toBe('2.1.0')
    expect(screen.getByRole('link', { name: /nullthrone\/kenny/ })).toHaveAttribute('href', 'https://github.com/nullthrone/kenny')
  })

  /**
   * The reported bug from the operator's side: a server on 2.2.x showing a
   * 2.1.0 agent. CI stamps one git tag into both `KENNY_SERVER_VERSION` and
   * `KENNY_AGENT_VERSION`, so the dialog names the anomaly instead of leaving
   * the operator to notice two numbers do not match.
   */
  it('flags a staged agent version that lags the server', async () => {
    mockApi()
    renderModal()
    await waitFor(() => expect(screen.getByText(/expected 2\.2\.0/)).toBeInTheDocument())
  })

  it('says nothing when the staged agent matches the server', async () => {
    mockApi({ '/api/agent-binary': { ...BINARY, version: '2.2.0' } })
    renderModal()
    await waitFor(() => expect(valueOf('staged agent version')).toBe('2.2.0'))
    expect(screen.queryByText(/^expected /)).not.toBeInTheDocument()
  })

  /**
   * A dev-channel server (`0.0.0-dev`) and a stable staged agent legitimately
   * differ. Claiming a lag there would cry wolf on every dev build.
   */
  it('claims no lag when either side is not a stable release', async () => {
    mockApi({ '/api/about': { ...ABOUT, server_version: '0.0.0-dev' } })
    renderModal()
    await waitFor(() => expect(valueOf('staged agent version')).toBe('2.1.0'))
    expect(screen.queryByText(/^expected /)).not.toBeInTheDocument()
  })

  it('reports why the last refresh failed, ahead of any version hint', async () => {
    mockApi({
      '/api/agent-binary': {
        ...BINARY,
        last_check: {
          ok: false,
          message: 'GitHub API 401 (token expired)',
          checked_at: '2026-08-29T10:00:00Z',
          version: '2.1.0',
        },
      },
    })
    renderModal()
    await waitFor(() =>
      expect(screen.getByText(/last refresh failed — GitHub API 401/)).toBeInTheDocument(),
    )
    // the stale number stays visible: it is what a new PC would still receive
    expect(valueOf('staged agent version')).toBe('2.1.0')
  })

  // A test named 'names the missing setting when auto-fetch is switched off' stood
  // here. There is no such setting any more: releases are read anonymously
  // (ADR-0057), so there is no configuration that can switch the fetch off and
  // nothing for the row to name.

  it('offers an operator the refresh right there', async () => {
    mockApi({ '/api/me': { user_id: 'u1', username: 'op', role: 'operator' } })
    renderModal()
    expect(await screen.findByRole('button', { name: 'FETCH NOW' })).toBeInTheDocument()
  })

  it('keeps the refresh out of a scoped user reach', async () => {
    mockApi()
    renderModal()
    await waitFor(() => expect(valueOf('server version')).toBe('2.2.0'))
    expect(screen.queryByRole('button', { name: 'FETCH NOW' })).not.toBeInTheDocument()
  })

  it('fetches nothing while closed, so opening the page never reaches GitHub', () => {
    mockApi()
    renderModal(false)
    expect(apiGetMock).not.toHaveBeenCalledWith('/api/changelog')
    expect(apiGetMock).not.toHaveBeenCalledWith('/api/agent-binary')
  })

  // The two guards below pin what the legacy modal's `.catch(() => null)` and
  // `.catch(() => ({releases: []}))` encoded: a degraded read must never cost
  // the operator the versions.
  it('still shows the versions when the agent-binary read fails', async () => {
    mockApi({ '/api/agent-binary': new Error('boom') })
    renderModal()
    // Wait on the error signal, not on the value: "unknown" is also what the
    // row shows while the request is still in flight.
    await waitFor(() => expect(screen.getByText('binary status unavailable')).toBeInTheDocument())
    expect(valueOf('staged agent version')).toBe('unknown')
    await waitFor(() => expect(valueOf('server version')).toBe('2.2.0'))
    expect(screen.getByText('kenny 2.2.0')).toBeInTheDocument()
  })

  it('still shows the versions when GitHub is unreachable', async () => {
    mockApi({ '/api/changelog': new Error('offline') })
    renderModal()
    await waitFor(() => expect(screen.getByText('Could not reach GitHub for release notes.')).toBeInTheDocument())
    expect(valueOf('server version')).toBe('2.2.0')
    expect(screen.getByRole('link', { name: /view full changelog/ })).toBeInTheDocument()
  })

  it('renders and keeps the repo fallback when /api/about fails', async () => {
    mockApi({ '/api/about': new Error('nope') })
    renderModal()
    await waitFor(() => expect(screen.getByText(/Could not load server identity/)).toBeInTheDocument())
    expect(valueOf('server version')).toBe('unknown')
    // Deliberately unlike the legacy modal, which replaced the whole body with
    // one failure line. The changelog does not depend on /api/about.
    expect(screen.getByText('kenny 2.2.0')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /nullthrone\/kenny/ })).toHaveAttribute('href', 'https://github.com/nullthrone/kenny')
  })

  it('falls back to nullthrone/kenny when the server reports no repo', async () => {
    mockApi({ '/api/about': { ...ABOUT, repo: null } })
    renderModal()
    await waitFor(() =>
      expect(screen.getByRole('link', { name: /view full changelog/ })).toHaveAttribute(
        'href',
        'https://github.com/nullthrone/kenny/releases',
      ),
    )
  })

  it('preselects the running release and shows only it', async () => {
    mockApi()
    renderModal()
    const select = await screen.findByLabelText('Filter release notes by version')
    expect(select).toHaveValue('2.2.0')
    expect(screen.getByRole('option', { name: '2.2.0 (running)' })).toBeInTheDocument()
    expect(screen.getByText('kenny 2.2.0')).toBeInTheDocument()
    expect(screen.queryByText('kenny 2.1.0')).not.toBeInTheDocument()
  })

  it('defaults to all versions when no release matches the running server', async () => {
    mockApi({ '/api/about': { ...ABOUT, server_version: '2.3.0-dev' } })
    renderModal()
    const select = await screen.findByLabelText('Filter release notes by version')
    expect(select).toHaveValue('')
    expect(screen.getByText('kenny 2.2.0')).toBeInTheDocument()
    expect(screen.getByText('kenny 2.1.0')).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /running/ })).not.toBeInTheDocument()
  })

  /**
   * The bug a single `useState('')` would have: choosing "all versions" is a
   * real choice and must not snap back to the running-version default on the
   * next render.
   */
  it('keeps "all versions" selected once chosen', async () => {
    mockApi()
    renderModal()
    const select = await screen.findByLabelText('Filter release notes by version')
    fireEvent.change(select, { target: { value: '' } })
    expect(select).toHaveValue('')
    expect(screen.getByText('kenny 2.2.0')).toBeInTheDocument()
    expect(screen.getByText('kenny 2.1.0')).toBeInTheDocument()
  })

  it('filters to a single release when one is chosen', async () => {
    mockApi()
    renderModal()
    const select = await screen.findByLabelText('Filter release notes by version')
    fireEvent.change(select, { target: { value: '2.1.0' } })
    expect(screen.getByText('kenny 2.1.0')).toBeInTheDocument()
    expect(screen.queryByText('kenny 2.2.0')).not.toBeInTheDocument()
  })

  it('renders release notes as markdown, not as literal marks', async () => {
    mockApi({ '/api/changelog': { repo: 'nullthrone/kenny', ok: true, releases: [{ ...RELEASES[0], body: KENNY_REPLY }] } })
    renderModal()
    await waitFor(() => expect(document.body.querySelector('strong')).toBeInTheDocument())
    expect(document.body.querySelectorAll('li').length).toBeGreaterThan(0)
    expect(document.body.textContent).not.toContain('**')
  })

  /**
   * Release bodies are third-party text from GitHub, so the renderer's two hard
   * rules matter more here than anywhere: no raw HTML, and no images (an image
   * would make an operator's browser fetch an attacker-chosen URL).
   */
  it('never turns a hostile release body into live markup', async () => {
    mockApi({ '/api/changelog': { repo: 'nullthrone/kenny', ok: true, releases: [{ ...RELEASES[0], body: HOSTILE }] } })
    renderModal()
    await waitFor(() => expect(screen.getByText('kenny 2.2.0')).toBeInTheDocument())
    expect(document.body.querySelector('script')).toBeNull()
    expect(document.body.querySelector('img')).toBeNull()
    expect(document.body.querySelector('a[href^="javascript:"]')).toBeNull()
  })

  it('says so plainly when the repo has published nothing', async () => {
    mockApi({ '/api/changelog': { repo: 'nullthrone/kenny', ok: true, releases: [] } })
    renderModal()
    await waitFor(() => expect(screen.getByText('No release notes')).toBeInTheDocument())
    expect(screen.queryByLabelText('Filter release notes by version')).not.toBeInTheDocument()
  })

  /**
   * The reported bug from the changelog's side. This dialog used to render a
   * dead token as "No releases published on GitHub for <repo> yet" — a claim
   * about GitHub made from a request that never reached it.
   */
  it('distinguishes a failed GitHub read from an empty repo', async () => {
    mockApi({
      '/api/changelog': {
        repo: 'nullthrone/kenny',
        ok: false,
        error: 'GitHub rejected the credentials (401) — KENNY_GITHUB_TOKEN is invalid or expired.',
        releases: [],
      },
    })
    renderModal()
    await waitFor(() =>
      expect(screen.getByText(/KENNY_GITHUB_TOKEN is invalid or expired/)).toBeInTheDocument(),
    )
    expect(screen.queryByText('No release notes')).not.toBeInTheDocument()
  })

  it('labels cached notes as cached when the refresh failed', async () => {
    mockApi({
      '/api/changelog': {
        repo: 'nullthrone/kenny',
        ok: false,
        error: 'GitHub unreachable: connection refused',
        stale: true,
        fetched_at: '2026-08-29T10:00:00Z',
        releases: RELEASES,
      },
    })
    renderModal()
    await waitFor(() => expect(screen.getByText(/Showing cached notes/)).toBeInTheDocument())
    // degrading, not blanking: the notes themselves still render
    expect(screen.getByText('kenny 2.2.0')).toBeInTheDocument()
  })

  /**
   * A server older than this bundle sends no `ok`. Absent must read as success,
   * or every such deployment would show a permanent failure line.
   */
  it('treats a server that sends no ok as a success', async () => {
    mockApi({ '/api/changelog': { repo: 'nullthrone/kenny', releases: RELEASES } })
    renderModal()
    await waitFor(() => expect(screen.getByText('kenny 2.2.0')).toBeInTheDocument())
    expect(screen.queryByText(/Showing cached notes/)).not.toBeInTheDocument()
    expect(screen.queryByText('Release notes unavailable')).not.toBeInTheDocument()
  })

  it('closes on Escape', async () => {
    mockApi()
    const onClose = vi.fn()
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <AboutModal open onClose={onClose} />
      </QueryClientProvider>,
    )
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })
})

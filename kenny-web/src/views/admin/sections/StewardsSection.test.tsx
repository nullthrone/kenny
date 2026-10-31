import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  StewardAuthorization,
  StewardParamsResponse,
  StewardRun,
  Steward,
  StewardsResponse,
} from '../types'

const { apiGetMock, apiPostMock, apiPutMock, apiDeleteMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  apiPostMock: vi.fn(),
  apiPutMock: vi.fn(),
  apiDeleteMock: vi.fn(),
}))
vi.mock('../../../api/client', () => ({
  api: { get: apiGetMock, post: apiPostMock, put: apiPutMock, patch: vi.fn(), delete: apiDeleteMock },
  ApiError: class ApiError extends Error {
    status: number
    constructor(message: string, status = 0) {
      super(message)
      this.status = status
    }
  },
}))

const { ApiError } = await import('../../../api/client')
const { default: StewardsSection } = await import('./StewardsSection')

const HASH = 'a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90'

function run(over: Partial<StewardRun> = {}): StewardRun {
  return {
    id: 'run-1',
    steward_id: 'patch',
    spec_hash: 'f'.repeat(64),
    trigger: 'schedule',
    subject: 'study-pc',
    host_id: 'study-pc',
    mode: 'shadow',
    status: 'completed',
    verdict: 'acted',
    summary: 'Updated 7zip; the second winget_list shows no update pending.',
    input_tokens: 1200,
    output_tokens: 300,
    cache_read_tokens: 50,
    cache_creation_tokens: 10,
    ticket_id: null,
    error: null,
    actions: [
      {
        tool: 'winget_update',
        args: { id: '7zip.7zip', timeout_s: 600 },
        agent_id: 'study-pc',
        tool_class: 'standard_change',
        ok: true,
        authorization_id: 'auth-77',
      },
    ],
    recommendations: [
      {
        tool: 'winget_install',
        args: { id: 'Mozilla.Firefox' },
        agent_id: 'study-pc',
        tool_class: 'normal_change',
      },
    ],
    started_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
    finished_at: new Date(Date.now() - 2 * 3600_000 + 60_000).toISOString(),
    params: null,
    effective_hash: HASH,
    ...over,
  }
}

function steward(over: Partial<Steward> = {}): Steward {
  return {
    id: 'patch',
    title: 'Package updates',
    description: 'Updates allowlisted packages in a maintenance window.',
    trigger: { kind: 'schedule', event: null },
    tools: ['steward_verdict', 'shell_exec', 'winget_install', 'winget_list', 'winget_update'],
    tool_classes: {
      steward_verdict: 'standard_change',
      shell_exec: 'normal_change',
      winget_install: 'normal_change',
      winget_list: 'read_only',
      winget_update: 'standard_change',
    },
    verdict_tool: 'steward_verdict',
    budget: { max_iterations: 12 },
    constraints: [
      { tool: 'winget_update', arg: 'id', param: 'packages' },
      { tool: 'winget_install', arg: 'id', allowed: ['Mozilla.Firefox'] },
    ],
    timeouts: [{ tool: 'winget_update', max_s: 600 }],
    sensitive_ok: false,
    default_mode: 'shadow',
    version: 1,
    spec_hash: 'f'.repeat(64),
    mode: 'shadow',
    params: { hosts: ['study-pc'], packages: ['7zip.7zip'], require_idle: true },
    effective_hash: HASH,
    act_bound: false,
    latest_run: run(),
    ...over,
  }
}

const TRIAGE: Steward = steward({
  id: 'triage',
  title: 'Ticket triage',
  description: 'Investigates a new ticket before you open it.',
  trigger: { kind: 'event', event: 'ticket_created' },
  tools: ['ticket_triage_verdict', 'steward_health'],
  tool_classes: { ticket_triage_verdict: 'standard_change', steward_health: 'read_only' },
  verdict_tool: 'ticket_triage_verdict',
  constraints: [],
  timeouts: [],
  mode: 'act',
  params: {},
  act_bound: null,
  effective_hash: 'beef'.repeat(16),
  latest_run: null,
})

const PARAMS: StewardParamsResponse = {
  steward_id: 'patch',
  declared: ['hosts', 'packages', 'require_idle', 'window'],
  params: {
    hosts: ['study-pc'],
    packages: ['7zip.7zip'],
    require_idle: true,
    window: { days: ['sat'], start: '02:00', end: '05:00', tz: 'Europe/Berlin' },
  },
  effective_hash: HASH,
}

function auth(over: Partial<StewardAuthorization> = {}): StewardAuthorization {
  return {
    id: 'auth-77',
    steward_id: 'patch',
    effective_hash: HASH,
    tool: 'winget_install',
    scope: ['study-pc'],
    max_attempts_per_day: 2,
    expires_at: new Date(Date.now() + 30 * 86400_000).toISOString(),
    granted_by: 'thomas',
    granted_at: '2026-10-01T00:00:00Z',
    revoked_at: null,
    revoked_by: null,
    voided_at: null,
    voided_by: null,
    note: '',
    status: 'live',
    attempts_last_24h: { 'study-pc': 1 },
    ...over,
  }
}

interface Setup {
  stewards?: Steward[]
  enabled?: boolean
  authorizations?: StewardAuthorization[]
  runs?: StewardRun[]
  routes?: Record<string, unknown>
}

function mockApi({ stewards = [steward(), TRIAGE], enabled = true, authorizations = [auth()], runs = [run()], routes = {} }: Setup = {}) {
  const table: Record<string, unknown> = {
    '/api/stewards': { enabled, stewards } satisfies StewardsResponse,
    '/api/stewards/runs': { runs },
    '/api/stewards/runs/run-1': runs[0] ?? run(),
    '/api/stewards/patch/params': PARAMS,
    '/api/stewards/triage/params': { steward_id: 'triage', declared: [], params: {}, effective_hash: TRIAGE.effective_hash },
    '/api/stewards/patch/authorizations': { steward_id: 'patch', effective_hash: HASH, authorizations },
    '/api/stewards/triage/authorizations': { steward_id: 'triage', effective_hash: TRIAGE.effective_hash, authorizations: [] },
    '/api/fleet': {
      overall: 'ok',
      agents: [{ agent_id: 'papa-pc' }, { agent_id: 'study-pc' }],
    },
    ...routes,
  }
  apiGetMock.mockImplementation((path: string) => {
    const hit = table[path.split('?')[0]]
    if (hit === undefined) return Promise.reject(new ApiError(`${path} -> 404`, 404))
    return hit instanceof Error ? Promise.reject(hit) : Promise.resolve(hit)
  })
}

function renderSection(path: string, canManage: boolean) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/admin/:section" element={<StewardsSection canManage={canManage} />} />
          <Route path="/admin/:section/:detail" element={<StewardsSection canManage={canManage} />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  for (const m of [apiGetMock, apiPostMock, apiPutMock, apiDeleteMock]) m.mockReset()
  vi.restoreAllMocks()
})

describe('StewardsSection — the list', () => {
  it('shows each steward with its trigger, mode, short hash and latest run', async () => {
    mockApi()
    renderSection('/admin/stewards', false)

    expect(await screen.findByRole('link', { name: 'Package updates' })).toHaveAttribute('href', '/admin/stewards/patch')
    expect(screen.getByText('Updates allowlisted packages in a maintenance window.')).toBeInTheDocument()
    expect(screen.getByText('trigger: on a schedule')).toBeInTheDocument()
    expect(screen.getByText('trigger: a new ticket')).toBeInTheDocument()
    // The short form only: the whole fingerprint is never printed.
    expect(screen.getAllByText('a1b2c3d4').length).toBeGreaterThan(0)
    expect(screen.queryByText(HASH)).not.toBeInTheDocument()
    expect(screen.getByText('SHADOW')).toBeInTheDocument()
    expect(screen.getByText('ACT')).toBeInTheDocument()
    expect(screen.getByText('COMPLETED')).toBeInTheDocument()
    expect(screen.getByText(/last run 2h ago · acted/)).toBeInTheDocument()
    expect(screen.getByText('no runs yet')).toBeInTheDocument()
  })

  it('says "act unbound" for an act that no longer matches the effective hash', async () => {
    mockApi({ stewards: [steward({ mode: 'act', act_bound: false })] })
    renderSection('/admin/stewards', false)

    expect(await screen.findByText('ACT UNBOUND')).toBeInTheDocument()
  })

  it('does not call an act-bound steward, or triage (no binding), unbound', async () => {
    mockApi({ stewards: [steward({ mode: 'act', act_bound: true }), TRIAGE] })
    renderSection('/admin/stewards', false)

    await screen.findByRole('link', { name: 'Package updates' })
    expect(screen.queryByText('ACT UNBOUND')).not.toBeInTheDocument()
  })

  it('warns when the global switch is off', async () => {
    mockApi({ enabled: false })
    renderSection('/admin/stewards', false)

    expect(await screen.findByText(/switched off for the whole install/)).toBeInTheDocument()
  })

  it('reports an unconfigured server instead of an empty list', async () => {
    apiGetMock.mockRejectedValue(new ApiError('stewards not configured', 503))
    renderSection('/admin/stewards', false)

    expect(await screen.findByText('Stewards are not configured')).toBeInTheDocument()
  })

  it('says so for a steward id that is not in the catalog', async () => {
    mockApi()
    renderSection('/admin/stewards/nope', false)

    expect(await screen.findByText('Unknown steward')).toBeInTheDocument()
  })
})

describe('StewardsSection — the detail', () => {
  it('shows tools with their tiers, the constraints by kind, and the timeouts', async () => {
    mockApi()
    renderSection('/admin/stewards/patch', false)

    const tools = await screen.findByRole('list', { name: 'Tools of Package updates' })
    const updateRow = within(tools).getByText('winget_update').closest('li') as HTMLElement
    expect(within(updateRow).getByText('STANDARD CHANGE')).toBeInTheDocument()
    const installRow = within(tools).getByText('winget_install').closest('li') as HTMLElement
    expect(within(installRow).getByText('NORMAL CHANGE')).toBeInTheDocument()
    const listRow = within(tools).getByText('winget_list').closest('li') as HTMLElement
    expect(within(listRow).getByText('READ-ONLY')).toBeInTheDocument()

    const constraints = screen.getByRole('list', { name: 'Argument constraints' })
    expect(within(constraints).getByText('PARAM')).toBeInTheDocument()
    expect(within(constraints).getByText(/packages — currently 7zip\.7zip/)).toBeInTheDocument()
    expect(within(constraints).getByText('LITERAL')).toBeInTheDocument()
    expect(within(constraints).getByText('Mozilla.Firefox')).toBeInTheDocument()
    expect(screen.getByText(/at most 600 s/)).toBeInTheDocument()
  })

  it('keeps every write control away from an operator, and still lets them read', async () => {
    mockApi()
    renderSection('/admin/stewards/patch', false)

    await screen.findByRole('list', { name: 'Tools of Package updates' })
    // Read-only parameters.
    expect(await screen.findByText(/Editing parameters needs a superuser/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'SAVE PARAMETERS' })).not.toBeInTheDocument()
    // Mode buttons are drawn but cannot be used.
    expect(screen.getByRole('button', { name: 'ACT' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'OFF' })).toBeDisabled()
    // The authorizations are readable, without grant or revoke.
    expect(await screen.findByRole('region', { name: 'Standing authorizations' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /REVOKE/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'GRANT AUTHORIZATION' })).not.toBeInTheDocument()
    // But an operator may start a preview.
    expect(screen.getByRole('button', { name: 'RUN PREVIEW' })).toBeInTheDocument()
  })
})

describe('StewardsSection — the mode switch', () => {
  it('sends the effective hash that is displayed when the superuser confirms act', async () => {
    mockApi()
    apiPutMock.mockResolvedValue({ steward_id: 'patch', mode: 'act', requested: 'act' })
    renderSection('/admin/stewards/patch', true)

    fireEvent.click(await screen.findByRole('button', { name: 'ACT' }))
    // The confirmation names the version about to be bound.
    const confirm = screen.getByRole('group', { name: 'Confirm act' })
    expect(within(confirm).getByText('a1b2c3d4')).toBeInTheDocument()
    expect(apiPutMock).not.toHaveBeenCalled()
    fireEvent.click(within(confirm).getByRole('button', { name: 'CONFIRM ACT' }))

    await waitFor(() =>
      expect(apiPutMock).toHaveBeenCalledWith('/api/stewards/patch/mode', { mode: 'act', effective_hash: HASH }),
    )
    expect(await screen.findByText('Mode is now act.')).toBeInTheDocument()
  })

  it('needs no hash to go to off or shadow', async () => {
    mockApi({ stewards: [steward({ mode: 'act', act_bound: true })] })
    apiPutMock.mockResolvedValue({ steward_id: 'patch', mode: 'off', requested: 'off' })
    renderSection('/admin/stewards/patch', true)

    fireEvent.click(await screen.findByRole('button', { name: 'OFF' }))

    await waitFor(() => expect(apiPutMock).toHaveBeenCalledWith('/api/stewards/patch/mode', { mode: 'off' }))
  })

  it('offers act again when the stored act is unbound', async () => {
    mockApi({ stewards: [steward({ mode: 'act', act_bound: false })] })
    renderSection('/admin/stewards/patch', true)

    expect(await screen.findByRole('button', { name: 'ACT' })).toBeEnabled()
  })

  it('explains a 409 as "the steward changed since you loaded it" and offers a reload', async () => {
    mockApi()
    apiPutMock.mockRejectedValue(new ApiError('steward patch has changed since it was shown', 409))
    renderSection('/admin/stewards/patch', true)

    fireEvent.click(await screen.findByRole('button', { name: 'ACT' }))
    fireEvent.click(screen.getByRole('button', { name: 'CONFIRM ACT' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The steward changed since you loaded it — reload and review it, then try again.')
    const before = apiGetMock.mock.calls.length
    fireEvent.click(within(alert).getByRole('button', { name: 'RELOAD' }))
    await waitFor(() => expect(apiGetMock.mock.calls.length).toBeGreaterThan(before))
  })

  it('explains a 403 as "sign in to the dashboard as a superuser"', async () => {
    mockApi()
    apiPutMock.mockRejectedValue(new ApiError('this needs a person at a browser', 403))
    renderSection('/admin/stewards/patch', true)

    fireEvent.click(await screen.findByRole('button', { name: 'OFF' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/Sign in to the dashboard as a superuser/)
  })

  it('says so when the mode in force is not the one chosen (triage with no AI configured)', async () => {
    mockApi()
    apiPutMock.mockResolvedValue({ steward_id: 'triage', mode: 'off', requested: 'shadow' })
    renderSection('/admin/stewards/triage', true)

    fireEvent.click(await screen.findByRole('button', { name: 'SHADOW' }))

    expect(await screen.findByText(/You chose shadow, but the steward is running as off/)).toBeInTheDocument()
  })
})

describe('StewardsSection — the parameters editor', () => {
  it('loads the stored values and warns that saving drops act back to shadow', async () => {
    mockApi()
    renderSection('/admin/stewards/patch', true)

    const form = await screen.findByRole('form', { name: 'Parameters of Package updates' })
    expect(within(form).getByRole('checkbox', { name: 'Sat' })).toBeChecked()
    expect(within(form).getByRole('checkbox', { name: 'Mon' })).not.toBeChecked()
    expect(within(form).getByLabelText('START')).toHaveValue('02:00')
    expect(within(form).getByLabelText('TIME ZONE')).toHaveValue('Europe/Berlin')
    expect(await within(form).findByRole('checkbox', { name: 'study-pc' })).toBeChecked()
    expect(within(form).getByRole('checkbox', { name: 'papa-pc' })).not.toBeChecked()
    expect(within(form).getByLabelText('ALLOWED PACKAGE IDS')).toHaveValue('7zip.7zip')
    expect(within(form).getByRole('checkbox', { name: /Only when nobody is signed in/ })).toBeChecked()
    expect(within(form).getByRole('note')).toHaveTextContent(/drops this steward back to shadow/)
  })

  it('saves the edited values and reports the demotion the server made', async () => {
    mockApi()
    apiPutMock.mockResolvedValue({
      steward_id: 'patch',
      params: {},
      effective_hash: 'c'.repeat(64),
      mode: 'shadow',
      voided: 2,
    })
    renderSection('/admin/stewards/patch', true)

    const form = await screen.findByRole('form', { name: 'Parameters of Package updates' })
    fireEvent.click(within(form).getByRole('checkbox', { name: 'Mon' }))
    fireEvent.click(await within(form).findByRole('checkbox', { name: 'papa-pc' }))
    fireEvent.change(within(form).getByLabelText('ALLOWED PACKAGE IDS'), {
      target: { value: 'Mozilla.Firefox\n7zip.7zip, 7zip.7zip' },
    })
    fireEvent.click(within(form).getByRole('checkbox', { name: /Only when nobody is signed in/ }))
    fireEvent.click(within(form).getByRole('button', { name: 'SAVE PARAMETERS' }))

    await waitFor(() =>
      expect(apiPutMock).toHaveBeenCalledWith('/api/stewards/patch/params', {
        params: {
          hosts: ['study-pc', 'papa-pc'],
          packages: ['7zip.7zip', 'Mozilla.Firefox'],
          require_idle: false,
          window: { days: ['sat', 'mon'], start: '02:00', end: '05:00', tz: 'Europe/Berlin' },
        },
      }),
    )
    expect(await screen.findByText('Saved. The steward is in shadow. 2 standing authorizations voided.')).toBeInTheDocument()
  })

  it('shows a 403 on save as a request to sign in as a superuser', async () => {
    mockApi()
    apiPutMock.mockRejectedValue(new ApiError('forbidden', 403))
    renderSection('/admin/stewards/patch', true)

    const form = await screen.findByRole('form', { name: 'Parameters of Package updates' })
    fireEvent.click(within(form).getByRole('button', { name: 'SAVE PARAMETERS' }))

    expect(await within(form).findByRole('alert')).toHaveTextContent(/Sign in to the dashboard as a superuser/)
  })

  it('has no editor for a steward that takes no parameters', async () => {
    mockApi()
    renderSection('/admin/stewards/triage', true)

    expect(await screen.findByText('This steward takes no parameters.')).toBeInTheDocument()
  })
})

describe('StewardsSection — standing authorizations', () => {
  it('lists tool, scope, attempts per day, expiry, grantor and state', async () => {
    mockApi({
      authorizations: [
        auth(),
        auth({ id: 'a2', status: 'revoked', tool: 'winget_install', scope: ['papa-pc'], granted_by: 'mia' }),
        auth({ id: 'a3', status: 'voided', scope: 'server' }),
        auth({ id: 'a4', status: 'expired', attempts_last_24h: {} }),
      ],
    })
    renderSection('/admin/stewards/patch', false)

    const region = await screen.findByRole('region', { name: 'Standing authorizations' })
    expect(within(region).getByText('LIVE')).toBeInTheDocument()
    expect(within(region).getByText('REVOKED')).toBeInTheDocument()
    expect(within(region).getByText('VOIDED')).toBeInTheDocument()
    expect(within(region).getByText('EXPIRED')).toBeInTheDocument()
    expect(within(region).getByText('the server')).toBeInTheDocument()
    expect(within(region).getByText('mia')).toBeInTheDocument()
    expect(within(region).getAllByText('spent in 24 h: study-pc 1').length).toBeGreaterThan(0)
    expect(within(region).getAllByRole('columnheader').map((c) => c.textContent)).toEqual([
      'TOOL',
      'SCOPE',
      'ATTEMPTS / DAY',
      'EXPIRES',
      'GRANTED BY',
      'STATE',
    ])
  })

  it('offers only the normal_change tools of this steward in the grant form', async () => {
    mockApi()
    renderSection('/admin/stewards/patch', true)

    const form = await screen.findByRole('form', { name: 'Grant a standing authorization' })
    const options = within(within(form).getByLabelText('TOOL')).getAllByRole('option').map((o) => o.textContent)
    // winget_update is a standard_change, winget_list read-only; shell_exec can never be authorized.
    expect(options).toEqual(['winget_install'])
  })

  it('has no form for a steward without an authorizable normal_change', async () => {
    mockApi()
    renderSection('/admin/stewards/triage', true)

    expect(await screen.findByText(/names no normal-change tool that can be authorized/)).toBeInTheDocument()
    expect(screen.queryByRole('form', { name: 'Grant a standing authorization' })).not.toBeInTheDocument()
  })

  it('refuses an expiry beyond 180 days before anything is sent', async () => {
    mockApi()
    renderSection('/admin/stewards/patch', true)

    const form = await screen.findByRole('form', { name: 'Grant a standing authorization' })
    fireEvent.click(await within(form).findByRole('checkbox', { name: 'study-pc' }))
    const days = within(form).getByLabelText(/EXPIRES IN/)
    const grant = within(form).getByRole('button', { name: 'GRANT AUTHORIZATION' })
    expect(grant).toBeEnabled()

    fireEvent.change(days, { target: { value: '181' } })
    expect(grant).toBeDisabled()
    expect(within(form).getByRole('alert')).toHaveTextContent(/1 to 180 days/)

    fireEvent.change(days, { target: { value: '0' } })
    expect(grant).toBeDisabled()

    fireEvent.change(days, { target: { value: '180' } })
    expect(grant).toBeEnabled()
    fireEvent.submit(form)
    await waitFor(() => expect(apiPostMock).toHaveBeenCalledTimes(1))
    const [path, body] = apiPostMock.mock.calls[0]
    expect(path).toBe('/api/stewards/patch/authorizations')
    const outDays = (new Date(body.expires_at).getTime() - Date.now()) / 86400_000
    expect(outDays).toBeGreaterThan(179.9)
    expect(outDays).toBeLessThanOrEqual(180)
  })

  it('grants against the displayed hash, with an explicit host scope', async () => {
    mockApi()
    apiPostMock.mockResolvedValue(auth({ id: 'new' }))
    renderSection('/admin/stewards/patch', true)

    const form = await screen.findByRole('form', { name: 'Grant a standing authorization' })
    const grant = within(form).getByRole('button', { name: 'GRANT AUTHORIZATION' })
    // An empty scope is never "all PCs".
    expect(grant).toBeDisabled()
    fireEvent.click(await within(form).findByRole('checkbox', { name: 'study-pc' }))
    fireEvent.change(within(form).getByLabelText('ATTEMPTS PER HOST PER DAY'), { target: { value: '2' } })
    fireEvent.change(within(form).getByLabelText(/EXPIRES IN/), { target: { value: '30' } })
    fireEvent.change(within(form).getByLabelText('NOTE (OPTIONAL)'), { target: { value: 'for the October round' } })
    fireEvent.click(grant)

    await waitFor(() => expect(apiPostMock).toHaveBeenCalledTimes(1))
    const [path, body] = apiPostMock.mock.calls[0]
    expect(path).toBe('/api/stewards/patch/authorizations')
    expect(body).toMatchObject({
      tool: 'winget_install',
      scope: ['study-pc'],
      max_attempts_per_day: 2,
      note: 'for the October round',
      effective_hash: HASH,
    })
    expect(new Date(body.expires_at).getTime()).toBeGreaterThan(Date.now())
    expect(await screen.findByText(/^Granted: winget_install on study-pc for 30 days\./)).toBeInTheDocument()
  })

  it('sends the server sentinel for a server-side change', async () => {
    mockApi()
    apiPostMock.mockResolvedValue(auth({ id: 'new', scope: 'server' }))
    renderSection('/admin/stewards/patch', true)

    const form = await screen.findByRole('form', { name: 'Grant a standing authorization' })
    fireEvent.click(within(form).getByRole('radio', { name: /The server/ }))
    fireEvent.click(within(form).getByRole('button', { name: 'GRANT AUTHORIZATION' }))

    await waitFor(() => expect(apiPostMock).toHaveBeenCalledTimes(1))
    expect(apiPostMock.mock.calls[0][1]).toMatchObject({ scope: 'server' })
  })

  describe('where a tool runs (tool_targets)', () => {
    const TWO_TOOLS = steward({
      tools: ['winget_install', 'maintenance_run'],
      tool_classes: { winget_install: 'normal_change', maintenance_run: 'normal_change' },
      tool_targets: { winget_install: 'host', maintenance_run: 'server' },
      constraints: [],
      timeouts: [],
    })

    it('offers only PCs for a host tool and only the server for a server tool', async () => {
      mockApi({ stewards: [TWO_TOOLS] })
      renderSection('/admin/stewards/patch', true)

      const form = await screen.findByRole('form', { name: 'Grant a standing authorization' })
      // winget_install runs on a PC: the PC picker, no server scope.
      expect(await within(form).findByRole('checkbox', { name: 'study-pc' })).toBeInTheDocument()
      expect(within(form).getByRole('radio', { name: /These PCs/ })).toBeInTheDocument()
      expect(within(form).queryByRole('radio', { name: /The server/ })).not.toBeInTheDocument()

      fireEvent.change(within(form).getByLabelText('TOOL'), { target: { value: 'maintenance_run' } })
      expect(within(form).getByRole('radio', { name: /The server/ })).toBeChecked()
      expect(within(form).queryByRole('radio', { name: /These PCs/ })).not.toBeInTheDocument()
      expect(within(form).queryByRole('checkbox', { name: 'study-pc' })).not.toBeInTheDocument()
    })

    it('grants a server tool on the server without any PC picked', async () => {
      mockApi({ stewards: [TWO_TOOLS] })
      apiPostMock.mockResolvedValue(auth({ id: 'new', scope: 'server' }))
      renderSection('/admin/stewards/patch', true)

      const form = await screen.findByRole('form', { name: 'Grant a standing authorization' })
      fireEvent.change(within(form).getByLabelText('TOOL'), { target: { value: 'maintenance_run' } })
      fireEvent.click(within(form).getByRole('button', { name: 'GRANT AUTHORIZATION' }))

      await waitFor(() => expect(apiPostMock).toHaveBeenCalledTimes(1))
      expect(apiPostMock.mock.calls[0][1]).toMatchObject({ tool: 'maintenance_run', scope: 'server' })
    })

    it('offers both scopes when the server does not say where a tool runs', async () => {
      mockApi()
      renderSection('/admin/stewards/patch', true)

      const form = await screen.findByRole('form', { name: 'Grant a standing authorization' })
      expect(within(form).getByRole('radio', { name: /These PCs/ })).toBeInTheDocument()
      expect(within(form).getByRole('radio', { name: /The server/ })).toBeInTheDocument()
    })
  })

  it('shows a 409 on grant as a changed steward', async () => {
    mockApi()
    apiPostMock.mockRejectedValue(new ApiError('changed', 409))
    renderSection('/admin/stewards/patch', true)

    const form = await screen.findByRole('form', { name: 'Grant a standing authorization' })
    fireEvent.click(await within(form).findByRole('checkbox', { name: 'study-pc' }))
    fireEvent.click(within(form).getByRole('button', { name: 'GRANT AUTHORIZATION' }))

    expect(await within(form).findByRole('alert')).toHaveTextContent(/changed since you loaded it/)
  })

  it('revokes a live authorization after confirmation, and only a live one', async () => {
    mockApi({ authorizations: [auth(), auth({ id: 'a2', status: 'revoked' })] })
    apiDeleteMock.mockResolvedValue(auth({ status: 'revoked' }))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderSection('/admin/stewards/patch', true)

    const buttons = await screen.findAllByRole('button', { name: /^Revoke winget_install/ })
    expect(buttons).toHaveLength(1)
    fireEvent.click(buttons[0])

    await waitFor(() =>
      expect(apiDeleteMock).toHaveBeenCalledWith('/api/stewards/patch/authorizations/auth-77'),
    )
  })

  it('does not revoke when the confirmation is declined', async () => {
    mockApi()
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderSection('/admin/stewards/patch', true)

    fireEvent.click(await screen.findByRole('button', { name: /^Revoke winget_install/ }))

    expect(apiDeleteMock).not.toHaveBeenCalled()
  })
})

describe('StewardsSection — preview runs', () => {
  it('explains that a preview never changes anything', async () => {
    mockApi()
    renderSection('/admin/stewards/patch', false)

    expect(await screen.findByText('A preview never changes anything')).toBeInTheDocument()
  })

  it('needs a PC for a host-bound steward and posts it', async () => {
    mockApi()
    apiPostMock.mockResolvedValue({ run_id: 'run-1' })
    renderSection('/admin/stewards/patch', false)

    const form = await screen.findByRole('form', { name: 'Preview Package updates' })
    const button = within(form).getByRole('button', { name: 'RUN PREVIEW' })
    await waitFor(() => expect(button).toBeDisabled())
    // The steward's configured hosts, not the whole fleet.
    const picker = await within(form).findByLabelText('PC')
    expect(within(picker).getAllByRole('option').map((o) => o.textContent)).toEqual(['choose a PC…', 'study-pc'])
    fireEvent.change(picker, { target: { value: 'study-pc' } })
    expect(button).toBeEnabled()
    fireEvent.click(button)

    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/stewards/patch/runs', { host_id: 'study-pc' }),
    )
    expect(await screen.findByText(/Preview started on study-pc/)).toBeInTheDocument()
    // The new run is opened in the runs list.
    expect(await screen.findByRole('region', { name: 'Run run-1' })).toBeInTheDocument()
  })

  it('degrades gracefully when the server has no preview route yet', async () => {
    mockApi()
    apiPostMock.mockRejectedValue(new ApiError('/api/stewards/patch/runs -> 404', 404))
    renderSection('/admin/stewards/patch', false)

    const form = await screen.findByRole('form', { name: 'Preview Package updates' })
    const picker = await within(form).findByLabelText('PC')
    fireEvent.change(picker, { target: { value: 'study-pc' } })
    fireEvent.click(within(form).getByRole('button', { name: 'RUN PREVIEW' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/does not offer preview runs yet/)
    // Nothing else broke.
    expect(screen.getByRole('button', { name: 'RUN PREVIEW' })).toBeEnabled()
  })

  it('shows the server reason for any other refusal', async () => {
    mockApi()
    apiPostMock.mockRejectedValue(new ApiError('steward runs spent 1000 tokens in the last 24 hours; the cap is 1000', 429))
    renderSection('/admin/stewards/patch', false)

    const form = await screen.findByRole('form', { name: 'Preview Package updates' })
    fireEvent.change(await within(form).findByLabelText('PC'), { target: { value: 'study-pc' } })
    fireEvent.click(within(form).getByRole('button', { name: 'RUN PREVIEW' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/the cap is 1000/)
  })

  it('offers no button for a steward that starts from a ticket', async () => {
    mockApi()
    renderSection('/admin/stewards/triage', false)

    expect(await screen.findByText(/starts from a new ticket, so there is nothing to preview by hand/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'RUN PREVIEW' })).not.toBeInTheDocument()
  })
})

describe('StewardsSection — runs', () => {
  it('lists the runs and drills into one: actions, recommendations, verdict, finding, tokens', async () => {
    mockApi({ runs: [run(), run({ id: 'run-2', status: 'failed', verdict: null, host_id: 'papa-pc', subject: 'papa-pc' })] })
    renderSection('/admin/stewards/patch', false)

    const runs = await screen.findByRole('region', { name: 'Runs' })
    expect(within(runs).getAllByRole('row')).toHaveLength(3)
    expect(within(runs).getByText('FAILED')).toBeInTheDocument()

    fireEvent.click(within(runs).getAllByRole('button', { name: /^Open the run/ })[0])

    const detail = await screen.findByRole('region', { name: 'Run run-1' })
    expect(within(detail).getByText('acted')).toBeInTheDocument()
    expect(within(detail).getByText('Updated 7zip; the second winget_list shows no update pending.')).toBeInTheDocument()
    expect(within(detail).getByText(/1,560 in total/)).toBeInTheDocument()

    const actions = within(detail).getByRole('region', { name: 'Actions of this run' })
    expect(within(actions).getByText('winget_update')).toBeInTheDocument()
    expect(within(actions).getByText('{"id":"7zip.7zip","timeout_s":600}')).toBeInTheDocument()
    // The action names the authorization it ran under.
    expect(within(actions).getByText('auth-77')).toBeInTheDocument()

    const recs = within(detail).getByRole('region', { name: 'Recommendations of this run' })
    expect(within(recs).getByText('winget_install')).toBeInTheDocument()
    expect(within(recs).getByText('NORMAL CHANGE')).toBeInTheDocument()
  })

  it('says a run has not happened yet when there are none', async () => {
    mockApi({ runs: [] })
    renderSection('/admin/stewards/patch', false)

    expect(await screen.findByText('This steward has not run yet.')).toBeInTheDocument()
  })
})

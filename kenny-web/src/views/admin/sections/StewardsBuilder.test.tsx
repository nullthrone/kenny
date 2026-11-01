import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  BuilderCatalog,
  BuilderClonePreview,
  BuilderData,
  BuiltStewardRecord,
  InvalidSteward,
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
    body?: unknown
    constructor(message: string, status = 0, _code?: string, body?: unknown) {
      super(message)
      this.status = status
      this.body = body
    }
  },
}))

const { ApiError } = await import('../../../api/client')
const { default: StewardsSection } = await import('./StewardsSection')

const HASH = 'a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90'

const CATALOG: BuilderCatalog = {
  tools: [
    {
      name: 'steward_verdict',
      tier: 'standard_change',
      description: 'Report how the run ended.',
      args: [{ name: 'verdict', type: 'string', required: true, bindable: true }],
      os: null,
      sensitive: false,
      target: 'server',
      verdict: true,
    },
    {
      name: 'winget_list',
      tier: 'read_only',
      description: 'List installed packages.',
      args: [],
      os: ['windows'],
      sensitive: false,
      target: 'host',
      verdict: false,
    },
    {
      name: 'winget_update',
      tier: 'standard_change',
      description: 'Update one package.',
      args: [
        { name: 'id', type: 'string', required: true, bindable: true },
        { name: 'timeout_s', type: 'integer', required: false, bindable: false },
      ],
      os: ['windows'],
      sensitive: false,
      target: 'host',
      verdict: false,
    },
    {
      name: 'winget_install',
      tier: 'normal_change',
      description: 'Install one package.',
      args: [{ name: 'id', type: 'string', required: true, bindable: true }],
      os: ['windows'],
      sensitive: false,
      target: 'host',
      verdict: false,
    },
    {
      name: 'screen_capture',
      tier: 'read_only',
      description: 'Take a screenshot of the PC.',
      args: [],
      os: null,
      sensitive: true,
      target: 'host',
      verdict: false,
    },
  ],
  verdict_tool: 'steward_verdict',
  verdicts: ['ok', 'acted'],
  param_kinds: { window: 'window', require_idle: 'bool', min_percent_used: 'percent' },
  default_param_kind: 'list',
  preconditions: ['host_online', 'host_idle'],
  trigger_kinds: ['event', 'schedule'],
  events: ['alert:offline', 'alert:disk_forecast'],
  limits: {
    title_max: 80,
    description_max: 400,
    prompt_max: 8000,
    tools_max: 24,
    max_iterations_min: 1,
    max_iterations_max: 20,
    timeout_max_s: 600,
    params_max: 16,
  },
  id_pattern: '^custom_[a-z][a-z0-9_]{1,32}$',
  never_authorized: ['shell_exec', 'powershell_exec', 'agent_update', 'screen_capture'],
  max_expiry_days: 90,
}

function steward(over: Partial<Steward> = {}): Steward {
  return {
    id: 'patch',
    title: 'Package updates',
    description: 'Updates allowlisted packages in a maintenance window.',
    trigger: { kind: 'schedule', event: null },
    tools: ['steward_verdict', 'winget_update'],
    tool_classes: { steward_verdict: 'standard_change', winget_update: 'standard_change' },
    verdict_tool: 'steward_verdict',
    budget: { max_iterations: 12 },
    constraints: [],
    timeouts: [],
    sensitive_ok: false,
    default_mode: 'shadow',
    version: 1,
    spec_hash: 'f'.repeat(64),
    param_kinds: {},
    mode: 'shadow',
    params: {},
    effective_hash: HASH,
    act_bound: false,
    latest_run: null,
    builtin: false,
    ...over,
  }
}

const BUILT = steward({
  id: 'custom_weekly',
  title: 'Weekly check',
  description: 'Lists packages.',
  builtin: true,
  source: 'blank',
  version: 2,
  tools: ['steward_verdict', 'winget_list'],
  tool_classes: { steward_verdict: 'standard_change', winget_list: 'read_only' },
})

const INVALID: InvalidSteward = {
  id: 'custom_broken',
  title: 'Broken one',
  description: 'Used a tool that is gone.',
  builtin: true,
  invalid: true,
  errors: [{ field: 'tools[0]', error: 'old_tool: kenny has no such tool' }],
  mode: 'off',
  effective_hash: 'dead'.repeat(16),
  latest_run: null,
  source: 'blank',
  version: 3,
}

const BUILT_DATA: BuilderData = {
  id: 'custom_weekly',
  title: 'Weekly check',
  description: 'Lists packages.',
  prompt: 'List the packages.',
  trigger: { kind: 'schedule', min_interval_days: 7 },
  triggers_allowed: { schedule: true, events: [], command: false },
  tools: ['steward_verdict', 'winget_list'],
  constraints: [],
  timeouts: [],
  params: ['hosts', 'window'],
  preconditions: [],
  budget: { max_iterations: 6 },
  sensitive_ok: false,
}

function record(over: Partial<BuiltStewardRecord> = {}): BuiltStewardRecord {
  return {
    id: 'custom_weekly',
    data: BUILT_DATA,
    version: 2,
    source: 'blank',
    created_at: '2026-11-01T00:00:00Z',
    created_by: 'thomas',
    updated_at: '2026-11-01T00:00:00Z',
    updated_by: 'thomas',
    effective_hash: HASH,
    invalid: false,
    errors: [],
    ...over,
  }
}

interface Setup {
  stewards?: Array<Steward | InvalidSteward>
  routes?: Record<string, unknown>
}

function mockApi({ stewards = [steward()], routes = {} }: Setup = {}) {
  const table: Record<string, unknown> = {
    '/api/stewards': { enabled: true, stewards } satisfies StewardsResponse,
    '/api/stewards/builder/catalog': CATALOG,
    '/api/stewards/custom/custom_weekly': record(),
    '/api/stewards/runs': { runs: [] },
    '/api/fleet': { overall: 'ok', agents: [] },
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

function type(label: string | RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } })
}

function pick(tool: string) {
  fireEvent.click(screen.getByRole('checkbox', { name: tool }))
}

const field400 = (errors: Array<{ field: string; error: string }>) =>
  new ApiError('the steward is not valid', 400, undefined, { error: 'the steward is not valid', errors })

beforeEach(() => {
  for (const m of [apiGetMock, apiPostMock, apiPutMock, apiDeleteMock]) m.mockReset()
  vi.restoreAllMocks()
  sessionStorage.clear()
})

describe('the builder — a new steward', () => {
  it('posts the data form with source blank, and warns that it starts in shadow', async () => {
    mockApi()
    apiPostMock.mockResolvedValue({ steward_id: 'custom_weekly', version: 1, mode: 'shadow', voided: 0 })
    renderSection('/admin/stewards/new', true)

    await screen.findByRole('form', { name: 'New steward' })
    expect(screen.getByRole('note')).toHaveTextContent(/starts in\s*shadow/)

    type('ID', 'custom_weekly')
    type('TITLE', 'Weekly check')
    type('PROMPT', 'Update the allowlisted packages.')
    pick('winget_list')
    pick('winget_update')
    // A change tool brings a row per required argument, bound by literal values here.
    fireEvent.change(screen.getByLabelText('winget_update.id allowed values'), {
      target: { value: '7zip.7zip\n Git.Git \n' },
    })
    fireEvent.change(screen.getByLabelText('Timeout for winget_update'), { target: { value: '300' } })
    fireEvent.click(screen.getByRole('checkbox', { name: 'host_online' }))
    type(/MODEL ROUND-TRIPS/, '5')
    fireEvent.click(screen.getByRole('button', { name: 'CREATE STEWARD IN SHADOW' }))

    await waitFor(() => expect(apiPostMock).toHaveBeenCalledTimes(1))
    expect(apiPostMock).toHaveBeenCalledWith('/api/stewards/custom', {
      source: 'blank',
      spec: {
        id: 'custom_weekly',
        title: 'Weekly check',
        description: '',
        prompt: 'Update the allowlisted packages.',
        trigger: { kind: 'schedule', min_interval_days: null },
        triggers_allowed: { schedule: true, events: [], command: false },
        tools: ['winget_list', 'winget_update', 'steward_verdict'],
        constraints: [{ tool: 'winget_update', arg: 'id', allowed: ['7zip.7zip', 'Git.Git'] }],
        timeouts: [{ tool: 'winget_update', max_s: 300 }],
        params: ['hosts', 'window'],
        preconditions: ['host_online'],
        budget: { max_iterations: 5 },
        sensitive_ok: false,
      },
    })
  })

  it('binds an argument to a parameter of the steward, picked from the catalog kinds', async () => {
    mockApi()
    apiPostMock.mockResolvedValue({ steward_id: 'custom_pk', version: 1, mode: 'shadow', voided: 0 })
    renderSection('/admin/stewards/new', true)
    await screen.findByRole('form', { name: 'New steward' })

    fireEvent.change(screen.getByLabelText('Add a parameter'), { target: { value: '__own__' } })
    fireEvent.change(screen.getByLabelText('NAME OF THE LIST'), { target: { value: 'packages' } })
    fireEvent.click(screen.getByRole('button', { name: 'ADD LIST' }))
    // A known name carries its kind from the catalog; any other name is a list.
    const params = screen.getByRole('list', { name: 'Declared parameters' })
    expect(within(params).getByText('WINDOW', { selector: 'span' })).toBeInTheDocument()
    // hosts (from the empty form) and packages.
    expect(within(params).getAllByText('LIST').length).toBe(2)

    pick('winget_update')
    fireEvent.change(screen.getByLabelText('winget_update.id is bound by'), { target: { value: 'param' } })
    fireEvent.change(screen.getByLabelText('winget_update.id parameter'), { target: { value: 'packages' } })
    type('ID', 'custom_pk')
    type('TITLE', 'Packages')
    type('PROMPT', 'Go.')
    fireEvent.click(screen.getByRole('button', { name: 'CREATE STEWARD IN SHADOW' }))

    await waitFor(() => expect(apiPostMock).toHaveBeenCalled())
    const body = apiPostMock.mock.calls[0][1] as { spec: BuilderData }
    expect(body.spec.constraints).toEqual([{ tool: 'winget_update', arg: 'id', param: 'packages' }])
    expect(body.spec.params).toEqual(['hosts', 'window', 'packages'])
  })

  it('marks tools with their tier, OS scope and sensitivity', async () => {
    mockApi()
    renderSection('/admin/stewards/new', true)
    await screen.findByRole('form', { name: 'New steward' })

    const list = screen.getByRole('checkbox', { name: 'winget_list' }).closest('li') as HTMLElement
    expect(within(list).getByText('READ-ONLY')).toBeInTheDocument()
    expect(within(list).getByText('WINDOWS ONLY')).toBeInTheDocument()
    const install = screen.getByRole('checkbox', { name: 'winget_install' }).closest('li') as HTMLElement
    expect(within(install).getByText('NORMAL CHANGE')).toBeInTheDocument()
    const shot = screen.getByRole('checkbox', { name: 'screen_capture' }).closest('li') as HTMLElement
    expect(within(shot).getByText('SENSITIVE')).toBeInTheDocument()
  })

  it('will not save a sensitive tool until sensitive_ok is ticked, then sends the flag', async () => {
    mockApi()
    apiPostMock.mockResolvedValue({ steward_id: 'custom_eyes', version: 1, mode: 'shadow', voided: 0 })
    renderSection('/admin/stewards/new', true)
    await screen.findByRole('form', { name: 'New steward' })
    type('ID', 'custom_eyes')
    type('TITLE', 'Eyes')
    type('PROMPT', 'Look.')

    pick('screen_capture')
    const save = screen.getByRole('button', { name: 'CREATE STEWARD IN SHADOW' })
    expect(save).toBeDisabled()
    expect(screen.getAllByText(/reads private data/).length).toBeGreaterThan(0)
    fireEvent.submit(screen.getByRole('form', { name: 'New steward' }))
    expect(apiPostMock).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('checkbox', { name: /I allow this steward to read private data/ }))
    expect(save).toBeEnabled()
    fireEvent.click(save)

    await waitFor(() => expect(apiPostMock).toHaveBeenCalled())
    const body = apiPostMock.mock.calls[0][1] as { spec: BuilderData }
    expect(body.spec.sensitive_ok).toBe(true)
    expect(body.spec.tools).toContain('screen_capture')
  })

  it('shows each field error of a 400 beside its field', async () => {
    mockApi()
    apiPostMock.mockRejectedValue(
      field400([
        { field: 'title', error: 'is required' },
        { field: 'tools[1]', error: 'winget_update: changes something; bind its arguments with a constraint' },
        { field: 'constraints[0].param', error: 'packages is not one of this steward’s parameters' },
        { field: 'params', error: 'a schedule needs a window parameter' },
        { field: 'timeouts[0].max_s', error: 'must be 1 to 600 whole seconds' },
        { field: 'spec', error: 'something only the shipped checks catch' },
      ]),
    )
    renderSection('/admin/stewards/new', true)
    await screen.findByRole('form', { name: 'New steward' })
    pick('winget_list')
    pick('winget_update')
    fireEvent.change(screen.getByLabelText('winget_update.id is bound by'), { target: { value: 'param' } })
    fireEvent.change(screen.getByLabelText('Timeout for winget_update'), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: 'CREATE STEWARD IN SHADOW' }))

    expect(await screen.findByText('is required')).toBeInTheDocument()
    // The title's error sits with the title, not in the summary.
    const title = screen.getByLabelText('TITLE').closest('div') as HTMLElement
    expect(within(title.parentElement as HTMLElement).getByText('is required')).toBeInTheDocument()
    // tools[1] is the second tool of the submitted list: winget_update.
    const row = screen.getByRole('checkbox', { name: 'winget_update' }).closest('li') as HTMLElement
    expect(within(row).getByText(/bind its arguments with a constraint/)).toBeInTheDocument()
    const group = screen.getByRole('group', { name: 'Constraints on winget_update' })
    expect(within(group).getByText(/param: packages is not one of/)).toBeInTheDocument()
    const params = screen.getByRole('region', { name: 'Parameters' })
    expect(within(params).getByText('a schedule needs a window parameter')).toBeInTheDocument()
    const budget = screen.getByRole('region', { name: 'Budget and timeouts' })
    expect(within(budget).getByText(/max_s: must be 1 to 600/)).toBeInTheDocument()
    // What has no field of its own is named in the summary.
    expect(screen.getByRole('alert')).toHaveTextContent('6 problems to fix')
    expect(screen.getByRole('alert')).toHaveTextContent('spec: something only the shipped checks catch')
  })

  it('shows a taken id at the id field (409 on create)', async () => {
    mockApi()
    apiPostMock.mockRejectedValue(new ApiError('a steward custom_weekly already exists', 409))
    renderSection('/admin/stewards/new', true)
    await screen.findByRole('form', { name: 'New steward' })
    type('ID', 'custom_weekly')
    type('TITLE', 'x')
    type('PROMPT', 'x')
    fireEvent.click(screen.getByRole('button', { name: 'CREATE STEWARD IN SHADOW' }))

    expect(await screen.findByText('a steward custom_weekly already exists')).toBeInTheDocument()
  })

  it('opens a draft from Ask kenny, removes it from storage, and saves it with source copilot', async () => {
    mockApi()
    apiPostMock.mockResolvedValue({ steward_id: 'custom_drafted', version: 1, mode: 'shadow', voided: 0 })
    sessionStorage.setItem(
      'kenny.stewardDraft',
      JSON.stringify({
        ...BUILT_DATA,
        id: 'custom_drafted',
        title: 'Drafted by Ask kenny',
        prompt: 'Read the packages weekly.',
        tools: ['steward_verdict', 'winget_list', 'not-a-tool'],
        mystery: { ignored: true },
      }),
    )
    renderSection('/admin/stewards/new?draft=1', true)

    expect(await screen.findByText(/Draft from Ask kenny — review before saving/)).toBeInTheDocument()
    expect(screen.getByLabelText('TITLE')).toHaveValue('Drafted by Ask kenny')
    expect(screen.getByLabelText('ID')).toHaveValue('custom_drafted')
    expect(screen.getByRole('checkbox', { name: 'winget_list' })).toBeChecked()
    // Taken once: a reload finds nothing.
    expect(sessionStorage.getItem('kenny.stewardDraft')).toBeNull()
    // Nothing is saved until the person presses save.
    expect(apiPostMock).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'CREATE STEWARD IN SHADOW' }))
    await waitFor(() => expect(apiPostMock).toHaveBeenCalled())
    const body = apiPostMock.mock.calls[0][1] as { spec: BuilderData; source: string }
    expect(body.source).toBe('copilot')
    expect(body.spec.title).toBe('Drafted by Ask kenny')
    expect(body.spec).not.toHaveProperty('mystery')
  })

  it('falls back to an empty form when the draft cannot be read', async () => {
    mockApi()
    sessionStorage.setItem('kenny.stewardDraft', '{not json')
    renderSection('/admin/stewards/new?draft=1', true)

    expect(await screen.findByText(/could not be read/)).toBeInTheDocument()
    expect(screen.queryByText(/Draft from Ask kenny — review/)).not.toBeInTheDocument()
    expect(screen.getByLabelText('TITLE')).toHaveValue('')
    expect(sessionStorage.getItem('kenny.stewardDraft')).toBeNull()
  })

  it('says so when ?draft=1 finds no draft, and ignores a draft without the flag', async () => {
    mockApi()
    sessionStorage.setItem('kenny.stewardDraft', JSON.stringify({ ...BUILT_DATA, title: 'Left behind' }))
    renderSection('/admin/stewards/new', true)
    await screen.findByRole('form', { name: 'New steward' })
    expect(screen.getByLabelText('TITLE')).toHaveValue('')
    expect(sessionStorage.getItem('kenny.stewardDraft')).not.toBeNull()
  })

  it('keeps the editor away from an operator, and the draft untouched', async () => {
    mockApi()
    sessionStorage.setItem('kenny.stewardDraft', JSON.stringify(BUILT_DATA))
    renderSection('/admin/stewards/new?draft=1', false)

    expect(await screen.findByText('Creating a steward needs a superuser')).toBeInTheDocument()
    expect(screen.queryByRole('form', { name: 'New steward' })).not.toBeInTheDocument()
    expect(sessionStorage.getItem('kenny.stewardDraft')).not.toBeNull()
  })
})

describe('the builder — entry points', () => {
  it('offers NEW STEWARD to a superuser only', async () => {
    mockApi()
    const { unmount } = renderSection('/admin/stewards', true)
    expect(await screen.findByRole('link', { name: 'NEW STEWARD' })).toHaveAttribute('href', '/admin/stewards/new')
    unmount()

    renderSection('/admin/stewards', false)
    await screen.findByRole('link', { name: 'Package updates' })
    expect(screen.queryByRole('link', { name: 'NEW STEWARD' })).not.toBeInTheDocument()
  })

  it('previews a clone with the errors the server names, and saves it as clone:<id>', async () => {
    const data: BuilderData = {
      ...BUILT_DATA,
      id: 'custom_patch',
      title: 'Package updates (copy)',
      tools: ['shell_exec', 'steward_verdict', 'winget_update'],
      constraints: [{ tool: 'winget_update', arg: 'id', evidence: 'pending_updates' }],
    }
    const preview: BuilderClonePreview = {
      data,
      source: 'clone:patch',
      errors: [
        { field: 'tools[0]', error: 'shell_exec: a shell or the agent’s own update; a built steward never names one' },
        { field: 'constraints[0].evidence', error: 'values the server computes need server code' },
      ],
    }
    mockApi({ routes: { '/api/stewards/builder/clone/patch': preview } })
    apiPostMock.mockRejectedValueOnce(field400([{ field: 'tools[0]', error: 'still wrong' }]))
    renderSection('/admin/stewards/patch', true)

    fireEvent.click(await screen.findByRole('button', { name: 'CLONE AS A NEW STEWARD' }))
    await screen.findByRole('form', { name: 'New steward' })
    expect(apiGetMock).toHaveBeenCalledWith('/api/stewards/builder/clone/patch?id=custom_patch')

    // Its errors are there before anything is sent, beside the things they name.
    const shell = screen.getByRole('checkbox', { name: 'shell_exec' }).closest('li') as HTMLElement
    expect(within(shell).getByText(/a built steward never names one/)).toBeInTheDocument()
    const group = screen.getByRole('group', { name: 'Constraints on winget_update' })
    expect(within(group).getByText(/evidence: values the server computes need server code/)).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('2 problems to fix')
    expect(apiPostMock).not.toHaveBeenCalled()

    // The person fixes it: drop the shell, bind the argument to values, and save.
    pick('shell_exec')
    fireEvent.change(screen.getByLabelText('winget_update.id is bound by'), { target: { value: 'allowed' } })
    fireEvent.change(screen.getByLabelText('winget_update.id allowed values'), { target: { value: '7zip.7zip' } })
    apiPostMock.mockReset()
    apiPostMock.mockResolvedValue({ steward_id: 'custom_patch', version: 1, mode: 'shadow', voided: 0 })
    fireEvent.click(screen.getByRole('button', { name: 'CREATE STEWARD IN SHADOW' }))

    await waitFor(() => expect(apiPostMock).toHaveBeenCalled())
    const [path, body] = apiPostMock.mock.calls[0] as [string, { spec: BuilderData; source: string }]
    expect(path).toBe('/api/stewards/custom')
    expect(body.source).toBe('clone:patch')
    expect(body.spec.tools).toEqual(['steward_verdict', 'winget_update'])
    expect(body.spec.constraints).toEqual([{ tool: 'winget_update', arg: 'id', allowed: ['7zip.7zip'] }])
  })

  it('offers an operator no write button on a shipped or a built steward', async () => {
    mockApi({ stewards: [steward(), BUILT] })
    const { unmount } = renderSection('/admin/stewards/patch', false)
    await screen.findByText('Package updates')
    expect(screen.queryByRole('button', { name: /CLONE/ })).not.toBeInTheDocument()
    unmount()

    renderSection('/admin/stewards/custom_weekly', false)
    await screen.findByText('Weekly check')
    expect(screen.getByText(/Built in the dashboard \(blank\), version 2/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'EDIT' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'DELETE' })).not.toBeInTheDocument()
  })
})

describe('the builder — editing and deleting', () => {
  it('saves with the effective hash it was shown', async () => {
    mockApi({ stewards: [BUILT] })
    apiPutMock.mockResolvedValue({ steward_id: 'custom_weekly', version: 3, mode: 'shadow', voided: 2, effective_hash: 'x' })
    renderSection('/admin/stewards/custom_weekly', true)

    fireEvent.click(await screen.findByRole('button', { name: 'EDIT' }))
    await screen.findByRole('form', { name: 'Edit custom_weekly' })
    expect(screen.getByLabelText('TITLE')).toHaveValue('Weekly check')
    // The id of a built steward is not editable.
    expect(screen.queryByRole('textbox', { name: 'ID' })).not.toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent(/voided for good/)

    type('TITLE', 'Weekly package check')
    fireEvent.click(screen.getByRole('button', { name: 'SAVE NEW VERSION' }))

    await waitFor(() => expect(apiPutMock).toHaveBeenCalled())
    const [path, body] = apiPutMock.mock.calls[0] as [string, { spec: BuilderData; effective_hash: string }]
    expect(path).toBe('/api/stewards/custom/custom_weekly')
    expect(body.effective_hash).toBe(HASH)
    expect(body.spec.id).toBe('custom_weekly')
    expect(body.spec.title).toBe('Weekly package check')
    expect(body.spec.tools).toEqual(['steward_verdict', 'winget_list'])
    // Back on the detail, with what the save did.
    expect(await screen.findByText(/Saved as version 3. The steward is in shadow. 2 standing authorizations voided./)).toBeInTheDocument()
  })

  it('explains a 409 and reloads the stored definition on request', async () => {
    mockApi({ stewards: [BUILT] })
    apiPutMock.mockRejectedValue(new ApiError('steward custom_weekly changed since it was shown', 409))
    renderSection('/admin/stewards/custom_weekly', true)

    fireEvent.click(await screen.findByRole('button', { name: 'EDIT' }))
    await screen.findByRole('form', { name: 'Edit custom_weekly' })
    type('TITLE', 'My edit')
    fireEvent.click(screen.getByRole('button', { name: 'SAVE NEW VERSION' }))

    expect(await screen.findByText(/changed since you opened it .* so nothing was saved/)).toBeInTheDocument()
    const reads = apiGetMock.mock.calls.filter(([p]) => p === '/api/stewards/custom/custom_weekly').length
    apiGetMock.mockImplementation((path: string) =>
      path === '/api/stewards/custom/custom_weekly'
        ? Promise.resolve(record({ data: { ...BUILT_DATA, title: 'Changed elsewhere' }, effective_hash: 'b'.repeat(64) }))
        : path === '/api/stewards'
          ? Promise.resolve({ enabled: true, stewards: [BUILT] })
          : path === '/api/stewards/builder/catalog'
            ? Promise.resolve(CATALOG)
            : Promise.reject(new ApiError('404', 404)),
    )
    fireEvent.click(screen.getByRole('button', { name: 'RELOAD' }))

    await waitFor(() => expect(screen.getByLabelText('TITLE')).toHaveValue('Changed elsewhere'))
    expect(apiGetMock.mock.calls.filter(([p]) => p === '/api/stewards/custom/custom_weekly').length).toBeGreaterThan(reads)
  })

  it('asks before deleting, saying the authorizations are voided for good', async () => {
    mockApi({ stewards: [BUILT] })
    apiDeleteMock.mockResolvedValue({ steward_id: 'custom_weekly', deleted: true, voided: 1 })
    renderSection('/admin/stewards/custom_weekly', true)

    fireEvent.click(await screen.findByRole('button', { name: 'DELETE' }))
    await screen.findByText(/voided for good/)
    expect(screen.getByText(/revives none of it/)).toBeInTheDocument()
    expect(apiDeleteMock).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'DELETE STEWARD' }))
    await waitFor(() => expect(apiDeleteMock).toHaveBeenCalledWith('/api/stewards/custom/custom_weekly'))
  })

  it('does not delete when the person cancels', async () => {
    mockApi({ stewards: [BUILT] })
    renderSection('/admin/stewards/custom_weekly', true)

    fireEvent.click(await screen.findByRole('button', { name: 'DELETE' }))
    fireEvent.click(await screen.findByRole('button', { name: 'CANCEL' }))
    expect(apiDeleteMock).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'DELETE STEWARD' })).not.toBeInTheDocument()
  })
})

describe('the builder — an invalid built steward', () => {
  it('is listed with its first error and a plain statement that it does not run', async () => {
    mockApi({ stewards: [steward(), INVALID] })
    renderSection('/admin/stewards', false)

    const link = await screen.findByRole('link', { name: 'Broken one' })
    const row = link.closest('li') as HTMLElement
    expect(within(row).getByText('INVALID')).toBeInTheDocument()
    expect(within(row).getByText(/does not run: 1 problem/)).toBeInTheDocument()
    expect(within(row).getByText(/old_tool: kenny has no such tool/)).toBeInTheDocument()
  })

  it('shows its errors prominently and lets a superuser edit it', async () => {
    mockApi({
      stewards: [INVALID],
      routes: {
        '/api/stewards/custom/custom_broken': record({
          id: 'custom_broken',
          data: { ...BUILT_DATA, id: 'custom_broken', title: 'Broken one', tools: ['old_tool', 'steward_verdict'] },
          invalid: true,
          errors: INVALID.errors,
          effective_hash: INVALID.effective_hash,
        }),
      },
    })
    apiPutMock.mockResolvedValue({ steward_id: 'custom_broken', version: 4, mode: 'shadow', voided: 0 })
    renderSection('/admin/stewards/custom_broken', true)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('This steward does not run.')
    expect(alert).toHaveTextContent('old_tool: kenny has no such tool')
    expect(screen.getByText('INVALID')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'EDIT' }))
    await screen.findByRole('form', { name: 'Edit custom_broken' })
    // The withdrawn tool stays ticked, with the server's reason, so it can be unticked.
    const tool = screen.getByRole('checkbox', { name: 'old_tool' }).closest('li') as HTMLElement
    expect(within(tool).getByText(/kenny has no such tool/)).toBeInTheDocument()
    pick('old_tool')
    fireEvent.click(screen.getByRole('button', { name: 'SAVE NEW VERSION' }))

    await waitFor(() => expect(apiPutMock).toHaveBeenCalled())
    const [path, body] = apiPutMock.mock.calls[0] as [string, { spec: BuilderData; effective_hash: string }]
    expect(path).toBe('/api/stewards/custom/custom_broken')
    expect(body.effective_hash).toBe(INVALID.effective_hash)
    expect(body.spec.tools).toEqual(['steward_verdict'])
  })

  it('gives an operator the errors and no button', async () => {
    mockApi({ stewards: [INVALID] })
    renderSection('/admin/stewards/custom_broken', false)

    expect(await screen.findByRole('alert')).toHaveTextContent('old_tool: kenny has no such tool')
    expect(screen.queryByRole('button', { name: 'EDIT' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'DELETE' })).not.toBeInTheDocument()
  })
})

describe('authorization limits come from the builder catalog', () => {
  const normal = steward({
    tools: ['steward_verdict', 'winget_install', 'screen_capture'],
    tool_classes: { steward_verdict: 'standard_change', winget_install: 'normal_change', screen_capture: 'normal_change' },
    tool_targets: { winget_install: 'host', screen_capture: 'host' },
  })

  it('uses the server’s ceiling and never-list', async () => {
    mockApi({
      stewards: [normal],
      routes: { '/api/stewards/patch/authorizations': { steward_id: 'patch', effective_hash: HASH, authorizations: [] } },
    })
    renderSection('/admin/stewards/patch', true)

    expect(await screen.findByText('EXPIRES IN (DAYS, AT MOST 90)')).toBeInTheDocument()
    expect(screen.getByText(/shell_exec, powershell_exec, agent_update, screen_capture can never be authorized/)).toBeInTheDocument()
    // screen_capture is on the server's never-list, so it is not offered for a grant.
    const grant = screen.getByRole('form', { name: 'Grant a standing authorization' })
    const options = within(grant).getAllByRole('option').map((o) => o.textContent)
    expect(options).toContain('winget_install')
    expect(options).not.toContain('screen_capture')
    expect(screen.queryByText(/Could not read the limits/)).not.toBeInTheDocument()
  })

  it('falls back to built-in copies when the catalog cannot be read, and says so', async () => {
    mockApi({
      stewards: [normal],
      routes: {
        '/api/stewards/builder/catalog': new ApiError('boom', 500),
        '/api/stewards/patch/authorizations': { steward_id: 'patch', effective_hash: HASH, authorizations: [] },
      },
    })
    renderSection('/admin/stewards/patch', true)

    expect(await screen.findByText(/Could not read the limits from the server/)).toBeInTheDocument()
    expect(screen.getByText('EXPIRES IN (DAYS, AT MOST 180)')).toBeInTheDocument()
  })
})

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import type { AdminRow, PolicyRule } from '../types'

const { apiGetMock, apiPostMock, apiDeleteMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  apiPostMock: vi.fn(),
  apiDeleteMock: vi.fn(),
}))
vi.mock('../../../api/client', () => ({
  api: { get: apiGetMock, post: apiPostMock, put: vi.fn(), patch: vi.fn(), delete: apiDeleteMock },
  ApiError: class ApiError extends Error {},
}))

const { default: ShellPolicySection } = await import('./ShellPolicySection')

function modeRow(value: string): AdminRow {
  return {
    key: 'KENNY_SHELL_POLICY_MODE',
    label: 'Shell execution mode',
    help: 'What powershell_exec and shell_exec may run fleet-wide.',
    value,
    source: 'db',
    editable: true,
    type: 'enum',
    choices: ['unrestricted', 'allowlist', 'off'],
    min: null,
    max: null,
    isSet: true,
    lifecycle: 'live',
    pendingRestart: false,
  }
}

function rule(over: Partial<PolicyRule> = {}): PolicyRule {
  return { id: 'al_uname', applies_to: 'posix', pattern: 'uname -a', reason: 'read kernel', ...over }
}

/** Route the two GETs this section makes. */
function wire({
  allow = [] as PolicyRule[],
  defaults = [] as PolicyRule[],
  operator = [] as PolicyRule[],
  builtin = [] as PolicyRule[],
} = {}) {
  apiGetMock.mockImplementation((path: string) => {
    if (path === '/api/policy/shell-allow') return Promise.resolve({ mode: 'allowlist', allow, defaults })
    if (path === '/api/policy/rules') return Promise.resolve({ builtin, operator })
    throw new Error(`unexpected GET ${path}`)
  })
}

function renderSection(value = 'allowlist') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ShellPolicySection rows={[modeRow(value)]} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  apiGetMock.mockReset()
  apiPostMock.mockReset()
  apiDeleteMock.mockReset()
})

it('warns that allowlist mode with an empty list blocks every shell call', async () => {
  // The state an operator must not discover by surprise: the mode is on, the
  // list is empty, and the fleet's shell is therefore entirely shut.
  wire({ allow: [] })
  renderSection('allowlist')
  expect(await screen.findByText(/blocks every shell call on every host/i)).toBeInTheDocument()
})

it('does not warn once the allowlist has a rule', async () => {
  wire({ allow: [rule()] })
  renderSection('allowlist')
  expect(await screen.findByText('al_uname')).toBeInTheDocument()
  expect(screen.queryByText(/blocks every shell call on every host/i)).not.toBeInTheDocument()
})

it('says so plainly when the mode is off', async () => {
  wire({ allow: [rule()] })
  renderSection('off')
  expect(await screen.findByText(/shell execution is off fleet-wide/i)).toBeInTheDocument()
})

it('offers only the two command surfaces for an allow rule', async () => {
  // An allow rule is matched against a command string, so `path` and
  // `self_protection` cannot carry one — the deny form still offers all four.
  wire({ allow: [] })
  renderSection('allowlist')
  await screen.findByText('ALLOW RULES')
  const selects = screen.getAllByRole('combobox')
  const allowOptions = Array.from(selects[0].querySelectorAll('option')).map((o) => o.getAttribute('value'))
  const denyOptions = Array.from(selects[1].querySelectorAll('option')).map((o) => o.getAttribute('value'))
  expect(allowOptions).toEqual(['posix', 'powershell'])
  expect(denyOptions).toEqual(['posix', 'powershell', 'path', 'self_protection'])
})

it('posts a new allow rule and refreshes the list', async () => {
  wire({ allow: [] })
  apiPostMock.mockResolvedValue({ mode: 'allowlist', allow: [rule()] })
  renderSection('allowlist')
  await screen.findByText('ALLOW RULES')

  fireEvent.change(screen.getAllByLabelText(/^ID$/i)[0], { target: { value: 'al_uname' } })
  fireEvent.change(screen.getAllByLabelText(/PATTERN/i)[0], { target: { value: 'uname -a' } })
  fireEvent.change(screen.getAllByLabelText(/^REASON$/i)[0], { target: { value: 'read kernel' } })
  fireEvent.click(screen.getByRole('button', { name: 'ADD ALLOW RULE' }))

  await waitFor(() =>
    expect(apiPostMock).toHaveBeenCalledWith('/api/policy/shell-allow', {
      id: 'al_uname',
      applies_to: 'posix',
      pattern: 'uname -a',
      reason: 'read kernel',
    }),
  )
})

it('removes an allow rule through its own route', async () => {
  wire({ allow: [rule()] })
  apiDeleteMock.mockResolvedValue({ ok: true, removed: true, allow: [] })
  renderSection('allowlist')
  await screen.findByText('al_uname')

  fireEvent.click(screen.getAllByRole('button', { name: 'REMOVE' })[0])
  await waitFor(() => expect(apiDeleteMock).toHaveBeenCalledWith('/api/policy/shell-allow/al_uname'))
})

it('shows the built-in catalog read-only', async () => {
  // The floor the operator cannot lower: visible, counted, with no remove control.
  const builtin = [rule({ id: 'posix_rm_rf_root', pattern: 'rm -rf /', reason: 'recursive delete of root' })]
  wire({ allow: [rule()], builtin })
  renderSection('allowlist')
  expect(await screen.findByText(/BUILT-IN DENY RULES \(1\)/)).toBeInTheDocument()
  expect(screen.getByText('posix_rm_rf_root')).toBeInTheDocument()
  // One REMOVE button: the allow rule's. The built-in has none.
  expect(screen.getAllByRole('button', { name: 'REMOVE' })).toHaveLength(1)
})

// -- shipped defaults -----------------------------------------------------------

const shipped = [
  rule({ id: 'al_posix_system_info', pattern: 'uname( -a)?', reason: 'identify the host' }),
  rule({ id: 'al_posix_services', pattern: 'systemctl status [a-z]+', reason: 'service state' }),
  rule({ id: 'al_posix_users', pattern: 'who', reason: 'sessions' }),
]

it('says when the list matches the shipped defaults and offers no reset', async () => {
  wire({ allow: shipped, defaults: shipped })
  renderSection('allowlist')
  expect(await screen.findByText(/matches the shipped defaults \(3 rules\)/i)).toBeInTheDocument()
  expect(screen.getAllByText('DEFAULT')).toHaveLength(3)
  expect(screen.queryByRole('button', { name: 'RESET TO DEFAULTS' })).not.toBeInTheDocument()
})

it('names how the list drifted and tags each rule', async () => {
  // One shipped rule edited, one removed, one custom added.
  const allow = [shipped[0], { ...shipped[1], pattern: 'systemctl .*' }, rule({ id: 'al_mine' })]
  wire({ allow, defaults: shipped })
  renderSection('allowlist')
  expect(await screen.findByText(/differs from the shipped defaults: 1 custom, 1 changed, 1 removed/i)).toBeInTheDocument()
  expect(screen.getAllByText('DEFAULT')).toHaveLength(1)
  expect(screen.getAllByText('CHANGED')).toHaveLength(1)
})

it('resets to the shipped defaults only after the operator confirms', async () => {
  wire({ allow: [rule({ id: 'al_mine' })], defaults: shipped })
  apiPostMock.mockResolvedValue({ mode: 'allowlist', allow: shipped, defaults: shipped })
  const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
  renderSection('allowlist')
  const button = await screen.findByRole('button', { name: 'RESET TO DEFAULTS' })

  fireEvent.click(button)
  expect(confirm).toHaveBeenCalledWith(expect.stringMatching(/replace all 1 allow rules with the 3 shipped defaults/i))
  expect(apiPostMock).not.toHaveBeenCalled()

  fireEvent.click(button)
  await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith('/api/policy/shell-allow/reset', {}))
})

it('loads the shipped defaults into an empty list without asking', async () => {
  // Nothing would be lost, so there is nothing to confirm.
  wire({ allow: [], defaults: shipped })
  apiPostMock.mockResolvedValue({ mode: 'allowlist', allow: shipped, defaults: shipped })
  const confirm = vi.spyOn(window, 'confirm')
  renderSection('allowlist')
  fireEvent.click(await screen.findByRole('button', { name: 'LOAD SHIPPED DEFAULTS' }))
  await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith('/api/policy/shell-allow/reset', {}))
  expect(confirm).not.toHaveBeenCalled()
})

it('offers no reset when the server ships no defaults', async () => {
  wire({ allow: [], defaults: [] })
  renderSection('allowlist')
  await screen.findByText('ALLOW RULES')
  expect(screen.queryByRole('button', { name: 'LOAD SHIPPED DEFAULTS' })).not.toBeInTheDocument()
  expect(screen.queryByText(/shipped defaults \(/i)).not.toBeInTheDocument()
})

it('folds a long pattern behind a toggle', async () => {
  const long = rule({ id: 'al_long', pattern: `(${'a|'.repeat(80)}b)` })
  wire({ allow: [long, rule()] })
  renderSection('allowlist')
  expect(await screen.findByText(`pattern · ${long.pattern.length} characters`)).toBeInTheDocument()
  // The short one stays inline: one toggle for two rules.
  expect(screen.getAllByText(/^pattern · /)).toHaveLength(1)
})

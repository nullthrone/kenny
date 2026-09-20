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
  }
}

function rule(over: Partial<PolicyRule> = {}): PolicyRule {
  return { id: 'al_uname', applies_to: 'posix', pattern: 'uname -a', reason: 'read kernel', ...over }
}

/** Route the two GETs this section makes. */
function wire({ allow = [] as PolicyRule[], operator = [] as PolicyRule[], builtin = [] as PolicyRule[] } = {}) {
  apiGetMock.mockImplementation((path: string) => {
    if (path === '/api/policy/shell-allow') return Promise.resolve({ mode: 'allowlist', allow })
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

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { UpdateAgentRow, UpdatesResponse } from '../types'

const { apiGetMock, apiPostMock, apiPutMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  apiPostMock: vi.fn(),
  apiPutMock: vi.fn(),
}))
vi.mock('../../../api/client', () => ({
  api: { get: apiGetMock, post: apiPostMock, put: apiPutMock, patch: vi.fn(), delete: vi.fn() },
  ApiError: class ApiError extends Error {},
}))

const { default: UpdatesSection } = await import('./UpdatesSection')

function agentRow(over: Partial<UpdateAgentRow> = {}): UpdateAgentRow {
  return {
    agent_id: 'thomas-pc',
    online: true,
    os: 'windows',
    arch: 'x86_64',
    channel: 'stable',
    desired_channel: 'stable',
    current_version: '2.1.0',
    eligible: true,
    attempts: 0,
    held: false,
    updated: false,
    ...over,
  }
}

function response(over: Partial<UpdatesResponse> = {}): UpdatesResponse {
  return {
    available: {
      agent: {
        component: 'agent',
        version: '2.2.0',
        url: null,
        sha256: null,
        digest: null,
        ok: true,
        message: null,
        checked_at: new Date(Date.now() - 3 * 3600_000).toISOString(),
      },
    },
    active_campaign: {
      id: 'c1',
      channel: 'stable',
      version: '2.2.0',
      on_connect: true,
      status: 'active',
      expires_at: null,
      created_at: '2026-08-29T00:00:00Z',
    },
    campaigns: [],
    agents: [agentRow()],
    active_campaign_dev: null,
    campaigns_dev: [],
    agents_dev: [],
    server_apply: null,
    config: { check_interval_secs: 86400, rollout_on_connect: true, server_image_ref: null },
    ...over,
  }
}

function renderSection() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <UpdatesSection />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  apiGetMock.mockReset()
  apiPostMock.mockReset()
  apiPutMock.mockReset()
})

describe('UpdatesSection — desired channel', () => {
  // The seam: this console is the only caller of the route the server
  // registers as PUT /api/agent/{id}/channel (webui/__init__.py, covered
  // server-side by tests/test_distribution.py). Asserting method, path and
  // body literally makes the test fail if either half moves.
  it('PUTs the agent channel route when an operator flips desired to dev', async () => {
    apiGetMock.mockResolvedValue(response())
    apiPutMock.mockResolvedValue({ ok: true, agent_id: 'thomas-pc', desired_channel: 'dev' })

    renderSection()

    const select = await screen.findByLabelText('desired channel for thomas-pc')
    fireEvent.change(select, { target: { value: 'dev' } })

    await waitFor(() => expect(apiPutMock).toHaveBeenCalledWith('/api/agent/thomas-pc/channel', { channel: 'dev' }))
  })

  it('shows the built channel alongside the desired one, and preselects desired', async () => {
    apiGetMock.mockResolvedValue(response({ agents: [agentRow({ channel: 'stable', desired_channel: 'dev' })] }))

    renderSection()

    expect(await screen.findByLabelText('desired channel for thomas-pc')).toHaveValue('dev')
    expect(screen.getByText('stable')).toBeInTheDocument()
  })
})

describe('UpdatesSection — per-agent detail', () => {
  it('renders each host os/arch and current version', async () => {
    apiGetMock.mockResolvedValue(response({ agents: [agentRow({ current_version: '2.1.0', os: 'linux', arch: 'aarch64' })] }))

    renderSection()

    expect(await screen.findByText('linux/aarch64')).toBeInTheDocument()
    expect(screen.getByText('2.1.0')).toBeInTheDocument()
  })

  it('reports the detection check time so a stalled check is visible', async () => {
    apiGetMock.mockResolvedValue(response())

    renderSection()

    expect(await screen.findByText(/checked 3h ago/)).toBeInTheDocument()
  })
})

describe('UpdatesSection — eligibility labels', () => {
  it('labels an arch/channel mismatch NOT ELIGIBLE, never ON CONNECT', async () => {
    apiGetMock.mockResolvedValue(response({ agents: [agentRow({ eligible: false, online: false })] }))

    renderSection()

    expect(await screen.findByText('NOT ELIGIBLE')).toBeInTheDocument()
    expect(screen.queryByText('ON CONNECT')).not.toBeInTheDocument()
  })

  it('does not promise ON CONNECT while the global rollout-on-connect gate is off', async () => {
    apiGetMock.mockResolvedValue(
      response({
        agents: [agentRow({ online: false })],
        config: { check_interval_secs: 86400, rollout_on_connect: false, server_image_ref: null },
      }),
    )

    renderSection()

    expect(await screen.findByText('OFFLINE')).toBeInTheDocument()
    expect(screen.queryByText('ON CONNECT')).not.toBeInTheDocument()
    expect(screen.getByText(/offline agents wait for APPLY NOW/)).toBeInTheDocument()
  })

  it('says ON CONNECT for an offline eligible agent once the gate is open', async () => {
    apiGetMock.mockResolvedValue(response({ agents: [agentRow({ online: false })] }))

    renderSection()

    expect(await screen.findByText('ON CONNECT')).toBeInTheDocument()
  })
})

describe('UpdatesSection — approve', () => {
  it('sends the on-connect choice the checkbox shows', async () => {
    apiGetMock.mockResolvedValue(response({ active_campaign: null, agents: [] }))
    apiPostMock.mockResolvedValue({ ok: true })

    renderSection()

    fireEvent.click(await screen.findByLabelText(/apply on connect/i))
    fireEvent.click(screen.getByText('APPROVE ROLLOUT · PIN 2.2.0'))

    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/updates/campaigns', { channel: 'stable', on_connect: false }),
    )
  })
})

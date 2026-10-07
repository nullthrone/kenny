import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { HostSection } from '../../api/types'
import { loadSnapshot } from '../../test/contractFixtures'
import type { RawSection } from './types'
import {
  humanizeSectionName,
  isDiskSection,
  isFansSection,
  isGpuSection,
  isHardwareErrorsSection,
  sectionIcon,
} from './sections'

vi.mock('../../api/client', () => ({
  api: { get: vi.fn(() => Promise.resolve({})), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('../../api/sse', () => ({ streamChatEvents: vi.fn(() => () => {}) }))

const { default: SectionModal } = await import('./SectionModal')
const { api } = await import('../../api/client')

const WINDOWS = loadSnapshot('telemetry_snapshot.json')
const LINUX = loadSnapshot('telemetry_snapshot_linux.json')

function section(name: string, over: Partial<HostSection> = {}): HostSection {
  return { name, status: 'warn', attention: true, tier: 'incident', reason: 'because', ...over }
}

function open(sec: HostSection, snapshot: Record<string, RawSection> | null) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <SectionModal agentId="pc-1" section={sec} snapshot={snapshot} aiEnabled={false} onClose={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('section routing and labels', () => {
  it('routes disk and disk_smart to the disk body, and each new section to its own', () => {
    expect(isDiskSection('disk')).toBe(true)
    expect(isDiskSection('disk_smart')).toBe(true)
    expect(isDiskSection('hardware_errors')).toBe(false)
    expect(isHardwareErrorsSection('hardware_errors')).toBe(true)
    expect(isGpuSection('gpu')).toBe(true)
    expect(isFansSection('fans')).toBe(true)
    expect(isFansSection('gpu')).toBe(false)
  })

  it('labels the new sections and tells disk health apart from the volume card', () => {
    expect(humanizeSectionName('hardware_errors')).toBe('Hardware errors')
    expect(humanizeSectionName('gpu')).toBe('GPU')
    expect(humanizeSectionName('fans')).toBe('Fans')
    expect(humanizeSectionName('disk_smart')).toBe('Disk health')
    expect(humanizeSectionName('disk')).toBe('Disk & SMART')
  })

  it('gives each of them its own icon rather than the generic fallback', () => {
    const generic = sectionIcon('no_such_section')
    for (const name of ['hardware_errors', 'gpu', 'fans']) expect(sectionIcon(name)).not.toBe(generic)
    expect(sectionIcon('disk_smart')).toBe(sectionIcon('disk'))
  })
})

describe('SectionModal body per section', () => {
  it('opens the physical-disk view for a flagged disk_smart, not the generic key/value dump', () => {
    open(section('disk_smart', { reason: 'WD_BLACK SN850X: spare capacity low ⇒ crit', status: 'crit' }), WINDOWS)

    expect(screen.getByText('DISK HEALTH · PC-1')).toBeInTheDocument()
    expect(screen.getByText('PHYSICAL DISKS · 3')).toBeInTheDocument()
    expect(screen.getByText('WD_BLACK SN850X 2000GB')).toBeInTheDocument()
    // The two SATA drives in the fixture carry the ATA attribute table.
    expect(screen.getAllByText('SMART ATTRIBUTES (RAW)')).toHaveLength(2)
  })

  it('opens the same body for disk, volumes first', () => {
    open(section('disk'), {
      ...WINDOWS,
      disk: { status: 'warn', summary: '', volumes: [{ mount: 'C:', total_bytes: 1000, free_bytes: 10, percent_used: 99 }], top_dirs: [] },
    })
    expect(screen.getByText('C:')).toBeInTheDocument()
    expect(screen.getByText('PHYSICAL DISKS · 3')).toBeInTheDocument()
  })

  it('opens disk_smart on a host that reports no volumes', () => {
    open(section('disk_smart'), { disk_smart: LINUX.disk_smart })
    expect(screen.getByText('Samsung SSD 980 PRO 1TB')).toBeInTheDocument()
  })

  it('falls back when neither half of the disk data is in the snapshot', () => {
    open(section('disk_smart'), {})
    expect(screen.getByText('No disk data recorded for this host yet.')).toBeInTheDocument()
  })

  it('routes hardware_errors to its body and hands it the verdict details', () => {
    open(
      section('hardware_errors', { details: { findings: ['Graphics driver resets with hardware corroboration'] } }),
      WINDOWS,
    )
    expect(screen.getByText('HARDWARE ERRORS · PC-1')).toBeInTheDocument()
    expect(screen.getByText('EVENT GROUPS · 3')).toBeInTheDocument()
    expect(screen.getByText('FINDINGS')).toBeInTheDocument()
    expect(screen.getByText('Graphics driver resets with hardware corroboration')).toBeInTheDocument()
    // The reliability body's suppression UI is not part of this section.
    expect(screen.queryByText('SUPPRESSION RULES')).not.toBeInTheDocument()
  })

  it('routes gpu to its body', () => {
    open(section('gpu'), WINDOWS)
    expect(screen.getByText('GPU · PC-1')).toBeInTheDocument()
    expect(screen.getByText('NVIDIA GeForce RTX 4080')).toBeInTheDocument()
  })

  it('routes fans to its body, including the empty Windows case', () => {
    const { unmount } = open(section('fans'), LINUX)
    expect(screen.getByText('FANS · PC-1')).toBeInTheDocument()
    expect(screen.getByText('CPU_FAN')).toBeInTheDocument()
    unmount()

    open(section('fans'), WINDOWS)
    expect(screen.getByText(/read on Windows only while LibreHardwareMonitor runs/)).toBeInTheDocument()
  })

  it.each([
    ['hardware_errors', 'No hardware error data recorded for this host yet.'],
    ['gpu', 'No GPU data recorded for this host yet.'],
    ['fans', 'No fan data recorded for this host yet.'],
  ])('says so when %s is not in the snapshot', (name, message) => {
    open(section(name), null)
    expect(screen.getByText(message)).toBeInTheDocument()
  })
})

describe('SectionModal device history', () => {
  const FAN_TRENDS = {
    agent_id: 'pc-1',
    hardware: {
      window_days: 180,
      devices: [
        {
          device_key: 'fan:nct6798.fan1',
          kind: 'fan',
          label: 'CPU_FAN',
          series: {
            rpm_duty_50_70: [
              { day: '2026-06-03', value: 1200 },
              { day: '2026-06-04', value: 1100 },
              { day: '2026-06-05', value: 1000 },
            ],
          },
        },
      ],
      forecasts: [],
    },
  }

  beforeEach(() => {
    vi.mocked(api.get).mockReset()
  })
  afterEach(() => {
    vi.mocked(api.get).mockImplementation(() => Promise.resolve({}))
  })

  it('fetches the trends once and hands the matching series to the body', async () => {
    vi.mocked(api.get).mockResolvedValue(FAN_TRENDS)
    open(section('fans'), LINUX)
    expect(await screen.findByRole('img', { name: 'RPM at 50–70 % duty over 3 days' })).toBeInTheDocument()
    const trendCalls = vi.mocked(api.get).mock.calls.filter(([url]) => String(url).includes('/trends'))
    expect(trendCalls).toEqual([['/api/agent/pc-1/trends']])
  })

  it('leaves the body as it was when the server sends no hardware key', async () => {
    vi.mocked(api.get).mockResolvedValue({ agent_id: 'pc-1', disk: [], battery: null })
    open(section('fans'), LINUX)
    expect(screen.getByText('CPU_FAN')).toBeInTheDocument()
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/agent/pc-1/trends'))
    expect(screen.queryByText('HISTORY')).not.toBeInTheDocument()
  })

  it('does not ask for trends on a section that has no history', () => {
    vi.mocked(api.get).mockResolvedValue({})
    open(section('some_other_section'), { some_other_section: { status: 'ok', summary: '' } })
    expect(vi.mocked(api.get).mock.calls.filter(([url]) => String(url).includes('/trends'))).toHaveLength(0)
  })
})

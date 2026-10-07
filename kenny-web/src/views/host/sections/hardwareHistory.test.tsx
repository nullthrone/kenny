import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { loadSnapshot } from '../../../test/contractFixtures'
import {
  normalizeHardwareTrends,
  type DiskSection,
  type DiskSmartSection,
  type FansSection,
  type GpuSection,
  type HardwareDevice,
  type HardwareErrorsSection,
  type HardwareForecast,
  type HardwareTrends,
} from '../types'
import DiskBody from './DiskBody'
import FansBody from './FansBody'
import GpuBody from './GpuBody'
import HardwareErrorsBody from './HardwareErrorsBody'
import { formatDaysUntil, spanDays } from './hardwareHistory'

const WINDOWS = loadSnapshot('telemetry_snapshot.json')
const LINUX = loadSnapshot('telemetry_snapshot_linux.json')

/** Consecutive UTC days ending 2026-06-05, one value each. */
function days(values: number[]) {
  const end = Date.UTC(2026, 5, 5)
  return values.map((value, i) => ({
    day: new Date(end - (values.length - 1 - i) * 86_400_000).toISOString().slice(0, 10),
    value,
  }))
}

function device(device_key: string, kind: string, series: Record<string, number[]>, label = device_key): HardwareDevice {
  return {
    device_key,
    kind,
    label,
    series: Object.fromEntries(Object.entries(series).map(([metric, values]) => [metric, days(values)])),
  }
}

function trends(devices: HardwareDevice[], forecasts: HardwareForecast[] = []): HardwareTrends {
  return { window_days: 180, devices, forecasts }
}

describe('normalizeHardwareTrends', () => {
  it('reads an absent or malformed hardware key as no history', () => {
    expect(normalizeHardwareTrends(undefined)).toBeNull()
    expect(normalizeHardwareTrends(null)).toBeNull()
    expect(normalizeHardwareTrends({ agent_id: 'pc', disk: [], battery: null })).toBeNull()
    expect(normalizeHardwareTrends({ hardware: null })).toBeNull()
    expect(normalizeHardwareTrends({ hardware: 'nope' })).toBeNull()
    // Some tests answer every URL with the AI-status payload; that is not a trends response either.
    expect(normalizeHardwareTrends({ enabled: true, features: { forecast: true } })).toBeNull()
  })

  it('keeps well-formed devices and forecasts and drops what cannot be shown', () => {
    const out = normalizeHardwareTrends({
      hardware: {
        window_days: 180,
        devices: [
          {
            device_key: 'disk:ABC',
            kind: 'disk',
            label: 'WD',
            series: {
              percentage_used: [
                { day: '2026-06-02', value: 3 },
                { day: '2026-06-01', value: 2 },
                { day: '2026-06-03', value: 'x' },
              ],
              bad: 'not-an-array',
            },
          },
          { kind: 'disk', label: 'no key' },
        ],
        forecasts: [
          { device_key: 'disk:ABC', kind: 'disk', label: 'WD', reason: 'wear_out', symptom: 'Wears out.', days_until: 12.4 },
          { device_key: 'disk:ABC', kind: 'disk', label: 'WD', reason: 'first_error', symptom: 'First error.', days_until: null },
          { device_key: 'disk:ABC', reason: 'wear_out', symptom: '   ' },
        ],
      },
    })
    expect(out?.window_days).toBe(180)
    expect(out?.devices).toHaveLength(1)
    // Oldest first, the non-numeric point and the non-array metric gone.
    expect(out?.devices[0].series).toEqual({
      percentage_used: [
        { day: '2026-06-01', value: 2 },
        { day: '2026-06-02', value: 3 },
      ],
    })
    expect(out?.forecasts.map((f) => f.days_until)).toEqual([12.4, null])
  })

  it('tolerates a hardware block without devices or forecasts', () => {
    expect(normalizeHardwareTrends({ hardware: {} })).toEqual({ window_days: null, devices: [], forecasts: [] })
  })
})

describe('history helpers', () => {
  it('counts the calendar days a series spans, both ends included', () => {
    expect(spanDays(days([1, 2, 3]))).toBe(3)
    expect(spanDays([{ day: '2026-01-01', value: 1 }, { day: '2026-06-29', value: 1 }])).toBe(180)
  })

  it('words a forecast date, and says nothing without one', () => {
    expect(formatDaysUntil(143.5)).toBe('in ~144 days')
    expect(formatDaysUntil(1)).toBe('in ~1 day')
    expect(formatDaysUntil(-3)).toBe('in ~0 days')
    expect(formatDaysUntil(null)).toBeNull()
    expect(formatDaysUntil(undefined)).toBeNull()
  })
})

describe('DiskBody history sparklines', () => {
  const disk = WINDOWS.disk as unknown as DiskSection
  const diskSmart = WINDOWS.disk_smart as unknown as DiskSmartSection
  const NVME = 'disk:23012A800123'

  const history = trends([
    device(NVME, 'disk', {
      percentage_used: [1, 1, 2, 2],
      available_spare: [100, 100, 99, 98],
      available_spare_threshold: [10, 10, 10, 10],
      media_errors: [0, 0, 0, 0],
      read_errors_uncorrected: [0, 0, 1, 3],
      smart_197: [0, 0, 0, 0],
    }),
  ])

  it('draws wear, spare against its threshold and only the error counters that rose, on the matching disk', () => {
    render(<DiskBody disk={disk} diskSmart={diskSmart} focus="physical" history={history} />)

    expect(screen.getByRole('img', { name: 'Endurance used over 4 days' })).toBeInTheDocument()
    const spare = screen.getByRole('img', { name: 'Spare capacity over 4 days' })
    expect(within(spare as unknown as HTMLElement).getByTestId('sparkline-reference')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Uncorrected read errors over 4 days' })).toBeInTheDocument()
    // Flat-zero counters are not worth a chart.
    expect(screen.queryByRole('img', { name: /Media errors/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('img', { name: /Pending sectors/ })).not.toBeInTheDocument()
    // One disk matched by serial; the other two have no series and gain nothing.
    expect(screen.getAllByText('HISTORY')).toHaveLength(1)
  })

  it('matches by serial, not by position', () => {
    render(
      <DiskBody
        diskSmart={diskSmart}
        focus="physical"
        history={trends([device('disk:S5Y2NX0T123456A', 'disk', { percentage_used: [5, 6, 7] })])}
      />,
    )
    expect(screen.getAllByRole('img', { name: /over/ })).toHaveLength(1)
    // The first fixture disk is the Samsung SATA drive with that serial.
    const card = screen.getByText('Samsung SSD 870 EVO 1TB').parentElement?.parentElement as HTMLElement
    expect(within(card).getByRole('img', { name: 'Endurance used over 3 days' })).toBeInTheDocument()
  })

  it('renders exactly as before when the history is absent, empty or for another disk', () => {
    const { container: bare, unmount: unmountBare } = render(<DiskBody disk={disk} diskSmart={diskSmart} focus="physical" />)
    const base = bare.innerHTML
    unmountBare()
    for (const h of [null, trends([]), trends([device('disk:OTHER', 'disk', { percentage_used: [1, 2, 3] })])]) {
      const { container, unmount } = render(<DiskBody disk={disk} diskSmart={diskSmart} focus="physical" history={h} />)
      expect(container.innerHTML).toBe(base)
      unmount()
    }
  })

  it('does not draw a one-point series', () => {
    render(<DiskBody diskSmart={diskSmart} focus="physical" history={trends([device(NVME, 'disk', { percentage_used: [2] })])} />)
    expect(screen.queryByText('HISTORY')).not.toBeInTheDocument()
  })
})

describe('FansBody history sparklines', () => {
  const fans = LINUX.fans as unknown as FansSection
  const history = trends(
    [
      device('fan:nct6798.fan1', 'fan', {
        rpm_duty_30_50: [900, 905, 890],
        rpm_duty_50_70: [1200, 1190, 1100, 1050],
        stall_seen: [0, 0, 0],
      }),
    ],
    [
      {
        device_key: 'fan:nct6798.fan1',
        kind: 'fan',
        label: 'CPU_FAN',
        reason: 'fan_drift',
        symptom: 'CPU_FAN is slowing at the same power setting — bearing wear likely.',
        days_until: null,
      },
      {
        device_key: 'fan:nct6798.fan2',
        kind: 'fan',
        label: 'CHA_FAN1',
        reason: 'wear_out',
        symptom: 'Not a drift forecast.',
        days_until: null,
      },
    ],
  )

  it('draws one sparkline per duty band that has data, for the fan with that key', () => {
    render(<FansBody fans={fans} history={history} />)
    expect(screen.getByRole('img', { name: 'RPM at 30–50 % duty over 3 days' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'RPM at 50–70 % duty over 4 days' })).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: /70–90/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('img', { name: /Stall/i })).not.toBeInTheDocument()
    expect(screen.getAllByRole('img', { name: /duty/ })).toHaveLength(2)
  })

  it('shows the fan_drift symptom for that fan only', () => {
    render(<FansBody fans={fans} history={history} />)
    expect(screen.getByText(/CPU_FAN is slowing at the same power setting/)).toBeInTheDocument()
    expect(screen.queryByText('Not a drift forecast.')).not.toBeInTheDocument()
  })

  it('renders exactly as before without history, or when the key matches no fan', () => {
    const { container: bare, unmount: unmountBare } = render(<FansBody fans={fans} />)
    const base = bare.innerHTML
    unmountBare()
    for (const h of [null, trends([]), trends([device('fan:other', 'fan', { rpm_duty_50_70: [1, 2, 3] })])]) {
      const { container, unmount } = render(<FansBody fans={fans} history={h} />)
      expect(container.innerHTML).toBe(base)
      unmount()
    }
  })
})

describe('GpuBody history sparkline', () => {
  const gpu = WINDOWS.gpu as unknown as GpuSection
  const UUID = 'GPU-4f1c2a6e-8d3b-7c59-1e20-a9b3c4d5e6f7'

  it('draws the loaded PCIe width against the card maximum', () => {
    const history = trends([
      device(`gpu:${UUID}`, 'gpu', { pcie_width_loaded_max: [16, 16, 8, 8], pcie_width_max: [16, 16, 16, 16] }),
    ])
    render(<GpuBody gpu={gpu} history={history} />)
    const chart = screen.getByRole('img', { name: 'PCIe width under load over 4 days' })
    expect(within(chart as unknown as HTMLElement).getByTestId('sparkline-reference')).toBeInTheDocument()
    expect(screen.getByText('×8')).toBeInTheDocument()
  })

  it('matches a card without a uuid by its bus id, as the server keys it', () => {
    const noUuid = { ...gpu, gpus: (gpu.gpus ?? []).map((g) => ({ ...g, uuid: null })) } as GpuSection
    const busId = (gpu.gpus ?? [])[0]?.bus_id
    const history = trends([device(`gpu:${busId}`, 'gpu', { pcie_width_loaded_max: [16, 16, 16] })])
    render(<GpuBody gpu={noUuid} history={history} />)
    expect(screen.getByRole('img', { name: 'PCIe width under load over 3 days' })).toBeInTheDocument()
  })

  it('adds nothing without a series for this card', () => {
    const { container: bare, unmount: unmountBare } = render(<GpuBody gpu={gpu} />)
    const base = bare.innerHTML
    unmountBare()
    for (const h of [
      null,
      trends([]),
      trends([device(`gpu:${UUID}`, 'gpu', { hw_slowdown_seen: [0, 1, 0] })]),
      trends([device('gpu:OTHER', 'gpu', { pcie_width_loaded_max: [16, 8, 8] })]),
    ]) {
      const { container, unmount } = render(<GpuBody gpu={gpu} history={h} />)
      expect(container.innerHTML).toBe(base)
      unmount()
    }
  })
})

describe('HardwareErrorsBody history sparklines', () => {
  const hardware = WINDOWS.hardware_errors as unknown as HardwareErrorsSection
  const history = trends([
    device('host:memory', 'component', { corrected_events: [0, 2, 5], fatal_events: [0, 0, 0] }, 'Memory'),
    device('host:gpu', 'component', { instability_events: [0, 1, 0], corrected_events: [0, 0, 0] }, 'Graphics'),
    device('host:cpu', 'component', { corrected_events: [0, 0, 0] }, 'Processor'),
    device('disk:X', 'disk', { corrected_events: [1, 2, 3] }, 'Some disk'),
  ])

  it('draws per-component daily counts for components that logged events', () => {
    render(<HardwareErrorsBody hardware={hardware} history={history} />)
    expect(screen.getByText('EVENT HISTORY · PER COMPONENT')).toBeInTheDocument()
    expect(screen.getByText('MEMORY')).toBeInTheDocument()
    expect(screen.getByText('GRAPHICS')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Corrected events over 3 days' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Instability events over 3 days' })).toBeInTheDocument()
    // Quiet components and non-component devices stay out.
    expect(screen.queryByText('PROCESSOR')).not.toBeInTheDocument()
    expect(screen.queryByText('SOME DISK')).not.toBeInTheDocument()
    expect(screen.queryByRole('img', { name: /Fatal/ })).not.toBeInTheDocument()
    expect(screen.getAllByRole('img', { name: /over 3 days/ })).toHaveLength(2)
  })

  it('renders exactly as before without history', () => {
    const { container: bare, unmount: unmountBare } = render(<HardwareErrorsBody hardware={hardware} />)
    const base = bare.innerHTML
    unmountBare()
    for (const h of [null, trends([]), trends([device('host:cpu', 'component', { corrected_events: [0, 0, 0] })])]) {
      const { container, unmount } = render(<HardwareErrorsBody hardware={hardware} history={h} />)
      expect(container.innerHTML).toBe(base)
      unmount()
    }
  })
})

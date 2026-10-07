import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { loadSnapshot } from '../../../test/contractFixtures'
import type {
  DiskSection,
  DiskSmartRow,
  DiskSmartSection,
  FansSection,
  GpuSection,
  HardwareErrorsSection,
} from '../types'
import DiskBody, { decodeNvmeWarning } from './DiskBody'
import FansBody, { formatRpm } from './FansBody'
import GpuBody, { activeThrottles } from './GpuBody'
import HardwareErrorsBody, { readFindings } from './HardwareErrorsBody'
import { formatPcieLink, formatPowerOnHours, formatTemp } from './hardwareFormat'

const WINDOWS = loadSnapshot('telemetry_snapshot.json')
const LINUX = loadSnapshot('telemetry_snapshot_linux.json')

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-06-05T12:00:00Z'))
})
afterEach(() => vi.useRealTimers())

function smart(...disks: DiskSmartRow[]): DiskSmartSection {
  return { status: 'ok', summary: 'SMART healthy', disks }
}

/** The labelled `<dd>` next to a `<dt>` — the row a counter is read from. */
function statValue(scope: HTMLElement, label: string): HTMLElement {
  const dt = within(scope).getByText(label, { selector: 'dt' })
  return dt.nextElementSibling as HTMLElement
}

describe('hardware formatting helpers', () => {
  it('answers a dash for an unreported reading, never zero', () => {
    expect(formatTemp(null)).toBe('—')
    expect(formatTemp(undefined, undefined)).toBe('—')
    expect(formatPowerOnHours(null)).toBe('—')
    expect(formatPcieLink(null)).toBe('—')
    expect(formatRpm(null)).toBe('—')
  })

  it('formats temperature, power-on time and the PCIe link', () => {
    expect(formatTemp(41, 74)).toBe('41 °C (max 74 °C)')
    expect(formatTemp(41)).toBe('41 °C')
    expect(formatPowerOnHours(12)).toBe('12 h')
    expect(formatPowerOnHours(1520)).toBe('1,520 h (63 d)')
    expect(formatPcieLink({ gen_current: 1, gen_max: 4, width_current: 16, width_max: 16 })).toBe('Gen 1 ×16 (max Gen 4 ×16)')
    expect(formatPcieLink({ gen_current: 3, width_current: 8 })).toBe('Gen 3 ×8')
  })
})

describe('DiskBody — physical disks (disk_smart)', () => {
  const disk = WINDOWS.disk as unknown as DiskSection
  const diskSmart = WINDOWS.disk_smart as unknown as DiskSmartSection

  it('shows each fixture disk with identity, health, temperature and power-on time', () => {
    render(<DiskBody disk={disk} diskSmart={diskSmart} focus="physical" />)

    expect(screen.getByText('PHYSICAL DISKS · 3')).toBeInTheDocument()
    expect(screen.getByText('Samsung SSD 870 EVO 1TB')).toBeInTheDocument()
    expect(screen.getByText('S5Y2NX0T123456A')).toBeInTheDocument()
    expect(screen.getByText('SATA · SSD')).toBeInTheDocument()
    expect(screen.getByText('SATA · HDD')).toBeInTheDocument()
    expect(screen.getByText('NVMe · SSD')).toBeInTheDocument()
    expect(screen.getAllByText('HEALTHY')).toHaveLength(3)
    expect(screen.getByText('34 °C (max 58 °C)')).toBeInTheDocument()
    expect(screen.getByText('8,123 h (338 d)')).toBeInTheDocument()
  })

  it('lists the physical disks before the volumes when disk_smart is the section opened', () => {
    const { container } = render(<DiskBody disk={{ ...disk, volumes: [{ mount: 'C:', total_bytes: 100, free_bytes: 50, percent_used: 50 }] }} diskSmart={diskSmart} focus="physical" />)
    const text = container.textContent ?? ''
    expect(text.indexOf('PHYSICAL DISKS')).toBeLessThan(text.indexOf('C:'))
  })

  it('names the six SMART attributes and highlights a non-zero one', () => {
    const sata = diskSmart.disks![0]
    render(
      <DiskBody
        diskSmart={smart({ ...sata, smart_attributes: { '5': 0, '187': 0, '188': 0, '197': 8, '198': 0, '199': 3 } })}
        focus="physical"
      />,
    )
    const card = screen.getByText('Samsung SSD 870 EVO 1TB').closest('div')!.parentElement as HTMLElement
    for (const name of [
      '5 · Reallocated sectors',
      '187 · Reported uncorrectable',
      '188 · Command timeouts',
      '197 · Pending sectors',
      '198 · Offline uncorrectable',
      '199 · Interface CRC errors',
    ]) {
      expect(within(card).getByText(name)).toBeInTheDocument()
    }
    expect(statValue(card, '197 · Pending sectors')).toHaveAttribute('data-tone', 'alert')
    expect(statValue(card, '199 · Interface CRC errors')).toHaveAttribute('data-tone', 'alert')
    expect(statValue(card, '5 · Reallocated sectors')).not.toHaveAttribute('data-tone')
  })

  it('shows the NVMe health log from the fixture, spare against its threshold', () => {
    render(<DiskBody diskSmart={LINUX.disk_smart as unknown as DiskSmartSection} focus="physical" />)
    expect(screen.getByText('NVME HEALTH LOG')).toBeInTheDocument()
    expect(screen.getByText('100% (threshold 10%)')).toBeInTheDocument()
    expect(screen.getByText('Endurance used').nextElementSibling).toHaveTextContent('4%')
    expect(screen.getByText('Media errors').nextElementSibling).toHaveTextContent('0')
    expect(screen.getByText('Unsafe shutdowns').nextElementSibling).toHaveTextContent('31')
    expect(screen.getByText('Critical warning').nextElementSibling).toHaveTextContent('none')
  })

  it('highlights non-zero error counters and a SMART predict-failure flag', () => {
    const nvme = (LINUX.disk_smart as unknown as DiskSmartSection).disks![0]
    render(
      <DiskBody
        diskSmart={smart({
          ...nvme,
          health_status: 'Unhealthy',
          predictive_failure: true,
          read_errors_uncorrected: 4,
          nvme: { ...nvme.nvme!, media_errors: 12, critical_warning: 0b1 },
        })}
        focus="physical"
      />,
    )
    expect(screen.getByText('PREDICTS FAILURE')).toBeInTheDocument()
    expect(screen.getByText('UNHEALTHY')).toBeInTheDocument()
    expect(screen.getByText('Uncorrected read errors').nextElementSibling).toHaveAttribute('data-tone', 'alert')
    expect(screen.getByText('Media errors').nextElementSibling).toHaveAttribute('data-tone', 'alert')
    expect(screen.getByText('Critical warning').nextElementSibling).toHaveTextContent('spare below threshold')
    expect(screen.getByText('Uncorrected write errors').nextElementSibling).not.toHaveAttribute('data-tone')
  })

  it('does not alarm on the lifetime read-error total of a healthy HDD', () => {
    render(<DiskBody diskSmart={diskSmart} focus="physical" />)
    const hdd = screen.getByText('ST2000DM008-2FR102').closest('div')!.parentElement as HTMLElement
    expect(statValue(hdd, 'Read errors (all)')).toHaveTextContent('184,223,611')
    expect(statValue(hdd, 'Read errors (all)')).toHaveAttribute('data-tone', 'muted')
  })

  it('says why the health log is missing and never reads it as healthy', () => {
    const nvme = (LINUX.disk_smart as unknown as DiskSmartSection).disks![0]
    render(<DiskBody diskSmart={smart({ ...nvme, nvme: null, nvme_error: 'unsupported by driver', health_status: 'Unknown' })} focus="physical" />)
    expect(screen.getByText('health log unavailable: unsupported by driver')).toBeInTheDocument()
    expect(screen.queryByText('NVME HEALTH LOG')).not.toBeInTheDocument()
    expect(screen.queryByText('HEALTHY')).not.toBeInTheDocument()
  })

  it('notes that raw-disk reads are paused', () => {
    render(<DiskBody diskSmart={smart({ model: 'X', paused: true, nvme: null, smart_attributes: null, predictive_failure: null })} focus="physical" />)
    expect(screen.getByText('raw-disk reads paused while a protected game runs')).toBeInTheDocument()
  })

  it('renders an old-shaped row (none of the 0.22 fields) without inventing values', () => {
    render(<DiskBody diskSmart={smart({ model: 'Legacy', health_status: 'Healthy', wear: null, read_errors_uncorrected: 0 })} focus="physical" />)
    expect(screen.getByText('Legacy')).toBeInTheDocument()
    expect(screen.getByText('Serial').nextElementSibling).toHaveTextContent('—')
    expect(screen.getByText('Temperature').nextElementSibling).toHaveTextContent('—')
    expect(screen.queryByText('SMART ATTRIBUTES (RAW)')).not.toBeInTheDocument()
  })

  it('says so when no physical disk can be listed', () => {
    render(<DiskBody diskSmart={smart()} focus="physical" />)
    expect(screen.getByText('No physical disk could be listed on this host.')).toBeInTheDocument()
  })

  it('keeps the volume list intact for the disk section', () => {
    render(
      <DiskBody
        disk={{ status: 'warn', summary: '', reason: 'C: 96% full (>=95%)', volumes: [{ mount: 'C:', total_bytes: 1024 ** 3 * 100, free_bytes: 1024 ** 3 * 4, percent_used: 96 }], top_dirs: [{ path: 'C:\\Users', bytes: 1024 ** 3 * 30 }] }}
        diskSmart={diskSmart}
      />,
    )
    expect(screen.getByText('C:')).toBeInTheDocument()
    expect(screen.getByText('96 GB / 100 GB (96%)')).toBeInTheDocument()
    expect(screen.getByText('LARGEST DIRECTORIES')).toBeInTheDocument()
    expect(screen.getByText('PHYSICAL DISKS · 3')).toBeInTheDocument()
  })

  it('falls back to the one-line SMART summary for a payload without rows', () => {
    render(<DiskBody diskSmart={{ status: 'ok', summary: 'SMART healthy' }} />)
    expect(screen.getByText('SMART: SMART healthy')).toBeInTheDocument()
  })

  it('decodes the NVMe critical-warning bitfield', () => {
    expect(decodeNvmeWarning(0)).toEqual({ labels: [], serious: false })
    expect(decodeNvmeWarning(null)).toEqual({ labels: [], serious: false })
    expect(decodeNvmeWarning(0b10)).toEqual({ labels: ['temperature'], serious: false })
    expect(decodeNvmeWarning(0b1101)).toEqual({ labels: ['spare below threshold', 'reliability degraded', 'read-only'], serious: true })
  })
})

describe('HardwareErrorsBody', () => {
  const windows = WINDOWS.hardware_errors as unknown as HardwareErrorsSection
  const linux = LINUX.hardware_errors as unknown as HardwareErrorsSection

  it('lists the fixture groups with count, recency, active days and sample', () => {
    render(<HardwareErrorsBody hardware={windows} />)

    expect(screen.getByText('EVENT GROUPS · 3')).toBeInTheDocument()
    const row = screen.getByText('Microsoft-Windows-WHEA-Logger').closest('tr') as HTMLElement
    expect(within(row).getByText('#19')).toBeInTheDocument()
    expect(within(row).getByText('WARNING')).toBeInTheDocument()
    expect(within(row).getByText('3×')).toBeInTheDocument()
    expect(within(row).getByText('3 days')).toBeInTheDocument()
    expect(within(row).getByText('A corrected hardware error has occurred.')).toBeInTheDocument()
    expect(within(row).getByText(/ago$/)).toBeInTheDocument()
  })

  it('expands a group to its details key/value counts', () => {
    render(<HardwareErrorsBody hardware={windows} />)
    expect(screen.queryByText('error_source')).not.toBeInTheDocument()

    const row = screen.getByText('Microsoft-Windows-WHEA-Logger').closest('tr') as HTMLElement
    const toggle = within(row).getByRole('button', { name: 'DETAILS' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)

    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('error_source')).toBeInTheDocument()
    expect(screen.getByText('Bus/Interconnect Error')).toBeInTheDocument()
    expect(screen.getByText('processor_apic_id')).toBeInTheDocument()

    fireEvent.click(toggle)
    expect(screen.queryByText('error_source')).not.toBeInTheDocument()
  })

  it('summarises application crashes', () => {
    render(<HardwareErrorsBody hardware={windows} />)
    expect(screen.getByText('APPLICATION CRASHES')).toBeInTheDocument()
    expect(screen.getByText('Distinct applications').nextElementSibling).toHaveTextContent('5')
    expect(screen.getByText('Days with crashes').nextElementSibling).toHaveTextContent('4')
    expect(screen.getByText(/0xc0000005/)).toBeInTheDocument()
  })

  it('shows no EDAC or AER table when both lists are empty', () => {
    render(<HardwareErrorsBody hardware={windows} />)
    expect(screen.queryByText('MEMORY CONTROLLERS (EDAC)')).not.toBeInTheDocument()
    expect(screen.queryByText('PCIE ERRORS (AER)')).not.toBeInTheDocument()
  })

  it('shows the Linux EDAC and AER tables and a source-keyed group without an event id', () => {
    render(<HardwareErrorsBody hardware={linux} />)
    expect(screen.getByText('MEMORY CONTROLLERS (EDAC)')).toBeInTheDocument()
    expect(screen.getByText('mc0')).toBeInTheDocument()
    expect(screen.getByText('PCIE ERRORS (AER)')).toBeInTheDocument()
    expect(screen.getByText('0000:01:00.0')).toBeInTheDocument()
    expect(screen.getByText('nvrm_xid')).toBeInTheDocument()
    expect(screen.queryByText('#0')).not.toBeInTheDocument()
    // Linux has no application-crash aggregate.
    expect(screen.queryByText('APPLICATION CRASHES')).not.toBeInTheDocument()
  })

  it('flags an uncorrected EDAC count', () => {
    render(<HardwareErrorsBody hardware={{ ...linux, edac: [{ controller: 'mc1', ce_count: 0, ue_count: 2 }] }} />)
    const cells = screen.getByText('mc1').closest('tr')!.querySelectorAll('td')
    expect(cells[1]).not.toHaveAttribute('data-tone')
    expect(cells[2]).toHaveAttribute('data-tone', 'alert')
  })

  it('warns when the event log reaches back less far than the window', () => {
    render(<HardwareErrorsBody hardware={{ ...windows, effective_window_days: 3 }} />)
    expect(screen.getByText(/Event log only reaches back 3 days \(queried 14\)/)).toBeInTheDocument()
  })

  it('says nothing about the window when it is fully covered or unknown', () => {
    const { rerender } = render(<HardwareErrorsBody hardware={windows} />)
    expect(screen.queryByText(/only reaches back/)).not.toBeInTheDocument()
    rerender(<HardwareErrorsBody hardware={{ ...windows, effective_window_days: null }} />)
    expect(screen.queryByText(/only reaches back/)).not.toBeInTheDocument()
  })

  it('renders the verdict findings from the section details when present', () => {
    render(
      <HardwareErrorsBody
        hardware={windows}
        details={{
          findings: [
            { component: 'gpu', reason: 'The graphics driver crashed and recovered 6× in 4 days', status: 'warn' },
            'Retries on an internal disk',
          ],
        }}
      />,
    )
    expect(screen.getByText('FINDINGS')).toBeInTheDocument()
    expect(screen.getByText('The graphics driver crashed and recovered 6× in 4 days')).toBeInTheDocument()
    expect(screen.getByText('GPU')).toBeInTheDocument()
    expect(screen.getByText('Retries on an internal disk')).toBeInTheDocument()
  })

  it('has no findings block without verdict evidence', () => {
    render(<HardwareErrorsBody hardware={windows} details={undefined} />)
    expect(screen.queryByText('FINDINGS')).not.toBeInTheDocument()
    expect(readFindings({ findings: 'nope' })).toEqual([])
    expect(readFindings({ findings: [{ component: 'cpu' }, 7, null] })).toEqual([])
  })

  it('has an empty state, reports dropped groups and lists probe errors', () => {
    const { rerender } = render(<HardwareErrorsBody hardware={{ status: 'ok', summary: '0 hardware-relevant events in 14d', groups: [] }} />)
    expect(screen.getByText('No hardware-relevant events in the window.')).toBeInTheDocument()

    rerender(
      <HardwareErrorsBody
        hardware={{ ...windows, truncated: true, truncated_count: 5, errors: ['Get-WinEvent timed out'] }}
      />,
    )
    expect(screen.getByText('EVENT GROUPS · 3+')).toBeInTheDocument()
    expect(screen.getByText(/5 more groups not reported/)).toBeInTheDocument()
    expect(screen.getByText('PROBE ERRORS')).toBeInTheDocument()
    expect(screen.getByText('Get-WinEvent timed out')).toBeInTheDocument()
  })

  it('tolerates a payload with nothing but status and summary', () => {
    render(<HardwareErrorsBody hardware={{ status: 'ok', summary: 'n/a' }} />)
    expect(screen.getByText('No hardware-relevant events in the window.')).toBeInTheDocument()
  })

  it('tolerates null by_day, details and last_seen on a group', () => {
    render(
      <HardwareErrorsBody
        hardware={{ status: 'ok', groups: [{ source: 'disk', event_id: 7, level: 'error', count: 1, last_seen: null, by_day: null, sample: null, details: null }] }}
      />,
    )
    expect(screen.getByText('0 days')).toBeInTheDocument()
    expect(screen.getByText('never')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'DETAILS' })).not.toBeInTheDocument()
  })
})

describe('GpuBody', () => {
  const windows = WINDOWS.gpu as unknown as GpuSection
  const linux = LINUX.gpu as unknown as GpuSection

  it('shows the fixture card: identity, load, power, fan target and the PCIe link', () => {
    render(<GpuBody gpu={windows} />)

    expect(screen.getByText('GRAPHICS ADAPTERS · 1')).toBeInTheDocument()
    expect(screen.getByText('NVIDIA GeForce RTX 4080')).toBeInTheDocument()
    expect(screen.getByText('NVIDIA')).toBeInTheDocument()
    expect(screen.getByText('560.94')).toBeInTheDocument()
    expect(screen.getByText('47 °C')).toBeInTheDocument()
    expect(screen.getByText('Utilization').nextElementSibling).toHaveTextContent('6%')
    expect(screen.getByText('38.5 W / 320 W')).toBeInTheDocument()
    expect(screen.getByText('30% target')).toBeInTheDocument()
    expect(screen.getByText('Gen 1 ×16 (max Gen 4 ×16)')).toBeInTheDocument()
    expect(screen.getByText(/downshifts at idle by design/)).toBeInTheDocument()
  })

  it('says no throttling when every reason is false, and shows no badge', () => {
    render(<GpuBody gpu={windows} />)
    expect(screen.getByText('No clock throttling reported.')).toBeInTheDocument()
    expect(screen.queryByText('HW SLOWDOWN')).not.toBeInTheDocument()
  })

  it('shows a badge for each asserted throttle reason', () => {
    const card = windows.gpus![0]
    render(
      <GpuBody
        gpu={{ ...windows, gpus: [{ ...card, throttle: { hw_slowdown: true, hw_thermal_slowdown: false, hw_power_brake_slowdown: true, sw_thermal_slowdown: false } }] }}
      />,
    )
    expect(screen.getByText('HW SLOWDOWN')).toBeInTheDocument()
    expect(screen.getByText('POWER BRAKE')).toBeInTheDocument()
    expect(screen.queryByText('HW THERMAL SLOWDOWN')).not.toBeInTheDocument()
    expect(screen.queryByText('No clock throttling reported.')).not.toBeInTheDocument()
  })

  it('treats unreported throttle reasons as unknown, not as none', () => {
    render(<GpuBody gpu={linux} />)
    expect(screen.getByText('Throttle reasons are not reported by this driver.')).toBeInTheDocument()
    expect(activeThrottles(null)).toBeNull()
    expect(activeThrottles({})).toBeNull()
    expect(activeThrottles({ hw_slowdown: false })).toEqual([])
  })

  it('renders nulls as dashes and the amdgpu RAS counts', () => {
    render(<GpuBody gpu={linux} />)
    expect(screen.getByText('AMD Radeon RX 7800 XT')).toBeInTheDocument()
    expect(screen.getByText('Driver').nextElementSibling).toHaveTextContent('—')
    expect(screen.getByText('Fan').nextElementSibling).toHaveTextContent('—')
    expect(screen.getByText('RAS ERROR COUNTS')).toBeInTheDocument()
    expect(screen.getByText('UE 0 · CE 2')).toBeInTheDocument()
  })

  it('shows ECC and highlights uncorrected errors and pending retired pages', () => {
    const card = windows.gpus![0]
    render(
      <GpuBody
        gpu={{ ...windows, gpus: [{ ...card, ecc: { uncorrected_volatile: 3, retired_pages_pending: true, remapped_rows: { correctable: 1, uncorrectable: 0, pending: 0, failure: 0 } } }] }}
      />,
    )
    expect(screen.getByText('ECC / RETIRED PAGES')).toBeInTheDocument()
    expect(screen.getByText('Uncorrected (volatile)').nextElementSibling).toHaveAttribute('data-tone', 'alert')
    expect(screen.getByText('Retired pages pending').nextElementSibling).toHaveTextContent('yes')
    expect(screen.getByText('Remapped rows (uncorrectable)').nextElementSibling).not.toHaveAttribute('data-tone')
  })

  it('shows no ECC block on a consumer card', () => {
    render(<GpuBody gpu={windows} />)
    expect(screen.queryByText('ECC / RETIRED PAGES')).not.toBeInTheDocument()
  })

  it('has an empty state for a host without a readable GPU', () => {
    render(<GpuBody gpu={{ status: 'ok', gpus: [] }} />)
    expect(screen.getByText('No graphics adapter could be read on this host.')).toBeInTheDocument()
  })

  it('does not call the link a downshift when it runs at its maximum', () => {
    const card = windows.gpus![0]
    render(<GpuBody gpu={{ ...windows, gpus: [{ ...card, pcie: { gen_current: 4, gen_max: 4, width_current: 16, width_max: 16 } }] }} />)
    expect(screen.queryByText(/downshifts at idle/)).not.toBeInTheDocument()
  })
})

describe('FansBody', () => {
  const linux = LINUX.fans as unknown as FansSection
  const windows = WINDOWS.fans as unknown as FansSection

  it('shows each fan with mean, min–max, duty, mode and a sparkline', () => {
    render(<FansBody fans={linux} />)

    expect(screen.getByText('FANS · 3')).toBeInTheDocument()
    const cpu = screen.getByText('CPU_FAN').closest('div')!.parentElement as HTMLElement
    expect(within(cpu).getByText('HWMON')).toBeInTheDocument()
    expect(within(cpu).getByText('nct6798.fan1')).toBeInTheDocument()
    expect(statValue(cpu, 'Mean speed')).toHaveTextContent('1,180 rpm')
    expect(statValue(cpu, 'Min–max')).toHaveTextContent('1,176–1,182')
    expect(statValue(cpu, 'Duty')).toHaveTextContent('45%')
    expect(statValue(cpu, 'Mode')).toHaveTextContent('pwm')
    expect(within(cpu).getByRole('img', { name: 'Trend' })).toBeInTheDocument()
  })

  it('shows a dash for an unreadable duty', () => {
    render(<FansBody fans={linux} />)
    const cha = screen.getByText('CHA_FAN1').closest('div')!.parentElement as HTMLElement
    expect(statValue(cha, 'Duty')).toHaveTextContent('—')
    expect(statValue(cha, 'Mode')).toHaveTextContent('auto')
  })

  it('collapses idle_or_absent channels under "unused channels" and keeps them out of the fan list', () => {
    render(
      <FansBody
        fans={{
          ...linux,
          fans: [
            ...linux.fans!,
            { key: 'nct6798.fan4', label: 'AIO_PUMP', source: 'hwmon', rpm_samples: [0, 0, 0, 0, 0], duty_percent: null, mode: 'unknown', idle_or_absent: true },
          ],
        }}
      />,
    )
    expect(screen.getByText('FANS · 3')).toBeInTheDocument()
    const unused = screen.getByText('UNUSED CHANNELS · 1').closest('details') as HTMLElement
    expect(unused).not.toHaveAttribute('open')
    expect(within(unused).getByText(/AIO_PUMP/)).toBeInTheDocument()
    // Not rendered as a fan card: no stat rows for it.
    expect(screen.getAllByText('Mean speed')).toHaveLength(3)
  })

  it('explains the empty list on a Windows host without LibreHardwareMonitor', () => {
    render(<FansBody fans={windows} />)
    expect(
      screen.getByText(
        'No fan speed was read. Fan speeds are read on Windows only while LibreHardwareMonitor runs; on Linux from hwmon.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByText('Sources tried: lhm_wmi.')).toBeInTheDocument()
    expect(screen.queryByText(/^FANS/)).not.toBeInTheDocument()
  })

  it('draws no sparkline for a single sample and tolerates a fan without samples', () => {
    render(
      <FansBody
        fans={{
          status: 'ok',
          fans: [
            { key: 'a', label: null, source: 'lhm', rpm_samples: [900], duty_percent: null, mode: null },
            { key: 'b', rpm_samples: null },
          ],
        }}
      />,
    )
    expect(screen.queryByRole('img', { name: 'Trend' })).not.toBeInTheDocument()
    const b = screen.getByText('b').closest('div')!.parentElement as HTMLElement
    expect(statValue(b, 'Mean speed')).toHaveTextContent('—')
    expect(statValue(b, 'Min–max')).toHaveTextContent('—')
  })

  it('lists probe errors', () => {
    render(<FansBody fans={{ ...linux, errors: ['hwmon: permission denied'] }} />)
    expect(screen.getByText('hwmon: permission denied')).toBeInTheDocument()
  })
})

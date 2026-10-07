import type { GpuCard, GpuEcc, GpuSection, GpuThrottle, HardwareTrends } from '../types'
import { DASH, finite, formatCount, formatPcieLink, formatPercent, formatTemp, formatWatts, isNonZero } from './hardwareFormat'
import { EmptyNote, Eyebrow, HwCard, HwCards, HwChip, Note, StatList, Subhead, type StatItem } from './HardwareParts'
import { HistoryList, HistorySpark } from './HistorySpark'
import { drawable, findDevice, lastValue } from './hardwareHistory'
import styles from './GpuBody.module.css'

export interface GpuBodyProps {
  gpu: GpuSection
  /** The long-lived device history (`/trends` -> `hardware`); absent on an older server. */
  history?: HardwareTrends | null
}

const THROTTLE_LABELS: { key: keyof GpuThrottle; label: string; title: string }[] = [
  { key: 'hw_slowdown', label: 'HW SLOWDOWN', title: 'The GPU slowed itself down in hardware (power or temperature protection).' },
  { key: 'hw_thermal_slowdown', label: 'HW THERMAL SLOWDOWN', title: 'Hardware thermal protection reduced the clocks.' },
  { key: 'hw_power_brake_slowdown', label: 'POWER BRAKE', title: 'An external power-brake signal slowed the GPU (a PSU or cable signal).' },
  { key: 'sw_thermal_slowdown', label: 'SW THERMAL SLOWDOWN', title: 'The driver reduced the clocks to stay under the temperature limit.' },
]

/** Reasons currently asserted. `null` when the driver reports none of them at all. */
export function activeThrottles(throttle: GpuThrottle | null | undefined): typeof THROTTLE_LABELS | null {
  if (!throttle) return null
  const reported = THROTTLE_LABELS.filter(({ key }) => typeof throttle[key] === 'boolean')
  if (reported.length === 0) return null
  return reported.filter(({ key }) => throttle[key] === true)
}

function yesNo(flag: boolean | null | undefined): string {
  return flag == null ? DASH : flag ? 'yes' : 'no'
}

function eccItems(ecc: GpuEcc): StatItem[] {
  const rows = ecc.remapped_rows
  const items: StatItem[] = [
    {
      label: 'Uncorrected (volatile)',
      value: formatCount(ecc.uncorrected_volatile),
      tone: isNonZero(ecc.uncorrected_volatile) ? 'alert' : undefined,
    },
    {
      label: 'Retired pages pending',
      value: yesNo(ecc.retired_pages_pending),
      tone: ecc.retired_pages_pending === true ? 'alert' : undefined,
    },
  ]
  if (rows) {
    items.push(
      { label: 'Remapped rows (correctable)', value: formatCount(rows.correctable) },
      { label: 'Remapped rows (uncorrectable)', value: formatCount(rows.uncorrectable), tone: isNonZero(rows.uncorrectable) ? 'alert' : undefined },
      { label: 'Remapped rows (pending)', value: yesNo(rows.pending), tone: rows.pending === true ? 'alert' : undefined },
      { label: 'Row-remap failure', value: yesNo(rows.failure), tone: rows.failure === true ? 'alert' : undefined },
    )
  }
  return items
}

function rasItems(ras: NonNullable<GpuCard['ras']>): StatItem[] {
  return Object.entries(ras).map(([block, counts]) => ({
    label: block,
    value: `UE ${formatCount(counts?.ue)} · CE ${formatCount(counts?.ce)}`,
    tone: isNonZero(counts?.ue) ? 'alert' : undefined,
  }))
}

function pcieIsDownshifted(pcie: GpuCard['pcie']): boolean {
  if (!pcie) return false
  const gen = [finite(pcie.gen_current), finite(pcie.gen_max)]
  const width = [finite(pcie.width_current), finite(pcie.width_max)]
  return (gen[0] !== null && gen[1] !== null && gen[0] < gen[1]) || (width[0] !== null && width[1] !== null && width[0] < width[1])
}

/** The widest PCIe link seen under load per day, against the card's own maximum. */
function GpuHistory({ gpu, history }: { gpu: GpuCard; history?: HardwareTrends | null }) {
  // The server keys a GPU's history by its first identity field present
  // (kenny_server/hardware_metrics.py `_gpu_key`): uuid, then bus id, then PCI id.
  const id = gpu.uuid ?? gpu.bus_id ?? gpu.pci_id
  const device = findDevice(history, id ? `gpu:${id}` : null)
  const loaded = drawable(device, 'pcie_width_loaded_max')
  if (!device || !loaded) return null
  const max = device.series.pcie_width_max
  return (
    <>
      <Subhead>HISTORY</Subhead>
      <HistoryList>
        <HistorySpark
          label="PCIe width under load"
          points={loaded}
          reference={max && max.length > 0 ? lastValue(max) : undefined}
          format={(v) => `×${v}`}
        />
      </HistoryList>
    </>
  )
}

function GpuCardView({ gpu, history }: { gpu: GpuCard; history?: HardwareTrends | null }) {
  const throttles = activeThrottles(gpu.throttle)
  const power =
    finite(gpu.power_draw_w) === null && finite(gpu.power_limit_w) === null
      ? DASH
      : `${formatWatts(gpu.power_draw_w)} / ${formatWatts(gpu.power_limit_w)}`
  const fan = finite(gpu.fan_target_percent)

  const items: StatItem[] = [
    { label: 'Driver', value: gpu.driver_version || DASH },
    { label: 'Temperature', value: formatTemp(gpu.temperature_c) },
    { label: 'Utilization', value: formatPercent(gpu.utilization_percent) },
    { label: 'Power draw / limit', value: power },
    {
      label: 'Fan',
      value: fan === null ? DASH : `${fan}% target`,
      title: 'The driver’s requested fan speed, not a measured one. Measured RPM is in the Fans section.',
    },
    { label: 'PCIe link', value: formatPcieLink(gpu.pcie) },
  ]
  if (gpu.bus_id) items.push({ label: 'PCI address', value: gpu.bus_id })

  return (
    <HwCard
      title={gpu.name || 'Unknown GPU'}
      chips={
        <>
          {gpu.vendor && gpu.vendor !== 'unknown' && <HwChip>{gpu.vendor.toUpperCase()}</HwChip>}
          {throttles?.map((t) => (
            <HwChip key={t.key} tone="warn" title={t.title}>
              {t.label}
            </HwChip>
          ))}
        </>
      }
    >
      <StatList items={items} />
      {pcieIsDownshifted(gpu.pcie) && (
        <Note>The PCIe link downshifts at idle by design; read it under load, not from a quiet snapshot.</Note>
      )}
      {throttles === null ? (
        <p className={styles.throttle}>Throttle reasons are not reported by this driver.</p>
      ) : throttles.length === 0 ? (
        <p className={styles.throttle}>No clock throttling reported.</p>
      ) : null}
      {gpu.ecc && (
        <>
          <Subhead>ECC / RETIRED PAGES</Subhead>
          <StatList items={eccItems(gpu.ecc)} />
        </>
      )}
      {gpu.ras && Object.keys(gpu.ras).length > 0 && (
        <>
          <Subhead>RAS ERROR COUNTS</Subhead>
          <StatList items={rasItems(gpu.ras)} />
        </>
      )}
      <GpuHistory gpu={gpu} history={history} />
    </HwCard>
  )
}

/**
 * Graphics adapters (`snapshot.gpu`, docs/protocol.md): identity, live
 * temperature / load / power, the PCIe link, clock-throttle reasons and ECC.
 * Raw facts only — the agent does not grade this section, so a reading here is
 * context for the server's verdict in the header, never a verdict of its own.
 */
export default function GpuBody({ gpu, history }: GpuBodyProps) {
  const cards = gpu.gpus ?? []
  const errors = gpu.errors ?? []
  return (
    <div>
      <Eyebrow>GRAPHICS ADAPTERS · {cards.length}{gpu.truncated ? '+' : ''}</Eyebrow>
      {cards.length === 0 ? (
        <EmptyNote>No graphics adapter could be read on this host.</EmptyNote>
      ) : (
        <HwCards>
          {cards.map((card, i) => (
            <GpuCardView key={card.uuid || card.bus_id || `${card.name}-${i}`} gpu={card} history={history} />
          ))}
        </HwCards>
      )}
      {gpu.truncated && <Note>Only the first adapters are listed.</Note>}
      {errors.length > 0 && (
        <>
          <Eyebrow>PROBE ERRORS</Eyebrow>
          <ul className={styles.errors}>
            {errors.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

import type { DiskNvmeHealth, DiskSection, DiskSmartRow, DiskSmartSection, HardwareTrends } from '../types'
import { severityColor } from '../../../components/tone'
import { formatBytes } from '../format'
import { DASH, finite, formatCount, formatPercent, formatPowerOnHours, formatTemp, isNonZero } from './hardwareFormat'
import { EmptyNote, Eyebrow, HwCard, HwCards, HwChip, Note, StatList, Subhead, type StatItem, type Tone } from './HardwareParts'
import { HistoryList, HistorySpark } from './HistorySpark'
import { drawable, findDevice, hasNonZero, lastValue } from './hardwareHistory'
import styles from './DiskBody.module.css'

export interface DiskBodyProps {
  /** `snapshot.disk` — absent when the host reports no volumes. */
  disk?: DiskSection
  /** `snapshot.disk_smart` — the physical-disk health rows. */
  diskSmart?: DiskSmartSection
  /** Which half the opened section is about; it is listed first. */
  focus?: 'volumes' | 'physical'
  /** The long-lived device history (`/trends` -> `hardware`); absent on an older server. */
  history?: HardwareTrends | null
}

/** Best-effort extraction of the mount `disk.reason` names ("C: 96% full
 * (>=95%)" → "C:"), so the one volume the server actually flagged can be
 * highlighted in the section's own colour. This reads the server's own
 * reason text; it never recomputes the 80/95% thresholds themselves — those
 * stay exclusively in `health_rules.py`. */
function worstMountPrefix(reason?: string): string | null {
  if (!reason) return null
  const m = reason.match(/^([A-Za-z]:|\/\S*)/)
  return m ? m[1] : null
}

/** The ATA attributes the contract restricts `smart_attributes` to, by their
 * conventional names. An id outside this set is still shown (as "Attribute N"). */
export const SMART_ATTRIBUTE_NAMES: Record<string, string> = {
  '5': 'Reallocated sectors',
  '187': 'Reported uncorrectable',
  '188': 'Command timeouts',
  '197': 'Pending sectors',
  '198': 'Offline uncorrectable',
  '199': 'Interface CRC errors',
}

/** NVMe `critical_warning` bits (docs/protocol.md, disk_smart). `serious` bits
 * are the ones that mean the drive itself is failing; bit 1 is a temperature
 * excursion. Names only — whether a bit is a finding is the server's call. */
const NVME_WARNING_BITS: { bit: number; label: string; serious: boolean }[] = [
  { bit: 0, label: 'spare below threshold', serious: true },
  { bit: 1, label: 'temperature', serious: false },
  { bit: 2, label: 'reliability degraded', serious: true },
  { bit: 3, label: 'read-only', serious: true },
  { bit: 4, label: 'volatile backup failed', serious: true },
]

export function decodeNvmeWarning(value: number | null | undefined): { labels: string[]; serious: boolean } {
  const v = finite(value)
  if (v === null || v === 0) return { labels: [], serious: false }
  const set = NVME_WARNING_BITS.filter(({ bit }) => (v & (1 << bit)) !== 0)
  return { labels: set.map((b) => b.label), serious: set.some((b) => b.serious) }
}

function healthTone(status: string | null | undefined): Tone {
  switch ((status ?? '').toLowerCase()) {
    case 'healthy':
      return 'ok'
    case 'warning':
      return 'warn'
    case 'unhealthy':
      return 'alert'
    default:
      return 'muted'
  }
}

/** A counter that is above zero reads in the alert colour; 0 and unknown stay plain. */
function counter(label: string, value: number | null | undefined, extra: Partial<StatItem> = {}): StatItem {
  return { label, value: formatCount(value), tone: isNonZero(value) ? 'alert' : undefined, ...extra }
}

function nvmeItems(n: DiskNvmeHealth): StatItem[] {
  const warning = decodeNvmeWarning(n.critical_warning)
  const spare = finite(n.available_spare)
  const threshold = finite(n.available_spare_threshold)
  const written = finite(n.data_units_written)
  return [
    {
      label: 'Critical warning',
      value: warning.labels.length ? warning.labels.join(', ') : finite(n.critical_warning) === null ? DASH : 'none',
      tone: warning.labels.length ? (warning.serious ? 'alert' : 'warn') : undefined,
    },
    {
      label: 'Spare capacity',
      value: spare === null ? DASH : `${spare}%${threshold === null ? '' : ` (threshold ${threshold}%)`}`,
    },
    { label: 'Endurance used', value: formatPercent(n.percentage_used) },
    counter('Media errors', n.media_errors),
    // Unsafe shutdowns and error-log entries accumulate on healthy drives (power
    // cuts, benign log entries), so they are shown plain, not as alarms.
    { label: 'Unsafe shutdowns', value: formatCount(n.unsafe_shutdowns) },
    { label: 'Error log entries', value: formatCount(n.error_log_entries) },
    {
      label: 'Data written',
      // NVMe data units are 1000 x 512 bytes (NVMe base spec, SMART / health log).
      value: written === null ? DASH : formatBytes(written * 512_000),
      title: written === null ? undefined : `${formatCount(written)} data units`,
    },
  ]
}

function attributeItems(attrs: Record<string, number | null>): StatItem[] {
  return Object.entries(attrs)
    .sort(([a], [b]) => Number(a) - Number(b))
    .map(([id, raw]) => counter(`${id} · ${SMART_ATTRIBUTE_NAMES[id] ?? `Attribute ${id}`}`, raw))
}

/** Error-counter series worth a sparkline once they have risen above zero. */
const ERROR_COUNTER_SERIES: { metric: string; label: string }[] = [
  { metric: 'media_errors', label: 'Media errors' },
  { metric: 'read_errors_uncorrected', label: 'Uncorrected read errors' },
  { metric: 'write_errors_uncorrected', label: 'Uncorrected write errors' },
  { metric: 'smart_5', label: `5 · ${SMART_ATTRIBUTE_NAMES['5']}` },
  { metric: 'smart_187', label: `187 · ${SMART_ATTRIBUTE_NAMES['187']}` },
  { metric: 'smart_197', label: `197 · ${SMART_ATTRIBUTE_NAMES['197']}` },
  { metric: 'smart_198', label: `198 · ${SMART_ATTRIBUTE_NAMES['198']}` },
]

/** The disk's own daily history, matched by `disk:` + serial; nothing without one. */
function DiskHistory({ serial, history }: { serial?: string | null; history?: HardwareTrends | null }) {
  const device = serial ? findDevice(history, `disk:${serial}`) : null
  if (!device) return null
  const used = drawable(device, 'percentage_used')
  const spare = drawable(device, 'available_spare')
  const threshold = device.series.available_spare_threshold
  const errors = ERROR_COUNTER_SERIES.flatMap(({ metric, label }) => {
    const points = drawable(device, metric)
    return points && hasNonZero(points) ? [{ metric, label, points }] : []
  })
  if (!used && !spare && errors.length === 0) return null
  return (
    <>
      <Subhead>HISTORY</Subhead>
      <HistoryList>
        {used && <HistorySpark label="Endurance used" points={used} format={formatPercent} />}
        {spare && (
          <HistorySpark
            label="Spare capacity"
            points={spare}
            reference={threshold && threshold.length > 0 ? lastValue(threshold) : undefined}
            format={formatPercent}
          />
        )}
        {errors.map((e) => (
          <HistorySpark key={e.metric} label={e.label} points={e.points} tone="alert" />
        ))}
      </HistoryList>
    </>
  )
}

function PhysicalDisk({ row, history }: { row: DiskSmartRow; history?: HardwareTrends | null }) {
  const tone = healthTone(row.health_status)
  const kind = [row.bus_type, row.media_type && row.media_type !== 'Unspecified' ? row.media_type : null]
    .filter(Boolean)
    .join(' · ')
  const size = finite(row.size_bytes)
  const attrs = row.smart_attributes && Object.keys(row.smart_attributes).length > 0 ? row.smart_attributes : null

  const items: StatItem[] = [
    { label: 'Serial', value: row.serial || DASH },
    { label: 'Temperature', value: formatTemp(row.temperature_c ?? row.nvme?.temperature_c, row.temperature_max_c) },
    { label: 'Power-on time', value: formatPowerOnHours(row.power_on_hours ?? row.nvme?.power_on_hours) },
    { label: 'Wear', value: formatPercent(row.wear) },
    counter('Uncorrected read errors', row.read_errors_uncorrected),
    counter('Uncorrected write errors', row.write_errors_uncorrected),
    // Mostly corrected and scaled differently by every vendor: shown, never alarmed on.
    {
      label: 'Read errors (all)',
      value: formatCount(row.read_errors_total),
      tone: 'muted',
      title: 'Every read error the drive counted, almost all corrected; vendors scale it differently.',
    },
  ]

  return (
    <HwCard
      title={row.model || 'Unknown disk'}
      chips={
        <>
          {kind && <HwChip>{kind}</HwChip>}
          {size !== null && <HwChip>{formatBytes(size)}</HwChip>}
          {row.removable && <HwChip>REMOVABLE</HwChip>}
          {row.health_status && <HwChip tone={tone}>{row.health_status.toUpperCase()}</HwChip>}
          {row.predictive_failure === true && <HwChip tone="alert">PREDICTS FAILURE</HwChip>}
        </>
      }
    >
      <StatList items={items} />
      {row.nvme && (
        <>
          <Subhead>NVME HEALTH LOG</Subhead>
          <StatList items={nvmeItems(row.nvme)} />
        </>
      )}
      {attrs && (
        <>
          <Subhead>SMART ATTRIBUTES (RAW)</Subhead>
          <StatList items={attributeItems(attrs)} />
        </>
      )}
      <DiskHistory serial={row.serial} history={history} />
      {row.nvme_error && <Note>health log unavailable: {row.nvme_error}</Note>}
      {row.paused && <Note>raw-disk reads paused while a protected game runs</Note>}
    </HwCard>
  )
}

function PhysicalDisks({ diskSmart, history }: { diskSmart?: DiskSmartSection; history?: HardwareTrends | null }) {
  const disks = diskSmart?.disks
  if (!disks) {
    // An old or failed push: no rows, but the agent's one-line summary still stands.
    return diskSmart?.summary ? <p className={styles.smart}>SMART: {diskSmart.summary}</p> : null
  }
  return (
    <>
      <Eyebrow>PHYSICAL DISKS · {disks.length}</Eyebrow>
      {disks.length === 0 ? (
        <EmptyNote>No physical disk could be listed on this host.</EmptyNote>
      ) : (
        <HwCards>
          {disks.map((row, i) => (
            <PhysicalDisk key={`${row.device_number ?? ''}-${row.serial ?? ''}-${i}`} row={row} history={history} />
          ))}
        </HwCards>
      )}
    </>
  )
}

function Volumes({ disk }: { disk: DiskSection }) {
  const color = severityColor(disk.status)
  const worstMount = worstMountPrefix(disk.reason)
  const topDirs = disk.top_dirs ?? []

  return (
    <div>
      <div className={styles.volumes}>
        {disk.volumes.map((v) => {
          const highlighted = worstMount !== null && v.mount === worstMount
          const pct = Math.max(0, Math.min(100, v.percent_used))
          return (
            <div key={v.mount} className={styles.row}>
              <span className={styles.name}>{v.mount}</span>
              <span className={styles.bar}>
                <span
                  className={styles.fill}
                  style={{ width: `${pct}%`, background: highlighted ? color : 'var(--ink-200)' }}
                />
              </span>
              <span className={styles.used} style={{ color: highlighted ? color : 'var(--text-muted)' }}>
                {formatBytes(v.total_bytes - v.free_bytes)} / {formatBytes(v.total_bytes)} ({pct.toFixed(0)}%)
              </span>
            </div>
          )
        })}
      </div>

      {topDirs.length > 0 && (
        <>
          {/* The wire payload (docs/protocol.md "disk") carries current directory
              sizes, not a 30-day growth delta — unlike the prototype's demo
              flavour text ("+38 GB"), there's nothing to diff against here, so
              this reports what the contract actually has: current size. */}
          <div className={styles.eyebrow}>LARGEST DIRECTORIES</div>
          <div className={styles.dirs}>
            {topDirs.map((d) => (
              <div key={d.path}>
                {d.path} — {formatBytes(d.bytes)}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

/**
 * The storage modal body, opened by `disk` (volumes) or `disk_smart` (physical
 * disks). Both halves render whichever section was opened, the opened one
 * first: a full volume and a failing drive are one question — "is this
 * machine's storage fine".
 */
export default function DiskBody({ disk, diskSmart, focus = 'volumes', history }: DiskBodyProps) {
  const volumes = disk?.volumes ? <Volumes disk={disk} /> : null
  const physical = <PhysicalDisks diskSmart={diskSmart} history={history} />
  return <div>{focus === 'physical' ? <>{physical}{volumes}</> : <>{volumes}{physical}</>}</div>
}

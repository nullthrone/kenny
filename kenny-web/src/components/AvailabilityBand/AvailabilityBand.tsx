import { useId, useMemo, useState } from 'react'
import type { PointerEvent as ReactPointerEvent } from 'react'
import type { AgentAvailability } from '../../views/host/types'
import { availabilityColor, formatAvailabilityPct } from '../availability'
import {
  availabilitySummary,
  dayTicks,
  describeBoot,
  describeSegment,
  formatDay,
  formatDuration,
  formatInstant,
  hitTest,
  placeSegments,
  xOf,
  type BandHit,
} from './geometry'
import styles from './AvailabilityBand.module.css'

export interface AvailabilityBandProps {
  /** Absent while the first window loads, or when it failed. */
  data?: AgentAvailability
  days: number
  onDaysChange: (days: number) => void
  dayOptions?: readonly number[]
  /** Set while `data` is the previous window standing in for the one requested. */
  stale?: boolean
  error?: string | null
  className?: string
}

/* Vertical layout of the plot, in px. Horizontal positions are all `%` of the
   window (see geometry.ts), so only the heights are fixed here. */
const LEDGER_LABEL_Y = 10
const BAND_TOP = 18
const BAND_H = 20
const BAND_BOTTOM = BAND_TOP + BAND_H
const BOOT_TOP = 12
const TICK_MINOR = 4
const TICK_MAJOR = 6
const AXIS_LABEL_Y = 56
const PLOT_H = 60
/** Below this width (%) a non-online segment is drawn as a 2px hairline, so a
 * two-minute drop in a 30-day window is still there to see. */
const HAIRLINE_BELOW = 0.25
/** How close (px) the pointer has to come to a reboot tick to name it. */
const BOOT_SLOP_PX = 6

/** Anchors a label at an x (%) so it never runs off either end of the plot. */
function anchorAt(x: number): 'start' | 'middle' | 'end' {
  if (x < 4) return 'start'
  if (x > 96) return 'end'
  return 'middle'
}

/**
 * The host page's availability timeline — hand-written inline SVG like
 * `Sparkline` and `Donut`, no charting library. One row of segments placed
 * by time (online / offline / unknown), a hatch over the stretch that was
 * reconstructed from telemetry rather than recorded, reboot ticks through
 * the band, and a day axis below. The server has already classified every
 * second; this only draws it and says it back on hover.
 */
export default function AvailabilityBand({
  data,
  days,
  onDaysChange,
  dayOptions = [7, 30],
  stale = false,
  error,
  className,
}: AvailabilityBandProps) {
  // useId's value is not a valid url(#…) fragment in every React version.
  const hatchId = `avail-hatch-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`
  const [hover, setHover] = useState<BandHit | null>(null)

  const model = useMemo(() => {
    if (!data) return null
    const w = { startMs: Date.parse(data.window?.start ?? ''), endMs: Date.parse(data.window?.end ?? '') }
    if (Number.isNaN(w.startMs) || Number.isNaN(w.endMs) || w.endMs <= w.startMs) return null
    const placed = placeSegments(data.segments ?? [], w)
    const boots = (data.boots ?? [])
      .map((b) => Date.parse(b))
      .filter((ms) => !Number.isNaN(ms) && ms >= w.startMs && ms <= w.endMs)
      .map((ms) => ({ ms, x: xOf(ms, w) }))
    const ledgerMs = data.ledger_since ? Date.parse(data.ledger_since) : NaN
    const ledger = ledgerMs > w.startMs && ledgerMs < w.endMs ? { ms: ledgerMs, x: xOf(ledgerMs, w) } : null
    // Adjacent offline segments (an approx. stretch running into an exact
    // one) are one outage, not two.
    const outages = placed.filter(
      (p, i) => p.segment.state === 'offline' && placed[i - 1]?.segment.state !== 'offline',
    ).length
    return { w, placed, boots, ledger, outages, ticks: dayTicks(w) }
  }, [data])

  function track(e: ReactPointerEvent<HTMLDivElement>) {
    if (!model) return
    const rect = e.currentTarget.getBoundingClientRect()
    if (!(rect.width > 0)) return
    const x = ((e.clientX - rect.left) / rect.width) * 100
    if (x < 0 || x > 100) return setHover(null)
    setHover(hitTest(x, model.placed, model.boots, (BOOT_SLOP_PX / rect.width) * 100))
  }

  const pct = data?.online_pct ?? null
  const totals = data?.totals

  return (
    <section className={`${styles.panel}${className ? ` ${className}` : ''}`} aria-busy={stale || undefined}>
      <div className={styles.head}>
        <span className={styles.eyebrow}>AVAILABILITY · {days} DAYS</span>
        <div className={styles.toggle} role="group" aria-label="Availability window">
          {dayOptions.map((d) => (
            <button
              key={d}
              type="button"
              className={`${styles.toggleBtn}${d === days ? ` ${styles.toggleActive}` : ''}`}
              aria-pressed={d === days}
              onClick={() => d !== days && onDaysChange(d)}
            >
              {d} D
            </button>
          ))}
        </div>
      </div>

      <div className={styles.summaryRow}>
        <div className={styles.figure}>
          <span className={styles.pct} data-testid="availability-pct">
            {data ? formatAvailabilityPct(pct) : '—'}
          </span>
          <span className={styles.pctCaption}>online</span>
        </div>
        <ul className={styles.legend} aria-label="Legend">
          <li title="The host's agent was connected to kenny">
            <span className={styles.swatch} style={{ background: availabilityColor('online') }} />
            online
          </li>
          <li title="kenny was running, the host was not connected">
            <span className={styles.swatch} style={{ background: availabilityColor('offline') }} />
            offline
          </li>
          <li title="kenny itself was down, or the host was not enrolled yet">
            <span className={styles.swatch} style={{ background: availabilityColor('unknown') }} />
            unknown
          </li>
          <li title="Reconstructed from telemetry arrival times, to about 15 minutes">
            <span className={`${styles.swatch} ${styles.swatchApprox}`} />
            approx.
          </li>
          <li>
            <span className={styles.swatchBoot} />
            reboot
          </li>
        </ul>
      </div>

      {error && !data ? (
        <p className={styles.note}>Could not load availability: {error}</p>
      ) : !data ? (
        <p className={styles.note}>Reading the last {days} days…</p>
      ) : !model || model.placed.length === 0 ? (
        <p className={styles.note}>No availability recorded yet.</p>
      ) : (
        <>
          <div
            className={`${styles.plot}${stale ? ` ${styles.stale}` : ''}`}
            data-testid="availability-plot"
            onPointerMove={track}
            onPointerDown={track}
            // A touch lifts off the plot at the end of every tap, so only a
            // mouse leaving clears the readout; a tap elsewhere moves it.
            onPointerLeave={(e) => e.pointerType !== 'touch' && setHover(null)}
          >
            <svg
              width="100%"
              height={PLOT_H}
              className={styles.svg}
              role="img"
              aria-label={availabilitySummary(days, pct, model.outages, model.boots.length)}
            >
              <defs>
                <pattern id={hatchId} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
                  <line x1="0" y1="0" x2="0" y2="6" stroke="var(--surface-card)" strokeWidth="2" strokeOpacity="0.45" />
                </pattern>
              </defs>

              <rect x="0" y={BAND_TOP} width="100%" height={BAND_H} fill="var(--surface-inset)" />

              {model.placed.map((p) => (
                <g key={`${p.startMs}-${p.segment.state}`} data-state={p.segment.state} data-approx={p.segment.approx}>
                  <rect
                    x={`${p.x}%`}
                    y={BAND_TOP}
                    width={`${p.width}%`}
                    height={BAND_H}
                    fill={availabilityColor(p.segment.state)}
                    data-segment=""
                  />
                  {p.segment.approx && (
                    <rect
                      x={`${p.x}%`}
                      y={BAND_TOP}
                      width={`${p.width}%`}
                      height={BAND_H}
                      fill={`url(#${hatchId})`}
                      data-hatch=""
                    />
                  )}
                  {p.segment.state !== 'online' && p.width < HAIRLINE_BELOW && (
                    <line
                      x1={`${p.x + p.width / 2}%`}
                      x2={`${p.x + p.width / 2}%`}
                      y1={BAND_TOP}
                      y2={BAND_BOTTOM}
                      stroke={availabilityColor(p.segment.state)}
                      strokeWidth="2"
                    />
                  )}
                </g>
              ))}

              {model.ticks.map((t) => (
                <g key={t.ms}>
                  <line
                    x1={`${t.x}%`}
                    x2={`${t.x}%`}
                    y1={BAND_BOTTOM}
                    y2={BAND_BOTTOM + (t.labelled ? TICK_MAJOR : TICK_MINOR)}
                    stroke="var(--border-mid)"
                  />
                  {t.labelled && (
                    <text x={`${t.x}%`} y={AXIS_LABEL_Y} textAnchor={anchorAt(t.x)} className={styles.axisLabel}>
                      {formatDay(t.ms)}
                    </text>
                  )}
                </g>
              ))}

              {model.ledger && (
                <g data-ledger="">
                  <line
                    x1={`${model.ledger.x}%`}
                    x2={`${model.ledger.x}%`}
                    y1={LEDGER_LABEL_Y + 3}
                    y2={BAND_BOTTOM + TICK_MAJOR}
                    stroke="var(--text-muted)"
                    strokeDasharray="2 2"
                  />
                  <text
                    x={`${model.ledger.x}%`}
                    dx={model.ledger.x < 60 ? 4 : -4}
                    y={LEDGER_LABEL_Y}
                    textAnchor={model.ledger.x < 60 ? 'start' : 'end'}
                    className={styles.ledgerLabel}
                  >
                    exact since {formatDay(model.ledger.ms)}
                  </text>
                </g>
              )}

              {model.boots.map((b) => (
                <g key={b.ms} data-boot="">
                  <line
                    x1={`${b.x}%`}
                    x2={`${b.x}%`}
                    y1={BOOT_TOP}
                    y2={BAND_BOTTOM}
                    stroke="var(--text-body)"
                    strokeWidth="1.5"
                  />
                  <circle cx={`${b.x}%`} cy={BOOT_TOP} r="2" fill="var(--text-body)" />
                </g>
              ))}

              {hover && (
                <line
                  x1={`${hover.x}%`}
                  x2={`${hover.x}%`}
                  y1={BAND_TOP - 2}
                  y2={BAND_BOTTOM + 2}
                  stroke="var(--text-body)"
                  strokeOpacity="0.45"
                  pointerEvents="none"
                />
              )}
            </svg>

            {hover && (
              <div
                className={styles.tooltip}
                style={{ left: `${hover.x}%`, top: BAND_BOTTOM + 6, transform: `translateX(-${hover.x}%)` }}
                data-testid="availability-tooltip"
              >
                {hover.kind === 'boot' ? describeBoot(hover.ms) : describeSegment(hover.placed)}
              </div>
            )}
          </div>

          {totals && (
            <p className={styles.caption}>
              {model.outages === 1 ? '1 outage' : `${model.outages} outages`} · offline{' '}
              {formatDuration(totals.offline_secs)} ·{' '}
              {model.boots.length === 1 ? '1 reboot' : `${model.boots.length} reboots`}
              {totals.unknown_secs > 0 && ` · unknown ${formatDuration(totals.unknown_secs)}`}
              {model.ledger &&
                ` · exact since ${formatInstant(model.ledger.ms)}, reconstructed from telemetry before`}
            </p>
          )}
        </>
      )}
    </section>
  )
}

import { HardDrive, RotateCw, ICON_STROKE_WIDTH, type LucideIcon } from '../../components/icons'
import {
  Cpu,
  MemoryStick,
  Activity,
  Timer,
  Network,
  Wifi,
  ShieldCheck,
  Flame,
  Lock,
  RefreshCcw,
  Package,
  MonitorCog,
  Cog,
  PlayCircle,
  CalendarClock,
  Usb,
  Printer,
  Users,
  Radio,
  DatabaseBackup,
  Gauge,
  Clock,
  Globe,
  Hourglass,
  KeyRound,
  ListTree,
  type LucideIcon as LucideIconType,
} from 'lucide-react'

export { ICON_STROKE_WIDTH }

/**
 * Section icons beyond `HardDrive`/`RotateCw`, which are the only two icons.ts
 * curates (that shared module — src/components/icons.ts — is out of this
 * view's ownership, and its own doc comment scopes it to "the prototype
 * uses", i.e. the two sections the demo data actually shows). Imported
 * directly from `lucide-react` here, at the same stroke width, rather than
 * widening the shared enumerable set from outside its own file.
 */
const EXTRA_ICONS: Record<string, LucideIconType> = {
  cpu: Cpu,
  thermals: Flame,
  memory: MemoryStick,
  processes: Activity,
  uptime: Timer,
  network: Network,
  routing: Network,
  wifi_quality: Wifi,
  defender: ShieldCheck,
  defender_quarantine: ShieldCheck,
  firewall: Flame,
  encryption: Lock,
  av_thirdparty: ShieldCheck,
  win_update: RefreshCcw,
  app_updates: Package,
  os_support: MonitorCog,
  services: Cog,
  autostart: PlayCircle,
  scheduled_tasks: CalendarClock,
  peripherals: Usb,
  printers: Printer,
  local_accounts: Users,
  logon_failures: KeyRound,
  listening_ports: Radio,
  backup_status: DatabaseBackup,
  net_quality: Gauge,
  time_sync: Clock,
  web_activity: Globe,
  screen_time: Hourglass,
  reliability: ListTree,
  installed_software: Package,
  browser_extensions: Globe,
  battery: Gauge,
}

/** A section's problem-card icon. `HardDrive`/`RotateCw` come from the shared,
 * enumerable icon set; everything else falls back to a locally-imported icon,
 * and finally to a generic glyph if the name matches nothing known. */
export function sectionIcon(name: string): LucideIcon {
  const key = name.toLowerCase()
  if (key === 'disk') return HardDrive
  if (key === 'reboot_pending') return RotateCw
  return (EXTRA_ICONS[key] as LucideIcon) ?? Cog
}

/**
 * `HostSection.name` is the raw snapshot key (`disk`, `local_accounts`, …) —
 * there is no server-side display-name catalog (grepped `health_rules.py`,
 * `event_categories.py`; neither exists). This is a display-only heuristic
 * (snake_case → Title Case with a few acronym/ampersand overrides matching
 * the prototype's demo labels), never a health judgement — the section's
 * `status`/`attention` values are always read from the server, never
 * re-derived here.
 */
const NAME_OVERRIDES: Record<string, string> = {
  disk: 'Disk & SMART',
  disk_smart: 'Disk & SMART',
  cpu: 'CPU & thermals',
  thermals: 'CPU & thermals',
  os_support: 'OS support',
  win_update: 'Windows Update',
  wifi_quality: 'Wi-Fi quality',
  net_quality: 'Network & routing',
  routing: 'Network & routing',
  av_thirdparty: 'Third-party antivirus',
  defender_quarantine: 'Defender quarantine',
  web_activity: 'Web filter',
  local_accounts: 'Local accounts',
  logon_failures: 'Logon failures',
  backup_status: 'Backup status',
  time_sync: 'Time sync',
  screen_time: 'Screen time',
  app_updates: 'App updates',
  scheduled_tasks: 'Scheduled tasks',
  listening_ports: 'Listening ports',
  installed_software: 'Installed software',
  browser_extensions: 'Browser extensions',
}

export function humanizeSectionName(name: string): string {
  const key = name.toLowerCase()
  if (NAME_OVERRIDES[key]) return NAME_OVERRIDES[key]
  return name
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

/** These three sections open a specialized, full-edit modal body instead of
 * (or alongside) the generic raw-snapshot view — matching per
 * notes/view-endpoint-map.md's Host table. Matched by raw key first (the
 * contract-accurate name), then loosely by substring so a display-label
 * variant of `name` still routes correctly. */
export function isWebFilterSection(name: string): boolean {
  const key = name.toLowerCase()
  return key === 'web_activity' || (key.includes('web') && key.includes('filter'))
}

export function isAccountsSection(name: string): boolean {
  const key = name.toLowerCase()
  return key === 'local_accounts' || key.includes('account')
}

export function isReliabilitySection(name: string): boolean {
  const key = name.toLowerCase()
  return key === 'reliability'
}

export function isDiskSection(name: string): boolean {
  return name.toLowerCase() === 'disk'
}

/**
 * Query param on the host route that names the section whose detail is open:
 * `#/fleet/{host}?section={name}`.
 *
 * This is the console half of a seam with the server's `section_target()`
 * (`kenny_server/webui/__init__.py`): a queue row in Inbox or Today links to
 * the finding it is about, and the host page opens that finding rather than
 * leaving the reader to spot it again among the machine's other sections. The
 * value is a raw `HostSection.name` (`defender`, `disk`, …), matched exactly.
 */
export const SECTION_PARAM = 'section'

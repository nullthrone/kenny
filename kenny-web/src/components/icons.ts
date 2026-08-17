/**
 * The complete set of lucide glyphs the prototype uses (grepped from every
 * `data-lucide="…"` in Kenny Console.dc.html, including the ones only
 * reachable through data arrays — hostActions, problems, wizard steps,
 * nav, theme toggle). Render at stroke-width 1.75 (Nullthrone spec) —
 * `ICON_STROKE_WIDTH` below, pass it as the `strokeWidth` prop.
 *
 * Import icons from here, not from `lucide-react` directly, so the set
 * stays enumerable and every view agent uses the same stroke width.
 */
export {
  AppWindow,
  ArrowUp,
  ArrowUpCircle,
  Check,
  Download,
  HardDrive,
  Inbox,
  LifeBuoy,
  Link,
  LogOut,
  Monitor,
  Moon,
  Package,
  Plus,
  RefreshCw,
  RotateCw,
  ScrollText,
  Server,
  Settings2,
  Sun,
  SunMedium,
  Terminal,
  Trash2,
  X,
  type LucideIcon,
  type LucideProps,
} from 'lucide-react'

/** Nullthrone spec: render icons at this stroke width, 16/20px, currentColor. */
export const ICON_STROKE_WIDTH = 1.75

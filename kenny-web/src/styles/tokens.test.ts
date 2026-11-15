import { readdirSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * Seam guard: every custom property the app reads is one the app declares.
 *
 * A `var(--name)` whose name is declared nowhere does not fail loudly — the
 * declaration silently falls back to its inherited or initial value. A banner
 * button written against `--border` instead of `--border-mid` simply loses its
 * border, and nothing but a careful look at a screenshot notices. The token
 * files (styles/tokens/, global.css) are one half of this seam; every
 * stylesheet and inline style that reads them is the other.
 *
 * Declarations are collected from every stylesheet and from TS/TSX sources
 * (inline styles may set a property, e.g. `{'--bar': '40%'}`), so a property a
 * component sets for its own children counts as declared.
 */

const SRC = join(process.cwd(), 'src')

function files(dir: string, exts: string[]): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) return files(path, exts)
    if (/\.test\.tsx?$/.test(entry.name)) return []
    return exts.some((ext) => entry.name.endsWith(ext)) ? [path] : []
  })
}

const SOURCES: Record<string, string> = Object.fromEntries(
  files(SRC, ['.css', '.ts', '.tsx']).map((f) => [relative(SRC, f), readFileSync(f, 'utf8')]),
)

function uncomment(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, '')
}

const DECLARED = new Set<string>()
const USED = new Map<string, Set<string>>()

for (const [file, raw] of Object.entries(SOURCES)) {
  const text = uncomment(raw)
  // `--name:` in CSS, or `'--name':` / `"--name":` as an inline-style key.
  for (const m of text.matchAll(/(?:^|[\s;{'"])(--[a-zA-Z0-9-]+)['"]?\s*:/g)) DECLARED.add(m[1])
  // `setProperty('--name', …)`
  for (const m of text.matchAll(/setProperty\(\s*['"](--[a-zA-Z0-9-]+)['"]/g)) DECLARED.add(m[1])
  for (const m of text.matchAll(/var\(\s*(--[a-zA-Z0-9-]+)/g)) {
    const files = USED.get(m[1]) ?? new Set<string>()
    files.add(file)
    USED.set(m[1], files)
  }
}

describe('design tokens', () => {
  it('reads the app sources', () => {
    // A walker that matched nothing would pass the assertion below vacuously.
    expect(Object.keys(SOURCES).length).toBeGreaterThan(100)
    expect(DECLARED.has('--text-body')).toBe(true)
    expect(USED.size).toBeGreaterThan(50)
  })

  it('declares every custom property the app reads', () => {
    const undeclared = [...USED.entries()]
      .filter(([name]) => !DECLARED.has(name))
      .map(([name, where]) => `${name} — ${[...where].sort().join(', ')}`)
    expect(undeclared).toEqual([])
  })

  /**
   * Dark mode is the light palette inverted at primitive level, so every value
   * one theme block declares the other must declare too. A primitive missing
   * from the dark block keeps its light value in dark mode — cream ink on a
   * dark ground, or a light wash behind light text.
   */
  it.each([
    ['tokens/colors.css', "[data-theme='dark']"],
    ['tokens/effects.css', "[data-theme='dark']"],
  ])('%s declares the same properties for light and dark', (file, darkSelector) => {
    const css = uncomment(SOURCES[join('styles', file)])
    const block = (selector: string) => {
      const blocks = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(
        ([, prelude]) => prelude.split(',').map((p) => p.trim()).includes(selector),
      )
      expect(blocks.length, `${file} has a ${selector} block`).toBeGreaterThan(0)
      return new Set(blocks.flatMap(([, , body]) => [...body.matchAll(/(--[a-zA-Z0-9-]+)\s*:/g)].map((m) => m[1])))
    }
    const light = block("[data-theme='light']")
    const dark = block(darkSelector)
    expect([...dark].sort()).toEqual([...light].sort())
  })
})

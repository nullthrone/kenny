import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { RawSection } from '../views/host/types'

/**
 * The golden telemetry snapshots under `docs/fixtures/` — the same files the
 * Python and Rust sides round-trip. Read from disk (vitest runs from
 * `kenny-web/`) so a body is tested against what the contract actually
 * contains, not against a hand-copied shape that can drift from it.
 */
export type SnapshotFixture = 'telemetry_snapshot.json' | 'telemetry_snapshot_linux.json'

export function loadSnapshot(name: SnapshotFixture): Record<string, RawSection> {
  const file = join(process.cwd(), '..', 'docs', 'fixtures', name)
  return JSON.parse(readFileSync(file, 'utf8')).snapshot
}

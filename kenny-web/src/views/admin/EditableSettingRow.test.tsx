import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { RawSettingRow } from './types'
import { mapSettingsGroups } from './settingsMap'

vi.mock('../../api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  ApiError: class ApiError extends Error {},
}))

const { default: EditableSettingRow } = await import('./EditableSettingRow')

/** One row exactly as `config.py::Settings.describe` puts it on the wire. */
function wireRow(over: Partial<RawSettingRow> = {}): RawSettingRow {
  return {
    key: 'KENNY_DISCORD_ENABLED',
    group: 'Discord & Tickets',
    type: 'bool',
    label: 'Discord bot enabled',
    help: 'Connect the Discord bot surface at startup.',
    lifecycle: 'restart',
    source: 'db',
    editable: true,
    choices: null,
    min: null,
    max: null,
    sensitive: false,
    pending_restart: false,
    value: true,
    default: false,
    ...over,
  }
}

function renderWire(raw: RawSettingRow) {
  const [section] = mapSettingsGroups({ groups: [{ name: raw.group, slug: 'g', settings: [raw] }] })
  const client = new QueryClient()
  return render(
    <QueryClientProvider client={client}>
      <EditableSettingRow row={section.rows[0]} />
    </QueryClientProvider>,
  )
}

/**
 * ADR-0032 makes `lifecycle` the honesty mechanism: a stored `restart` value is
 * not the running one until the server restarts. The server says which rows are
 * in that state (`pending_restart`); this is the seam from that field to what
 * the operator sees.
 */
describe('EditableSettingRow — restart settings', () => {
  it('marks a stored change the server is not running with yet', () => {
    renderWire(wireRow({ pending_restart: true }))
    expect(screen.getByText('RESTART PENDING')).toBeInTheDocument()
  })

  it('shows no marker once the running value is the stored one', () => {
    renderWire(wireRow({ pending_restart: false }))
    expect(screen.queryByText('RESTART PENDING')).not.toBeInTheDocument()
  })

  it('says in the editor that a restart setting applies on restart', () => {
    renderWire(wireRow())
    fireEvent.click(screen.getByRole('button', { name: 'EDIT' }))
    expect(screen.getByText('Takes effect after the server restarts.')).toBeInTheDocument()
  })

  it('does not warn about a restart for a live setting', () => {
    renderWire(wireRow({ key: 'KENNY_TRIAGE_ENABLED', lifecycle: 'live', label: 'Triage' }))
    fireEvent.click(screen.getByRole('button', { name: 'EDIT' }))
    expect(screen.queryByText('Takes effect after the server restarts.')).not.toBeInTheDocument()
  })
})

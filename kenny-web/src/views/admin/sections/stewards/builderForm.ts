import type {
  BuilderCatalog,
  BuilderConstraintData,
  BuilderData,
  BuilderFieldError,
  BuilderTool,
  ToolTier,
} from '../../types'

/**
 * The pure half of the steward builder editor (ADR-0074): the form's state, its conversion to
 * and from the server's data form (`stewards/builder.py::spec_to_data`), and the placing of the
 * server's field errors. Nothing here decides what a built steward may do; the server does, and
 * this only keeps the person from sending what it will plainly refuse.
 */

/** Where Ask kenny leaves a draft for the editor (`#/admin/stewards/new?draft=1`). */
export const DRAFT_STORAGE_KEY = 'kenny.stewardDraft'

export const ID_PREFIX = 'custom_'

export type ConstraintKind = 'allowed' | 'param' | 'evidence'

/** One argument of one change-tier tool and what bounds it. `evidence` is kept only so the person can see and replace it. */
export interface ConstraintRow {
  key: string
  tool: string
  arg: string
  kind: ConstraintKind
  /** Literal values as typed, one per line. */
  allowedText: string
  param: string
  evidence: string
}

export interface TimeoutRow {
  tool: string
  /** Seconds as typed. */
  text: string
}

export interface FormState {
  id: string
  title: string
  description: string
  prompt: string
  /** In the order the person picked them: the server addresses a tool error as `tools[i]` in this order. */
  tools: string[]
  constraints: ConstraintRow[]
  timeouts: TimeoutRow[]
  params: string[]
  preconditions: string[]
  budget: string
  sensitiveOk: boolean
  triggerKind: 'schedule' | 'event'
  triggerEvent: string
  minIntervalDays: string
  allowSchedule: boolean
  allowEvents: string[]
  allowCommand: boolean
}

/** What the lists of a form looked like when a request was made: an error's `tools[3]` means the fourth of these. */
export interface Snapshot {
  tools: string[]
  constraintKeys: string[]
  timeoutTools: string[]
  params: string[]
  preconditions: string[]
  events: string[]
}

export const TIER_ORDER: readonly ToolTier[] = ['read_only', 'standard_change', 'normal_change']

// -- reading data --------------------------------------------------------------

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const text = (v: unknown, fallback = ''): string => (typeof v === 'string' ? v : fallback)
const texts = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [])

/** An empty built steward: a weekly-style schedule, no tools beyond the verdict, the default budget. */
export function emptyData(): BuilderData {
  return {
    id: ID_PREFIX,
    title: '',
    description: '',
    prompt: '',
    trigger: { kind: 'schedule', min_interval_days: null },
    triggers_allowed: { schedule: true, events: [], command: false },
    tools: [],
    constraints: [],
    timeouts: [],
    params: ['hosts', 'window'],
    preconditions: [],
    budget: { max_iterations: 8 },
    sensitive_ok: false,
  }
}

/**
 * Read anything (a response, a stored draft) as the data form, defensively: a field of the
 * wrong type falls back to its empty value rather than throwing. The server validates what is
 * finally sent, so this only has to be safe to render. `null` when `raw` is not an object.
 */
export function normalizeData(raw: unknown): BuilderData | null {
  if (!isRecord(raw)) return null
  const base = emptyData()
  const trigger = isRecord(raw.trigger) ? raw.trigger : {}
  const allowed = isRecord(raw.triggers_allowed) ? raw.triggers_allowed : null
  const budget = isRecord(raw.budget) ? raw.budget : {}
  const interval = trigger.min_interval_days
  const triggerKind = trigger.kind === 'event' ? 'event' : 'schedule'
  return {
    id: text(raw.id, ID_PREFIX),
    title: text(raw.title),
    description: text(raw.description),
    prompt: text(raw.prompt),
    trigger:
      triggerKind === 'event'
        ? { kind: 'event', event: text(trigger.event) }
        : { kind: 'schedule', min_interval_days: typeof interval === 'number' || typeof interval === 'string' ? interval : null },
    triggers_allowed: allowed
      ? { schedule: allowed.schedule === true, events: texts(allowed.events), command: allowed.command === true }
      : triggerKind === 'event'
        ? { schedule: false, events: texts([trigger.event]), command: false }
        : { schedule: true, events: [], command: false },
    tools: texts(raw.tools),
    constraints: Array.isArray(raw.constraints)
      ? raw.constraints.filter(isRecord).map((c): BuilderConstraintData => {
          const out: BuilderConstraintData = { tool: text(c.tool), arg: text(c.arg) }
          if ('allowed' in c) out.allowed = texts(c.allowed)
          if ('param' in c) out.param = text(c.param)
          if ('evidence' in c) out.evidence = text(c.evidence)
          return out
        })
      : [],
    timeouts: Array.isArray(raw.timeouts)
      ? raw.timeouts
          .filter(isRecord)
          .map((t) => ({ tool: text(t.tool), max_s: typeof t.max_s === 'number' || typeof t.max_s === 'string' ? t.max_s : '' }))
      : [],
    params: Array.isArray(raw.params) ? texts(raw.params) : base.params,
    preconditions: texts(raw.preconditions),
    budget: {
      max_iterations:
        typeof budget.max_iterations === 'number' || typeof budget.max_iterations === 'string'
          ? budget.max_iterations
          : base.budget.max_iterations,
    },
    sensitive_ok: raw.sensitive_ok === true,
  }
}

export type DraftResult = { data: BuilderData } | { unreadable: true }

/**
 * Take (read, then remove) the draft Ask kenny left in `sessionStorage`. `null` when there is
 * none; `{unreadable: true}` when something is there that is not a steward draft. It is removed
 * either way, so a reload never replays a draft the person already dealt with.
 */
export function takeDraft(storage: Pick<Storage, 'getItem' | 'removeItem'> | null = safeSession()): DraftResult | null {
  if (!storage) return null
  let raw: string | null
  try {
    raw = storage.getItem(DRAFT_STORAGE_KEY)
  } catch {
    return null
  }
  if (raw === null) return null
  try {
    storage.removeItem(DRAFT_STORAGE_KEY)
  } catch {
    // Nothing to do: the draft is read; a later read may find it again, which is harmless.
  }
  try {
    let parsed: unknown = JSON.parse(raw)
    // Tolerate the draft wrapped as `{spec: …}` (the shape a save takes).
    if (isRecord(parsed) && isRecord(parsed.spec) && !('title' in parsed)) parsed = parsed.spec
    const data = normalizeData(parsed)
    return data ? { data } : { unreadable: true }
  } catch {
    return { unreadable: true }
  }
}

function safeSession(): Storage | null {
  try {
    return typeof sessionStorage === 'undefined' ? null : sessionStorage
  } catch {
    return null
  }
}

// -- form <-> data -------------------------------------------------------------

let rowCounter = 0
const nextKey = () => `c${++rowCounter}`

export const isChangeTool = (tool: BuilderTool | undefined, verdictTool: string): boolean =>
  tool !== undefined && tool.tier !== 'read_only' && tool.name !== verdictTool

function toolOf(catalog: BuilderCatalog, name: string): BuilderTool | undefined {
  return catalog.tools.find((t) => t.name === name)
}

/** Add an empty row for each required, bindable argument of each change tool in `tools` that has none. */
export function withRequiredRows(rows: ConstraintRow[], tools: string[], catalog: BuilderCatalog): ConstraintRow[] {
  const out = [...rows]
  for (const name of tools) {
    const tool = toolOf(catalog, name)
    if (!isChangeTool(tool, catalog.verdict_tool) || !tool) continue
    for (const arg of tool.args) {
      if (arg.required && arg.bindable && !out.some((r) => r.tool === name && r.arg === arg.name)) {
        out.push(blankRow(name, arg.name))
      }
    }
  }
  return out
}

export function blankRow(tool: string, arg: string): ConstraintRow {
  return { key: nextKey(), tool, arg, kind: 'allowed', allowedText: '', param: '', evidence: '' }
}

export function formFromData(data: BuilderData, catalog: BuilderCatalog): FormState {
  const rows = data.constraints.map((c): ConstraintRow => {
    const kind: ConstraintKind = c.evidence !== undefined ? 'evidence' : c.param !== undefined ? 'param' : 'allowed'
    return {
      key: nextKey(),
      tool: c.tool,
      arg: c.arg,
      kind,
      allowedText: (c.allowed ?? []).join('\n'),
      param: c.param ?? '',
      evidence: c.evidence ?? '',
    }
  })
  const event = data.trigger.kind === 'event'
  return {
    id: data.id,
    title: data.title,
    description: data.description,
    prompt: data.prompt,
    tools: [...data.tools],
    constraints: withRequiredRows(rows, data.tools, catalog),
    timeouts: data.timeouts.map((t) => ({ tool: t.tool, text: String(t.max_s) })),
    params: [...data.params],
    preconditions: [...data.preconditions],
    budget: String(data.budget.max_iterations),
    sensitiveOk: data.sensitive_ok,
    triggerKind: event ? 'event' : 'schedule',
    triggerEvent: event ? (data.trigger.event ?? '') : (catalog.events[0] ?? ''),
    minIntervalDays:
      !event && data.trigger.min_interval_days !== null && data.trigger.min_interval_days !== undefined
        ? String(data.trigger.min_interval_days)
        : '',
    allowSchedule: data.triggers_allowed.schedule,
    allowEvents: [...data.triggers_allowed.events],
    allowCommand: data.triggers_allowed.command,
  }
}

/** A number the person typed, or the text itself when it is not a whole number (the server then says why). */
function wholeOrText(value: string): number | string {
  const t = value.trim()
  return /^\d+$/.test(t) ? Number(t) : t
}

/** The tools as sent: the picked ones, then the verdict tool every built steward ends with. */
export function submittedTools(form: FormState, catalog: BuilderCatalog): string[] {
  return form.tools.includes(catalog.verdict_tool) ? [...form.tools] : [...form.tools, catalog.verdict_tool]
}

export function submittedTimeouts(form: FormState): TimeoutRow[] {
  return form.timeouts.filter((t) => t.text.trim() !== '')
}

export function selectedSensitive(form: FormState, catalog: BuilderCatalog): string[] {
  return form.tools.filter((t) => toolOf(catalog, t)?.sensitive)
}

export function snapshotOf(form: FormState, catalog: BuilderCatalog): Snapshot {
  return {
    tools: submittedTools(form, catalog),
    constraintKeys: form.constraints.map((c) => c.key),
    timeoutTools: submittedTimeouts(form).map((t) => t.tool),
    params: [...form.params],
    preconditions: [...form.preconditions],
    events: [...form.allowEvents],
  }
}

export function dataFromForm(form: FormState, catalog: BuilderCatalog): BuilderData {
  const lines = (t: string) => t.split('\n').map((l) => l.trim()).filter((l) => l !== '')
  const interval = form.minIntervalDays.trim()
  return {
    id: form.id.trim(),
    title: form.title,
    description: form.description,
    prompt: form.prompt,
    trigger:
      form.triggerKind === 'event'
        ? { kind: 'event', event: form.triggerEvent }
        : { kind: 'schedule', min_interval_days: interval === '' ? null : wholeOrText(interval) },
    triggers_allowed: {
      schedule: form.allowSchedule,
      events: [...form.allowEvents],
      command: form.allowCommand,
    },
    tools: submittedTools(form, catalog),
    constraints: form.constraints.map((r): BuilderConstraintData => {
      if (r.kind === 'param') return { tool: r.tool, arg: r.arg, param: r.param }
      if (r.kind === 'evidence') return { tool: r.tool, arg: r.arg, evidence: r.evidence }
      return { tool: r.tool, arg: r.arg, allowed: lines(r.allowedText) }
    }),
    timeouts: submittedTimeouts(form).map((t) => ({ tool: t.tool, max_s: wholeOrText(t.text) })),
    params: [...form.params],
    preconditions: [...form.preconditions],
    budget: { max_iterations: wholeOrText(form.budget) },
    // Least privilege: the flag only travels while a tool that needs it is picked.
    sensitive_ok: form.sensitiveOk && selectedSensitive(form, catalog).length > 0,
  }
}

// -- errors --------------------------------------------------------------------

/** The part of the form an error belongs to; `general` is whatever has no field of its own. */
export type Region = string

export const region = {
  id: 'id',
  title: 'title',
  description: 'description',
  prompt: 'prompt',
  tools: 'tools',
  tool: (name: string) => `tool:${name}`,
  constraints: 'constraints',
  constraint: (key: string) => `constraint:${key}`,
  timeouts: 'timeouts',
  timeout: (tool: string) => `timeout:${tool}`,
  params: 'params',
  param: (name: string) => `param:${name}`,
  preconditions: 'preconditions',
  precondition: (name: string) => `precondition:${name}`,
  trigger: 'trigger',
  ceiling: 'ceiling',
  event: (name: string) => `event:${name}`,
  budget: 'budget',
  sensitive: 'sensitive',
  general: 'general',
} as const

const INDEXED = /^(tools|constraints|timeouts|params|preconditions|triggers_allowed\.events)\[(\d+)\](?:\.(.+))?$/

/** Where the editor shows `error`; the sub-field (`param` in `constraints[0].param`) is kept to prefix the message. */
export function placeError(error: BuilderFieldError, snapshot: Snapshot): { region: Region; sub: string | null } {
  const { field } = error
  const m = INDEXED.exec(field)
  if (m) {
    const [, list, index, sub] = m
    const i = Number(index)
    const named = (names: string[], make: (n: string) => Region, fallback: Region) =>
      names[i] !== undefined ? { region: make(names[i]), sub: sub ?? null } : { region: fallback, sub: sub ?? null }
    switch (list) {
      case 'tools':
        return named(snapshot.tools, region.tool, region.tools)
      case 'constraints':
        return named(snapshot.constraintKeys, region.constraint, region.constraints)
      case 'timeouts':
        return named(snapshot.timeoutTools, region.timeout, region.timeouts)
      case 'params':
        return named(snapshot.params, region.param, region.params)
      case 'preconditions':
        return named(snapshot.preconditions, region.precondition, region.preconditions)
      default:
        return named(snapshot.events, region.event, region.ceiling)
    }
  }
  const direct: Record<string, Region> = {
    id: region.id,
    title: region.title,
    description: region.description,
    prompt: region.prompt,
    tools: region.tools,
    constraints: region.constraints,
    timeouts: region.timeouts,
    params: region.params,
    preconditions: region.preconditions,
    sensitive_ok: region.sensitive,
  }
  if (field in direct) return { region: direct[field], sub: null }
  if (field === 'trigger' || field.startsWith('trigger.')) return { region: region.trigger, sub: null }
  if (field === 'triggers_allowed' || field.startsWith('triggers_allowed.')) return { region: region.ceiling, sub: null }
  if (field === 'budget' || field.startsWith('budget.')) return { region: region.budget, sub: null }
  return { region: region.general, sub: null }
}

export interface PlacedError {
  /** The message as shown: the server's, led by the sub-field when it names one. */
  message: string
}

export function placeErrors(errors: BuilderFieldError[], snapshot: Snapshot): Map<Region, PlacedError[]> {
  const out = new Map<Region, PlacedError[]>()
  for (const e of errors) {
    const { region: where, sub } = placeError(e, snapshot)
    const message = where === region.general ? `${e.field}: ${e.error}` : sub ? `${sub}: ${e.error}` : e.error
    out.set(where, [...(out.get(where) ?? []), { message }])
  }
  return out
}

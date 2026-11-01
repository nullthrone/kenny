import { useId, useState, type CSSProperties, type FormEvent, type ReactNode } from 'react'
import { ApiError } from '../../../../api/client'
import type { BuilderCatalog, BuilderData, BuilderFieldError, BuilderSaved, BuilderTool } from '../../types'
import { TierBadge } from './badges'
import {
  TIER_ORDER,
  blankRow,
  dataFromForm,
  formFromData,
  isChangeTool,
  placeErrors,
  region,
  selectedSensitive,
  snapshotOf,
  withRequiredRows,
  type ConstraintKind,
  type ConstraintRow,
  type FormState,
  type PlacedError,
  type Region,
  type Snapshot,
} from './builderForm'
import { builderFieldErrors, consentErrorMessage, useSaveBuilt } from './queries'
import { eventLabel } from './TriggersPanel'
import shared from '../../shared.module.css'
import styles from './stewards.module.css'
import b from './BuilderPanel.module.css'

const TIER_TITLE: Record<string, string> = {
  read_only: 'READ-ONLY TOOLS',
  standard_change: 'STANDARD-CHANGE TOOLS',
  normal_change: 'NORMAL-CHANGE TOOLS',
}

export interface BuilderPanelProps {
  catalog: BuilderCatalog
  /** `create` posts a new steward (the id is typed here); `edit` saves a new version of `initial.id`. */
  mode: 'create' | 'edit'
  initial: BuilderData
  /** Errors to show before anything is sent: what a clone cannot keep, or why a stored steward is invalid. */
  initialErrors?: BuilderFieldError[]
  /** Where a new steward came from: `blank`, `copilot` or `clone:<shipped id>`. */
  source?: string
  /** The hash the person was shown; a save names it and the server answers 409 when it moved. */
  effectiveHash?: string | null
  /** Explains how the form was seeded (a copy of X, a draft from Ask kenny). */
  banner?: ReactNode
  onSaved: (saved: BuilderSaved) => void
  onCancel: () => void
  /** Called by the RELOAD button shown after a 409 on a save. */
  onReload?: () => void
}

function FieldErrors({ errors, id }: { errors: PlacedError[] | undefined; id?: string }) {
  if (!errors || errors.length === 0) return null
  return (
    <ul className={b.fieldErrors} id={id}>
      {errors.map((e, i) => (
        <li key={i}>{e.message}</li>
      ))}
    </ul>
  )
}

function Counter({ value, max }: { value: string; max: number | undefined }) {
  if (max === undefined) return null
  const over = value.length > max
  return (
    <span className={over ? b.counterOver : b.counter} title={`${value.length} of ${max} characters`}>
      {value.length} / {max}
    </span>
  )
}

/** A labelled control with its help and the errors addressed to it kept outside the label (and so out of its name). */
function Field({
  id,
  label,
  counter,
  help,
  errors,
  children,
  style,
}: {
  id: string
  label: string
  counter?: ReactNode
  help?: ReactNode
  errors?: PlacedError[]
  children: ReactNode
  style?: CSSProperties
}) {
  return (
    <div className={shared.field} style={style}>
      <div className={b.labelRow}>
        <label htmlFor={id} className={shared.fieldLabel}>
          {label}
        </label>
        {counter}
      </div>
      {children}
      {help && <span className={shared.help}>{help}</span>}
      <FieldErrors errors={errors} id={`${id}-errors`} />
    </div>
  )
}

function ToolMeta({ tool }: { tool: BuilderTool }) {
  return (
    <>
      <TierBadge tier={tool.tier} />
      {tool.os && (
        <span className={`${styles.tag} ${styles.toneMuted}`} title={`Runs on ${tool.os.join(', ')} only`}>
          {tool.os.map((o) => o.toUpperCase()).join(' / ')} ONLY
        </span>
      )}
      {tool.sensitive && (
        <span className={`${styles.tag} ${styles.toneWarn}`} title="Reads private data: needs sensitive_ok">
          SENSITIVE
        </span>
      )}
      <span className={shared.help} style={{ display: 'inline' }}>
        {tool.target === 'server' ? 'on the server' : 'on a PC'}
      </span>
    </>
  )
}

/**
 * The form for a built steward's data (ADR-0074, docs/dashboard.md "Built stewards"). It composes
 * from `GET /api/stewards/builder/catalog` only — a tool, a parameter kind, an event, a
 * precondition the server lists — and sends the data form. What the server refuses comes back as
 * `{field, error}` and is shown beside its field; nothing is saved until the person presses save.
 */
export default function BuilderPanel({
  catalog,
  mode,
  initial,
  initialErrors = [],
  source = 'blank',
  effectiveHash = null,
  banner,
  onSaved,
  onCancel,
  onReload,
}: BuilderPanelProps) {
  const uid = useId()
  // The form is built once, and the errors it arrived with are placed against that same build:
  // a row's key is made when the form is, so a second build would address other rows.
  const [seed] = useState(() => {
    const seeded = formFromData(initial, catalog)
    return { form: seeded, snapshot: snapshotOf(seeded, catalog) }
  })
  const [form, setForm] = useState<FormState>(seed.form)
  const [errors, setErrors] = useState<{ list: BuilderFieldError[]; snapshot: Snapshot }>(() => ({
    list: initialErrors,
    snapshot: seed.snapshot,
  }))
  const [problem, setProblem] = useState<string | null>(null)
  const [stale, setStale] = useState(false)
  const [ownParam, setOwnParam] = useState<string | null>(null)

  const save = useSaveBuilt(mode === 'create' ? { mode, source } : { mode, stewardId: initial.id, effectiveHash })

  const limits = catalog.limits
  const placed = placeErrors(errors.list, errors.snapshot)
  const at = (r: Region) => placed.get(r)
  const tools = new Map(catalog.tools.map((t) => [t.name, t]))
  const kindOf = (name: string) => catalog.param_kinds[name] ?? catalog.default_param_kind

  const sensitivePicked = selectedSensitive(form, catalog)
  const sensitiveMissing = sensitivePicked.length > 0 && !form.sensitiveOk
  const toolCount = form.tools.includes(catalog.verdict_tool) ? form.tools.length : form.tools.length + 1

  function patch(change: Partial<FormState>) {
    setForm((f) => ({ ...f, ...change }))
    setStale(false)
  }

  function toggleTool(name: string, on: boolean) {
    setForm((f) => {
      if (!on) {
        return {
          ...f,
          tools: f.tools.filter((t) => t !== name),
          constraints: f.constraints.filter((c) => c.tool !== name),
          timeouts: f.timeouts.filter((t) => t.tool !== name),
        }
      }
      if (f.tools.includes(name)) return f
      const tools = [...f.tools, name]
      return { ...f, tools, constraints: withRequiredRows(f.constraints, tools, catalog) }
    })
  }

  function editRow(key: string, change: Partial<ConstraintRow>) {
    patch({ constraints: form.constraints.map((r) => (r.key === key ? { ...r, ...change } : r)) })
  }

  function setToolTimeout(tool: string, text: string) {
    const has = form.timeouts.some((t) => t.tool === tool)
    patch({
      timeouts: has
        ? form.timeouts.map((t) => (t.tool === tool ? { ...t, text } : t))
        : [...form.timeouts, { tool, text }],
    })
  }

  function toggleIn(list: string[], name: string, on: boolean): string[] {
    return on ? (list.includes(name) ? list : [...list, name]) : list.filter((x) => x !== name)
  }

  function addParam(name: string) {
    const clean = name.trim()
    if (clean && !form.params.includes(clean)) patch({ params: [...form.params, clean] })
  }

  function removeParam(name: string) {
    patch({
      params: form.params.filter((p) => p !== name),
      // A constraint bound to a parameter that is gone would only be refused; unbind it.
      constraints: form.constraints.map((r) => (r.kind === 'param' && r.param === name ? { ...r, param: '' } : r)),
    })
  }

  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    if (sensitiveMissing) return
    const data = dataFromForm(form, catalog)
    const snapshot = snapshotOf(form, catalog)
    setProblem(null)
    setStale(false)
    save.mutate(data, {
      onSuccess: onSaved,
      onError: (err) => {
        const fields = builderFieldErrors(err)
        if (fields) {
          setErrors({ list: fields, snapshot })
        } else if (err instanceof ApiError && err.status === 409) {
          if (mode === 'create') {
            setErrors({
              list: [{ field: 'id', error: err.message || 'a steward with this id already exists' }],
              snapshot,
            })
          } else {
            setErrors({ list: [], snapshot })
            setStale(true)
          }
        } else {
          setErrors({ list: [], snapshot })
          setProblem(consentErrorMessage(err, 'Could not save the steward.'))
        }
      },
    })
  }

  // -- tools, grouped by tier --------------------------------------------------
  const verdictTool = tools.get(catalog.verdict_tool)
  const catalogGroups = TIER_ORDER.map((tier) => ({
    tier,
    tools: catalog.tools.filter((t) => t.tier === tier && t.name !== catalog.verdict_tool),
  })).filter((g) => g.tools.length > 0)
  // A tool the form names that the catalog does not offer (a clone of a shipped steward brings some):
  // kept visible and ticked, with the server's reason, so the person can untick it.
  const foreign = form.tools.filter((t) => !tools.has(t))

  function toolRow(tool: BuilderTool) {
    const checked = form.tools.includes(tool.name)
    const errs = at(region.tool(tool.name))
    const needsFlag = checked && tool.sensitive && !form.sensitiveOk
    return (
      <li key={tool.name} className={b.toolRow}>
        <div className={b.toolHead}>
          <label className={styles.check}>
            <input
              type="checkbox"
              checked={checked}
              onChange={(e) => toggleTool(tool.name, e.target.checked)}
              aria-invalid={errs ? true : undefined}
            />
            <span className={styles.toolName}>{tool.name}</span>
          </label>
          <ToolMeta tool={tool} />
        </div>
        {tool.description && (
          <p className={b.toolDesc} title={tool.description}>
            {tool.description}
          </p>
        )}
        {needsFlag && (
          <p className={b.fieldErrors}>
            {tool.name} reads private data: tick “allow private data” below to name it.
          </p>
        )}
        <FieldErrors errors={errs} />
      </li>
    )
  }

  // -- constraints, grouped by tool -----------------------------------------------
  const constraintTools = [...new Set(form.constraints.map((c) => c.tool))]
  const listParams = form.params.filter((p) => kindOf(p) === 'list')

  function constraintGroup(toolName: string) {
    const tool = tools.get(toolName)
    const rows = form.constraints.filter((c) => c.tool === toolName)
    const bound = new Set(rows.map((r) => r.arg))
    const extraArgs = (tool?.args ?? []).filter((a) => a.bindable && !bound.has(a.name))
    return (
      <div key={toolName} role="group" aria-label={`Constraints on ${toolName}`} className={b.constraintGroup}>
        <div className={b.toolHead}>
          <span className={styles.toolName}>{toolName}</span>
          {tool && <TierBadge tier={tool.tier} />}
        </div>
        <ul className={b.constraintRows}>
          {rows.map((r) => {
            const arg = tool?.args.find((a) => a.name === r.arg)
            const required = arg?.required === true
            const removable = !required || r.kind === 'evidence'
            const label = `${r.tool}.${r.arg}`
            const errs = at(region.constraint(r.key))
            const options = r.param && !listParams.includes(r.param) ? [...listParams, r.param] : listParams
            return (
              <li key={r.key} className={b.constraintRow}>
                <div className={b.constraintHead}>
                  <span className={styles.toolName}>{r.arg}</span>
                  {required && <span className={`${styles.tag} ${styles.toneMuted}`}>REQUIRED</span>}
                  {arg && <span className={shared.help} style={{ display: 'inline' }}>{arg.type}</span>}
                  <select
                    className={shared.input}
                    style={{ width: 'auto' }}
                    aria-label={`${label} is bound by`}
                    value={r.kind}
                    onChange={(e) => editRow(r.key, { kind: e.target.value as ConstraintKind })}
                  >
                    <option value="allowed">literal values</option>
                    <option value="param">a parameter</option>
                    {r.kind === 'evidence' && <option value="evidence">computed by the server (not allowed)</option>}
                  </select>
                  {removable && (
                    <button
                      type="button"
                      className={styles.linkButton}
                      aria-label={`Remove the constraint on ${label}`}
                      onClick={() => patch({ constraints: form.constraints.filter((x) => x.key !== r.key) })}
                    >
                      REMOVE
                    </button>
                  )}
                </div>
                {r.kind === 'allowed' && (
                  <textarea
                    className={styles.textarea}
                    aria-label={`${label} allowed values`}
                    placeholder="one value per line"
                    value={r.allowedText}
                    aria-invalid={errs ? true : undefined}
                    onChange={(e) => editRow(r.key, { allowedText: e.target.value })}
                  />
                )}
                {r.kind === 'param' && (
                  <select
                    className={shared.input}
                    aria-label={`${label} parameter`}
                    value={r.param}
                    aria-invalid={errs ? true : undefined}
                    onChange={(e) => editRow(r.key, { param: e.target.value })}
                  >
                    <option value="">{options.length === 0 ? 'declare a list parameter first' : 'choose a parameter…'}</option>
                    {options.map((p) => (
                      <option key={p} value={p}>
                        {p}
                      </option>
                    ))}
                  </select>
                )}
                {r.kind === 'evidence' && (
                  <p className={shared.help}>
                    Bound to values the server computes ({r.evidence}). A built steward cannot use that: switch to literal
                    values or a parameter.
                  </p>
                )}
                <FieldErrors errors={errs} />
              </li>
            )
          })}
        </ul>
        {extraArgs.length > 0 && (
          <select
            className={shared.input}
            style={{ width: 'auto', marginTop: 6 }}
            aria-label={`Also bind an argument of ${toolName}`}
            value=""
            onChange={(e) => {
              if (e.target.value) patch({ constraints: [...form.constraints, blankRow(toolName, e.target.value)] })
            }}
          >
            <option value="">also bind another argument…</option>
            {extraArgs.map((a) => (
              <option key={a.name} value={a.name}>
                {a.name}
              </option>
            ))}
          </select>
        )}
      </div>
    )
  }

  // -- timeouts --------------------------------------------------------------------
  const timeoutTools = form.tools.filter((t) => isChangeTool(tools.get(t), catalog.verdict_tool))

  // -- triggers --------------------------------------------------------------------
  const eventNames = [...new Set([...catalog.events, ...form.allowEvents, ...(form.triggerKind === 'event' && form.triggerEvent ? [form.triggerEvent] : [])])]
  const needsHosts = form.tools.some((t) => tools.get(t)?.target === 'host') && !form.params.includes('hosts')
  const needsWindow = (form.triggerKind === 'schedule' || form.allowSchedule) && !form.params.includes('window')
  const preconditionNames = [...new Set([...catalog.preconditions, ...form.preconditions])]

  const generalErrors = at(region.general)
  const total = errors.list.length

  return (
    <form className={styles.form} onSubmit={submit} noValidate aria-label={mode === 'create' ? 'New steward' : `Edit ${initial.id}`}>
      {banner}
      {total > 0 && (
        <div className={shared.errorBox} role="alert" style={{ marginBottom: 0 }}>
          {total === 1 ? '1 problem to fix' : `${total} problems to fix`}: each is shown at its field below.
          <FieldErrors errors={generalErrors} />
        </div>
      )}

      <section className={shared.card} aria-label="Identity" style={{ marginBottom: 0 }}>
        <h3 className={styles.heading}>WHAT IT IS</h3>
        <div className={styles.form}>
          <Field
            id={`${uid}-id`}
            label="ID"
            help={
              mode === 'create'
                ? 'custom_ then 2 to 33 lowercase letters, digits or _. It cannot be changed afterwards.'
                : 'A built steward keeps its id.'
            }
            errors={at(region.id)}
          >
            {mode === 'create' ? (
              <input
                id={`${uid}-id`}
                className={`${shared.input} ${shared.mono}`}
                value={form.id}
                onChange={(e) => patch({ id: e.target.value })}
                autoComplete="off"
                spellCheck={false}
                aria-invalid={at(region.id) ? true : undefined}
                aria-describedby={`${uid}-id-errors`}
              />
            ) : (
              <code id={`${uid}-id`} className={shared.mono}>
                {form.id}
              </code>
            )}
          </Field>
          <Field
            id={`${uid}-title`}
            label="TITLE"
            counter={<Counter value={form.title} max={limits.title_max} />}
            errors={at(region.title)}
          >
            <input
              id={`${uid}-title`}
              className={shared.input}
              value={form.title}
              onChange={(e) => patch({ title: e.target.value })}
              aria-invalid={at(region.title) ? true : undefined}
              aria-describedby={`${uid}-title-errors`}
            />
          </Field>
          <Field
            id={`${uid}-description`}
            label="DESCRIPTION"
            counter={<Counter value={form.description} max={limits.description_max} />}
            errors={at(region.description)}
          >
            <input
              id={`${uid}-description`}
              className={shared.input}
              value={form.description}
              onChange={(e) => patch({ description: e.target.value })}
              aria-invalid={at(region.description) ? true : undefined}
              aria-describedby={`${uid}-description-errors`}
            />
          </Field>
          <Field
            id={`${uid}-prompt`}
            label="PROMPT"
            counter={<Counter value={form.prompt} max={limits.prompt_max} />}
            help="What the steward is asked to do each run. The prompt is not the boundary: the tools, constraints and authorizations below are."
            errors={at(region.prompt)}
          >
            <textarea
              id={`${uid}-prompt`}
              className={styles.textarea}
              style={{ minHeight: 160 }}
              value={form.prompt}
              onChange={(e) => patch({ prompt: e.target.value })}
              aria-invalid={at(region.prompt) ? true : undefined}
              aria-describedby={`${uid}-prompt-errors`}
            />
          </Field>
        </div>
      </section>

      <section className={shared.card} aria-label="Tools" style={{ marginBottom: 0 }}>
        <h3 className={styles.heading}>
          TOOLS{' '}
          <span className={b.counter} style={{ marginLeft: 8 }}>
            {toolCount} / {limits.tools_max}
          </span>
        </h3>
        <FieldErrors errors={at(region.tools)} />
        {verdictTool && (
          <p className={b.verdictRow}>
            <span className={styles.toolName}>{verdictTool.name}</span> <TierBadge tier={verdictTool.tier} /> is always
            included: every run ends by reporting its verdict.
          </p>
        )}
        {catalogGroups.map((g) => (
          <fieldset key={g.tier} className={styles.fieldset} style={{ marginBottom: 12 }}>
            <legend className={styles.legend}>{TIER_TITLE[g.tier] ?? g.tier.toUpperCase()}</legend>
            <ul className={b.toolRows}>{g.tools.map(toolRow)}</ul>
          </fieldset>
        ))}
        {foreign.length > 0 && (
          <fieldset className={styles.fieldset} style={{ marginBottom: 12 }}>
            <legend className={styles.legend}>NOT ALLOWED IN A BUILT STEWARD</legend>
            <ul className={b.toolRows}>
              {foreign.map((name) => (
                <li key={name} className={b.toolRow}>
                  <div className={b.toolHead}>
                    <label className={styles.check}>
                      <input type="checkbox" checked onChange={() => toggleTool(name, false)} />
                      <span className={styles.toolName}>{name}</span>
                    </label>
                  </div>
                  <FieldErrors errors={at(region.tool(name))} />
                </li>
              ))}
            </ul>
          </fieldset>
        )}
        {sensitivePicked.length > 0 && (
          <div className={shared.warnBox} role="group" aria-label="Private data" style={{ marginBottom: 0 }}>
            <label className={styles.check}>
              <input type="checkbox" checked={form.sensitiveOk} onChange={(e) => patch({ sensitiveOk: e.target.checked })} />
              <strong>I allow this steward to read private data ({sensitivePicked.join(', ')})</strong>
            </label>
            <p className={shared.help} style={{ marginTop: 6 }}>
              A screenshot, a file, a person’s web history: these tools can show things the steward’s prompt never meant to
              see. This is the sensitive_ok flag; without it the steward cannot be saved with them.
            </p>
            <FieldErrors errors={at(region.sensitive)} />
          </div>
        )}
      </section>

      <section className={shared.card} aria-label="Constraints" style={{ marginBottom: 0 }}>
        <h3 className={styles.heading}>CONSTRAINTS</h3>
        <p className={shared.help} style={{ marginBottom: 12 }}>
          A tool that changes something only runs with every required argument bound: to literal values you list here, or to
          a list parameter a person sets on each install. An argument outside its values is refused.
        </p>
        <FieldErrors errors={at(region.constraints)} />
        {constraintTools.length === 0 ? (
          <p className={shared.help}>No tool picked changes anything, so nothing needs binding.</p>
        ) : (
          constraintTools.map(constraintGroup)
        )}
      </section>

      <section className={shared.card} aria-label="Parameters" style={{ marginBottom: 0 }}>
        <h3 className={styles.heading}>PARAMETERS</h3>
        <p className={shared.help} style={{ marginBottom: 10 }}>
          Values a person sets on each install after saving. A steward that works on PCs takes <code>hosts</code>; one that
          runs on a schedule takes a <code>window</code>.
        </p>
        <FieldErrors errors={at(region.params)} />
        {form.params.length > 0 && (
          <ul className={b.chips} aria-label="Declared parameters">
            {form.params.map((p) => (
              <li key={p} className={b.chip}>
                <span className={styles.toolName}>{p}</span>
                <span className={`${styles.tag} ${styles.toneMuted}`}>{kindOf(p).toUpperCase()}</span>
                <button
                  type="button"
                  className={styles.linkButton}
                  aria-label={`Remove parameter ${p}`}
                  onClick={() => removeParam(p)}
                >
                  REMOVE
                </button>
                <FieldErrors errors={at(region.param(p))} />
              </li>
            ))}
          </ul>
        )}
        <div className={styles.row} style={{ marginTop: 10 }}>
          <label className={shared.field}>
            <span className={shared.fieldLabel}>ADD A PARAMETER</span>
            <select
              className={shared.input}
              aria-label="Add a parameter"
              value=""
              onChange={(e) => {
                if (e.target.value === '__own__') setOwnParam('')
                else if (e.target.value) addParam(e.target.value)
              }}
            >
              <option value="">choose a kind…</option>
              {Object.entries(catalog.param_kinds).map(([name, kind]) => (
                <option key={name} value={name} disabled={form.params.includes(name)}>
                  {name} — {kind}
                </option>
              ))}
              <option value="__own__">a list of values, named by you…</option>
            </select>
          </label>
          {ownParam !== null && (
            <>
              <label className={shared.field}>
                <span className={shared.fieldLabel}>NAME OF THE LIST</span>
                <input
                  className={`${shared.input} ${shared.mono}`}
                  value={ownParam}
                  onChange={(e) => setOwnParam(e.target.value)}
                  placeholder="packages"
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>
              <button
                type="button"
                className={shared.btn}
                disabled={ownParam.trim() === ''}
                onClick={() => {
                  addParam(ownParam)
                  setOwnParam(null)
                }}
              >
                ADD LIST
              </button>
            </>
          )}
        </div>
        {(needsHosts || needsWindow) && (
          <p className={shared.help} style={{ marginTop: 8 }}>
            {needsHosts && (
              <>
                This steward works on PCs but declares no <code>hosts</code>.{' '}
                <button type="button" className={styles.linkButton} onClick={() => addParam('hosts')}>
                  ADD HOSTS
                </button>{' '}
              </>
            )}
            {needsWindow && (
              <>
                A schedule needs a <code>window</code>.{' '}
                <button type="button" className={styles.linkButton} onClick={() => addParam('window')}>
                  ADD WINDOW
                </button>
              </>
            )}
          </p>
        )}
      </section>

      <section className={shared.card} aria-label="Triggers" style={{ marginBottom: 0 }}>
        <h3 className={styles.heading}>WHAT MAY START IT</h3>
        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>CEILING</legend>
          <div className={styles.checkGrid}>
            <label className={styles.check}>
              <input type="checkbox" checked={form.allowSchedule} onChange={(e) => patch({ allowSchedule: e.target.checked })} />
              A schedule
            </label>
            <label className={styles.check}>
              <input type="checkbox" checked={form.allowCommand} onChange={(e) => patch({ allowCommand: e.target.checked })} />
              A command (Ask kenny or the dashboard)
            </label>
          </div>
          <div className={styles.checkGrid} style={{ marginTop: 8 }} role="group" aria-label="Events">
            {eventNames.map((name) => (
              <span key={name}>
                <label className={styles.check}>
                  <input
                    type="checkbox"
                    checked={form.allowEvents.includes(name)}
                    onChange={(e) => patch({ allowEvents: toggleIn(form.allowEvents, name, e.target.checked) })}
                  />
                  {eventLabel(name)}
                </label>
                <FieldErrors errors={at(region.event(name))} />
              </span>
            ))}
          </div>
          <p className={shared.help} style={{ marginTop: 8 }}>
            What a person may switch on for this steward on an install. A new ticket is never among them: that starts
            triage only.
          </p>
          <FieldErrors errors={at(region.ceiling)} />
        </fieldset>
        <fieldset className={styles.fieldset} style={{ marginTop: 12 }}>
          <legend className={styles.legend}>STARTS BY DEFAULT</legend>
          <div className={styles.checkGrid} role="radiogroup" aria-label="Baseline trigger">
            <label className={styles.check}>
              <input
                type="radio"
                name={`${uid}-trigger`}
                checked={form.triggerKind === 'schedule'}
                onChange={() => patch({ triggerKind: 'schedule', allowSchedule: true })}
              />
              On a schedule
            </label>
            <label className={styles.check}>
              <input
                type="radio"
                name={`${uid}-trigger`}
                checked={form.triggerKind === 'event'}
                onChange={() => {
                  const event = form.triggerEvent || catalog.events[0] || ''
                  patch({
                    triggerKind: 'event',
                    triggerEvent: event,
                    allowEvents: event ? toggleIn(form.allowEvents, event, true) : form.allowEvents,
                  })
                }}
              />
              When an event happens
            </label>
          </div>
          {form.triggerKind === 'schedule' ? (
            <label className={shared.field} style={{ marginTop: 10, maxWidth: 260 }}>
              <span className={shared.fieldLabel}>EVERY N DAYS (OPTIONAL)</span>
              <input
                className={shared.input}
                inputMode="numeric"
                value={form.minIntervalDays}
                placeholder="every window"
                onChange={(e) => patch({ minIntervalDays: e.target.value })}
              />
            </label>
          ) : (
            <label className={shared.field} style={{ marginTop: 10, maxWidth: 360 }}>
              <span className={shared.fieldLabel}>EVENT</span>
              <select
                className={shared.input}
                aria-label="Baseline event"
                value={form.triggerEvent}
                onChange={(e) =>
                  patch({
                    triggerEvent: e.target.value,
                    allowEvents: toggleIn(form.allowEvents, e.target.value, true),
                  })
                }
              >
                {form.triggerEvent === '' && <option value="">choose an event…</option>}
                {eventNames.map((name) => (
                  <option key={name} value={name}>
                    {eventLabel(name)}
                  </option>
                ))}
              </select>
            </label>
          )}
          <FieldErrors errors={at(region.trigger)} />
        </fieldset>
      </section>

      <section className={shared.card} aria-label="Budget and timeouts" style={{ marginBottom: 0 }}>
        <h3 className={styles.heading}>BUDGET AND TIMEOUTS</h3>
        <div className={styles.row}>
          <Field
            id={`${uid}-budget`}
            label={`MODEL ROUND-TRIPS PER RUN (${limits.max_iterations_min} TO ${limits.max_iterations_max})`}
            errors={at(region.budget)}
          >
            <input
              id={`${uid}-budget`}
              className={shared.input}
              inputMode="numeric"
              value={form.budget}
              onChange={(e) => patch({ budget: e.target.value })}
              aria-invalid={at(region.budget) ? true : undefined}
              aria-describedby={`${uid}-budget-errors`}
              style={{ maxWidth: 140 }}
            />
          </Field>
        </div>
        <h4 className={styles.sub}>TIMEOUT PER CHANGE TOOL (SECONDS, AT MOST {limits.timeout_max_s})</h4>
        <FieldErrors errors={at(region.timeouts)} />
        {timeoutTools.length === 0 ? (
          <p className={shared.help}>No tool picked changes anything. Every call is bounded by the global ten minutes.</p>
        ) : (
          <div className={styles.row}>
            {timeoutTools.map((t) => (
              <Field key={t} id={`${uid}-timeout-${t}`} label={t} errors={at(region.timeout(t))}>
                <input
                  id={`${uid}-timeout-${t}`}
                  className={shared.input}
                  inputMode="numeric"
                  aria-label={`Timeout for ${t}`}
                  value={form.timeouts.find((x) => x.tool === t)?.text ?? ''}
                  placeholder="global limit"
                  onChange={(e) => setToolTimeout(t, e.target.value)}
                  style={{ maxWidth: 140 }}
                />
              </Field>
            ))}
          </div>
        )}
      </section>

      <section className={shared.card} aria-label="Preconditions" style={{ marginBottom: 0 }}>
        <h3 className={styles.heading}>PRECONDITIONS</h3>
        <p className={shared.help} style={{ marginBottom: 10 }}>
          Checks the server makes before a run starts; one that fails skips the run. Only the ones this server registers.
        </p>
        <FieldErrors errors={at(region.preconditions)} />
        {preconditionNames.length === 0 ? (
          <p className={shared.help}>This server registers none.</p>
        ) : (
          <div className={styles.checkGrid} role="group" aria-label="Preconditions">
            {preconditionNames.map((name) => (
              <span key={name}>
                <label className={styles.check}>
                  <input
                    type="checkbox"
                    checked={form.preconditions.includes(name)}
                    onChange={(e) => patch({ preconditions: toggleIn(form.preconditions, name, e.target.checked) })}
                  />
                  <span className={styles.toolName}>{name}</span>
                </label>
                <FieldErrors errors={at(region.precondition(name))} />
              </span>
            ))}
          </div>
        )}
      </section>

      <div className={shared.warnBox} role="note" style={{ marginBottom: 0 }}>
        {mode === 'create' ? (
          <>
            A new steward starts in <strong>shadow</strong>: it does the whole job but every change is only recorded as a
            recommendation. Saving voids any standing authorization an earlier steward with this id had; nothing carries
            over. A superuser has to choose act, and grant what it may do, afterwards.
          </>
        ) : (
          <>
            Saving gives this steward a new version: it moves to <strong>shadow</strong> and every standing authorization it
            has is <strong>voided for good</strong>, even if you restore an earlier definition. A superuser has to choose act
            and grant again for what then stands.
          </>
        )}
      </div>

      <div className={shared.actions} style={{ marginTop: 0 }}>
        <button type="submit" className={shared.btnPrimary} disabled={save.isPending || sensitiveMissing}>
          {save.isPending ? 'SAVING…' : mode === 'create' ? 'CREATE STEWARD IN SHADOW' : 'SAVE NEW VERSION'}
        </button>
        <button type="button" className={shared.btn} onClick={onCancel} disabled={save.isPending}>
          CANCEL
        </button>
      </div>
      {sensitiveMissing && (
        <p className={shared.help}>Tick “I allow this steward to read private data” to save with {sensitivePicked.join(', ')}.</p>
      )}

      <div aria-live="polite">
        {stale && (
          <div className={shared.errorBox} role="alert">
            This steward changed since you opened it (another save, or a release), so nothing was saved. Reload to see its
            current definition; what you typed here is replaced.{' '}
            {onReload && (
              <button type="button" className={styles.linkButton} onClick={onReload}>
                RELOAD
              </button>
            )}
          </div>
        )}
        {problem && (
          <div className={shared.errorBox} role="alert">
            {problem}
          </div>
        )}
      </div>
    </form>
  )
}

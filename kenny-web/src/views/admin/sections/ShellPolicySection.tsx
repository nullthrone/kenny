import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../../api/client'
import EmptyState from '../../../components/EmptyState/EmptyState'
import EditableSettingRow from '../EditableSettingRow'
import type { AdminRow, PolicyRule, PolicyRulesResponse, ShellAllowResponse } from '../types'
import shared from '../shared.module.css'

export interface ShellPolicySectionProps {
  rows: AdminRow[]
}

const MODE_KEY = 'KENNY_SHELL_POLICY_MODE'

/** The two surfaces an allow rule can be matched against — the only ones with a command. */
const ALLOW_TARGETS: { value: string; label: string }[] = [
  { value: 'posix', label: 'shell_exec (Linux/macOS)' },
  { value: 'powershell', label: 'powershell_exec (Windows)' },
]

/** Deny rules cover two more surfaces, because they also guard paths and the agent itself. */
const DENY_TARGETS: { value: string; label: string }[] = [
  ...ALLOW_TARGETS,
  { value: 'path', label: 'file paths (fs_read / fs_list / fs_search)' },
  { value: 'self_protection', label: 'the agent itself' },
]

/**
 * Admin → Shell policy. What `powershell_exec` and `shell_exec` may run, fleet-wide.
 *
 * Two lists with different jobs, which is why they are not merged: **deny** rules say
 * what must never run and are evaluated first and always; the **allow** list only
 * matters under `allowlist` mode and can never lift a deny rule. The built-in catalog
 * is shown read-only because the agent compiles it in — it is a floor no operator can
 * lower. See ADR-0064.
 */
export default function ShellPolicySection({ rows }: ShellPolicySectionProps) {
  const queryClient = useQueryClient()
  const modeRow = rows.find((r) => r.key === MODE_KEY)
  const mode = modeRow?.value === null || modeRow?.value === undefined ? 'unrestricted' : String(modeRow.value)

  const allow = useQuery({
    queryKey: ['admin', 'shell-allow'],
    queryFn: () => api.get<ShellAllowResponse>('/api/policy/shell-allow'),
  })
  const deny = useQuery({
    queryKey: ['admin', 'policy-rules'],
    queryFn: () => api.get<PolicyRulesResponse>('/api/policy/rules'),
  })

  const [allowDraft, setAllowDraft] = useState({ id: '', applies_to: 'posix', pattern: '', reason: '' })
  const [denyDraft, setDenyDraft] = useState({ id: '', applies_to: 'posix', pattern: '', reason: '' })

  const addAllow = useMutation({
    mutationFn: () => api.post<ShellAllowResponse>('/api/policy/shell-allow', allowDraft),
    onSuccess: () => {
      setAllowDraft({ id: '', applies_to: allowDraft.applies_to, pattern: '', reason: '' })
      queryClient.invalidateQueries({ queryKey: ['admin', 'shell-allow'] })
    },
  })
  const removeAllow = useMutation({
    mutationFn: (id: string) => api.delete(`/api/policy/shell-allow/${encodeURIComponent(id)}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['admin', 'shell-allow'] }),
  })
  const addDeny = useMutation({
    mutationFn: () => api.post<PolicyRulesResponse>('/api/policy/rules', denyDraft),
    onSuccess: () => {
      setDenyDraft({ id: '', applies_to: denyDraft.applies_to, pattern: '', reason: '' })
      queryClient.invalidateQueries({ queryKey: ['admin', 'policy-rules'] })
    },
  })
  const removeDeny = useMutation({
    mutationFn: (id: string) => api.delete(`/api/policy/rules/${encodeURIComponent(id)}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['admin', 'policy-rules'] }),
  })

  if (allow.isLoading || deny.isLoading) return <div className={shared.loading}>Loading…</div>
  if (allow.isError || deny.isError) {
    return <EmptyState title="Could not load the shell policy" message="Something went wrong. Reload to try again." />
  }

  const allowRules = allow.data?.allow ?? []
  const operatorRules = deny.data?.operator ?? []
  const builtinRules = deny.data?.builtin ?? []

  return (
    <div>
      <p className={shared.help} style={{ marginBottom: 16 }}>
        What <code className={shared.mono}>powershell_exec</code> and <code className={shared.mono}>shell_exec</code> may
        run on every managed host. Deny rules are checked first and always, so an allow rule can never lift one.
      </p>

      {modeRow ? (
        <div className={shared.rows} style={{ marginBottom: 24 }}>
          <EditableSettingRow row={modeRow} />
        </div>
      ) : null}

      {mode === 'allowlist' && allowRules.length === 0 ? (
        <div className={shared.warnBox} style={{ marginBottom: 24 }}>
          Allowlist mode with an empty list blocks every shell call on every host. Add a rule below, or set the mode
          back to unrestricted.
        </div>
      ) : null}
      {mode === 'off' ? (
        <div className={shared.warnBox} style={{ marginBottom: 24 }}>
          Shell execution is off fleet-wide. Every <code className={shared.mono}>powershell_exec</code> and{' '}
          <code className={shared.mono}>shell_exec</code> call is refused.
        </div>
      ) : null}

      <div className={shared.cardTitle}>ALLOW RULES</div>
      <p className={shared.help} style={{ marginBottom: 12 }}>
        Used only in allowlist mode. A command must match a rule <strong>in full</strong> — a rule of{' '}
        <code className={shared.mono}>uname -a</code> does not admit <code className={shared.mono}>uname -a; rm -rf /</code>.
      </p>
      {allowRules.length === 0 ? (
        <EmptyState title="No allow rules" message="Nothing is permitted while the mode is allowlist." />
      ) : (
        <div className={shared.table} style={{ marginBottom: 24 }}>
          {allowRules.map((r) => (
            <div key={r.id} className={shared.tableRow}>
              <div className={shared.tableMeta}>
                <div className={shared.tableLabel}>{r.id}</div>
                <div className={shared.tableSub}>
                  {r.applies_to} · <span className={shared.mono}>{r.pattern}</span> · {r.reason}
                </div>
              </div>
              <button
                type="button"
                className={shared.btnDanger}
                onClick={() => removeAllow.mutate(r.id)}
                disabled={removeAllow.isPending}
              >
                REMOVE
              </button>
            </div>
          ))}
        </div>
      )}
      {addAllow.isError && (
        <div className={shared.errorBox}>
          {addAllow.error instanceof ApiError ? addAllow.error.message : 'Could not add the rule.'}
        </div>
      )}
      <RuleForm
        draft={allowDraft}
        onChange={setAllowDraft}
        targets={ALLOW_TARGETS}
        pending={addAllow.isPending}
        submitLabel="ADD ALLOW RULE"
        onSubmit={() => addAllow.mutate()}
      />

      <div className={shared.cardTitle} style={{ marginTop: 32 }}>
        DENY RULES
      </div>
      <p className={shared.help} style={{ marginBottom: 12 }}>
        Checked in every mode. These match anywhere in the command, not just the whole of it.
      </p>
      {operatorRules.length === 0 ? (
        <EmptyState title="No operator deny rules" message="The built-in catalog below still applies." />
      ) : (
        <div className={shared.table} style={{ marginBottom: 24 }}>
          {operatorRules.map((r) => (
            <div key={r.id} className={shared.tableRow}>
              <div className={shared.tableMeta}>
                <div className={shared.tableLabel}>{r.id}</div>
                <div className={shared.tableSub}>
                  {r.applies_to} · <span className={shared.mono}>{r.pattern}</span> · {r.reason}
                </div>
              </div>
              <button
                type="button"
                className={shared.btnDanger}
                onClick={() => removeDeny.mutate(r.id)}
                disabled={removeDeny.isPending}
              >
                REMOVE
              </button>
            </div>
          ))}
        </div>
      )}
      {addDeny.isError && (
        <div className={shared.errorBox}>
          {addDeny.error instanceof ApiError ? addDeny.error.message : 'Could not add the rule.'}
        </div>
      )}
      <RuleForm
        draft={denyDraft}
        onChange={setDenyDraft}
        targets={DENY_TARGETS}
        pending={addDeny.isPending}
        submitLabel="ADD DENY RULE"
        onSubmit={() => addDeny.mutate()}
      />

      <details style={{ marginTop: 32 }}>
        <summary className={shared.cardTitle} style={{ cursor: 'pointer' }}>
          BUILT-IN DENY RULES ({builtinRules.length})
        </summary>
        <p className={shared.help} style={{ margin: '12px 0' }}>
          Compiled into every agent and shipped with it. They cannot be edited or removed from here — they are the floor
          the rules above build on.
        </p>
        <div className={shared.table}>
          {builtinRules.map((r) => (
            <div key={r.id} className={shared.tableRow}>
              <div className={shared.tableMeta}>
                <div className={shared.tableLabel}>{r.id}</div>
                <div className={shared.tableSub}>
                  {r.applies_to} · <span className={shared.mono}>{r.pattern}</span> · {r.reason}
                </div>
              </div>
            </div>
          ))}
        </div>
      </details>
    </div>
  )
}

interface RuleFormProps {
  draft: PolicyRule
  onChange: (next: PolicyRule) => void
  targets: { value: string; label: string }[]
  pending: boolean
  submitLabel: string
  onSubmit: () => void
}

/** The add-a-rule form. Identical shape for allow and deny, so it is written once. */
function RuleForm({ draft, onChange, targets, pending, submitLabel, onSubmit }: RuleFormProps) {
  return (
    <form
      className={shared.actions}
      style={{ marginTop: 0, alignItems: 'flex-end' }}
      onSubmit={(e) => {
        e.preventDefault()
        onSubmit()
      }}
    >
      <label className={shared.field}>
        <span className={shared.fieldLabel}>ID</span>
        <input
          type="text"
          className={shared.input}
          value={draft.id}
          onChange={(e) => onChange({ ...draft, id: e.target.value })}
          required
        />
      </label>
      <label className={shared.field}>
        <span className={shared.fieldLabel}>APPLIES TO</span>
        <select
          className={shared.input}
          value={draft.applies_to}
          onChange={(e) => onChange({ ...draft, applies_to: e.target.value })}
        >
          {targets.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label}
            </option>
          ))}
        </select>
      </label>
      <label className={shared.field} style={{ minWidth: 200 }}>
        <span className={shared.fieldLabel}>PATTERN (REGEX)</span>
        <input
          type="text"
          className={`${shared.input} ${shared.mono}`}
          value={draft.pattern}
          onChange={(e) => onChange({ ...draft, pattern: e.target.value })}
          required
        />
      </label>
      <label className={shared.field} style={{ minWidth: 160 }}>
        <span className={shared.fieldLabel}>REASON</span>
        <input
          type="text"
          className={shared.input}
          value={draft.reason}
          onChange={(e) => onChange({ ...draft, reason: e.target.value })}
          required
        />
      </label>
      <button
        type="submit"
        className={shared.btnPrimary}
        disabled={pending || !draft.id || !draft.pattern || !draft.reason}
      >
        {pending ? 'ADDING…' : submitLabel}
      </button>
    </form>
  )
}

import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../api/client'
import KeyValueRow from '../../components/KeyValueRow/KeyValueRow'
import type { AdminRow } from './types'
import { CONFIG_SOURCE_COLOR, CONFIG_SOURCE_LABEL } from './types'
import styles from './EditableSettingRow.module.css'

export interface EditableSettingRowProps {
  row: AdminRow
}

function initialEditValue(row: AdminRow): string {
  if (row.type === 'bool') return row.value === true ? 'true' : 'false'
  // A secret's `value` is the masked "set"/"not set" text, never the real
  // value — start the editor blank rather than offering that as a draft.
  if (row.type === 'secret') return ''
  return row.value === null ? '' : String(row.value)
}

/**
 * One config row. View mode is `KeyValueRow` (label / help / value / source
 * badge + an EDIT action, the same Profile-row pattern the design already
 * uses). Edit mode swaps in a control matched to the setting's real type —
 * a toggle for `bool`, a `<select>` for `enum`, a number input with its
 * min/max for `int`/`float`, otherwise text — plus RESET when the value is
 * a `custom` override. Rows the server would reject (`row.editable === false`,
 * the server's own `SettingSpec.writable`) never get an edit control at all.
 */
export default function EditableSettingRow({ row }: EditableSettingRowProps) {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(() => initialEditValue(row))
  const [localError, setLocalError] = useState<string | null>(null)

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ['settings'] })
  }

  const save = useMutation({
    mutationFn: (value: string | boolean) => api.put(`/api/settings/${row.key}`, { value }),
    onSuccess: () => {
      invalidate()
      setEditing(false)
    },
    onError: (err) => setLocalError(err instanceof ApiError ? err.message : 'Could not save. Try again.'),
  })

  const reset = useMutation({
    mutationFn: () => api.delete(`/api/settings/${row.key}`),
    onSuccess: () => {
      invalidate()
      setEditing(false)
    },
    onError: (err) => setLocalError(err instanceof ApiError ? err.message : 'Could not reset. Try again.'),
  })

  function openEditor() {
    setDraft(initialEditValue(row))
    setLocalError(null)
    setEditing(true)
  }

  function submit() {
    setLocalError(null)
    if (row.type === 'bool') {
      save.mutate(draft === 'true')
      return
    }
    save.mutate(draft)
  }

  if (!editing) {
    return (
      <KeyValueRow
        label={row.label}
        help={row.help}
        value={row.value === null || row.value === '' ? <span style={{ color: 'var(--text-faint)' }}>not set</span> : String(row.value)}
        sourceBadge={{ label: CONFIG_SOURCE_LABEL[row.source], color: CONFIG_SOURCE_COLOR[row.source] }}
        action={row.editable ? { label: 'EDIT', onClick: openEditor } : undefined}
      />
    )
  }

  const busy = save.isPending || reset.isPending

  return (
    <div className={styles.editRow}>
      <div className={styles.meta}>
        <div className={styles.label}>{row.label}</div>
        {row.help && <div className={styles.help}>{row.help}</div>}
      </div>
      <div className={styles.editor}>
        <div className={styles.controlRow}>
          {row.type === 'bool' ? (
            <>
              <button
                type="button"
                className={`${styles.toggleBtn}${draft === 'true' ? ` ${styles.active}` : ''}`}
                onClick={() => setDraft('true')}
              >
                ENABLED
              </button>
              <button
                type="button"
                className={`${styles.toggleBtn}${draft === 'false' ? ` ${styles.active}` : ''}`}
                onClick={() => setDraft('false')}
              >
                DISABLED
              </button>
            </>
          ) : row.type === 'enum' && row.choices ? (
            <select className={styles.input} value={draft} onChange={(e) => setDraft(e.target.value)}>
              {row.choices.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          ) : row.type === 'int' || row.type === 'float' ? (
            <input
              type="number"
              className={styles.input}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              min={row.min ?? undefined}
              max={row.max ?? undefined}
              step={row.type === 'int' ? 1 : 'any'}
            />
          ) : (
            <input
              type={row.type === 'secret' ? 'password' : 'text'}
              className={styles.input}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={row.type === 'secret' ? 'new value' : undefined}
            />
          )}
          <button
            type="button"
            className={styles.saveBtn}
            onClick={submit}
            disabled={busy || (row.type === 'secret' && draft.trim() === '')}
          >
            {save.isPending ? 'SAVING…' : 'SAVE'}
          </button>
          <button
            type="button"
            className={styles.cancelBtn}
            onClick={() => {
              setEditing(false)
              setLocalError(null)
            }}
            disabled={busy}
          >
            CANCEL
          </button>
        </div>
        {row.source === 'db' && (
          <button type="button" className={styles.resetBtn} onClick={() => reset.mutate()} disabled={busy}>
            {reset.isPending ? 'RESETTING…' : 'RESET TO DEFAULT'}
          </button>
        )}
        {localError && <span className={styles.error}>{localError}</span>}
      </div>
    </div>
  )
}

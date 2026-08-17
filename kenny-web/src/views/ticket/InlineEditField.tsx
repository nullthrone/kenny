import { useState } from 'react'
import styles from './InlineEditField.module.css'

export interface InlineEditFieldProps {
  label: string
  value: string
  /** What to show instead of the raw value when not editing (e.g. an uppercased priority). Defaults to `value`. */
  displayValue?: string
  /** A closed vocabulary (from `GET /api/tickets/vocabulary`) renders a `<select>`; omit for free text (title). */
  options?: string[]
  saving: boolean
  onSave: (value: string) => void
}

/** One `PATCH /api/tickets/{id}` field — label, current value, an EDIT toggle. */
export default function InlineEditField({ label, value, displayValue, options, saving, onSave }: InlineEditFieldProps) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value)

  if (!editing) {
    return (
      <div className={styles.row}>
        <span className={styles.label}>{label}</span>
        <span className={styles.value}>{displayValue ?? value}</span>
        <button
          type="button"
          className={styles.editBtn}
          onClick={() => {
            setDraft(value)
            setEditing(true)
          }}
        >
          EDIT
        </button>
      </div>
    )
  }

  function commit() {
    onSave(draft)
    setEditing(false)
  }

  return (
    <div className={styles.row}>
      <span className={styles.label}>{label}</span>
      {options ? (
        <select className={styles.select} value={draft} onChange={(e) => setDraft(e.target.value)} disabled={saving}>
          {options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      ) : (
        <input
          className={styles.input}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          disabled={saving}
          autoFocus
        />
      )}
      <button type="button" className={styles.saveBtn} onClick={commit} disabled={saving}>
        SAVE
      </button>
      <button type="button" className={styles.cancelBtn} onClick={() => setEditing(false)} disabled={saving}>
        CANCEL
      </button>
    </div>
  )
}

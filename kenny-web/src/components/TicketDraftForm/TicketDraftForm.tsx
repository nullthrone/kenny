import styles from './TicketDraftForm.module.css'

/** What both surfaces collect before a ticket is opened. */
export interface TicketDraftValue {
  /** Only edited where it is shown; the inbox's form derives it on submit. */
  title: string
  description: string
  /** `null` is the deliberate "no PC yet" answer, not a missing one. */
  host: string | null
  startImmediately: boolean
}

export interface TicketDraftFormProps {
  value: TicketDraftValue
  onChange: (next: TicketDraftValue) => void
  /** Hosts to offer. "No PC yet" is always offered on top of these. */
  hosts: string[]
  /** Prefixes every `id`, so two of these can be on one page. */
  idPrefix: string
  /**
   * Show the title as its own field. The inbox's form does not: it asks one
   * question and derives a title from the answer. A drafted ticket does,
   * because kenny wrote a title and the operator is reviewing it.
   */
  showTitle?: boolean
  disabled?: boolean
}

/**
 * The fields a new ticket is made of, shared by the inbox's NEW TICKET modal
 * and the draft card in the Ask kenny drawer.
 *
 * Shared rather than copied because the two surfaces must ask the same
 * questions: a ticket drafted out of a conversation and one typed by hand are
 * the same object, opened through the same route, and a field that existed on
 * only one of them would be a ticket whose shape depended on where it came
 * from. Layout and submission stay with each caller — this owns the questions,
 * not the chrome.
 */
export default function TicketDraftForm({
  value,
  onChange,
  hosts,
  idPrefix,
  showTitle = false,
  disabled = false,
}: TicketDraftFormProps) {
  const set = (patch: Partial<TicketDraftValue>) => onChange({ ...value, ...patch })

  return (
    <>
      <label className={styles.label} htmlFor={`${idPrefix}-host-group`}>
        Which PC?
      </label>
      <div
        className={styles.hosts}
        id={`${idPrefix}-host-group`}
        role="group"
        aria-label="Which PC?"
      >
        {hosts.map((host) => (
          <button
            key={host}
            type="button"
            disabled={disabled}
            aria-pressed={value.host === host}
            className={`${styles.hostPill} kc-btn${value.host === host ? ` ${styles.hostPillActive}` : ''}`}
            onClick={() => set({ host })}
          >
            {host}
          </button>
        ))}
        <button
          type="button"
          disabled={disabled}
          aria-pressed={value.host === null}
          className={`${styles.hostPill} kc-btn${value.host === null ? ` ${styles.hostPillActive}` : ''}`}
          onClick={() => set({ host: null })}
        >
          No PC yet
        </button>
      </div>

      {showTitle && (
        <>
          <label className={styles.label} htmlFor={`${idPrefix}-title`}>
            Title
          </label>
          <input
            id={`${idPrefix}-title`}
            className={styles.input}
            value={value.title}
            disabled={disabled}
            onChange={(e) => set({ title: e.target.value })}
          />
        </>
      )}

      <label className={styles.label} htmlFor={`${idPrefix}-description`}>
        What should kenny do?
      </label>
      <textarea
        id={`${idPrefix}-description`}
        className={styles.textarea}
        placeholder="Describe the problem or task — kenny plans the steps and asks before changing anything."
        value={value.description}
        disabled={disabled}
        onChange={(e) => set({ description: e.target.value })}
      />
      <label className={styles.checkboxRow}>
        <input
          type="checkbox"
          className={styles.checkbox}
          checked={value.startImmediately}
          disabled={disabled}
          onChange={(e) => set({ startImmediately: e.target.checked })}
        />
        Start working immediately (read-only steps only until I approve)
      </label>
    </>
  )
}

import { useEffect, useState, type ReactNode } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { useQueryClient } from '@tanstack/react-query'
import EmptyState from '../../../../components/EmptyState/EmptyState'
import Modal from '../../../../components/Modal/Modal'
import type { BuilderCatalog, InvalidSteward, Steward } from '../../types'
import BuilderPanel from './BuilderPanel'
import { emptyData, normalizeData, takeDraft, type DraftResult } from './builderForm'
import {
  STEWARDS_KEY,
  consentErrorMessage,
  useBuilderCatalog,
  useBuiltSteward,
  useCloneDraft,
  useDeleteBuilt,
} from './queries'
import shared from '../../shared.module.css'
import styles from './stewards.module.css'

/** Runs `children` once the builder catalog has been read; says so when it cannot be. */
function WithCatalog({ children }: { children: (catalog: BuilderCatalog) => ReactNode }) {
  const catalog = useBuilderCatalog()
  if (catalog.isLoading) return <div className={shared.loading}>Loading…</div>
  if (catalog.isError || !catalog.data) {
    return (
      <EmptyState
        title="Could not load what a steward may be made of"
        message="The builder needs the server's catalog of tools and limits. Reload to try again."
      />
    )
  }
  return <>{children(catalog.data)}</>
}

/** `#/admin/stewards/new` (and `?draft=1`, a draft Ask kenny left): the empty form. Superuser only. */
export function NewStewardPage({ canManage }: { canManage: boolean }) {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const wantsDraft = params.get('draft') === '1'
  // `undefined`: not looked yet. `'none'`: asked for a draft and there was none.
  const [draft, setDraft] = useState<DraftResult | 'none' | undefined>(undefined)

  useEffect(() => {
    if (!wantsDraft || !canManage) return
    // The draft is taken (read and removed) exactly once. A second run of this effect (React
    // strict mode) finds nothing and must not overwrite what the first one read.
    const taken = takeDraft()
    setDraft((prev) => taken ?? prev ?? 'none')
  }, [wantsDraft, canManage])

  const back = (
    <Link to="/admin/stewards" className={styles.back}>
      ← ALL STEWARDS
    </Link>
  )

  if (!canManage) {
    return (
      <div>
        {back}
        <EmptyState
          title="Creating a steward needs a superuser"
          message="Sign in to the dashboard as a superuser to define a steward. Operators can read the ones that exist."
        />
      </div>
    )
  }

  return (
    <div>
      {back}
      <div className={styles.detailHead}>
        <h2 className={styles.detailTitle}>New steward</h2>
      </div>
      <p className={shared.help} style={{ marginBottom: 20 }}>
        A steward you define runs through the same runner and gate as a shipped one. It only chooses among and narrows what
        the server provides.
      </p>
      <WithCatalog>
        {(catalog) => {
          if (wantsDraft && draft === undefined) return <div className={shared.loading}>Loading…</div>
          const fromDraft = draft !== undefined && draft !== 'none' && 'data' in draft ? draft.data : null
          let banner = null
          if (fromDraft) {
            banner = (
              <div className={shared.warnBox} role="status" style={{ marginBottom: 0 }}>
                Draft from Ask kenny — review before saving. Nothing is saved until you press create.
              </div>
            )
          } else if (wantsDraft) {
            banner = (
              <div className={shared.warnBox} role="status" style={{ marginBottom: 0 }}>
                {draft === 'none'
                  ? 'There is no draft from Ask kenny to open (it may already have been opened). Starting from an empty form.'
                  : 'The draft from Ask kenny could not be read. Starting from an empty form.'}
              </div>
            )
          }
          return (
            <BuilderPanel
              catalog={catalog}
              mode="create"
              initial={fromDraft ?? emptyData()}
              source={fromDraft ? 'copilot' : 'blank'}
              banner={banner}
              onSaved={(saved) => navigate(`/admin/stewards/${encodeURIComponent(saved.steward_id)}`)}
              onCancel={() => navigate('/admin/stewards')}
            />
          )
        }}
      </WithCatalog>
    </div>
  )
}

/** A shipped steward as a draft built steward: loads the server's copy, with what a built steward may not keep. */
export function CloneEditor({ steward, onCancel }: { steward: Steward; onCancel: () => void }) {
  const navigate = useNavigate()
  const preview = useCloneDraft(steward.id, `custom_${steward.id}`)
  return (
    <div>
      <button type="button" className={styles.back} style={{ background: 'transparent', border: 'none', padding: 0 }} onClick={onCancel}>
        ← BACK TO {steward.title.toUpperCase()}
      </button>
      <div className={styles.detailHead}>
        <h2 className={styles.detailTitle}>Copy of {steward.title}</h2>
      </div>
      <WithCatalog>
        {(catalog) => {
          if (preview.isLoading) return <div className={shared.loading}>Loading…</div>
          const data = normalizeData(preview.data?.data)
          if (preview.isError || !preview.data || !data) {
            return <EmptyState title="Could not copy this steward" message="Something went wrong. Reload to try again." />
          }
          const errors = preview.data.errors ?? []
          return (
            <BuilderPanel
              catalog={catalog}
              mode="create"
              initial={data}
              initialErrors={errors}
              source={preview.data.source}
              banner={
                <div className={errors.length > 0 ? shared.warnBox : shared.okBox} role="status" style={{ marginBottom: 0 }}>
                  An unsaved copy of {steward.title}, as a steward you define.{' '}
                  {errors.length > 0
                    ? `${errors.length} thing${errors.length === 1 ? '' : 's'} in it a built steward may not keep ${errors.length === 1 ? 'is' : 'are'} marked below; fix ${errors.length === 1 ? 'it' : 'them'} to save.`
                    : 'A built steward may keep all of it.'}
                </div>
              }
              onSaved={(saved) => navigate(`/admin/stewards/${encodeURIComponent(saved.steward_id)}`)}
              onCancel={onCancel}
            />
          )
        }}
      </WithCatalog>
    </div>
  )
}

/** The editor of one built steward, loaded from `GET /api/stewards/custom/{id}` (an invalid one included). */
export function EditEditor({
  stewardId,
  title,
  onCancel,
  onSaved,
}: {
  stewardId: string
  title: string
  onCancel: () => void
  onSaved: (message: string) => void
}) {
  const queryClient = useQueryClient()
  const query = useBuiltSteward(stewardId)
  return (
    <div>
      <button type="button" className={styles.back} style={{ background: 'transparent', border: 'none', padding: 0 }} onClick={onCancel}>
        ← BACK TO {title.toUpperCase()}
      </button>
      <div className={styles.detailHead}>
        <h2 className={styles.detailTitle}>Edit {title}</h2>
      </div>
      <WithCatalog>
        {(catalog) => {
          if (query.isLoading) return <div className={shared.loading}>Loading…</div>
          const data = normalizeData(query.data?.data)
          if (query.isError || !query.data || !data) {
            return <EmptyState title="Could not load this steward" message="Something went wrong. Reload to try again." />
          }
          const record = query.data
          return (
            <BuilderPanel
              // Re-keyed on what the server holds, so a reload shows the stored definition, not a stale draft.
              key={`${record.effective_hash}:${record.version}`}
              catalog={catalog}
              mode="edit"
              initial={{ ...data, id: stewardId }}
              initialErrors={record.errors}
              effectiveHash={record.effective_hash}
              banner={
                record.invalid ? (
                  <div className={shared.warnBox} role="status" style={{ marginBottom: 0 }}>
                    This steward no longer validates on this server, so it does not run. Fix what is marked and save; it then
                    runs again, in shadow.
                  </div>
                ) : undefined
              }
              onSaved={(saved) =>
                onSaved(
                  `Saved as version ${saved.version}. The steward is in ${saved.mode}.` +
                    (saved.voided > 0
                      ? ` ${saved.voided} standing authorization${saved.voided === 1 ? '' : 's'} voided.`
                      : ''),
                )
              }
              onCancel={onCancel}
              onReload={() => queryClient.invalidateQueries({ queryKey: STEWARDS_KEY })}
            />
          )
        }}
      </WithCatalog>
    </div>
  )
}

/** Deleting a built steward removes it from the catalog and voids what was granted it, for good. */
export function DeleteBuiltModal({
  steward,
  onClose,
}: {
  steward: Pick<Steward | InvalidSteward, 'id' | 'title'>
  onClose: () => void
}) {
  const navigate = useNavigate()
  const del = useDeleteBuilt(steward.id)
  const titleId = `delete-${steward.id}`
  return (
    <Modal open onClose={onClose} labelledBy={titleId} width={480}>
      <div id={titleId} className={shared.cardTitle} style={{ padding: '20px 22px 0', color: 'var(--danger)' }}>
        Delete {steward.title}
      </div>
      <div style={{ padding: '16px 22px' }}>
        <div className={shared.warnBox}>
          This removes <strong className={shared.mono}>{steward.id}</strong> from the catalog at once. Every standing
          authorization it has is <strong>voided for good</strong>, its act setting, parameters and triggers are removed, and a
          run in flight stops acting. Creating a steward with the same id later revives none of it.
        </div>
        {del.isError && (
          <div className={shared.errorBox} role="alert">
            {consentErrorMessage(del.error, 'Could not delete the steward.')}
          </div>
        )}
      </div>
      <div className={shared.actions} style={{ padding: '0 22px 20px' }}>
        <button
          type="button"
          className={shared.btnDanger}
          disabled={del.isPending}
          onClick={() => del.mutate(undefined, { onSuccess: () => navigate('/admin/stewards') })}
        >
          {del.isPending ? 'DELETING…' : 'DELETE STEWARD'}
        </button>
        <button type="button" className={shared.btn} onClick={onClose} disabled={del.isPending}>
          CANCEL
        </button>
      </div>
    </Modal>
  )
}

/** The row of buttons a superuser gets on a steward: copy a shipped one, edit or delete a built one. */
export function BuilderActions({
  builtin,
  onClone,
  onEdit,
  onDelete,
}: {
  builtin: boolean
  onClone: () => void
  onEdit: () => void
  onDelete: () => void
}) {
  return (
    <div className={shared.actions} style={{ marginTop: 0, marginBottom: 16 }}>
      {builtin ? (
        <>
          <button type="button" className={shared.btn} onClick={onEdit}>
            EDIT
          </button>
          <button type="button" className={shared.btnDanger} onClick={onDelete}>
            DELETE
          </button>
        </>
      ) : (
        <button type="button" className={shared.btn} onClick={onClone}>
          CLONE AS A NEW STEWARD
        </button>
      )}
    </div>
  )
}

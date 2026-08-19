import { useState } from 'react'
import { Link, useParams } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { FleetResponse, Me } from '../api/types'
import EmptyState from '../components/EmptyState/EmptyState'
import { ScrollText } from '../components/icons'
import ApprovalGate, { type DecisionOutcome } from './ticket/ApprovalGate'
import InlineEditField from './ticket/InlineEditField'
import NoteComposer from './ticket/NoteComposer'
import TicketActions from './ticket/TicketActions'
import TicketChat from './ticket/TicketChat'
import Timeline from './ticket/Timeline'
import { formatAge } from './inbox/age'
import { actorLabel } from './ticket/eventFormat'
import { ticketApprovalKey, ticketEventsKey, ticketKey } from './ticket/queries'
import { ticketStatusChip } from './ticket/statusChip'
import type { DirectoryUser, Ticket, TicketEvent, TicketVocabulary, TicketApproval } from './ticket/types'
import styles from './ticket/InboxTicket.module.css'

interface ApprovalsListResponse {
  approvals: TicketApproval[]
}
interface DirectoryResponse {
  users: DirectoryUser[]
}

/** `#/inbox/ticket/:id` — status, origin, the timeline, the gate, every lifecycle action, and the ticket's own Ask-kenny composer. */
export default function InboxTicket() {
  const { id } = useParams<{ id: string }>()
  const queryClient = useQueryClient()
  const [banner, setBanner] = useState<{ text: string; warn: boolean } | null>(null)

  const me = useQuery({ queryKey: ['me'], queryFn: () => api.get<Me>('/api/me') })
  const isOperator = me.data ? me.data.role !== 'user' : false
  const meUserId = me.data ? Number(me.data.user_id) : NaN

  const directory = useQuery({
    queryKey: ['users', 'directory'],
    queryFn: () => api.get<DirectoryResponse>('/api/users/directory'),
    enabled: isOperator,
  })

  const vocabulary = useQuery({
    queryKey: ['tickets', 'vocabulary'],
    queryFn: () => api.get<TicketVocabulary>('/api/tickets/vocabulary'),
    staleTime: Infinity,
  })

  const fleet = useQuery({ queryKey: ['fleet'], queryFn: () => api.get<FleetResponse>('/api/fleet') })

  const ticket = useQuery({
    queryKey: id ? ticketKey(id) : ['ticket', 'missing'],
    queryFn: () => api.get<Ticket>(`/api/tickets/${id}`),
    enabled: !!id,
  })

  const events = useQuery({
    queryKey: id ? ticketEventsKey(id) : ['ticket', 'missing', 'events'],
    queryFn: () => api.get<{ events: TicketEvent[] }>(`/api/tickets/${id}/events`),
    enabled: !!id,
  })

  const isBlockedOnApproval = ticket.data?.blocked_on === 'approval'

  const approvals = useQuery({
    queryKey: id ? ticketApprovalKey(id) : ['approvals', 'missing'],
    queryFn: () => api.get<ApprovalsListResponse>(`/api/approvals?ticket_id=${id}`),
    enabled: !!id && isBlockedOnApproval,
  })
  const openApproval = approvals.data?.approvals.find((a) => a.status === 'pending')

  const patch = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.patch<Ticket>(`/api/tickets/${id}`, body),
    onSuccess: () => refetchTicket(),
  })

  function refetchTicket() {
    if (!id) return
    void queryClient.invalidateQueries({ queryKey: ticketKey(id) })
    void queryClient.invalidateQueries({ queryKey: ticketEventsKey(id) })
  }

  function refetchApproval() {
    if (!id) return
    void queryClient.invalidateQueries({ queryKey: ticketApprovalKey(id) })
  }

  function handleDecided(outcome: DecisionOutcome) {
    setBanner({ text: outcome.message, warn: !outcome.resumed })
    refetchTicket()
    refetchApproval()
  }

  function handleTurnDone() {
    refetchTicket()
  }

  function handleNeedsApprovalRefetch() {
    refetchTicket()
    refetchApproval()
  }

  if (!id) return null

  if (ticket.isError) {
    return (
      <div className={`${styles.root} kc-content kc-view`}>
        <Link to="/inbox" className={styles.back}>
          ← INBOX
        </Link>
        <EmptyState icon={ScrollText} title="Could not load this ticket" message={(ticket.error as Error).message} />
      </div>
    )
  }

  if (!ticket.data) {
    return (
      <div className={`${styles.root} kc-content kc-view`}>
        <Link to="/inbox" className={styles.back}>
          ← INBOX
        </Link>
      </div>
    )
  }

  const t = ticket.data
  const status = ticketStatusChip(t)
  const createdAgeSeconds = (Date.now() - Date.parse(t.created_at)) / 1000
  const requesterLabel =
    t.requester_user_id === null
      ? 'an alert'
      : actorLabel(`user:${t.requester_user_id}`, directory.data?.users)
  const requesterDisplayName = t.requester_user_id === null ? undefined : requesterLabel.toLowerCase()

  return (
    <div className={`${styles.root} kc-content kc-view`}>
      <Link to="/inbox" className={styles.back}>
        ← INBOX
      </Link>
      <div className={styles.headRow}>
        <h1 className={`kc-h1 ${styles.title}`}>
          Ticket #{t.number}
        </h1>
        <span className={styles.statusChip} style={{ color: status.color }}>
          {status.label}
        </span>
        {t.resolved_by === 'triage' && (
          // Said in the header, not only on the timeline where it scrolls: a
          // ticket nobody looked at was still decided by something, and the
          // reader has to know which before they read anything else. The
          // reopen button below is the ordinary `resolved -> in_progress`
          // affordance — no special case needed to disagree with it.
          <span className={styles.autoChip} title="Resolved by an unprompted investigation">
            RESOLVED BY KENNY
          </span>
        )}
      </div>
      <div className={styles.meta}>
        {t.agent_id ?? 'no host yet'} · opened {formatAge(createdAgeSeconds)} ago by {requesterLabel} via {t.origin} ·{' '}
        {t.priority} priority
      </div>

      {banner && (
        <div className={`${styles.banner}${banner.warn ? ` ${styles.bannerWarn}` : ''}`}>
          <span>{banner.text}</span>
          <button type="button" className={styles.bannerDismiss} onClick={() => setBanner(null)}>
            DISMISS
          </button>
        </div>
      )}

      <div className={styles.fields}>
        <InlineEditField
          label="TITLE"
          value={t.title}
          saving={patch.isPending}
          onSave={(value) => patch.mutate({ title: value })}
        />
        <InlineEditField
          label="PRIORITY"
          value={t.priority}
          displayValue={t.priority.toUpperCase()}
          options={vocabulary.data?.priorities}
          saving={patch.isPending}
          onSave={(value) => patch.mutate({ priority: value })}
        />
        <InlineEditField
          label="CATEGORY"
          value={t.category ?? ''}
          displayValue={t.category ?? 'uncategorised'}
          options={vocabulary.data ? ['', ...vocabulary.data.categories] : undefined}
          saving={patch.isPending}
          onSave={(value) => patch.mutate({ category: value || null })}
        />
      </div>

      <TicketActions
        ticket={t}
        isOperator={isOperator}
        meUserId={Number.isFinite(meUserId) ? meUserId : null}
        fleetAgents={fleet.data?.agents ?? []}
        onMutated={refetchTicket}
      />

      {events.data && (
        <div className={styles.sectionGap}>
          <Timeline
            events={events.data.events}
            directory={directory.data?.users}
            agentId={t.agent_id}
          />
        </div>
      )}

      {openApproval && (
        <div className={styles.gateWrap}>
          <ApprovalGate
            approvalId={openApproval.id}
            tool={openApproval.tool}
            args={openApproval.args}
            agentId={openApproval.agent_id}
            toolClass={openApproval.tool_class}
            onDecided={handleDecided}
          />
        </div>
      )}

      <TicketChat
        ticketId={id}
        discordThread={!!t.discord_thread}
        assistantAvailable={t.assistant_available}
        blockedOnApproval={isBlockedOnApproval}
        openApproval={openApproval}
        onNeedsApprovalRefetch={handleNeedsApprovalRefetch}
        onTurnDone={handleTurnDone}
        onDecided={handleDecided}
      />

      {isOperator && <NoteComposer ticketId={id} requesterLabel={requesterDisplayName} onPosted={refetchTicket} />}
    </div>
  )
}

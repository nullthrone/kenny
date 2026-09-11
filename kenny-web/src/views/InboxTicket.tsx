import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { FleetResponse, Me } from '../api/types'
import EmptyState from '../components/EmptyState/EmptyState'
import { ScrollText } from '../components/icons'
import ApprovalGate, { type DecisionOutcome } from './ticket/ApprovalGate'
import AuditTrail from './ticket/AuditTrail'
import InlineEditField from './ticket/InlineEditField'
import LinkedAlerts, { type TicketAlertsResponse } from './ticket/LinkedAlerts'
import NoteComposer from './ticket/NoteComposer'
import TicketActions from './ticket/TicketActions'
import Timeline from './ticket/Timeline'
import { formatAge } from './inbox/age'
import { actorLabel } from './ticket/eventFormat'
import { chatStore } from '../chat/chatStore'
import { ASK_KENNY_OPEN_EVENT } from './host/askKenny'
import { TICKET_TURN_EVENT } from '../chat/ticketTurn'
import {
  ticketAlertsKey,
  ticketApprovalKey,
  ticketEventsKey,
  ticketKey,
  ticketTimelineKey,
} from './ticket/queries'
import { ticketStatusChip } from './ticket/statusChip'
import type {
  DirectoryUser,
  Ticket,
  TicketEvent,
  TicketVocabulary,
  TicketApproval,
  TimelineEntry,
} from './ticket/types'
import styles from './ticket/InboxTicket.module.css'

interface ApprovalsListResponse {
  approvals: TicketApproval[]
}
interface DirectoryResponse {
  users: DirectoryUser[]
}

/**
 * `#/inbox/ticket/:id` — status, origin, the alerts this ticket is about and
 * whether they still hold, what happened on it, the gate, every lifecycle
 * action, and a note field.
 *
 * What happened is shown twice over, and the difference is the point.
 * *Analysis* is the ticket read as a story: findings, what people said, what
 * kenny changed — each already a sentence, composed by the server
 * (`ticket_timeline.py`). *Audit* is the trail itself, every row with the
 * arguments a call ran with (ADR-0046). Neither is a permission boundary; the
 * server applies the same ownership check to both.
 *
 * Talking to kenny about the ticket happens in the Ask kenny drawer, which
 * binds to this ticket while the page is open (`chatStore.openForTicket`) and
 * runs under the ticket's own gate (ADR-0050). The only composer here is the
 * note, because a note is a thing you write *onto* a ticket rather than a
 * conversation you have about it.
 *
 * This is the only surface that offers an approval decision: the queue shows
 * that a ticket waits for one, and the frozen call it would run is here
 * (ADR-0059).
 */
export default function InboxTicket() {
  const { id } = useParams<{ id: string }>()
  const queryClient = useQueryClient()
  const [banner, setBanner] = useState<{ text: string; warn: boolean } | null>(null)
  const [tab, setTab] = useState<'analysis' | 'audit'>('analysis')

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

  const alerts = useQuery({
    queryKey: id ? ticketAlertsKey(id) : ['ticket', 'missing', 'alerts'],
    queryFn: () => api.get<TicketAlertsResponse>(`/api/tickets/${id}/alerts`),
    enabled: !!id,
  })

  const timeline = useQuery({
    queryKey: id ? ticketTimelineKey(id) : ['ticket', 'missing', 'timeline'],
    queryFn: () => api.get<{ entries: TimelineEntry[] }>(`/api/tickets/${id}/timeline`),
    enabled: !!id,
  })

  // The raw trail is fetched only when somebody asks for it: it is the larger
  // of the two and the one nobody reads by default.
  const events = useQuery({
    queryKey: id ? ticketEventsKey(id) : ['ticket', 'missing', 'events'],
    queryFn: () => api.get<{ events: TicketEvent[] }>(`/api/tickets/${id}/events`),
    enabled: !!id && tab === 'audit',
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
    void queryClient.invalidateQueries({ queryKey: ticketTimelineKey(id) })
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

  const loaded = ticket.data

  // Hand the drawer everything it needs to be *this ticket's* chat: which
  // endpoint, which host the turn is frozen to, whether there is a thread to
  // mirror into, and whether a decision is outstanding. Re-bound on every
  // change so the drawer is never working from a stale answer to the last
  // question; binding the same ticket again keeps a live transcript.
  useEffect(() => {
    if (!loaded) return
    chatStore.openForTicket({
      id: loaded.id,
      number: loaded.number,
      agentId: loaded.agent_id ?? '',
      discordThread: !!loaded.discord_thread,
      assistantAvailable: loaded.assistant_available,
      blockedOnApproval: isBlockedOnApproval,
    })
  }, [loaded, isBlockedOnApproval])

  // A turn run from the drawer writes to this ticket's trail as it goes. The
  // drawer cannot reach this page's query client, so it says which ticket
  // moved and the page reads the durable record back — the transcript in the
  // drawer is the live view, the timeline below is the record.
  useEffect(() => {
    function onTurn(event: Event) {
      const detail = (event as CustomEvent<{ ticketId: string }>).detail
      if (!id || detail?.ticketId !== id) return
      refetchTicket()
      refetchApproval()
    }
    window.addEventListener(TICKET_TURN_EVENT, onTurn)
    return () => window.removeEventListener(TICKET_TURN_EVENT, onTurn)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id])

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

      {alerts.data && <LinkedAlerts data={alerts.data} />}

      <div className={styles.sectionGap}>
        <div className={styles.tabs} role="tablist" aria-label="What happened on this ticket">
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'analysis'}
            className={`${styles.tab}${tab === 'analysis' ? ` ${styles.tabActive}` : ''}`}
            onClick={() => setTab('analysis')}
          >
            ANALYSIS
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'audit'}
            className={`${styles.tab}${tab === 'audit' ? ` ${styles.tabActive}` : ''}`}
            onClick={() => setTab('audit')}
          >
            AUDIT
          </button>
          {tab === 'audit' && (
            <span className={styles.tabNote}>every row, with the arguments it ran with</span>
          )}
        </div>
        {tab === 'analysis'
          ? timeline.data && (
              <Timeline
                entries={timeline.data.entries}
                directory={directory.data?.users}
                agentId={t.agent_id}
              />
            )
          : events.data && <AuditTrail events={events.data.events} directory={directory.data?.users} />}
      </div>

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

      {t.assistant_available && (
        // The conversation itself is in the drawer, already bound to this
        // ticket by the effect above — this only opens it, so that "talk to
        // kenny about this" stays one click from the ticket it is about.
        <button
          type="button"
          className={styles.askButton}
          onClick={() => window.dispatchEvent(new CustomEvent(ASK_KENNY_OPEN_EVENT))}
        >
          ASK KENNY ABOUT THIS TICKET
        </button>
      )}

      {isOperator && <NoteComposer ticketId={id} requesterLabel={requesterDisplayName} onPosted={refetchTicket} />}
    </div>
  )
}

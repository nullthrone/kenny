# 0027. Persistent, resumable copilot chat history

- Status: accepted
- Amended by: [ADR-0050](0050-ticket-as-entity-chat-thread-as-binding.md)
- Date: 2026-07-02

## Context and Problem Statement

ADR-0009 introduced the server-hosted Claude chat: the operator talks to Claude directly
in the web dashboard, and `chat.py` drives an Anthropic tool-use loop server-side. That
ADR explicitly accepted an in-memory-only session store as a stated tradeoff: "chat
session state is in-memory (dev-grade), lost on restart — acceptable now."

That tradeoff is no longer acceptable. Operators expect a chat to survive a server
restart, and there is no way to see or return to a past conversation — closing the tab
(or a redeploy) silently discards the whole exchange, including any tool results Claude
already gathered. This ADR reverses that specific consequence of ADR-0009.

## Considered Options

- Keep sessions in-memory only (status quo; rejected — the problem this ADR exists to fix).
- Persist one JSON/flat file per conversation on disk. Rejected: no query/listing
  support without scanning the directory, no WAL-style concurrency story, and it
  diverges from every other kenny store, which all live in `kenny.sqlite`.
- Persist conversations in SQLite via a new store in `store.py`, following the existing
  multi-store pattern (`TelemetryStore`, `PolicyStore`, `WebFilterStore`: own
  connection, WAL, own schema, sharing the one DB file). Chosen.

## Decision Outcome

Chosen option: a new `ChatHistoryStore` (`kenny-server/kenny_server/store.py`) persists
one row per conversation — id, a title, the last-used agent context, and the *raw*
Anthropic `messages` list (the exact shape `ChatSession.messages` already uses,
including `tool_use`/`tool_result` blocks and any base64 screenshot data) as a JSON
blob. `ChatSessions` (`chat.py`) keeps its in-memory dict as a fast path for the
lifetime of one process, but now falls back to the store on a miss and rehydrates a
`ChatSession` — SQLite is the source of truth, the dict is just an accelerator a
restart trivially discards.

A conversation is persisted only once a turn has *settled* (`done`, or freshly paused on
a confirm-gate) — never mid-turn. Transient state (`pending`, `_staged_results`,
`_queue`) is process-local and is never written to the store; `heal_session()`, which
already repairs a session left mid-turn by an aborted stream, is reused unchanged as the
safety net for a conversation loaded back from a crash-interrupted save. The two SSE
routes (`/api/chat/stream`, `/api/chat/confirm/stream`) persist only after their event
generator fully drains, not per event, so an operator-triggered Stop simply skips that
turn's save instead of writing something inconsistent.

The dashboard gets two small additions next to the existing chat composer (not a new
top-level tab, since this is a feature of the chat, not a separate dashboard area): a
"new" button that starts a fresh, unsaved session, and a "history" button that opens a
list of past conversations (title, last-used time, last-used agent) reusing the existing
modal component. Selecting one replays it into the transcript via a new
`public_transcript()` helper that flattens the raw Anthropic blocks into the same
event shapes the live SSE stream already emits, so the frontend reuses its existing
event renderer instead of a second parser. Rows can be deleted individually
(`DELETE /api/chat/history/{id}`); nothing is deleted automatically.

**Retention is explicitly unlimited, manual-delete only, and screenshots are stored at
full base64 fidelity — a deliberate operator decision, not an oversight.** This is a
deviation from the auto-pruned pattern used by `TelemetryStore`/`EventStore`/
`WebFilterStore` (`RETENTION_DAYS`, `prune()`); chat history is operator-curated content
the operator explicitly chose to keep, the same rationale `PolicyStore`'s
append-until-explicitly-removed rules already rely on, not ambient high-volume
telemetry. The direct consequence: conversations that used `screen_capture` can be
large, and `ChatHistoryStore` is the store most likely to dominate `kenny.sqlite`'s size
over time. This is accepted now, the same way ADR-0009 accepted in-memory-only as
"acceptable now" — revisitable later (e.g. an opt-in prune) if it becomes a problem in
practice.

### Consequences

- Good, because chat conversations survive a server restart and the operator can browse
  and resume any of them with full context, including prior tool results and
  screenshots.
- Good, because it reuses the existing `store.py` multi-store/WAL pattern — no new
  infrastructure, no new dependency.
- Good, because persisting only after a turn settles (never mid-turn) means the existing
  `heal_session()` abort-recovery logic doubles as the DB-rehydration safety net, with no
  new repair logic needed.
- Bad, because retention is unbounded and screenshots are stored at full fidelity:
  `kenny.sqlite` can grow indefinitely if the operator never deletes old conversations.
  The only mitigation is the manual delete button; there is no automatic cap.
- Bad, because `ChatSessions.get()` becomes `async` (it may now hit the store). Both
  existing call sites were already inside `async def` route handlers, so this is a
  small, contained signature change.

## More Information

- New endpoints: `GET /api/chat/history` (list, summaries only — no message bodies),
  `GET /api/chat/history/{id}` (one conversation's replayable transcript), `DELETE
  /api/chat/history/{id}`. All inherit operator auth from the existing
  `OperatorAuthMiddleware` on `/api/*` — no new auth surface.
- Related: ADR-0009 (server-hosted chat + confirm-gate — this ADR reverses its
  in-memory-only consequence), ADR-0024 (untrusted tool-result data in the chat
  loop — unaffected; the same `_MAX_TOOL_RESULT_CHARS` cap on text tool results still
  applies before anything is persisted).
- Code: `kenny-server/kenny_server/store.py` (`ChatHistoryStore`),
  `kenny-server/kenny_server/chat.py` (`ChatSession.title`/`agent_id`,
  `ChatSessions.get`/`forget`, `persist_session`, `public_transcript`),
  `kenny-server/kenny_server/webui/__init__.py` (history routes, persist call sites),
  `kenny-server/kenny_server/webui/index.html` (history panel, `openHistoryPanel`,
  `loadConversation`, `deleteConversation`, `newChat`).

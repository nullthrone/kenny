# 0051. Serialize this process's SQLite writers behind an in-process lock

- Status: accepted
- Date: 2026-08-05
- Amends: [ADR-0007](0007-telemetry-push-model-and-sqlite-storage.md)
- Touches: [ADR-0039](0039-server-database-backup-and-restore.md)

## Context and Problem Statement

`POST /api/agent/{id}/refresh` was intermittently returning HTTP 500 with
`sqlite3.OperationalError: database is locked`, raised out of `TelemetryStore.insert`. The
failures were spread evenly across normal runtime — not clustered around the scheduled
backup's `VACUUM INTO` — so this was steady-state writer-vs-writer contention, not one
long-held lock.

Two initially plausible mechanisms turned out not to fit the evidence:

- **"`busy_timeout` isn't set."** It is: `_configure_connection` sets it on every store that
  calls it, `TelemetryStore` is one of them, and the deployment's
  `KENNY_SQLITE_BUSY_TIMEOUT_MS=20000` was confirmed live in the running container. Twenty
  seconds of waiting still wasn't enough.
- **"A read-then-write upgrade bypasses the busy handler."** This codebase's `sqlite3`
  connections run under the legacy `isolation_level=""`, which opens an implicit transaction
  only before the first DML statement, never before a `SELECT`. No production path issues an
  explicit `BEGIN`, so there is no read snapshot to upgrade out of.

What the evidence does fit: five stores — `SettingsStore`, `AgentTokenStore`, `KeyStore`,
`UserStore`, `OAuthStore` — never called `_configure_connection` and so ran with SQLite's
default `busy_timeout` of **0**. Each has a multi-statement write (e.g.
`OAuthStore.issue_token_pair`, two `INSERT`s) with **no `rollback` anywhere in any of the
four external-store modules**. If the second statement in such a sequence hit contention, it
raised immediately, Python did not roll back, and that connection kept holding the WAL
writer lock — for as long as until its *next* `commit()`, which could be arbitrarily far in
the future. Every other writer in the process then burned its full configured timeout
against a lock nobody was going to release soon, and raised anyway. `synchronous=FULL`
(an fsync per commit, against ~90 KB telemetry blobs across roughly sixteen long-lived
connections) widened the window further.

Fixing the missing-pragma gap (tracked alongside this ADR, not part of it) removes the
*trigger*. It does not remove the underlying fact this ADR is about: SQLite allows exactly
one writer at a time, `busy_timeout` only bounds how long a second writer waits for that one
slot, and this server runs roughly a dozen independent writers — one connection per store,
plus the alert engine, the log drain, the ticket sweep — all contending for it.

## Considered Options

- **Do nothing beyond fixing the missing pragmas.** Closes the specific trigger observed this
  time, but leaves the same blast radius for the next writer that manages to hold the lock
  longer than another connection's timeout — a coding mistake in any future store method, an
  unusually large batch, a slow disk. Rejected as insufficient on its own.
- **A single shared `aiosqlite` connection for the whole process**, replacing one connection
  per store. Removes writer-vs-writer contention by construction, but `aiosqlite` serializes
  *all* work on a connection's dedicated worker thread — reads would queue behind writes too,
  and `/api/fleet/overview` is already an N+1 read pattern. Rejected: trades a writer problem
  for a broader throughput regression.
- **Retry/backoff wrapper around `OperationalError`.** Retrying a write into contention it
  just lost adds more contention, not less, and does nothing about a connection stuck holding
  the lock. Rejected.
- **An in-process `asyncio.Lock` that this server's own writers acquire before touching the
  database, on top of the pragma fix.** Chosen.

## Decision Outcome

Chosen option: **a process-wide async write lock**, `store.write_lock()`, acquired inside
each store's individual write methods (never around a loop, a scheduled sweep, or an
`await` that reaches the agent tunnel, an LLM call, or a caller-supplied callback).

This does not change what SQLite itself allows — it still permits one writer at a time. What
it changes is *where contention is resolved*. Before: N connections raced for SQLite's one
write slot, and the loser's fate was decided by its `busy_timeout` against however long the
winner happened to hold the lock. After: this process's own writers queue on an in-process
lock first, so only one of them is ever contending for SQLite's write slot at a time. A
writer outside this process (the `sqlite3` CLI, a future second worker) is not covered by
this lock — `busy_timeout` remains the backstop for that case, which is why the pragma fix
ships alongside this, not instead of it.

Two correctness properties the implementation has to hold, both because a naive
`asyncio.Lock` gets them wrong:

- **Re-entrant, scoped to the acquiring task.** `ticketstore._insert_event` writes on its
  caller's transaction and is called *inside* five other write methods
  (`set_state`, `set_agent_id`, `set_blocked`, `set_assignee`, `mark_nudged`, plus
  `append_event`). A plain `asyncio.Lock` taken at both levels self-deadlocks. Re-entrancy is
  keyed on `asyncio.current_task()` identity, not a `ContextVar` — a task spawned *while* the
  lock is held inherits the parent's `ContextVar` state, and a flag-based approach would let
  that child believe it already holds the lock and write unserialized.
- **Scoped to the event loop, not the process.** `asyncio.Lock` binds to whichever loop first
  calls `acquire()` and raises if a later `acquire()` runs on a different one. Lock state
  lives in a `WeakKeyDictionary` keyed by the running loop rather than a single module-level
  lock, so this holds under `pytest-asyncio` (a fresh loop per test) as much as under the one
  long-running loop uvicorn drives in production.

`PRAGMA synchronous=NORMAL` ships in the same change, for the same underlying reason: it
shortens every write transaction (no fsync on commit) in exchange for a bounded, named risk —
in WAL mode the database cannot be corrupted by this setting; only the most recent commit(s)
can be lost, and only on an OS/host crash, never a process crash. [ADR-0039](0039-server-database-backup-and-restore.md)'s
backups are the durability net for that gap. `BEGIN IMMEDIATE` is added to the remaining
multi-statement writers (`WebFilterStore.upsert_events`, `TicketStore.delete`,
`TicketStore.prune`) as defense in depth, not as this ADR's mechanism: it takes the writer
lock as a transaction's first act rather than partway through, and protects against a writer
outside this process that the in-process lock cannot see.

### Rule that keeps this deadlock-free

The lock is taken only *inside* individual store methods, immediately around the statements
it protects. It is never held across a loop iteration, a scheduled sweep's full run, or any
`await` that leaves the store — the agent tunnel, an LLM call, or a caller-supplied callback
(e.g. a ticket gate resumer). Held across a loop, a large prune ties up every other writer in
the process for the loop's whole duration; `TelemetryStore.prune` is therefore chunked
(500 rows per transaction, yielding between chunks) rather than one unbounded `DELETE`. Held
across non-DB I/O, a slow tunnel round-trip or model call would stall unrelated writers for
that I/O's duration — the opposite of what this lock is for.

### Consequences

- Good: a writer contending for this process's SQLite write slot always yields to an
  in-process peer within that peer's own transaction, not within an unrelated connection's
  possibly-much-longer hold. The specific reported failure — a refresh 500ing on
  `database is locked` — stops.
- Good: the fix is local to `store.py`/`ticketstore.py`, one method at a time; every lock
  acquisition can be reverted independently without touching call sites elsewhere.
- Bad: writes that were previously concurrent (in the sense of racing SQLite's single writer
  slot independently) now explicitly queue in this process. This is not a new constraint —
  SQLite already allowed only one writer — but it is now visible and orderable within the
  process rather than left to the busy handler's retry loop.
- Bad: `synchronous=NORMAL` accepts loss of the most recent commit(s) on an OS/host crash.
  Judged acceptable for telemetry/event/ticket data with existing periodic backups
  ([ADR-0039](0039-server-database-backup-and-restore.md)), not acceptable to apply silently
  to a store carrying data where that loss window matters more — none currently do.
- Neutral: a writer outside this process is still bounded only by `busy_timeout`, unchanged
  from before this ADR.

## More Information

Regression coverage lives in `tests/test_store_pragmas.py` (`write_lock` exclusivity,
re-entrancy, cross-event-loop safety, and a reproduction of the original contention scenario)
and `tests/test_ticketstore.py` (the `_insert_event` nesting specifically, under a hard
timeout so a reintroduced deadlock fails fast instead of hanging the suite).

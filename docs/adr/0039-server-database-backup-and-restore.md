# 0039. Server database backup/restore with pluggable local + remote destinations

- Status: accepted
- Date: 2026-07-22

## Context and Problem Statement

`kenny-server` persists everything — telemetry, chat history (including embedded
screenshots), policy/webfilter state, users, keys, tokens — in one WAL-mode SQLite file
(ADR-0007, ADR-0025) on the `/data` volume (ADR-0010). The operator syncs that file
off-box with Syncthing for offsite durability. Syncing the *live* file caused lock
contention: Syncthing watches and hashes the file (and its `-wal`/`-shm` siblings) while
the server holds open connections across ~11 stores, producing `database is locked` 500s
on ordinary dashboard refreshes and tunnel flapping under load.

The DB still has to leave the box somehow — "stop syncing it" is not an option, only
"stop syncing the *live* file." The fix is to make backups the sync artifact: produce
consistent, static snapshot files on a schedule, and point Syncthing (or any sync tool)
at the directory holding them instead of at `kenny.sqlite` itself. The operator also
needs a way to trigger/inspect/configure that process and to restore from it, without
editing files on the host by hand.

This ADR does not touch `docs/protocol.md`/`docs/fixtures/` or `PROTOCOL_VERSION` — the
agent⇄server wire contract is unaffected; everything here is server-local.

## Considered Options

- **Keep syncing the live file.** Rejected — this is the bug being fixed.
- **Continuous WAL streaming (Litestream-style).** Rejected for v1: a bigger new
  dependency and operational surface (a shipping/replay process, remote storage
  credentials at a different layer) than periodic `VACUUM INTO` snapshots justify for a
  family-scale, single-operator deployment. Worth revisiting if point-in-time recovery
  granularity ever matters more than it does today.
- **A single fixed backup destination** (e.g. always local, or always one configured
  remote). Rejected — the whole point is operator choice: local-only is the minimum that
  fixes the Syncthing problem, but some operators will also want push-based offsite
  copies (HTTP/SCP/FTP) without relying on a third-party sync tool at all.
- **Periodic `VACUUM INTO` snapshots to a local directory (always-on) + pluggable
  operator-configured remote fan-out via a uniform destination interface.** Chosen.

## Decision Outcome

Chosen option: a `BackupManager` (`kenny-server/kenny_server/backup.py`) creates a
consistent snapshot via SQLite's `VACUUM INTO` on a timer (default 6h, configurable) and
on demand, writes it into a local `backups/` directory next to the DB, and fans it out to
every enabled operator-configured remote target. The local directory is the one that
solves the original problem — it *only ever* contains finished, static files, so pointing
Syncthing there instead of at `kenny.sqlite` eliminates the lock contention entirely, with
or without any remote target configured.

Remote targets are optional and pluggable through one uniform interface,
`BackupDestination` (`backup_targets.py`), implemented for **HTTP** (push to a simple
API), **SCP/SFTP** (via `asyncssh`), and **FTP/FTPS** (via `ftplib`), alongside the
always-active `LocalDestination`. Every implementation supports the full round trip —
`store`/`list`/`retrieve`/`delete`/`prune`/`test` — so listing, verifying, downloading,
and restoring all work uniformly regardless of where a given backup lives. Target
configuration (host, credentials, remote directory, …) is operator-managed through a new
`BackupTargetStore` (`store.py`) and a dashboard "Backup" page (superuser-only), the same
`op`/`su` role gating used for Settings (ADR-0033).

Restore is **apply-on-next-boot**, not a live file swap: ~11 stores hold open `aiosqlite`
connections to `kenny.sqlite` for the life of the process, so nothing can safely replace
the file while the server is running. `stage_restore()` retrieves and integrity-checks
(`PRAGMA quick_check`) the chosen backup, then writes it next to the DB behind a marker
file. `apply_pending_restore()` is a free, synchronous function called at the very start
of `main.py`'s `lifespan` — before any store opens a connection — that swaps the staged
file into place if a marker is present. The dashboard's restore action stages the file,
records an audit event, and then self-terminates the process (`SIGTERM`, after a short
delay to flush the HTTP response); the container's `restart: unless-stopped` policy
(ADR-0010) brings it back up, applying the restore on that next boot.

### New trust boundary

This is the one part of this decision worth naming explicitly rather than leaving
implicit: the server now **initiates outbound connections** to operator-configured
destinations and **carries full copies of the database** — including chat history,
tokens, and other secrets — to them. Destination credentials (SFTP password/private key,
FTP password, HTTP bearer token) are stored in the same `kenny.sqlite`, in cleartext,
consistent with how the server already stores every other secret (`AgentTokenStore`,
`KeyStore`, `OAuthStore`). The dashboard API masks these values in every response (never
echoes a secret, only reports whether one is set) and a PUT with an empty secret field
means "leave unchanged," but the values are not encrypted at rest. This is accepted as
consistent with the project's existing storage model, not a gap unique to this feature —
but an operator configuring a remote target should understand that both the DB copies in
flight and the credentials to reach them carry the same sensitivity as the live database.

### Known limitation

`ScpDestination` connects with `known_hosts=None` — no host-key pinning or
trust-on-first-use. This is a deliberate v1 simplification for a small, typically
trusted-network deployment, not an oversight; it is called out with a `# TODO(security)`
at the point of use in `backup_targets.py` and should be revisited before SCP/SFTP is
treated as more than a best-effort offsite copy over a network the operator already
trusts.

### Consequences

- Good, because pointing Syncthing at `backups/` instead of `kenny.sqlite` removes the
  lock-contention bug entirely, with zero remote configuration required.
- Good, because the uniform `BackupDestination` interface means list/verify/download/
  restore behave identically regardless of which target a backup came from, and adding a
  new destination kind later is a single new class, not a parallel API.
- Good, because restore-on-boot needs no special-casing of "which stores are open" — it
  runs before any store exists, sidestepping the ~11-open-connection problem completely.
- Bad / accepted, because the server now makes outbound network calls and carries
  sensitive data to operator-configured endpoints — a materially larger blast radius than
  "reads its own database," and destination credentials sit in cleartext like the
  project's other secrets.
- Bad / accepted, because SCP/SFTP has no host-key verification in this iteration
  (documented above).
- Bad / accepted, because there is no alerting yet if scheduled backups silently stop
  succeeding — see Follow-ups.

## More Information

- Code: `kenny-server/kenny_server/backup.py` (`BackupManager`, `apply_pending_restore`),
  `kenny-server/kenny_server/backup_targets.py` (`BackupDestination` + Local/Http/Scp/Ftp),
  `store.py`'s `BackupTargetStore`, the `"Backup"` settings group in `config.py`
  (`KENNY_BACKUP_INTERVAL_SECS`/`_INITIAL_DELAY`/`_RETENTION`/`_DIR`), and the dashboard's
  `/api/backups*` + `/api/backup-targets*` routes and Backup page in `webui/`.
- Related: ADR-0007 (SQLite storage rationale this backs up), ADR-0010 (`/data` volume and
  the container restart policy the boot-time restore relies on), ADR-0025 (the multi-store
  single-file pattern — chat history, with embedded screenshots, is typically the largest
  and most sensitive part of what a backup carries), ADR-0032 (the runtime-settings pattern
  used for the interval/retention knobs), ADR-0033 (the superuser role gate reused for the
  Backup page).
- **Disambiguation:** this ADR is unrelated to ADR-0028's `backup_status` telemetry
  section, which reports evidence of the *monitored PC's own* backup posture (Windows
  System Restore/File History/OneDrive) as observed by the agent — a read-only fleet
  health signal, not the server's own backup mechanism described here.
- **This feature does not touch the wire contract.** No frame or tool shape in
  `docs/protocol.md`/`docs/fixtures/` changes; nothing here requires agent changes or a
  `PROTOCOL_VERSION` bump.
- Follow-up (not built in this phase): a "backup overdue" / "last backup push failed"
  alert over the existing push-alerting channel (ADR-0027) once there is a natural signal
  to key it off (e.g. no successful `create()` within N× the configured interval).

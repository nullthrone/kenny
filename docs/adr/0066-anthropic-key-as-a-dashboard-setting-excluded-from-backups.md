# 0066. The Anthropic key is a dashboard setting, kept out of backups

- Status: accepted
- Boundary moved: **the storage and trust model for secrets.** The Anthropic API key moves
  out of the process environment into `kenny.sqlite`, where a superuser can set it from the
  dashboard, and it becomes the first stored value that is deliberately excluded from
  every backup copy. Every AI feature also gets its own live switch behind one access point.
- Amends: [ADR-0032](0032-runtime-settings-in-the-dashboard.md),
  [ADR-0054](0054-alert-channels-as-live-settings.md)
- Date: 2026-09-23

## Context and Problem Statement

kenny runs six AI features: Ask kenny, the section recommendation, the forecast prose,
reliability-event classification, the ticket assistant (dashboard and Discord) and triage.
They all depended on `ANTHROPIC_API_KEY` in the environment. Only triage could be switched
off, and each feature resolved the key its own way: a per-call `os.environ` read, a client
built once at startup, a process-lifetime `lru_cache`. Rotating the key or pausing a
feature meant shell access and a restart. The operator of a family-scale deployment runs it
from the dashboard (ADR-0032).

ADR-0032 and ADR-0054 kept this key `env_only` on purpose. It is billed per use, and every
database backup travels to remote targets that may be plain FTP or SCP (ADR-0039). So the
question is not just "make it editable". It is where a GUI-set key may live without ending
up on a backup server.

## Considered Options

- **Keep it env-only.** Leaves the problem as it is.
- **Store it in `kenny.sqlite` in cleartext, like the alert channels (ADR-0054).** Simple,
  but the key would then sit in every local and remote backup.
- **Store it in a separate secrets file** on the data volume, outside the database. It stays
  out of backups, but adds a second persistence mechanism beside SQLite, with its own
  permissions and its own restore story.
- **Store it in the settings table and scrub it from every backup copy.** Chosen.

## Decision Outcome

`ANTHROPIC_API_KEY` is a `live`, `sensitive` setting in the AI group. A value saved in the
dashboard wins over the environment (ADR-0032's DB > env > default). Like every
`sensitive` setting it is never serialised back out.

A catalog flag, `backup_excluded`, marks settings that must not leave the live database.
`BackupManager.create` deletes those rows from the staged `VACUUM INTO` copy and vacuums
the copy again before it is hashed, checked or pushed to any target. The live database
keeps the key. After a restore the key is unset, so the environment value applies if there
is one; the dashboard says so next to the key.

`kenny_server.ai` is the single access point:

- **Key:** it resolves the key through `Settings`.
- **Client:** it builds the Anthropic client for the key currently in force and rebuilds it
  when the key changes.
- **Feature switches:** it answers `enabled(feature)` from one live switch per feature.
  The switches are `KENNY_AI_ASK_ENABLED`, `_RECOMMEND_`, `_FORECAST_`, `_CLASSIFY_`,
  `_TICKET_ASSISTANT_ENABLED`, and `KENNY_TRIAGE_ENABLED` for triage.
- **Rule:** a feature runs only when a key is set **and** its switch is on.
- **Binding:** the composition root binds one `AiAccess` for the whole process (one
  process, one `Settings`). Module-level callers such as the classifier and the MCP
  `agent_health` path read it through `ai.current()`.

`GET /api/ai/status` tells the dashboard what to offer, so a switched-off feature shows no
control that would answer 503. `POST /api/ai/test` checks the key with the cheapest API
call.

The dashboard's settings list now contains only what it can write. The read-only view onto
`env_only` values that ADR-0032 described is gone; those stay in the environment and in
`docs/setup.md`.

### Consequences

- Good, because a superuser can set, rotate or clear the key and switch any AI feature off
  from the dashboard, and the next call honours it. There is no restart.
- Good, because no backup copy, local or remote, ever holds the key. ADR-0054's channel
  secrets still do, as that record accepted.
- Bad, because the key sits in cleartext in the live `kenny.sqlite`. Anyone with read
  access to that file can read the key. That is the same access that already reveals the
  server's Ed25519 seed and the TOTP secrets, so the key adds no new exposure class.
- Bad, because a restore silently loses a dashboard-set key. It is announced next to the
  key and falls back to the environment, but an operator who never set one there has AI off
  until they set it again.
- Bad, because `ai.current()` is process-global state. Tests unbind it after each test
  (`tests/conftest.py`), and anything built outside `build_app` reads the environment.

## More Information

[ADR-0009](0009-server-hosted-claude-chat.md) (the copilot),
[ADR-0026](0026-llm-categorization-of-reliability-events.md) (classification),
[ADR-0056](0056-unprompted-ticket-triage.md) (triage),
[ADR-0039](0039-server-database-backup-and-restore.md) (what a backup carries).

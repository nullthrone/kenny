# Architecture Decision Records

kenny's architecture decisions in [MADR](https://adr.github.io/madr/) format, and the only
kind of record kenny keeps. What qualifies: *When (not) to write an ADR* in the repository
`CLAUDE.md`, applied by `/new-adr` before it scaffolds anything.

- Numbered `NNNN-kebab-title.md` as a gap-free `0001..N` sequence; the index is the record
  set, and a number always names the record at that position.
- `0000-template.md` is the MADR template — copy it for a new record (`/new-adr`).
- A record is immutable once accepted; later decisions **amend** or **supersede** it via a
  new record. The one exception: an accepted record may gain a dated
  `## Update (YYYY-MM-DD)` section refining *how* it is implemented. Anything that changes
  what was decided needs a new record.
- A number is an address, not content — repairing a link or renumbering is not an edit to
  a decision.

## Index

| #    | Title                                                              | Status   |
|------|--------------------------------------------------------------------|----------|
| [0001](0001-use-madr-and-record-decisions.md) | Record architecture decisions using MADR | accepted |
| [0002](0002-python-server-rust-agent.md) | Python server, Rust agent | accepted |
| [0003](0003-self-built-websocket-tunnel.md) | Self-built WebSocket/HTTPS tunnel | accepted |
| [0004](0004-agent-initiated-outbound-connection.md) | Agent initiates the outbound connection | accepted |
| [0005](0005-contract-first-with-golden-fixtures.md) | Contract-first development with golden fixtures | accepted |
| [0006](0006-mcp-streamable-http-transport.md) | MCP over Streamable HTTP between Claude and the server | accepted |
| [0007](0007-telemetry-push-model-and-sqlite-storage.md) | Telemetry push model and SQLite storage | accepted |
| [0008](0008-operator-authentication.md) | Operator authentication | superseded by [0033](0033-multi-user-authentication.md) |
| [0009](0009-server-hosted-claude-chat.md) | Server-hosted Claude chat with a confirm-gate | accepted |
| [0010](0010-containerization-and-ghcr.md) | Containerization (Docker/Compose) and GHCR | accepted |
| [0011](0011-local-remote-control-kill-switch.md) | Local remote-control kill switch (tray on/off) | accepted |
| [0012](0012-agent-distribution-prebuilt-binary.md) | Agent distribution: prebuilt binary + config injection | accepted |
| [0013](0013-agent-windows-service-and-self-update.md) | Agent as a Windows service + server-triggered self-update | accepted |
| [0014](0014-auth-hardening.md) | Auth hardening: token store, rotation, multi-operator, TLS cookie | accepted |
| [0015](0015-agent-binary-auto-fetch.md) | Server auto-fetch of the prebuilt agent binary from GitHub Releases | accepted |
| [0016](0016-anthropic-native-tool-naming.md) | Anthropic-native (underscore) capability tool names | accepted |
| [0017](0017-observability-logging-and-event-store.md) | Observability: structured logging and a persistent event store | accepted |
| [0018](0018-screenshots-captured-in-user-session-via-tray.md) | Screenshots captured in the user session via the tray helper | accepted |
| [0019](0019-agent-side-deterministic-tool-guard.md) | Agent-side deterministic safety guard for dangerous tool calls | accepted |
| [0020](0020-shared-policy-catalog-operator-rules-and-server-mirror.md) | Shared policy catalog, operator deny rules, and a server-side mirror | accepted |
| [0021](0021-remote-help-concierge-via-user-session-launch.md) | Orchestrate Windows Quick Assist as a concierge via user-session launch | accepted |
| [0022](0022-mutual-agent-auth-ed25519.md) | Mutual agent⇄server authentication via per-agent Ed25519 signatures | accepted |
| [0023](0023-untrusted-agent-data-in-chat-context.md) | Treat agent-supplied data as untrusted in the chat tool-use loop | accepted |
| [0024](0024-parental-controls-web-activity-and-webfilter.md) | Parental controls: web-activity observability and on-demand web filtering | accepted |
| [0025](0025-persistent-chat-history.md) | Persistent, resumable copilot chat history | accepted |
| [0026](0026-llm-categorization-of-reliability-events.md) | LLM categorization of reliability events | accepted |
| [0027](0027-push-alerting-ntfy-webhook-and-weekly-digest.md) | Push alerting via ntfy/webhook and a weekly digest | accepted |
| [0028](0028-security-and-resilience-telemetry-sections.md) | Security-inventory and resilience telemetry sections | accepted |
| [0029](0029-screen-time-aggregated-session-minutes.md) | Screen time as aggregated whole-machine session minutes | accepted |
| [0030](0030-agent-self-elevating-bootstrap-installer.md) | Agent self-elevating bootstrap installer + fixed install location | accepted |
| [0031](0031-linux-agent-support.md) | First-class Linux agent support | accepted |
| [0032](0032-runtime-settings-in-the-dashboard.md) | Runtime settings resolved DB-over-env, editable in the dashboard | accepted |
| [0033](0033-multi-user-authentication.md) | Multi-user authentication: accounts, roles, sessions, and per-user access tokens | accepted |
| [0034](0034-linux-agent-distribution-convenience-script.md) | Linux agent distribution: convenience install script + server-triggered self-update | accepted |
| [0035](0035-anti-cheat-coexistence-and-publisher-legitimacy.md) | Anti-cheat coexistence and publisher legitimacy | accepted |
| [0036](0036-report-cpu-arch-via-telemetry.md) | Report CPU arch via telemetry; pin arch for fresh Linux installs | accepted |
| [0037](0037-oauth2-authorization-server-for-mcp.md) | OAuth 2.1 authorization server for the MCP connector | accepted |
| [0038](0038-explicit-per-call-agent-targeting.md) | Explicit per-call agent targeting for forwarded MCP capability tools | accepted |
| [0039](0039-server-database-backup-and-restore.md) | Server database backup/restore with pluggable local + remote destinations | accepted |
| [0040](0040-scheduled-update-detection-and-operator-approved-rollout.md) | Scheduled update detection with a pinned, operator-approved rollout | accepted |
| [0041](0041-reliability-alarm-suppression.md) | Operator-managed reliability alarm suppression | accepted |
| [0042](0042-account-governance-local-and-microsoft.md) | Account governance across local and Microsoft accounts | accepted |
| [0043](0043-account-governance-on-linux.md) | Account governance on Linux | accepted |
| [0044](0044-delegated-identity-from-a-chat-platform.md) | Delegated identity from a chat platform, with no parallel authorization | accepted |
| [0045](0045-tiered-tool-classification.md) | Tiered tool classification: the tier belongs to the tool, the gate to the surface | accepted |
| [0046](0046-ticket-as-entity-chat-thread-as-binding.md) | The ticket is the entity; the chat thread is a binding | accepted |
| [0047](0047-capability-profiles.md) | Capability profiles: named per-user tool allowlists that only narrow | accepted |
| [0048](0048-second-release-channel-dev-prereleases.md) | Second release channel: `dev` prereleases via the GitHub `prerelease` flag | accepted |
| [0049](0049-ticket-blocked-on-axis.md) | The ticket lifecycle splits into two axes: state and blocked-on | accepted |
| [0050](0050-the-ticket-is-its-own-chat-surface.md) | The ticket is its own chat surface | accepted |
| [0051](0051-process-wide-sqlite-write-serialization.md) | Serialize this process's SQLite writers behind an in-process lock | accepted |
| [0052](0052-dashboard-as-a-compiled-frontend.md) | Build the dashboard as a compiled frontend shipped in the wheel | accepted |
| [0053](0053-guarded-minting-open-redemption-for-agent-distribution.md) | Guarded minting, open redemption for agent distribution | accepted |
| [0054](0054-alert-channels-as-live-settings.md) | Alert delivery channels are live settings, resolved per dispatch | accepted |
| [0055](0055-scheduled-web-filter-enforcement.md) | Scheduled web-filter enforcement: a standing rule the server enacts on a clock | accepted |

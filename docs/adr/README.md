# Architecture Decision Records

This directory holds kenny's architecture decisions in [MADR](https://adr.github.io/madr/)
format. Each record is one **architectural** decision — hard to reverse, cross-cutting, or
moving a structural boundary. For the line between "this is an ADR" and "this is an
implementation detail", see *When (not) to write an ADR* in the repository `CLAUDE.md`.

- Records are numbered sequentially (`NNNN-kebab-title.md`), starting at `0001`.
- `0000-template.md` is the MADR template — copy it for a new record (`/new-adr`).
- A record is immutable once accepted; later decisions **amend** or **supersede** it via a
  new record rather than editing the old one in place.

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
| [0008](0008-operator-authentication.md) | Operator authentication | accepted |
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
| [0019](0019-ai-recommendations-and-auto-remediation.md) | AI recommendations and tool-aware auto-remediation | accepted |
| [0020](0020-agent-side-deterministic-tool-guard.md) | Agent-side deterministic safety guard for dangerous tool calls | accepted |
| [0021](0021-shared-policy-catalog-operator-rules-and-server-mirror.md) | Shared policy catalog, operator deny rules, and a server-side mirror | accepted |
| [0022](0022-remote-help-concierge-via-user-session-launch.md) | Orchestrate Windows Quick Assist as a concierge via user-session launch | accepted |

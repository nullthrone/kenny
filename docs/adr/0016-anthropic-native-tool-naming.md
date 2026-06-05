# 0016. Anthropic-native (underscore) capability tool names

- Status: accepted
- Date: 2026-06-05

## Context and Problem Statement

Capability tools were originally named with a dotted `family.verb` convention
(`powershell.exec`, `fs.list`, `net.dns_flush`, …) used identically across the
wire contract, the MCP endpoint, and the server-hosted chat (ADR-0009). This
convention was never recorded in an ADR — it was an unwritten grouping habit.

The server-hosted chat drives an Anthropic tool-use loop. The Anthropic Messages
API requires every tool `name` to match `^[a-zA-Z0-9_-]{1,128}$` — **dots are
not allowed**. As a result `POST /api/chat` returned HTTP 502 on every turn: the
API rejected the request at the first dotted tool (`powershell.exec`) before the
loop could start (issue #12). The bug was latent since the chat feature's first
commit.

How should we make tool names valid for Anthropic while keeping the agent⇄server
contract coherent?

## Considered Options

- **A. Translate at the Anthropic boundary.** Keep dotted names as the canonical
  identifier everywhere; in chat, encode to an API-safe form (`.`→`__`) and keep
  a reverse map to translate the model's chosen name back to the dotted contract
  name on dispatch.
- **B. Rename to underscores everywhere.** Make the single-underscore form
  (`powershell_exec`) the one canonical identifier across the wire contract,
  fixtures, MCP, chat, and the Rust agent. No translation layer.

## Decision Outcome

Chosen option: **B — rename to underscore names everywhere**, because Anthropic
is the only LLM provider kenny will ever support, so there is no value in
preserving a name form the API rejects, and a single canonical identifier across
all layers is simpler than maintaining two forms plus a translation map. The
dotted convention carried no recorded rationale beyond `family.verb` grouping,
which an underscore prefix (`fs_list`, `winget_install`) preserves.

Migration is a **clean break**: no dotted aliases. kenny is an early,
fully-controlled self-hosted fleet, so the server and all agents upgrade
together. This is a breaking tool-schema change, so `PROTOCOL_VERSION` bumps
`0.2 → 0.3`.

### Consequences

- Good — chat tool schemas now satisfy the Anthropic name pattern; the 502 is
  fixed at the source rather than papered over with a mapping. A regression test
  asserts every emitted tool name matches `^[a-zA-Z0-9_-]{1,128}$`.
- Good — one name everywhere: the contract, fixtures, MCP, chat, and the agent's
  dispatch/`is_mutating` tables all use the identical identifier; no boundary
  translation to keep in sync.
- Bad — a deployed agent built before the rename dispatches on the old dotted
  strings and would reject the new names as `unknown_tool`; server and agents
  must be upgraded in lockstep. Acceptable for a small, owner-controlled fleet.

## More Information

- Issue #12 (dashboard chat 502 from dotted tool names).
- Contract: `docs/protocol.md` tool catalog and `PROTOCOL_VERSION = "0.3"`.
- Related: ADR-0009 (server-hosted Claude chat), ADR-0005 (contract-first with
  golden fixtures).

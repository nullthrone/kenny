# 0042. Explicit per-call agent targeting for forwarded MCP capability tools

- Status: accepted
- Date: 2026-07-22
- Amends: [ADR-0037](0037-multi-user-authentication.md)

## Context and Problem Statement

Remote MCP servers get no per-conversation identifier from Claude clients (Claude Desktop,
claude.ai): the `Mcp-Session-Id` header the server hands back at init is not echoed on
follow-up requests. Every kenny capability tool (`powershell_exec`, `diag_*`, `webfilter_*`,
`winget_*`, `remotehelp_*`, `screen_capture`, `net_*`, `fs_*`) forwards to whatever agent was
last set as "active" by `select_agent`, and that active-agent slot was, until now, the only
notion of "which host does this call mean."

ADR-0037 made that slot per-caller (`registry._active_by_key`, keyed by
`principal.active_key` = session id / PAT id / OAuth token id) specifically to stop
concurrent operators from clobbering each other's selection. It closes the case where two
different credentials race. It does not close the case reported here: two concurrent Claude
sessions authenticated with the **same** credential (one user, two claude.ai tabs, or two
Claude Desktop windows sharing one PAT) resolve to the **identical** `active_key`, so they
share one slot. Confirmed in practice: `select_agent("linus-pc")` in session A, then
`select_agent("bob-pc")` in session B, then a forwarded call in session A — it silently runs
on `bob-pc`. No error, no signal; the wrong host just executes the command.

This is not a bug in the keying scheme; it's a ceiling on what keying by credential can ever
guarantee, because the server has no signal that distinguishes the two conversations. Any
routing decision that depends on shared state keyed only by what the client authenticated
with is subject to the same collision.

The required outcome: a call meant for agent X always reaches X regardless of how many other
sessions are doing things concurrently, and when the target can't be resolved unambiguously
the call fails loudly rather than guessing.

## Considered Options

- **A — Require an explicit `agent_id` argument on every forwarded MCP capability tool call.**
  The server resolves the routing target from that argument alone; no target means an
  explicit error, never a fallback to shared state. `select_agent` stays as an advisory
  discovery/back-compat helper only.
- **B — Optional `agent_id` with a fallback to the existing sticky slot.** Smaller surface
  change, more backward-compatible, but a call that omits `agent_id` still falls back to the
  shared, clobberable slot — the exact collision reported here survives for any caller (or any
  model turn) that forgets to pass it. Doesn't meet the "always X, or fails loudly" bar.
- **C — A finer sticky key, or a server-issued selection token echoed on every call.** A finer
  key is a dead end: the client gives no per-conversation signal to key on, so no scheme keyed
  by credential can distinguish two sessions on that credential. A server-issued opaque token
  the model must thread through every call is no more reliable than having it thread the agent
  id it already knows (it read it from `list_agents`/`select_agent`'s result), and adds a
  token-lifecycle problem (issuance, expiry, revocation on scope change) for no extra safety —
  the scope check still has to happen at forward time either way.

## Decision Outcome

Chosen option: **A**, applied differently on the two paths that forward capability tools,
because only one of them is actually racy:

- **MCP path (`kenny_server/tools.py`, the reported bug).** `agent_id` is **required**. The
  forwarder resolves the target as *explicit `agent_id` → else fail closed*; it does not
  consult the per-principal sticky slot for routing at all. Two conversations sharing a
  credential can no longer collide, structurally — there is no shared state to race on.
  `select_agent` remains registered: it still validates an id and reports online state (useful
  for discovery), and the keyed-slot plumbing from ADR-0037 stays in the registry, but it no
  longer decides where a forwarded call lands. If a future client-side fix ever supplies a
  reliable per-conversation identifier, the safe keyed fallback can be re-enabled without
  another redesign.
- **Server-hosted dashboard chat (`kenny_server/chat.py`).** Each `ChatSession` is already a
  distinct, non-shared object — its own `agent_id` field genuinely identifies one conversation,
  unlike a credential-keyed slot. So the chat path keeps a **sticky default** (the session's
  selection) with a **per-call override**: `_resolve_chat_target` prefers an explicit
  `agent_id` in the tool call's args, falls back to `session.agent_id`, and still fails closed
  if neither is set. What changed is *where* that state lives: capability calls used to read
  `registry.require_active()` — the same **process-global** slot shared by every concurrent
  chat session (a second, independently racy instance of the same bug class, not covered by
  ADR-0037 at all, since it never went through a key). `run_capability` now takes the resolved
  target as an explicit argument, and the target is resolved and frozen once per tool call
  *before* the confirm-gate pause, so a dashboard agent switch mid-confirmation can't retarget
  an already-pending state-changing call (`PendingCall.agent_id` carries the frozen value
  through to execution).
- In both paths, `agent_id` is **routing metadata the server consumes and pops off the args
  dict before building the wire `request` frame** — the agent never sees it. The wire contract
  is unaffected: `Request` is `{type, id, tool, args}` with `extra="forbid"`
  (`kenny_server/protocol.py`), carries no agent identifier, and is routed purely by which
  WebSocket connection the server sends it down (`tunnel.send_request(agent_id, ...)` already
  took `agent_id` as a plain Python argument, never part of the frame). No `PROTOCOL_VERSION`
  bump, no fixture change — only a clarifying note in `docs/protocol.md`.
- The resolved target is **always** re-checked with the existing `_require_scope` — an
  explicit `agent_id` is unvalidated client input, and a scoped `user` principal must not be
  able to reach a host outside their assigned set just because they named it directly.

### Consequences

- Good, because the race is now structurally impossible on the MCP path rather than merely
  narrowed: a forwarded call either names its host or is rejected before it ever reaches the
  tunnel. No sticky state is ever read for routing on that path.
- Good, because the fix is local and small: one resolver in `tools.py`, one in `chat.py`, no
  changes to the wire contract, the registry's data model, or the Rust agent.
- Good, because the two genuinely-safe forwarding paths that already passed an explicit
  `agent_id` per call — every `/api/agent/{id}/...` and `/api/agents/{id}/update` dashboard
  route, and the `webfilter_push` server-only tool — needed no change at all; only the two
  paths that actually relied on shared/global state were touched.
- Bad, because this is a breaking change to the MCP tool surface: every forwarded capability
  tool call must now carry `agent_id`. Existing callers that called `select_agent` then a bare
  forwarded tool will get an explicit `no_agent` error until they're updated to pass it —
  chosen deliberately over a silent optional fallback, since the whole point is to remove the
  possibility of a silent wrong-host call. Tool descriptions were updated to make the
  requirement discoverable to the model without a prompt change.
- Neutral: the dashboard's copilot chat keeps an optional `agent_id` override (not required)
  because its sticky default is per-session and therefore safe; only its *storage* moved from
  the shared registry slot to the session object.

## More Information

- Supersedes ADR-0037's per-principal active-agent isolation as the routing mechanism for MCP
  forwarding (ADR-0037's keyed slot and `select_agent` remain, now advisory/back-compat only).
- Implementation: `kenny_server/tools.py` (`_resolve_target`, forwarder), `kenny_server/chat.py`
  (`_resolve_chat_target`, `ChatExecutor.run_capability`, `_drive_events`, `_apply_confirmation`,
  `_select_agent`), `kenny_server/webui/__init__.py` (chat routes stop writing the global
  registry slot). Tests: `tests/test_tools.py` (new — the two-caller race, direct and fixed),
  `tests/test_chat.py` (session-scoped routing, including two concurrent sessions sharing one
  executor/registry).
- `docs/protocol.md` gained a prose note on the server-consumed `agent_id` routing argument;
  no frame, fixture, or `PROTOCOL_VERSION` change.

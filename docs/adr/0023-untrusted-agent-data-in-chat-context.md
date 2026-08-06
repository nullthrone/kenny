# 0023. Treat agent-supplied data as untrusted in the chat tool-use loop

- Status: accepted
- Date: 2026-06-07

## Context and Problem Statement

The server-hosted chat (ADR-0009) drives Claude through a tool-use loop. Many
tool results are influenced by the monitored machine rather than the operator:
telemetry section `summary`/raw fields (`Section` allows `extra` fields), `fs_read`
file contents, and command stdout/stderr. All of it is serialized verbatim into
`tool_result` blocks and fed back to the model.

A compromised or malicious agent therefore has a writable channel into the model's
context. Two concrete risks follow: (1) **second-order prompt injection** —
attacker text in telemetry or a read file tries to steer Claude into invoking
read-only-but-sensitive tools (e.g. `fs_read` of a secret, `screen_capture`) whose
auto-execution the confirm-gate does not stop; and (2) **resource/contex blow-up**
from very large tool results. Separately, `remotehelp_start`/`remotehelp_stop` are
classified mutating on the agent (`control.rs::is_mutating`, ADR-0021) but were
missing from the chat-side `STATE_CHANGING_TOOLS`, so they could be auto-invoked
without the operator confirmation every other mutating tool requires.

That read-only tools auto-execute while only state-changing tools are confirm-gated
is a deliberate, ADR-recorded decision (ADR-0009/0018) and is **not** revisited here.
This ADR records the threat model for agent-controlled data entering the model loop
and the mitigations we adopt.

## Considered Options

- Do nothing (rely on operator vigilance and the existing confirm-gate).
- Drop/blunt agent free-text before sending it to the model.
- Keep the data but mark it untrusted, bound its volume, and close the gate gap.

## Decision Outcome

Chosen option: keep the data (it is the point of the product) but **defend the loop**:

1. **Gate parity.** Add `remotehelp_start`/`remotehelp_stop` to the chat
   `STATE_CHANGING_TOOLS` so `control.rs::is_mutating` and the confirm-gate agree;
   "anything mutating is confirmed" holds with one source of truth.
2. **Treat tool output as data, not instructions.** The system prompt instructs the
   model that all tool results are untrusted data from the monitored machine, never
   to follow directives embedded in them, and that state-changing tools always
   require explicit operator confirmation regardless of tool-result content.
3. **Bound the volume.** A single tool result fed back to the model is capped
   (`_MAX_TOOL_RESULT_CHARS`), truncating oversized agent content with a marker.

This is defence-in-depth, not a guarantee: a model can still be steered. The
deterministic safety controls (agent-side tool guard ADR-0019/0020; the operator
confirm-gate for mutating tools) remain the hard boundaries; this ADR reduces the
prompt-injection surface and removes a gate gap.

### Consequences

- Good, because the most powerful auto-executing-then-mutating path
  (`remotehelp_start`) now requires operator confirmation in chat.
- Good, because oversized/hostile tool output can no longer blow up context, and the
  model is explicitly told to distrust embedded instructions.
- Bad, because prompt injection is not fully solved — a sufficiently clever payload
  may still influence read-only tool selection; we depend on the confirm-gate and the
  agent guard for anything that actually changes state.
- Bad, because the volume cap can truncate a legitimately large `fs_read`; the marker
  signals truncation and the operator can narrow the request.

## More Information

- Issue: `kenny-sec:chat/telemetry-prompt-injection-readonly-tools` (#55).
- Related: ADR-0009 (server-hosted chat + confirm-gate), ADR-0018 (screen capture is
  read-only), ADR-0019/0020 (deterministic agent-side tool guard + server mirror),
  ADR-0021 (remote-help concierge / `remotehelp_*`).
- Code: `kenny-server/kenny_server/chat.py` (`STATE_CHANGING_TOOLS`, `_SYSTEM_PROMPT`,
  `_tool_result_block`, `_MAX_TOOL_RESULT_CHARS`).

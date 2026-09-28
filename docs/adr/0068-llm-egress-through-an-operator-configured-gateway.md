# 0068. LLM egress through an operator-configured gateway

- Status: accepted
- Boundary moved: **the network and trust topology of model calls.** kenny's model
  traffic may go to an intermediary the operator chooses instead of straight to the
  Anthropic API. That intermediary sees every prompt and every answer, and its credentials
  become a second stored secret with the key's treatment.
- Amends: [ADR-0066](0066-anthropic-key-as-a-dashboard-setting-excluded-from-backups.md)
- Date: 2026-09-27

## Context and Problem Statement

Every AI feature reaches the model through `kenny_server.ai` (ADR-0066), and that module
builds a client for `api.anthropic.com` and nothing else. An operator who governs AI use
centrally routes model traffic through an AI gateway. Such a gateway enforces quotas,
inspects prompts and answers inline, keeps its own audit trail, and holds the provider
credentials so the applications behind it never see them. kenny had no way to join that
setup. Its fast-model id was also hard-coded, and gateways often expect their own model
names.

The question is how far kenny adapts. One option is to point the client it already has at
another host. The other is to abstract the model interface so it could talk to any
provider.

## Considered Options

- **Direct only.** Leaves operators who route through a gateway out.
- **A provider-neutral model layer**, translating to other wire formats as well. It
  opens kenny to non-Claude models, but the tool loop, streaming events, extended thinking
  and prompt caching would all need an abstraction. The confirm-gate would then have to
  hold across providers whose tool-call semantics differ.
- **The Anthropic Messages API behind a configurable base URL, with operator-supplied
  headers and model ids.** Chosen.

## Decision Outcome

kenny talks to a gateway only in the Anthropic Messages API (`/v1/messages`, with
streaming, tool use and `cache_control` passed through). A gateway that only speaks another
wire format is not supported. Three `live` settings in the AI group configure it, and
`AiAccess` resolves all three on every call:

- `ANTHROPIC_BASE_URL`: the gateway's base URL. Empty means the Anthropic API.
  - It requires `https`. Plain `http` is allowed only to a loopback host or a dotless
    container-network name.
  - It may not carry user info, a query or a fragment.
- `ANTHROPIC_CUSTOM_HEADERS`: `Name: Value` entries, sent with every call, for the
  gateway's authentication and routing.
  - The setting is `sensitive` and `backup_excluded`, like the key: it is never read back
    and never leaves the live database in a backup.
  - Headers that the HTTP client owns, `x-api-key` and `anthropic-version` are refused,
    so a setting can neither break framing nor quietly replace the key.
- `KENNY_FAST_MODEL`: the model id for recommendations, forecast prose and event
  classification. It sits alongside `KENNY_CHAT_MODEL`.
  - The classifier's stored verdict tag follows it, so a changed model re-classifies on
    the next start.

AI counts as available when a key **or** a gateway is set, because a gateway may hold the
provider key itself. Without a key, the client sends a fixed placeholder. The connection
test sends one single-token message on the fast model, which takes the same path, headers,
model id and inspection that every feature takes. Values from the environment are not
validated on write, so the test reports an invalid one without calling out.
`GET /api/ai/status` reports the gateway's host, never its headers.

### Consequences

- Good, because an operator can route all of kenny's model traffic through their
  governance point and switch it in the dashboard with no restart. The provider key can
  stay in the gateway.
- Good, because nothing below `AiAccess` changes: the tool loop, streaming, thinking,
  caching and the confirm-gate are exactly what they were.
- Bad, because the gateway sees everything the model sees. That includes telemetry facts,
  event-log samples, web-activity host names, ticket text and tool output. The gateway is
  trusted at the provider's level.
  - The tier gates (ADR-0045, ADR-0009) do not depend on the model. A state-changing tool
    call that a gateway injected or rewrote still stops at the confirm-gate.
  - A read-only call runs, as it would if the model had asked for it.
- Bad, because a gateway that blocks a request answers with its own status and wording.
  Features degrade as they do on any API error, and the connection test shows that answer
  to the operator verbatim.
- Bad, because a restore loses dashboard-set gateway headers the same way it loses the key
  (ADR-0066). If the environment does not supply them, calls reach the gateway
  unauthenticated until the headers are set again.

## More Information

- [ADR-0066](0066-anthropic-key-as-a-dashboard-setting-excluded-from-backups.md): the single
  access point and the backup exclusion.
- [ADR-0023](0023-untrusted-agent-data-in-chat-context.md): agent data is untrusted in model
  context.
- Setup: `docs/setup.md` → *AI gateway*.

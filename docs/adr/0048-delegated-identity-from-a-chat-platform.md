# 0048. Delegated identity from a chat platform, with no parallel authorization

- Status: proposed
- Date: 2026-08-01
- Amends: [ADR-0024](0024-untrusted-agent-data-in-chat-context.md)

## Context and Problem Statement

kenny has two operated surfaces: the MCP endpoint and the dashboard copilot. Both assume
an operator. The family members whose PCs kenny watches have no way in at all — they
report problems verbally and the operator works them off by hand. Giving them a chat
surface on a platform the household already uses (Discord) means kenny must answer a
question it has never had to answer: **when a message arrives from a system kenny does
not own, who is speaking, and what is kenny willing to take that system's word for?**

The tempting answer is to let the platform's own permission model govern — it already
has accounts, roles and channels. That would give kenny a second authorization system
sitting beside `webui/authz.py`, one kenny neither controls nor audits.

A second, related problem comes with the surface. ADR-0037 gated the dashboard copilot
at operator+ precisely because it drives arbitrary capability tools against any host, and
left the `user` role to the structured per-host dashboard. This surface hands
tool-driving power to exactly that role. That is either a carefully bounded delegation or
it is privilege escalation, and the difference has to be recorded.

## Considered Options

- **Mirror the platform's permission model** — derive kenny roles from platform roles and
  channels.
- **A separate authorization model for the chat surface** — its own permissions, evaluated
  beside the existing one.
- **Accept only the platform's identity assertion** and map it, through a binding kenny
  owns, onto the *existing* `Principal` — no second model.
- **Make chat users authenticate to kenny directly** (a credential presented in-channel per
  session) rather than trusting the platform at all.

## Decision Outcome

Chosen option: **accept only the identity assertion**. A message author is resolved
through a binding kenny stores, and the surface mints an ordinary `auth.Principal` from
that binding alone. Everything downstream — the route guard, the role and host-scope
checks inside the tools, the policy mirror, the agent-side guard, the kill switch —
applies unchanged, and every action attributes to a real kenny user in the existing audit
trail. Direct in-channel authentication was rejected as strictly worse: it would move
password-equivalent material into a third party's message store for no gain, since the
platform has already authenticated the account.

**What is trusted to the platform:** that it authenticated an account, and that this
account authored this message. Nothing else.

**What is not:** display names, platform roles, channel membership, and message content.

- **The binding is to an immutable platform account id, never a display name.** A display
  name is mutable and re-assignable, and a mutable identifier must never reach an
  authorization decision. This is enforced structurally rather than by convention: the
  adapter protocol carries no name field anywhere, so a name cannot enter an authorization
  path even by a later mistake. An author with no binding is completely inert — no case is
  opened, no reply is sent, and no model call is made. A disabled binding resolves to
  nothing, not to a weaker principal.
- **The guild allowlist is a hard trust boundary and fails closed.** The bot refuses
  service in any guild not listed, and an empty list denies everywhere. Being invited into
  an arbitrary server must not create a surface.
- **Platform roles are advisory only** — they steer routing and visibility, never a
  decision, neither granting nor narrowing. Whoever can hand out roles in the guild could
  otherwise hand out kenny privileges, outside kenny's audit and outside kenny's control.
  A test freezes this.

**Why a non-operator may drive tools here when the dashboard copilot deliberately may
not.** This is defensible only while **four controls hold simultaneously**, each
load-bearing on its own:

1. the target host is fixed when the case is opened and nothing in the conversation can
   change it — the ADR-0042 control, applied twice (the selection tool is withheld from
   the surface, and a target argument coming back from the model is discarded, not
   adopted);
2. a per-user capability allowlist narrows the reachable tool set (ADR-0051);
3. the tier gate stops anything consequential and routes it to an operator (ADR-0049);
4. a consent gate covers tools that touch someone's privacy, independently of
   authorization (ADR-0051).

Drop any one and this becomes privilege escalation. It is recorded here so that a later
change removing one is recognisable as reopening this decision rather than as a tidy-up.

**The conversation itself is untrusted input, and multi-party.** ADR-0024 established that
agent-supplied data entering the model loop is untrusted. This surface is qualitatively
different: a sibling can type into the same thread as the person who opened the case, and
the platform relaying it sits outside kenny's trust boundary. So every message enters the
model context carrying its provenance — only the requester's own messages are actionable,
messages from other bound members are context, and messages from unbound authors never
enter at all. The invariant on top: **nothing in a conversation ever changes the
principal, the target host, or whether a gate was answered.** Those are decided outside
the model loop, by the four controls above.

Finally, output that carries pixels, file contents, event-log text or browsing history off
a host is never echoed to the platform. The thread gets a paraphrase and a link into the
authenticated dashboard; the data stays inside kenny's boundary.

### Consequences

- Good, because there is exactly one authorization system. Nothing had to be re-implemented
  for the new surface, and no check can be true on one surface and false on another.
- Good, because the platform can never mint privilege — it can only assert who is speaking.
  Revoking someone is one flag on the binding, not a cleanup across systems.
- Good, because the household finally has self-service support without anyone becoming an
  operator to get it.
- Bad, because kenny now inherits the platform's account security: a compromised platform
  account is a compromised kenny user, bounded only by that user's profile and host scope.
  There is no second factor on this path, and there cannot be one kenny controls.
- Bad, because enrollment is a deliberate operator step — there is no self-service route
  from a platform account to a kenny account, and there should not be, since that mapping
  decides whose machines a person may ask about.
- Bad, because a hostile message can still steer the model's choice among the tools the
  caller already has, exactly as ADR-0024 concedes for agent data. The deterministic gates,
  not the model's judgement, remain the hard boundary.
- Neutral: the platform relationship is optional. Unconfigured, kenny behaves as before and
  the case-management surface works without it.

## More Information

- Extends [ADR-0024](0024-untrusted-agent-data-in-chat-context.md) from agent-supplied data
  to a multi-party conversation that a third party relays.
- Builds on [ADR-0037](0037-multi-user-authentication.md) (the `Principal`, roles, host
  scope) and [ADR-0042](0042-explicit-per-call-agent-targeting.md) (explicit, frozen
  targeting). Companion records: [ADR-0049](0049-tiered-tool-classification.md) (the tier
  gate), [ADR-0050](0050-ticket-as-entity-chat-thread-as-binding.md) (the case record),
  [ADR-0051](0051-capability-profiles.md) (the allowlist and the consent axis).
- The wire contract is untouched: this surface sits entirely above the tunnel, so
  `docs/protocol.md`, `docs/fixtures/` and `PROTOCOL_VERSION` are unchanged — the same
  posture as [ADR-0029](0029-push-alerting-ntfy-webhook-and-weekly-digest.md).

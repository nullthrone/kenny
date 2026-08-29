---
description: Review kenny's security weak points and file deduplicated English GitHub issues for new findings
argument-hint: "[area or severity filter, e.g. 'auth' or 'high+'] (optional)"
---

Run a **targeted** security review of kenny and document genuine findings as English-language
GitHub issues — without ever filing a duplicate. This is a read-only code investigation; the
only side effect is creating issues. If `$ARGUMENTS` is set, restrict the review to that area or
minimum severity.

## 1. Investigate kenny's weak points

This is a code investigation, not a checklist tick. Two steps: first **derive the attack surface
from the current code** (1a), then use the known hotspots (1b) only as a seed. Throughout, keep the
core rule: **do not invent generic advice — tie every finding to a file:line and assign a CWE where
it fits.**

### 1a. Discovery & coverage (do this first — derive surfaces from the code, not from a list)

A fixed list goes stale as the code grows; enumerate the live surface yourself so new code is never
silently out of scope. Before judging anything, build a coverage map from the code:

- **Enumerate every agent handler** in `kenny-agent/src/handlers/` (one by one — do not assume the
  set below is complete).
- **Enumerate every server entry point** under `kenny-server/kenny_server/`: FastMCP tools, HTTP/API
  routes, the agent WebSocket handler, and the download/distribution and auth routes.
- **Trace every place agent-controlled data is rendered** (dashboard / web UI) **or executed**
  (agent handlers, chat tool-calls) — these are the highest-value sinks.

Drive the review with established frameworks as a *coverage lens* (not as a substitute for concrete,
code-anchored findings). For kenny's context, walk at least:

- **STRIDE per trust boundary** — boundaries: operator↔server, server↔agent, agent↔Windows OS, and
  server↔external (GitHub Releases, Anthropic API). Going boundary by boundary forces full
  enumeration and catches surfaces a topic list would miss.
- **OWASP Top 10 for LLM Apps** — for the LLM surfaces (`chat.py`, `recommend.py`): prompt
  injection, excessive agency, sensitive-information disclosure.
- **OWASP ASVS / Top 10** — web/API baseline for the server and dashboard: authn/session, access
  control, XSS, input validation, rate-limiting.
- **CWE Top 25** (tag every finding with its CWE) **and SLSA** for the supply chain (self-update,
  `agent_release.py`, CI / release signing, image provenance).

### 1b. Known hotspots — a non-exhaustive seed, not the scope boundary (do not stop here)

These are high-signal places to look first, but they are **not** the full surface — anything 1a
surfaces is in scope even if it is absent below.

- **Operator auth** (`kenny-server/kenny_server/auth.py`): shared single token; login cookie
  carries the raw token; insecure dev fallback when `KENNY_OPERATOR_TOKEN` is unset; constant-time
  compare; `Secure` cookie only under `KENNY_TLS`; `/d/*` and `/login` auth exemptions; no
  rate-limiting on `/login` (operator-token brute force).
- **Agent auth** (`registry.py`, `tokenstore.py`, `keystore.py`): token-at-rest (hashed vs
  plaintext), rotation, dev fallback tokens, comparison timing, replay across reconnects; Ed25519
  mutual-auth key storage, key rotation / grace period (ADR-0022).
- **Self-update** (`kenny-agent/src/handlers/agent_update.rs`, server `distribution.py`): the agent
  fetches and executes a binary from a server-supplied `url` — who can trigger `agent_update`, is
  the `sha256` an *authenticated* integrity check (or MITM-forgeable, i.e. no code signature?), is
  TLS enforced on the download, and is the swap/rollback safe? (supply-chain / RCE, CWE-494/CWE-829).
- **Distribution links** (`distribution.py`): `/d/installer|binary/{nonce}` public endpoints —
  nonce entropy/expiry, one-time vs reusable (binary nonce is not consumed), and the installer ZIP
  embedding the agent token in plaintext `install.bat` (token leakage via a shared link).
- **Command-exec handlers** (`kenny-agent/src/handlers/` powershell/winget/fs/network): RCE/admin by
  design — check argument injection and **path traversal / arbitrary read** in `fs.*`, and timeouts.
- **Diagnostics & remote-presence handlers** (`diagnostics.rs`, `screenshot.rs`, `remotehelp.rs`):
  information disclosure and privileged actions; Session 0 → user-session delegation over tray IPC
  (named pipes) for `screen_capture` / `remotehelp_*` (ADR-0018/0021) — who can trigger them and
  what the IPC trusts.
- **Server-side chat & recommendations** (`kenny-server/kenny_server/chat.py`, `recommend.py`): can
  the confirm-gate for state-changing tools be bypassed; **prompt injection** from agent-controlled
  telemetry / `fs_read` content / cached facts / tool output steering Claude into
  read-only-but-sensitive calls (e.g. `screen_capture`, reading secrets); session isolation; API-key
  handling (CWE-77/CWE-94 adjacent).
- **Tool guard & policy** (`kenny-agent/src/...` tool guard, server `policy.py`): deterministic
  deny-rule enforcement and the server-side mirror (ADR-0019/0020) — can a denied tool slip through
  either side, regex/catalog gaps.
- **Web UI XSS** (`kenny-server/kenny_server/webui/index.html`): agent-controlled telemetry fields
  (e.g. section `summary`, hostnames) rendered into `innerHTML` without escaping → stored XSS in the
  operator's browser from a malicious/compromised agent (CWE-79).
- **Transport** (`docs/protocol.md`, agent `tunnel.rs`): `ws://` permitted (token in clear); server
  identity is TLS-only with no cert pinning.
- **Input/data** (`protocol.py`, `store.py`): frame validation limits, telemetry size/JSON-bomb,
  SQL parameterization, retention.
- **Supply chain / CI** (`.github/workflows/*`, `Dockerfile`, `agent_release.py`): action pinning
  (tags vs SHA), workflow `permissions` scoping, GitHub-token handling on binary auto-fetch, unsigned
  release binary default, image provenance.

## 2. Rule out deliberate decisions BEFORE judging (read the ADRs)

Before you assign severity or file anything, check whether the behaviour is a **deliberate,
ADR-recorded architectural decision** — not an oversight. Skipping this leads to filing
"findings" that merely restate an accepted trade-off (e.g. `ws://` permitted for local use,
`Secure` cookie only under TLS, RCE-by-design command handlers, agents trusted to push
telemetry).

- Read `docs/adr/` for the surface in question (grep the ADRs for the relevant terms, e.g.
  `ws://`/`TLS`/`transport`, `token`/`cookie`/`Secure`, `confirm`/`gate`, `self-update`). The
  ADR **Context**, **Decision Outcome**, **Consequences ("Bad, because …")**, and **More
  Information** sections often state the exact trade-off and its assumed deployment (e.g.
  "serve over `wss`/`https` in production; `ws://` is for local use only").
- If the behaviour is the documented intent, treat the ADR like a closed issue: **do not file
  it as a vulnerability.** Either drop it, or — if there is a genuine *residual* gap the ADR
  does **not** cover (e.g. the decision is sound but the code does not fail-closed/warn when its
  stated precondition is violated) — file only that narrow residual, at its true (usually lower)
  severity, and cite the ADR explaining why the rest is out of scope.
- An ADR can be wrong or outdated. You may still file against a documented decision, but only
  with **clear new evidence** that the decision's own assumptions no longer hold — say so
  explicitly and reference the ADR, rather than re-litigating a settled trade-off.

## 3. Dedup BEFORE filing (open AND closed issues)

For each candidate finding, derive a stable fingerprint slug: `kenny-sec:<area>/<short-slug>`
(e.g. `kenny-sec:webui/telemetry-innerhtml-xss`). Then, using the GitHub MCP tools, check whether it
is already tracked — **including past closed issues**:

- `mcp__github__search_issues` with `repo:nullthrone/kenny "kenny-sec:<slug>"` (do NOT add `is:open` — search
  must include closed). Also do a broader title/keyword search to catch issues filed before this
  convention existed.
- `mcp__github__list_issues` for label `security` (state `all`) to build a dedup map up front.

If a matching issue exists in **any** state (open or closed), DO NOT file again — record it as
"already tracked (#N, <state>)". A closed issue means a human already decided on it; reopen only if you
have clear new evidence, and say why in a comment instead of opening a new one.

## 4. File new findings only (English, templated)

For each genuinely new finding, create an issue with `mcp__github__issue_write`:

- Title: `[security] <concise finding>`
- Labels: `security` plus a severity label (`severity:critical|high|medium|low`); create the label
  with the GitHub tools if it does not exist.
- Body (English):
  - **Summary** — one sentence.
  - **Severity** — Critical/High/Medium/Low + CWE.
  - **Affected surface** — which weak point.
  - **Location** — `file:line` (and the relevant snippet).
  - **Impact / attack scenario** — concrete, who can do what.
  - **Recommendation** — the smallest sound fix; reference the relevant ADR if any (and, if the
    finding touches an ADR-recorded decision, state why it is still in scope per step 2).
  - Footer: `kenny-sec:<slug>` (the dedup fingerprint — keep it exact).

## 5. Report

Print a table: finding → severity → action (`filed #N` / `duplicate of #N (state)` /
`by design — ADR-#### (skipped)` / `skipped`).
Do not open pull requests or change code — this command only investigates and files issues.

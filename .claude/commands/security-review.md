---
description: Review kenny's security weak points and file deduplicated English GitHub issues for new findings
argument-hint: "[area or severity filter, e.g. 'auth' or 'high+'] (optional)"
---

Run a **targeted** security review of kenny and document genuine findings as English-language
GitHub issues — without ever filing a duplicate. This is a read-only code investigation; the
only side effect is creating issues. If `$ARGUMENTS` is set, restrict the review to that area or
minimum severity.

## 1. Investigate kenny's weak points

Go surface by surface. For each, read the cited code and look for concrete, exploitable issues
(assign a CWE where it fits). Do not invent generic advice — tie every finding to a file:line.

- **Operator auth** (`kenny-server/kenny_server/auth.py`): shared single token; login cookie
  carries the raw token; insecure dev fallback when `KENNY_OPERATOR_TOKEN` is unset; constant-time
  compare; `Secure` cookie only under `KENNY_TLS`; `/d/*` and `/login` auth exemptions; no
  rate-limiting on `/login` (operator-token brute force).
- **Agent auth** (`registry.py`, `tokenstore.py`): token-at-rest (hashed vs plaintext), rotation,
  dev fallback tokens, comparison timing, replay across reconnects.
- **Self-update** (`kenny-agent/src/handlers/agent_update.rs`, server `distribution.py`): the agent
  fetches and executes a binary from a server-supplied `url` — who can trigger `agent.update`, is
  the `sha256` an *authenticated* integrity check (or MITM-forgeable, i.e. no code signature?), is
  TLS enforced on the download, and is the swap/rollback safe? (supply-chain / RCE, CWE-494/CWE-829).
- **Distribution links** (`distribution.py`): `/d/installer|binary/{nonce}` public endpoints —
  nonce entropy/expiry, one-time vs reusable (binary nonce is not consumed), and the installer ZIP
  embedding the agent token in plaintext `install.bat` (token leakage via a shared link).
- **Command-exec handlers** (`kenny-agent/src/handlers/` powershell/winget/fs/network): RCE/admin by
  design — check argument injection and **path traversal / arbitrary read** in `fs.*`, and timeouts.
- **Server-side chat** (`kenny-server/kenny_server/chat.py`): can the confirm-gate for state-changing
  tools be bypassed; **prompt injection** from agent-controlled telemetry / `fs.read` content / tool
  output steering Claude into read-only-but-sensitive calls (e.g. `screen.capture`, reading secrets);
  session isolation; API-key handling (CWE-77/CWE-94 adjacent).
- **Web UI XSS** (`kenny-server/kenny_server/webui/index.html`): agent-controlled telemetry fields
  (e.g. section `summary`, hostnames) rendered into `innerHTML` without escaping → stored XSS in the
  operator's browser from a malicious/compromised agent (CWE-79).
- **Transport** (`docs/protocol.md`, agent `tunnel.rs`): `ws://` permitted (token in clear); server
  identity is TLS-only with no cert pinning.
- **Input/data** (`protocol.py`, `store.py`): frame validation limits, telemetry size/JSON-bomb,
  SQL parameterization, retention.
- **Supply chain / CI** (`.github/workflows/*`, `Dockerfile`): action pinning (tags vs SHA), workflow
  `permissions` scoping, unsigned release binary default, image provenance.

## 2. Dedup BEFORE filing (open AND closed issues)

For each candidate finding, derive a stable fingerprint slug: `kenny-sec:<area>/<short-slug>`
(e.g. `kenny-sec:webui/telemetry-innerhtml-xss`). Then, using the GitHub MCP tools, check whether it
is already tracked — **including past closed issues**:

- `mcp__github__search_issues` with `repo:t11z/kenny "kenny-sec:<slug>"` (do NOT add `is:open` — search
  must include closed). Also do a broader title/keyword search to catch issues filed before this
  convention existed.
- `mcp__github__list_issues` for label `security` (state `all`) to build a dedup map up front.

If a matching issue exists in **any** state (open or closed), DO NOT file again — record it as
"already tracked (#N, <state>)". A closed issue means a human already decided on it; reopen only if you
have clear new evidence, and say why in a comment instead of opening a new one.

## 3. File new findings only (English, templated)

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
  - **Recommendation** — the smallest sound fix; reference the relevant ADR if any.
  - Footer: `kenny-sec:<slug>` (the dedup fingerprint — keep it exact).

## 4. Report

Print a table: finding → severity → action (`filed #N` / `duplicate of #N (state)` / `skipped`).
Do not open pull requests or change code — this command only investigates and files issues.

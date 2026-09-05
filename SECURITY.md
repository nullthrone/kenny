# Security Policy

kenny is a **remote-administration tool**: a server that can run PowerShell or a shell, install
packages, and read files on enrolled Windows and Linux hosts. Please treat vulnerabilities
accordingly.

## Reporting a vulnerability

**Do not open a public issue, PR, or Discussion for a security problem** — that would
disclose it before a fix exists.

Instead, report privately via **[GitHub Security Advisories](https://github.com/nullthrone/kenny/security/advisories/new)**
("Report a vulnerability"). If you can't use that, contact the maintainer privately via
**[@t11z](https://github.com/t11z)**.

Please include:

- a description and impact assessment,
- steps to reproduce or a proof of concept,
- affected component(s) and version/commit (`kenny-server`, `kenny-agent`, the wire
  protocol), and
- any suggested remediation.

## What to expect

- **Acknowledgement** within a few days.
- An assessment and, for confirmed issues, a fix developed **privately** (via a security
  advisory / private fork) and released before public disclosure.
- Credit in the advisory if you'd like it.

This is a community project maintained on a best-effort basis — there is no paid bug-bounty,
but reports are taken seriously.

## Scope

In scope: authentication/authorization bypass, the agent-side safety guard or kill-switch
being bypassed, remote code execution beyond the documented tool surface, the agent⇄server
tunnel or the operator/agent token model, the agent self-update path, and secret handling.

Out of scope: the documented, intended capabilities (kenny *is* a remote-admin tool — an
authorized operator running PowerShell is expected), issues that require an already-trusted
operator token, and findings against your own misconfiguration (e.g. running `ws://` without
TLS, which the docs explicitly mark as local-only).

## Hardening notes for operators

- Always run the server behind **TLS** (`wss://` / `https://`) in production.
- Keep the **operator token** secret; rotate agent tokens via the rotation endpoint.
- The agent ships a deterministic, always-on **safety guard** and a local **kill-switch**;
  neither is a substitute for guarding the operator token.

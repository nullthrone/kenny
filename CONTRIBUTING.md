# Contributing to kenny

Thanks for your interest in kenny! 🐕 This guide covers how to build, test, and propose
changes. By participating you agree to our [Code of Conduct](CODE_OF_CONDUCT.md).

For questions and ideas, use [GitHub Discussions](https://github.com/nullthrone/kenny/discussions).
For **security issues, do not open a public issue** — see [SECURITY.md](SECURITY.md).

## Project layout

- `docs/protocol.md` + `docs/fixtures/` — the agent⇄server **wire contract** (single source of
  truth). Frame and tool schemas live here, nowhere else.
- `kenny-server/` — Python / FastMCP server (MCP endpoint, agent tunnel, telemetry store, web
  dashboard).
- `kenny-agent/` — Rust single-binary agent for each managed host (Windows, Linux).
- `docs/adr/` — architecture decisions (MADR).

## Build & test

```bash
# Dashboard (Node.js; contributors only — end users get a prebuilt UI)
npm --prefix kenny-web install && npm --prefix kenny-web run build
npm --prefix kenny-web run typecheck && npm --prefix kenny-web test

# Server (Python 3.11+)
cd kenny-server && pip install -e ".[dev]" && pytest -q && ruff check .

# Agent (Rust; builds on Linux too via cfg fallbacks)
cd kenny-agent && cargo test && cargo build
cargo fmt --check && cargo clippy --all-targets -- -D warnings
```

CI runs exactly these, plus a Windows job for `#[cfg(windows)]` code and a real
agent↔server end-to-end test. Please make sure the suites are green before opening a PR.

The dashboard builds from `kenny-web/` (Vite + React + TypeScript) to `kenny-server/kenny_server/webui/dist/`,
which the server serves and which ships inside the Python package. See `docs/adr/0052-dashboard-as-a-compiled-frontend.md`
for the rationale.

## The contract is authoritative

**Never change a frame or tool shape in only one language.** The Python server and the Rust
agent both round-trip the golden fixtures in `docs/fixtures/`, so they cannot drift.

When you touch the protocol:

1. Change `docs/protocol.md` + `docs/fixtures/` first (and bump `PROTOCOL_VERSION`).
2. Update **both** the server and the agent.
3. Run `/contract-check` (or both fixture round-trip tests) — it must be clean.

Recipes (also available as Claude Code commands): adding a capability tool
(`/add-tool`) and adding a telemetry collector (`/add-collector`).

## Architecture decisions (ADRs)

Write an ADR (`docs/adr/`, MADR format — copy `0000-template.md`, or `/new-adr`) when a
decision is **architectural**: hard to reverse, cross-cutting, or moving a structural
boundary (the wire contract / `PROTOCOL_VERSION`, the trust/auth model, the storage or
deployment shape, the agent/session model). Do **not** write one for a localized
implementation detail, bug fix, refactor, or UI tweak — record those in the commit message.

## Pull requests

- Keep PRs focused; describe the change and link any related issue.
- Update docs and add tests for new behavior.
- Use the PR checklist in the template (tests pass, `/contract-check` if the contract
  changed, an ADR if the change is architectural).
- Everything committed to the repo is **English** — code, comments, docs, commit messages,
  PR titles and bodies, identifiers.

### Sign your commits (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/). Sign off
each commit (certifying you wrote it / may submit it under the project license):

```bash
git commit -s -m "Your message"
```

This adds a `Signed-off-by: Your Name <you@example.com>` trailer.

## Labels

Issues and PRs are triaged (partly by an automated assistant) with: `bug`, `feature`,
`question`, `docs`, `area:server|agent|protocol|dashboard`, `needs-info`, `confirmed`,
`security`, `good first issue`.

## License

kenny is **AGPL-3.0-only**. Contributions are accepted under the same license; the DCO
sign-off is how you certify that.

<!-- Thanks for contributing to kenny! 🐕 -->

## What & why

<!-- What does this change and why? Link any related issue (e.g. "Closes #123"). -->

## Checklist

- [ ] Tests pass: `pytest -q` (server) and `cargo test` (agent)
- [ ] Lint/format clean: `ruff check .`, `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`
- [ ] If the **wire contract** changed: updated `docs/protocol.md` + `docs/fixtures/`, bumped
      `PROTOCOL_VERSION`, updated **both** server and agent, and `/contract-check` is clean
- [ ] If the change is **architectural**: added/updated an ADR in `docs/adr/`
- [ ] Docs updated where relevant
- [ ] Commits are signed off (DCO: `git commit -s`)

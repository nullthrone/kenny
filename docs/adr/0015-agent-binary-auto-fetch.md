# 0015. Server auto-fetch of the prebuilt agent binary from GitHub Releases

- Status: accepted
- Amended by: [ADR-0057](0057-anonymous-github-reads-for-agent-distribution.md)
- Date: 2026-06-05

## Context and Problem Statement

kenny serves a **prebuilt** `kenny-agent.exe` and injects per-install config at download time
(ADR-0012); the same artifact powers self-update (ADR-0013). Until now an operator had to **manually
place** that binary at `KENNY_AGENT_BINARY` (typically copying it into the `/data` volume) before the
dashboard could produce any installer. This created a first-agent chicken-and-egg: the *download
installer* button only existed inside an already-enrolled agent's view, and a fresh deployment had no
binary and no obvious way to get one onto the host.

How should the server obtain the agent binary for a brand-new fleet with the least operator effort,
without bloating the server image or weakening the verifiability of what gets shipped to PCs?

## Considered Options

- **A. Server auto-fetches the latest release asset from GitHub** when a token is configured, with a
  dashboard onboarding control + manual-instructions banner as the fallback.
- **B. Bake the agent `.exe` into the server image** at build time.
- **C. Keep manual-only placement** (status quo).

## Decision Outcome

Chosen option: **A**. On startup (and on demand via a dashboard button) the server fetches the latest
release's `kenny-agent-<tag>-x86_64-pc-windows-msvc.exe`, verifies it against the published `.sha256`,
and atomically caches it on the data volume. The fetch is **gated on a configured `KENNY_GITHUB_TOKEN`**
(unlocks private repos and avoids anonymous API rate limits), **best-effort and non-fatal** (a failure
never blocks startup), and **operator-placed `KENNY_AGENT_BINARY` always wins** over the cache. When no
token is set or the repo is unreachable, the dashboard shows a banner explaining how to download the
release asset and place it on the server. The dashboard always offers an **Add a PC** onboarding control
so the first installer can be downloaded without a pre-existing agent tile.

### Consequences

- Good: zero-touch first install when a token exists; no manual file shuffling into the volume.
- Good: no server-image bloat or per-architecture coupling; the same release artifact still drives self-update.
- Good: downloads are sha256-verified before use; the cache write is atomic (temp + `os.replace`).
- Good: graceful degradation — a clear manual path remains when fetch is unavailable.
- Bad: adds a runtime `httpx` dependency and outbound HTTP to GitHub from the server.
- Bad: relies on the release asset naming convention (`kenny-agent-*-x86_64-pc-windows-msvc.exe` + `.sha256`); a workflow rename would break auto-fetch (mitigated by the manual fallback).
- The **git release tag is the leading source of the agent version** end-to-end: `build.rs` stamps it into the binary (reported as `meta.version`), and on fetch the server persists it to a `<binary>.version` sidecar that `resolve_agent_version()` uses for `agent.update` and the dashboard. `KENNY_AGENT_VERSION` is only a fallback when no tag is known.

## More Information

- Supplements [ADR-0012](0012-agent-distribution-prebuilt-binary.md) and [ADR-0013](0013-agent-windows-service-and-self-update.md).
- Implementation: `kenny-server/kenny_server/agent_release.py`, the `/api/agent-binary[/fetch]` routes in `distribution.py`, the startup hook in `main.py`.
- Release asset source: `.github/workflows/release.yml`.

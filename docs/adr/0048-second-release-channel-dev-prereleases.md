# 0048. Second release channel: `dev` prereleases via the GitHub `prerelease` flag

- Status: accepted
- Date: 2026-08-02

## Context and Problem Statement

kenny has exactly one delivery stream today. `.github/workflows/release.yml` runs on
`push: tags: ["v*"]` and publishes three artifact families into **one** GitHub Release: the
`kenny-server` image to GHCR, the Windows `kenny-agent.exe`, and two Linux musl binaries
(`x86_64`/`aarch64`). The server resolves what to fetch and serve via
`GET /repos/{repo}/releases/latest` (`agent_release.py`, ADR-0015) and a GHCR tag poll
(`server_release.py`, ADR-0040). Nothing in the workflow, the wire contract, or the server
has a notion of a channel — every agent and every server instance is on the same stream.

The architect wants a second stream for in-development builds: agent and server built
together on every `main` push, so a single test PC can run it while the rest of the fleet
stays on stable. This has to compose with two decisions already on the books: ADR-0034's
Option C (autonomous agent-side auto-update, rejected) and ADR-0040 (a rollout is a pinned,
operator-approved campaign naming one exact artifact, never a live "track latest"
subscription). A dev channel must not reopen either — it should only add a second **source**
a campaign can be approved from, not a second way for an update to reach a PC without
approval.

## Considered Options

- **A. GitHub's `prerelease` flag drives the channel, one repo/one workflow family.**
  Every `main` push publishes a **prerelease** GitHub Release tagged
  `v<next-patch>-dev.<run_number>` via a reusable workflow shared with the stable release
  job, plus a `ghcr.io/nullthrone/kenny-server:edge` image tag. `GET /releases/latest` **excludes
  prereleases by definition**, so the stable path is untouched; a new dev path reads
  `GET /releases` and takes the newest entry with `prerelease: true`. A per-agent desired
  channel (stored server-side, reported by the agent on the wire) decides which agents an
  approved dev campaign can target — reusing ADR-0040's pinned-campaign mechanism verbatim,
  just fed from a second detection source.
- **B. Second repository or second branch as the dev stream.** A parallel `kenny-dev` repo
  or a long-lived `dev` branch with its own tag namespace. Rejected: doubles the CI/secrets
  surface (code-signing cert, GHCR credentials) for no benefit `prerelease` doesn't already
  give, and a second branch invites exactly the drift `CLAUDE.md`'s "Python and Rust must not
  drift" invariant exists to prevent — now doubled across branches too.
- **C. Rolling `dev` tag, assets overwritten each push.** Keeps the release list short, but
  the tag stops identifying the artifact: `agent_release.py`'s version-sidecar and
  `server_release.py`'s semver comparison both key on the tag, and `update_manager.py`'s
  campaign pinning (ADR-0040) exists specifically so an approval names one concrete artifact.
  A rolling tag would force a rebuild of all three around content-hash comparison instead of
  version comparison, for a problem (release-list length) a cleanup step in the dev workflow
  solves more cheaply.
- **D. Global `KENNY_RELEASE_CHANNEL` env var, one setting for the whole fleet.** Simplest to
  build, but defeats the stated purpose: testing dev would mean moving every agent onto it,
  not just one PC. Rejected on the explicit ask for mixed operation.
- **E. Fleet-wide dev toggle instead of an opt-in per PC.** Same rejection as D, phrased as a
  campaign-scoping question instead of a config question.

## Decision Outcome

Chosen option: **A**, because the `prerelease` flag is the one GitHub-native primitive built
for exactly this distinction, it costs no new infrastructure, and `releases/latest`'s
built-in prerelease exclusion means the stable path requires zero code changes to stay
correct.

- **Tag shape:** `v<next-patch>-dev.<run_number>` per `main` push (e.g. `v2.0.5-dev.17` when
  `v2.0.4` is the latest stable tag) — an immutable, individually-identifiable tag, not a
  rolling one (rejects Option C). Semver-prerelease precedence sorts it correctly between
  `2.0.4` and `2.0.5.` The dev workflow prunes old dev releases/tags (keeping the most recent
  ~10) so the release list doesn't grow without bound; this is a required step, not a nice-to-have.
- **Channel is a per-agent, server-held desired state**, mirroring ADR-0036's arch pattern
  exactly: the agent reports its **built-in** channel (`stable` by default, `dev` when built
  with `KENNY_AGENT_CHANNEL=dev`) once in `register.meta.channel` and periodically in the
  `os_support` telemetry section, additive to `docs/protocol.md` (`PROTOCOL_VERSION` 0.16 →
  0.17). The server separately stores a **desired** channel per agent (default `stable`,
  operator-editable in the dashboard) — the same soll/ist split ADR-0040 already uses for
  version vs. running version. A dev campaign's eligibility gains a second condition
  alongside `(os, arch)`: the target agent's desired channel must match the campaign's
  channel. This is what makes mixed fleet operation possible.
- **The rollout mechanism is unchanged.** A dev candidate is detected by the same
  `agent_release.py`/`server_release.py` polling ADR-0040 already runs, just pointed at
  `GET /releases` (filtered to the newest `prerelease: true`, non-draft entry) instead of
  `/releases/latest` when the channel is `dev`. Approving a campaign still snapshots one
  exact `{version, url, sha256}` (or GHCR digest) at approval time; nothing here lets a dev
  build reach a PC without that same explicit operator approval. A dev-channel build that
  broke the self-update loop would brick a real family PC exactly as a stable one would
  (ADR-0034's rejected-autonomous-update reasoning applies unchanged) — so dev artifacts pass
  through the **same publish gate** as stable: the image smoke test and the e2e round-trip
  against the exact release binary on Windows and Linux-x86_64, before anything is pushed.
- **Server image parallel:** `ghcr.io/nullthrone/kenny-server:edge` alongside the existing
  `:latest`/semver tags, built and smoke-tested by the same shared job — the server-side
  equivalent of a prerelease for `server_release.py`'s GHCR poll.
- **CI stays one artifact-producing definition.** `release.yml`'s three jobs move into a
  reusable `workflow_call` workflow parameterized by `version`/`prerelease`/`channel`; the
  existing tag-triggered `release.yml` and a new push-to-`main`-triggered `release-dev.yml`
  both call it. This avoids a fourth copy of the release-asset naming convention, which
  ADR-0015 already flags as duplicated three times.

### Consequences

- Good: the stable path is provably unchanged — `releases/latest` excludes prereleases by
  construction, so no new conditional logic can silently leak a dev build onto the stable
  path.
- Good: mixed-fleet operation is real, not simulated — channel is per-agent state, and
  campaign eligibility enforces it the same way `(os, arch)` eligibility already does.
- Good: no new trust boundary, no new repo, no new secrets — the reusable workflow reuses the
  existing code-signing cert and GHCR credentials via `secrets: inherit`.
- Good: does not reopen ADR-0034's rejected autonomous auto-update or ADR-0040's rejected
  "track latest" campaign shape — a dev candidate is detected automatically, but reaching a
  PC still requires the same named-artifact operator approval.
- Bad: every `main` push now costs a full release build (cross-compiled Linux targets,
  Windows codesign-capable build, e2e on two platforms) — materially more CI time/minutes
  than before. Mitigated with a path filter and `concurrency: cancel-in-progress`, not
  eliminated.
- Bad: the release list grows continuously without the cleanup step; that step is therefore
  load-bearing, not optional, and its failure mode (a runaway release list) is silent unless
  monitored.
- Bad: `release.yml`, the only working stable publish path, is being restructured into a
  reusable workflow at the same time this feature ships — regression risk on the one
  mechanism that must never break. Mitigated by keeping the reusable workflow's job bodies
  substantially identical to today's and dry-running it via `workflow_dispatch` before merge.

## More Information

- Builds on ADR-0012 (prebuilt binary distribution), ADR-0015 (tag leads the version,
  `releases/latest` resolution), ADR-0034 (Linux distribution + self-update — source of the
  rejected autonomous-auto-update option this decision must not reintroduce), ADR-0036 (the
  register+telemetry dual-reporting pattern this reuses for `channel`), ADR-0040 (pinned,
  operator-approved rollout campaigns — the mechanism a dev candidate feeds into unchanged).
- Implementation: `docs/protocol.md` + `docs/fixtures/register.json` (contract);
  `kenny-agent/build.rs`, `Cross.toml`, `src/protocol.rs`, `src/tunnel.rs`,
  `src/telemetry/collectors/os_support.rs` (agent); `kenny_server/agent_release.py`,
  `server_release.py`, `update_manager.py`, `store.py`, `distribution.py`, `changelog.py`,
  `registry.py`, `protocol.py` + the `webui/` Updates tab (server);
  `.github/workflows/_release-artifacts.yml` (new, reusable), `release.yml` (shrunk to the
  stable trigger), `release-dev.yml` (new, `main`-push trigger + cleanup).

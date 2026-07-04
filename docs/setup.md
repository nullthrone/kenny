# kenny — Setup & Operations

How to host **kenny-server**, configure it, build and distribute **kenny-agent**, and cut releases.
For day-to-day operator use, see [`user-guide.md`](user-guide.md).

## Topology

```mermaid
flowchart TB
  subgraph Cloud["Small cloud host (e.g. OCI Free Tier)"]
    Proxy["TLS proxy (Caddy)<br/>:443 https/wss"]
    subgraph Compose["docker compose"]
      Srv["kenny-server :8000"]
      Vol[("volume: /data<br/>kenny.sqlite")]
    end
    Proxy --> Srv
    Srv --- Vol
  end
  Op(("Operator<br/>browser / Claude")) -->|https| Proxy
  PC1["kenny-agent<br/>(Windows PC)"] -->|wss, dials out| Proxy
  PC2["kenny-agent<br/>(Windows PC)"] -->|wss, dials out| Proxy
  Anthropic["Anthropic API"] -. chat .- Srv
```

## Prerequisites

- A host with Docker + Docker Compose (server), reachable by the agents over TLS.
- A DNS name + TLS for production (the bundled Caddy profile can obtain certs automatically).
- For the dashboard chat: an `ANTHROPIC_API_KEY`.
- To build the agent / cut releases: a GitHub repo with Actions (the workflow targets `windows-latest`).

## Quick start (Docker Compose)

```bash
# from the repo root
KENNY_OPERATOR_TOKEN="$(openssl rand -hex 24)" \
KENNY_AGENT_TOKENS="example-pc=$(openssl rand -hex 16),example-laptop=$(openssl rand -hex 16)" \
ANTHROPIC_API_KEY="sk-ant-..." \
docker compose up --build -d
```

The server is now on `http://localhost:8000` (data persists on the `kenny-data` volume). Open `/`
and complete **first-run setup**: the first account you create becomes the **superuser**
(ADR-0037). From there a superuser manages accounts under the header user menu → *Users*
(roles `superuser` / `operator` / `user`, per-user host scope, and personal access tokens).
Claude authenticates to `/mcp` with a per-user access token (`Authorization: Bearer <pat>`);
the `KENNY_OPERATOR_TOKEN(S)` below stay accepted as a back-compat superuser so existing
installs upgrade with no manual steps. For TLS in front (port 443, `wss`), enable the Caddy profile:

```bash
KENNY_DOMAIN=kenny.example.com KENNY_OPERATOR_TOKEN=... docker compose --profile tls up -d
```

## Environment variables

| Variable | Used by | Default | Purpose |
|----------|---------|---------|---------|
| `KENNY_OPERATOR_TOKEN` | server | *insecure dev fallback* | Legacy shared bearer token; still accepted as a **back-compat superuser** for MCP + `/api` after the upgrade to accounts (ADR-0037). Deprecated in favour of per-user access tokens. |
| `KENNY_OPERATOR_TOKENS` | server | — | Optional comma-separated set of additional accepted shared tokens (each a back-compat superuser). |
| `KENNY_SESSION_TTL_SECS` | server | `604800` | Browser login session lifetime in seconds (default 7 days). |
| `KENNY_AGENT_TOKENS` | server | dev map | `id=token,id2=token2` — per-agent tokens (the token store is seeded from this). |
| `ANTHROPIC_API_KEY` | server | — | Enables the dashboard chat. |
| `KENNY_CHAT_MODEL` | server | `claude-sonnet-4-6` | Model for the chat loop. |
| `KENNY_TLS` | server | unset | Set `1` behind TLS so the login cookie gets the `Secure` flag. |
| `KENNY_PUBLIC_URL` | server | `http://localhost:<port>` | External base URL; used to build installer/update links and the agent `--server` `wss://…/agent/ws`. |
| `KENNY_AGENT_BINARY` | server | — | Path to the prebuilt `kenny-agent.exe` the server serves for installer download + self-update. Overrides the GitHub auto-fetch when set. |
| `KENNY_GITHUB_TOKEN` | server | — | GitHub token enabling auto-fetch of the agent binary from Releases (ADR-0015). When set (and `KENNY_AGENT_BINARY` is not), the server fetches `kenny-agent.exe` on startup. |
| `KENNY_GITHUB_REPO` | server | `t11z/kenny` | Repo to fetch the agent binary release from. |
| `KENNY_AGENT_BINARY_CACHE` | server | `<dir of KENNY_DB_PATH>/kenny-agent.exe` | Where the auto-fetched binary is cached (the `/data` volume in the container). |
| `KENNY_AGENT_VERSION` | server | `0.2.0` | **Fallback** version label only — the GitHub release tag of the fetched binary leads (ADR-0015). Used when no tag is known (e.g. a manually-placed binary without a `.version` sidecar). |
| `KENNY_HOST` / `KENNY_PORT` | server | `127.0.0.1` / `8000` | Bind address (container sets `0.0.0.0`). |
| `KENNY_DB_PATH` | server | `kenny.sqlite` | SQLite store (snapshots, events, tokens, keys, chat, web filter — one file). Container: `/data/kenny.sqlite`. |
| `KENNY_TELEMETRY_INTERVAL_SECS` | agent / server | `900` | Agent push interval; also pre-filled into generated installers. |
| `KENNY_SERVER_VERSION` | server | `0.0.0-dev` | Version string shown in the **About** box and `/api/about`. |
| `KENNY_LOG_LEVEL` | server | `INFO` | Root log level. Server logs are also persisted to the event store (ADR-0017). |

**Agent authentication & identity** (ADR-0023 mutual Ed25519 auth, token rotation):

| Variable | Default | Purpose |
|----------|---------|---------|
| `KENNY_SERVER_PRIVATE_KEY` | *generated + logged* | Base64 32-byte Ed25519 seed — the server identity agents pin. If unset the server generates one at startup and logs the public key to pin in installers; set it (or `KENNY_SERVER_PRIVATE_KEY_FILE`) to keep identity stable across restarts. |
| `KENNY_TOKEN_GRACE_SECS` | `604800` | Grace window (7 d) during which a rotated agent token still works; `0` = instant invalidation. |
| `KENNY_KEY_GRACE_SECS` | `604800` | Grace window for a rotated agent public key. |
| `KENNY_ALLOW_TOKEN_AUTH` | `1` | Accept the legacy bearer-token agent handshake during migration (disable once all agents are enrolled). |
| `KENNY_MAX_FRAME_BYTES` | `8388608` | Absolute inbound frame ceiling (8 MiB). |
| `KENNY_MAX_TELEMETRY_BYTES` | `262144` | Per-push byte cap (256 KiB). |
| `KENNY_MAX_TELEMETRY_SECTIONS` | `128` | Max sections per snapshot. |

**Alerting, digest & notifications** (see **[Alerting & digests](alerting.md)**):

| Variable | Default | Purpose |
|----------|---------|---------|
| `KENNY_ALERT_INTERVAL_SECS` | `60` | Alert-evaluation loop interval; `0` disables alerting. |
| `KENNY_ALERT_COOLDOWN_SECS` | `3600` | Per-scope flap-suppression cooldown for `warn` transitions. |
| `KENNY_ALERT_OFFLINE_AFTER_SECS` | `2700` | Mark an agent offline after this long without a push (≈ three missed 15-min pushes). |
| `KENNY_DIGEST_ENABLED` | `1` | Weekly digest on/off. |
| `KENNY_DIGEST_DAY` / `KENNY_DIGEST_HOUR` | `mon` / `8` | When to send the weekly digest. |
| `KENNY_NTFY_URL` / `KENNY_NTFY_TOKEN` | — | ntfy topic URL (+ optional bearer) for push alerts. |
| `KENNY_WEBHOOK_URL` | — | Generic JSON webhook for alerts. |

**Parental controls / web filter** (see **[Parental controls](parental-controls.md)**):

| Variable | Default | Purpose |
|----------|---------|---------|
| `KENNY_WEBFILTER_REFRESH_SECS` | `86400` | External adult/bypass list refresh interval; `0` disables. |
| `KENNY_WEBFILTER_ADULT_URL` | StevenBlack list | Source URL for the porn-only hosts list. |
| `KENNY_WEBFILTER_BYPASS_URL` | hagezi list | Source URL for the DoH/VPN/proxy bypass list. |
| `KENNY_WEBFILTER_MAX_BLOCK_DOMAINS` | `5000` | Cap on external-adult domains pushed to an agent (hard cap 10 000). |

> **Security:** if `KENNY_OPERATOR_TOKEN` is unset the server uses a loud, insecure dev token. Always
> set real tokens and serve over `wss`/`https` for anything non-local. See
> [`adr/0008-operator-authentication.md`](adr/0008-operator-authentication.md) and
> [`adr/0014-auth-hardening.md`](adr/0014-auth-hardening.md).

## Running from source (development)

```bash
# server
cd kenny-server && pip install -e ".[dev]"
KENNY_OPERATOR_TOKEN=dev KENNY_HOST=127.0.0.1 KENNY_PORT=8000 kenny-server

# agent (foreground; Linux builds via cfg fallbacks)
cd kenny-agent && cargo run -- --server ws://127.0.0.1:8000/agent/ws \
  --agent-id dev --token dev-token --telemetry-interval-secs 30
```

`/e2e` runs a real agent↔server smoke test; `/contract-check` verifies both sides match the wire
contract and golden fixtures.

## Enabling agent downloads from the GUI

The server serves a **prebuilt** binary; it does not compile per download (see
[`adr/0012-agent-distribution-prebuilt-binary.md`](adr/0012-agent-distribution-prebuilt-binary.md)). Point it at a binary and set the
public URL:

```yaml
# compose.yaml (server service) — excerpt
environment:
  KENNY_PUBLIC_URL: https://kenny.example.com
  KENNY_AGENT_BINARY: /data/kenny-agent.exe   # mount/copy the release artifact here
  KENNY_AGENT_VERSION: "0.2.0"
```

Then the dashboard's **download installer** / **share link** / **update agent** buttons work. The
installer bundles `setup.bat` + a `kenny-agent.setup.json` sidecar carrying the per-agent
`--server`, `--agent-id`, a minted one-time `--enroll-token`, and the pinned `--server-pubkey`. The
relative double-clicks `setup.bat`; the agent self-elevates and installs itself into
`%ProgramFiles%\kenny` (see
[`adr/0033-agent-self-elevating-bootstrap-installer.md`](adr/0033-agent-self-elevating-bootstrap-installer.md)).

### Auto-fetch from GitHub (no manual binary placement)

To avoid the first-agent chicken-and-egg (hand-placing the `.exe` into the volume before any
installer can be downloaded), the server can fetch the binary itself when a GitHub token is
configured (ADR-0015):

```yaml
environment:
  KENNY_GITHUB_TOKEN: ${KENNY_GITHUB_TOKEN}   # a token with read access to releases
  KENNY_GITHUB_REPO: t11z/kenny               # default
```

On startup (and via the dashboard's **retry GitHub fetch** button) the server downloads the latest
release's `kenny-agent-<tag>-x86_64-pc-windows-msvc.exe`, verifies it against the published
`.sha256`, and caches it at `/data/kenny-agent.exe`. The fetch is **best-effort** — if the repo is
unreachable or no token is set, the dashboard shows a banner with manual instructions instead. An
operator-placed `KENNY_AGENT_BINARY` always wins over the fetched cache. The dashboard's **Add a
PC** control lets you download an installer for the very first machine without a pre-existing agent.

## Installing the agent on Windows

The normal path is the dashboard bundle: **double-click `setup.bat` and approve the Windows
security prompt**. `setup` reads `kenny-agent.setup.json`, elevates via UAC, copies the binary into
`%ProgramFiles%\kenny`, and registers the auto-start service — no unzip-and-right-click ritual (see
[`adr/0033-agent-self-elevating-bootstrap-installer.md`](adr/0033-agent-self-elevating-bootstrap-installer.md)
and [`adr/0013-agent-windows-service-and-self-update.md`](adr/0013-agent-windows-service-and-self-update.md)).

The single binary manages its own service. For manual/debugging use:

```powershell
kenny-agent.exe setup              # self-elevating install (config from kenny-agent.setup.json,
                                   #   or pass the flags below explicitly)

# equivalent explicit install (run as Administrator):
kenny-agent.exe install --server wss://kenny.example.com/agent/ws `
  --agent-id example-pc --server-pubkey <base64> --enroll-token <token> `
  [--telemetry-interval-secs 900] [--service-name kenny-agent]

kenny-agent.exe uninstall          # remove the service (and the %ProgramFiles%\kenny install dir)
kenny-agent.exe run                # foreground (default when no subcommand) — for debugging
```

`--server-pubkey` pins the server identity and `--enroll-token` is the one-time enrollment secret
(ADR-0023); a bare legacy `--token` is only accepted during the migration window. `install` writes
`kenny-agent.config.json` next to the exe and registers an auto-start service with
restart-on-failure recovery. Updates are pushed from the server (no manual reinstall).

## Releases (GHCR image + Windows binary)

Tag a version to publish (`.github/workflows/release.yml`):

```bash
git tag v0.2.0 && git push origin v0.2.0
```

```mermaid
flowchart LR
  Tag["git tag v*"] --> RW["release.yml"]
  RW --> Img["server image →<br/>ghcr.io/&lt;owner&gt;/kenny-server:&lt;tag&gt;"]
  RW --> Exe["kenny-agent.exe<br/>(windows-latest)"]
  Exe --> Sha["+ SHA256"]
  Exe --> Sign["+ Authenticode<br/>(if cert secret set)"]
  Sha & Sign --> Rel["GitHub Release asset"]
```

- The server image lands in GHCR (semver + `latest`, with provenance).
- The agent binary is built on `windows-latest`, hashed, optionally code-signed when
  `WINDOWS_CERT_BASE64` / `WINDOWS_CERT_PASSWORD` repo secrets are set, and attached to the Release.
- Pull the release `kenny-agent.exe` to the host and point `KENNY_AGENT_BINARY` at it to enable GUI
  downloads/updates against that version.

## Persistence, backups, upgrades

- **Data**: the SQLite telemetry store lives on the `kenny-data` volume (`/data`). Back it up by
  snapshotting the volume / copying `kenny.sqlite`. Snapshots auto-prune after ~30 days.
- **Server upgrade**: `docker compose pull && docker compose up -d` (or bump the image tag).
- **Agent upgrade**: use **update agent** in the dashboard (server-triggered self-update).

## Dependencies & security automation

- Dependabot (`.github/dependabot.yml`) opens weekly update PRs for pip, cargo, GitHub Actions, and
  the Docker base image.
- Run `/security-review` to audit kenny's weak points and file deduplicated GitHub issues.

## CI

`.github/workflows/ci.yml` runs the server tests + lint, the agent `fmt`/`clippy`/`test`/`build`, a
Windows job for the `#[cfg(windows)]` code, and a real agent↔server `e2e` job on every PR.

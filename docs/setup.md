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
  LX["kenny-agent<br/>(Linux host)"] -->|wss, dials out| Proxy
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
Claude Desktop connects to `/mcp` through kenny's built-in **OAuth 2.1** flow
([ADR-0041](adr/0041-oauth2-authorization-server-for-mcp.md)): add a custom connector with the
`https://<server>/mcp` URL, sign in with your kenny account, and approve once — no token to paste.
Scripts and other MCP clients can still send a per-user access token (`Authorization: Bearer <pat>`)
instead. The `KENNY_OPERATOR_TOKEN(S)` below stay accepted as a back-compat superuser so existing
installs upgrade with no manual steps. For TLS in front (port 443, `wss`), enable the Caddy profile:

```bash
KENNY_DOMAIN=kenny.example.com KENNY_OPERATOR_TOKEN=... docker compose --profile tls up -d
```

Behind a reverse proxy, set `KENNY_FORWARDED_ALLOW_IPS` to the proxy's address so the
login rate-limiter throttles by the real client IP rather than the proxy's (otherwise all
clients share one bucket). The bundled TLS profile sets this for you.

## Environment variables

| Variable | Used by | Default | Purpose |
|----------|---------|---------|---------|
| `KENNY_OPERATOR_TOKEN` | server | *insecure dev fallback* | Legacy shared bearer token; still accepted as a **back-compat superuser** for MCP + `/api` after the upgrade to accounts (ADR-0037). Deprecated in favour of per-user access tokens. |
| `KENNY_OPERATOR_TOKENS` | server | — | Optional comma-separated set of additional accepted shared tokens (each a back-compat superuser). |
| `KENNY_SESSION_TTL_SECS` | server | `604800` | Browser login session lifetime in seconds (default 7 days). |
| `KENNY_OAUTH_ACCESS_TTL_SECS` | server | `3600` | Lifetime of an OAuth access token issued to a connected MCP client (default 1 hour). |
| `KENNY_OAUTH_REFRESH_TTL_SECS` | server | `2592000` | Lifetime of a rotating OAuth refresh token (default 30 days); reuse of a rotated token revokes the whole grant. |
| `KENNY_AGENT_TOKENS` | server | dev map | `id=token,id2=token2` — per-agent tokens (the token store is seeded from this). |
| `ANTHROPIC_API_KEY` | server | — | Enables the dashboard chat. |
| `KENNY_CHAT_MODEL` | server | `claude-sonnet-4-6` | Model for the chat loop. |
| `KENNY_TLS` | server | unset | Set `1` behind TLS so the login cookie gets the `Secure` flag. |
| `KENNY_FORWARDED_ALLOW_IPS` | server | `127.0.0.1` | Upstream proxy address(es) allowed to set `X-Forwarded-For`, so the login rate-limiter sees the real client IP behind a reverse proxy (not the proxy's). Set to your proxy's address when fronting kenny with the Caddy TLS profile. |
| `KENNY_PUBLIC_URL` | server | `http://localhost:<port>` | External base URL; used to build installer/update links, the agent `--server` `wss://…/agent/ws`, and the **OAuth** issuer / discovery-metadata / resource URLs. Set it to your public `https://…` origin so Claude Desktop's OAuth flow advertises reachable endpoints. |
| `KENNY_AGENT_BINARY` | server | — | Path to the prebuilt `kenny-agent.exe` the server serves for **Windows** installer download + self-update. Overrides the GitHub auto-fetch when set. |
| `KENNY_AGENT_BINARY_LINUX` | server | — | Path to the prebuilt **Linux** `x86_64` agent binary (static musl) the server serves for the Linux install script + self-update. Overrides the GitHub auto-fetch when set. |
| `KENNY_AGENT_BINARY_LINUX_AARCH64` | server | — | As above for **Linux `aarch64`** (Raspberry Pi / ARM NAS). |
| `KENNY_GITHUB_TOKEN` | server | — | GitHub token enabling auto-fetch of the agent binary from Releases (ADR-0015). When set (and `KENNY_AGENT_BINARY` is not), the server fetches `kenny-agent.exe` on startup. |
| `KENNY_GITHUB_REPO` | server | `t11z/kenny` | Repo to fetch the agent binary release from. |
| `KENNY_AGENT_BINARY_CACHE` | server | `<dir of KENNY_DB_PATH>/kenny-agent.exe` | Where the auto-fetched binary is cached (the `/data` volume in the container). |
| `KENNY_AGENT_VERSION` | server | `0.2.0` | **Fallback** version label only — the GitHub release tag of the fetched binary leads (ADR-0015). Used when no tag is known (e.g. a manually-placed binary without a `.version` sidecar). |
| `KENNY_HOST` / `KENNY_PORT` | server | `127.0.0.1` / `8000` | Bind address (container sets `0.0.0.0`). |
| `KENNY_DB_PATH` | server | `kenny.sqlite` | SQLite store (snapshots, events, tokens, keys, chat, web filter — one file). Container: `/data/kenny.sqlite`. |
| `KENNY_TELEMETRY_INTERVAL_SECS` | agent / server | `900` | Agent push interval; also pre-filled into generated installers. |
| `KENNY_COEXIST_ENABLED` | agent | `1` | Anti-cheat coexistence (ADR-0039): while a protected game runs, the agent suspends `screen_capture` (returns `paused`) and relaxes process/port telemetry. Set `0` to disable. |
| `KENNY_COEXIST_PROCESSES` | agent | anti-cheat set | Comma-separated extra process names to treat as "a protected game is running", extending the built-in anti-cheat list (`EasyAntiCheat.exe`, `BEService*.exe`, …). Add game exes here, e.g. `ARC-Raiders.exe`. Matched case- and `.exe`-insensitively. |
| `KENNY_COEXIST_POLL_SECS` | agent | `5` | How often the agent checks whether a watched process is running. |
| `KENNY_COEXIST_TELEMETRY_INTERVAL_SECS` | agent | `3600` | Telemetry push interval while a protected game is running (never shorter than `KENNY_TELEMETRY_INTERVAL_SECS`). |
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

The **Add a PC** panel has an OS selector. For a **Linux** target, point the server at a Linux
binary as well and the panel produces a one-line install command instead of a ZIP (see
[Installing the agent on Linux](#installing-the-agent-on-linux) and
[`adr/0038-linux-agent-distribution-convenience-script.md`](adr/0038-linux-agent-distribution-convenience-script.md)):

```yaml
environment:
  KENNY_AGENT_BINARY_LINUX: /data/kenny-agent-linux-x86_64          # static musl x86_64
  KENNY_AGENT_BINARY_LINUX_AARCH64: /data/kenny-agent-linux-aarch64 # optional, Raspberry Pi / ARM NAS
```

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
release's agent binaries — `kenny-agent-<tag>-x86_64-pc-windows-msvc.exe` and the Linux
`…-<arch>-unknown-linux-musl` variants — verifies each against its published `.sha256`, and caches
them on the `/data` volume. The fetch is **best-effort** and per-asset — if the repo is unreachable
or no token is set, the dashboard shows a banner with manual instructions instead. Operator-placed
`KENNY_AGENT_BINARY` / `KENNY_AGENT_BINARY_LINUX` always win over the fetched cache. The dashboard's
**Add a PC** control lets you onboard the very first machine without a pre-existing agent.

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

## Installing the agent on Linux

kenny-agent runs on Linux hosts too — headless servers, a NAS, a Raspberry Pi, or a Linux desktop
— reporting into the same fleet through the same server (ADR-0035). The agent is a **static musl
binary** with no runtime dependencies, installed as a **systemd service**. Distribution follows the
Docker/K3s convenience-script model (ADR-0038).

The normal path is the dashboard's **one-line install command**. In **Add a PC**, pick *Linux*,
enter an agent id, and copy the command it produces — then run it on the target host as root:

```bash
curl -fsSL https://kenny.example.com/d/install/<nonce> | sudo sh
```

The nonce-gated, single-use script carries the per-agent `--server`, `--agent-id`, a minted
one-time `--enroll-token`, and the pinned `--server-pubkey`. It detects the CPU architecture
(`x86_64` / `aarch64`), downloads the matching binary from the server, and runs `kenny-agent
setup`, which copies the binary into `/opt/kenny`, writes its config to `/etc/kenny`, and enables
an auto-start systemd unit. On first run the agent generates its Ed25519 keypair and enrolls its
public key. Verify with:

```bash
systemctl status kenny-agent          # unit active, ExecStart=/opt/kenny/kenny-agent run-service
journalctl -u kenny-agent -f          # follow the agent log
```

The single binary manages its own service. For a manual / air-gapped install, download
`kenny-agent-<tag>-<arch>-unknown-linux-musl` from the
[latest release](https://github.com/t11z/kenny/releases/latest), then (as root):

```bash
chmod +x kenny-agent-*-unknown-linux-musl
sudo ./kenny-agent-*-unknown-linux-musl setup \
  --server wss://kenny.example.com/agent/ws --agent-id study-pi \
  --server-pubkey <base64> --enroll-token <token> [--telemetry-interval-secs 900]

sudo kenny-agent uninstall             # disable + remove the systemd unit
```

Paths on Linux: binary in `/opt/kenny`, config in `/etc/kenny`, state/key in `/var/lib/kenny`,
logs in `/var/log/kenny`. There is no tray kill-switch or session-0/desktop launch on Linux (those
are Windows-only, ADR-0035); a headless server needs neither.

**Upgrades are server-triggered, exactly like Windows** — click **update** on the agent in the
dashboard (or `POST /api/agents/{id}/update`). The agent downloads the new binary, verifies its
SHA-256, atomically swaps `/opt/kenny/kenny-agent`, and restarts its systemd unit; no manual step
on the host (ADR-0038).

## Releases (GHCR image + agent binaries)

Tag a version to publish (`.github/workflows/release.yml`):

```bash
git tag v0.2.0 && git push origin v0.2.0
```

```mermaid
flowchart LR
  Tag["git tag v*"] --> RW["release.yml"]
  RW --> Img["server image →<br/>ghcr.io/&lt;owner&gt;/kenny-server:&lt;tag&gt;"]
  RW --> Exe["kenny-agent.exe<br/>(windows-latest)"]
  RW --> Lnx["kenny-agent musl<br/>(x86_64 + aarch64)"]
  Exe --> Sha["+ SHA256"]
  Exe --> Sign["+ Authenticode<br/>(if cert secret set)"]
  Lnx --> LSha["+ SHA256"]
  Sha & Sign & LSha --> Rel["GitHub Release asset"]
```

- The server image lands in GHCR (semver + `latest`, with provenance).
- The Windows agent binary is built on `windows-latest`, hashed, optionally code-signed when
  `WINDOWS_CERT_BASE64` / `WINDOWS_CERT_PASSWORD` repo secrets are set, and attached to the Release.
- The Linux agent binaries are cross-built as **static musl** artifacts (`cross`),
  `kenny-agent-<tag>-x86_64-unknown-linux-musl` and `…-aarch64-unknown-linux-musl`, each hashed and
  attached to the Release. The x86_64 build is e2e-gated before publish.
- Pull the release binaries to the host and point `KENNY_AGENT_BINARY` (Windows) and
  `KENNY_AGENT_BINARY_LINUX` / `KENNY_AGENT_BINARY_LINUX_AARCH64` (Linux) at them to enable GUI
  downloads/updates against that version. When `KENNY_GITHUB_TOKEN` is set the server auto-fetches
  all of them.

### Code signing (Authenticode)

An unsigned agent binary is more likely to be flagged by AV and game anti-cheat heuristics, and
carries no verifiable publisher identity. The Windows build already carries PE identity metadata
(CompanyName/ProductName/version + icon, ADR-0039); **signing it is the complementary step** and
is wired but off by default:

- Set the `WINDOWS_CERT_BASE64` (base64 of the signing cert) and `WINDOWS_CERT_PASSWORD` repo
  secrets; `release.yml` then Authenticode-signs `kenny-agent.exe` with a timestamp. The server
  ships the exe **unmodified** (ADR-0033/0012), so the signature survives distribution and
  self-update.
- Use a real **OV or EV** code-signing certificate whose subject matches the VERSIONINFO
  CompanyName. Since the 2023 CA/Browser-Forum change, code-signing keys must live on
  FIPS-140-2 hardware, so a plain PFX-in-secret may need swapping for a cloud-signing service
  (e.g. Azure Trusted Signing, DigiCert KeyLocker, SSL.com eSigner) invoked via `signtool`.
- A formal anti-cheat *allowlist* is generally not available to a self-hosted family tool;
  signing + the coexistence back-off (ADR-0039) are the practical levers. Never attempt to
  evade an anti-cheat — that risks banning the player's game account.

## Persistence, backups, upgrades

- **Data**: the SQLite telemetry store lives on the `kenny-data` volume (`/data`). Back it up by
  snapshotting the volume / copying `kenny.sqlite`. Snapshots auto-prune after ~30 days.
- **Server upgrade**: `docker compose pull && docker compose up -d` (or bump the image tag).
- **Agent upgrade**: use **update agent** in the dashboard (server-triggered self-update) — on both
  Windows and Linux (ADR-0038).

## Dependencies & security automation

- Dependabot (`.github/dependabot.yml`) opens weekly update PRs for pip, cargo, GitHub Actions, and
  the Docker base image.
- Run `/security-review` to audit kenny's weak points and file deduplicated GitHub issues.

## CI

`.github/workflows/ci.yml` runs the server tests + lint, the agent `fmt`/`clippy`/`test`/`build`, a
Windows job for the `#[cfg(windows)]` code, and a real agent↔server `e2e` job on every PR.

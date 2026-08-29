# 0010. Containerization (Docker/Compose) and GHCR

- Status: accepted
- Date: 2026-06-04

## Context and Problem Statement

`kenny-server` needs a reproducible, low-ops way to run on a small cloud host (e.g.
OCI Free Tier) and to ship released versions. The agent ships as a native Windows
binary (ADR-0012), so this decision is about the server and the registry.

## Considered Options

- Containerize the server (Dockerfile + Compose), publish images to **GHCR**.
- Ship a bare Python package and run under a process manager (systemd) on the host.
- A full PaaS.

## Decision Outcome

Chosen option: "Containerize + GHCR". A `python:3.11-slim` image installs the
`kenny-server` package and runs the `kenny-server` entrypoint; `compose.yaml` wires
the operator/agent/Anthropic env, persists the SQLite store on a named volume, and
offers an optional Caddy reverse-proxy profile for TLS termination (so agents dial
`wss://` and operators use `https://`, per ADR-0008). Release images are published to
`ghcr.io/nullthrone/kenny-server` by the release workflow (ADR-0012/WS4).

### Consequences

- Good, because deploys are `docker compose up`; the SQLite store persists on a volume.
- Good, because GHCR ties image tags to git tags and needs no extra registry account.
- Good, because TLS is a swappable front (Caddy profile) rather than baked into the app.
- Bad, because the app image serves plain HTTP; TLS must be provided by the proxy/profile
  or the platform — documented, and enforced by the `secure` cookie flag under TLS.

## More Information

- `webui/index.html` is shipped as package data (`[tool.setuptools.package-data]`) so the
  dashboard works in the non-editable container install.
- Build context is the repo root (`docker build -f kenny-server/Dockerfile .`) so the
  Dockerfile can copy the server package; `.dockerignore` keeps the context small.
- The release image is a multi-arch manifest (`linux/amd64` + `linux/arm64`) so it runs
  on the OCI Free Tier (Ampere/arm64) host as well as amd64; the release workflow builds
  arm64 via QEMU and smoke-tests the runner-native amd64 image before publishing.

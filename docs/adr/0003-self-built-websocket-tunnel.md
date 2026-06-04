# 0003. Self-built WebSocket/HTTPS tunnel

- Status: accepted
- Date: 2026-06-04

## Context and Problem Statement

The server must reach agents that sit behind home routers (NAT, no port forwarding).
We need a transport for forwarding MCP-driven tool calls and receiving telemetry.

## Considered Options

- Self-built persistent WebSocket (WSS) tunnel
- Cloudflare Tunnel (`cloudflared`) + Cloudflare Access
- Tailscale (agents and server on one tailnet)

## Decision Outcome

Chosen option: "Self-built WebSocket tunnel". The agent opens one outbound WSS
connection; the server multiplexes `request`/`response` and `telemetry` frames over
it. This keeps us free of third-party install/onboarding on each relative's PC and
gives full control over framing, auth, and reconnect.

### Consequences

- Good, because nothing extra to install on the target; one outbound 443 connection.
- Good, because framing is ours, so telemetry push and tool routing share one channel.
- Bad, because we own reconnect/backoff, heartbeats, and TLS termination ourselves.

## More Information

Tailscale/Cloudflare Access remain documented fallbacks for network-layer hardening
([ADR backlog]); the Engram note lists them as auth options.

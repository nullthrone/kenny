# 0004. Agent initiates the outbound connection

- Status: accepted
- Date: 2026-06-04

## Context and Problem Statement

Who dials whom? The target PCs are behind NAT/firewalls and run in untrusted home
networks; the server has a stable public address.

## Considered Options

- Agent dials server (outbound)
- Server dials agent (inbound, requires port forwarding / WinRM)

## Decision Outcome

Chosen option: "Agent dials server". The agent makes a single outbound WSS
connection and the server pushes `request` frames down it. No inbound ports, no
router configuration, no WinRM.

### Consequences

- Good, because zero firewall/router setup for relatives.
- Good, because the agent runs in the user's own session — screenshots and
  user-context actions work without remote-session blackout.
- Bad, because the server can only reach agents that are currently connected; offline
  agents are surfaced as such in the registry and dashboard.

## More Information

Pairs with [ADR-0003](0003-self-built-websocket-tunnel.md).

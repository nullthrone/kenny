# 0002. Python server, Rust agent

- Status: accepted
- Date: 2026-06-04

## Context and Problem Statement

kenny has two components: a cloud-hosted server that is the stable MCP endpoint for
Claude plus the operator web UI, and an agent that runs on family members' Windows
PCs. The two have very different constraints.

## Considered Options

- Python (FastMCP) for both
- Rust for both
- Python server + Rust agent

## Decision Outcome

Chosen option: "Python server + Rust agent". The server benefits from fast iteration
and FastMCP's batteries-included MCP + ASGI stack; the agent benefits from shipping
as a single static binary with no runtime dependency on the target machine and
first-class Win32 access via `windows-rs`.

### Consequences

- Good, because the agent is a drop-in `.exe` for non-technical relatives.
- Good, because server features (tools, dashboard) move quickly in Python.
- Bad, because the wire contract spans two languages and can drift — mitigated by
  [ADR-0005](0005-contract-first-with-golden-fixtures.md).

## More Information

Prototype-in-Python / production-in-Rust was considered per the Engram note; we split
by component instead of by phase because the server stays Python long-term.

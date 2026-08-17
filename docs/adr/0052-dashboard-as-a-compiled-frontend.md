# 0052. Build the dashboard as a compiled frontend shipped in the wheel

- Status: accepted
- Boundary moved: the distribution shape of the dashboard — the web UI stops being a
  hand-edited source file served verbatim as package data and becomes a build artifact
  produced by a Node toolchain at release time.
- Date: 2026-08-17
- Touches: [ADR-0010](0010-containerization-and-ghcr.md),
  [ADR-0009](0009-server-hosted-claude-chat.md),
  [ADR-0045](0045-tiered-tool-classification.md)

## Context and Problem Statement

The dashboard is one 390 KB `webui/index.html` holding inline CSS, a hash router, a mutable
`state` object and view functions that assemble HTML from template strings and assign it to
`innerHTML`. It is shipped verbatim as package data, so a self-hoster needs no toolchain
beyond Python.

That property is worth keeping. The rest of it no longer holds up. Correctness in this file
depends on every interpolation reaching `esc()`, and the surface that matters most —
the approval gate that renders a state-changing tool call before an operator authorises it
(ADR-0045) — is exactly where a missed escape is most expensive: the operator must see the
arguments that will actually execute. Escaping discipline enforced by review across 6757
lines is a weaker guarantee than escaping enforced by construction.

The console is also no longer document-shaped. It carries hash routing, a context-scoped
chat drawer, three modals, a multi-step wizard, filter chips, an SSE stream that interleaves
auto-run results with confirmation gates, and live rollout progress. Manual DOM assembly
against a shared mutable `state` is why the `navToken` stale-render guard exists at all.

## Considered Options

- Keep the single file; split it into native ES modules to bound its size.
- Compile a React + TypeScript application with Vite, shipped prebuilt.
- Compile with a smaller runtime (Preact, Svelte) on the same build pipeline.

## Decision Outcome

Chosen option: "React + TypeScript, built with Vite", because escaping by construction and a
typed boundary against `/api` are the two properties this surface most needs, and both come
from the compiler rather than from review. Splitting into ES modules would have bounded the
file's size without addressing either. A smaller runtime buys bundle bytes on a dashboard
that is already served from the same host as the data it renders.

Sources live in `kenny-web/`. The build emits to `kenny_server/webui/dist/`, which the
existing root mount serves and `[tool.setuptools.package-data]` carries into the wheel, so
`pip install kenny-server` and the container image both keep working with no Node present.
CI performs the build before packaging; `dist/` is not committed. A source checkout without
a build fails with an instruction to run it, rather than serving a blank page.

### Consequences

- Good, because a value can no longer reach the DOM as markup by omission, and the approval
  gate renders operator-facing arguments through a typed component rather than string
  concatenation.
- Good, because `/api` response shapes are stated once in TypeScript and checked at compile
  time, which makes a server-side field rename a build failure instead of an empty panel.
- Good, because end users are unaffected: the wheel and the image still contain a ready UI.
- Bad, because contributors who touch the dashboard now need Node, and release depends on a
  build step that can fail on its own.
- Bad, because the served asset is no longer readable at the point of service; debugging a
  production page means mapping back to sources.

## More Information

- The seam between the compiled asset and the server that serves it is covered by a test
  asserting the mount resolves the built entry point, so a packaging change that drops
  `dist/` fails the suite rather than the browser.
- The information architecture changed in the same release (eleven destinations to five,
  Ask kenny from page to global drawer). That is UI layout, not a boundary — it lives in the
  commit message and in `docs/dashboard.md`.

# 0018. Dashboard top-level navigation: fleet vs. fleet-wide activity

- Status: accepted
- Date: 2026-06-06

## Context and Problem Statement

The operator dashboard (`kenny-server/kenny_server/webui/index.html`) grew as a single
vertical page: stats, the Claude chat, the fleet list with a host-detail panel, and —
appended at the bottom — the tool-call audit log and the events/logs panel (added in
ADR-0017). The two log panels are **fleet-wide** observability surfaces: `/api/audit`
and `/api/events` return entries across all hosts, not the currently selected one. They
answer a different question than the host-centric part of the page ("what happened
across the fleet" vs. "how is this one PC"), yet they sat in the same scroll document
with no navigation, lengthening it for everyone regardless of intent.

Should these fleet-wide logs keep living under the host-centric console, or move into
their own top-level destination?

## Considered Options

- **Add a top-level tab navigation** (Fleet vs. Activity), moving the audit and event
  logs into an "Activity" tab with its own left-hand sub-navigation, addressable via
  `location.hash`.
- **Keep the single scrolling page**, optionally collapsing the log panels.
- **Scope the logs to the selected host** and embed them in the host-detail view.

## Decision Outcome

Chosen option: a top-level tab navigation, because the audit and event logs are
conceptually fleet-wide and deserve a peer destination to the host-centric console
rather than being buried beneath it. Concretely:

- Two tabs in the header: **Fleet** (stats, chat, fleet list, host detail) and
  **Activity** (the fleet-wide audit + event logs).
- The Activity tab uses a master/detail layout mirroring the fleet list: a left
  sub-navigation selects between the **tool-call audit log** and **events & logs**,
  rendered on the right via the existing, unchanged panel/render functions.
- Navigation is a minimal hash router (`#/fleet`, `#/activity/audit`,
  `#/activity/events`) — no framework, consistent with the dependency-light vanilla-JS
  page. Tabs are bookmarkable and survive reload; a bare URL normalizes to `#/fleet`.
- The change is frontend-only: the `/api/*` routes, the wire contract, and the server
  remain untouched. Fleet data still backs the header roll-up in both tabs; the
  fleet-wide logs are lazy-loaded on entry to the Activity tab.

### Consequences

- Good, because the host-centric console is shorter and focused, and fleet-wide
  observability has a clear, linkable home.
- Good, because it introduces a navigation paradigm the dashboard can grow into without
  new dependencies.
- Neutral, because the host-centric and fleet-wide views are now one click apart instead
  of one scroll; per-host log drill-down (filtering Activity by a selected host) is left
  as a possible follow-up.

## More Information

Builds on ADR-0017 (the audit/event store and the original fleet-wide logs panel).
Implemented entirely in `kenny-server/kenny_server/webui/index.html`.

# kenny docs / screenshots / CI map — IA + Nullthrone renewal

Inventory only. Nothing outside this file was edited. Produced to drive the
rewrite of `docs/` for the eleven→five IA consolidation (Overview+badge→**Today**,
Fleet+Add-a-PC→**Fleet**, Flagged+Tickets+Approvals→**Inbox**,
Activity+Events+logs→**Log**, Settings+Users+Profile+Updates→**Admin**, Ask
kenny page→global ⌘K overlay) and the "Nullthrone" visual system.

Legend used in the tables below:
- **NOOP** — matched the grep keyword but is not a navigation/IA reference (a
  generic English word, an MCP tool name, or a telemetry field name). Left in
  the table on purpose so the exhaustiveness of the sweep is auditable —
  these do **not** need to change for the IA work.
- Everything else names the concrete rewrite.

---

## 1. Doc content inventory

### docs/index.md

| Line | Excerpt | Becomes |
|---|---|---|
| 11 | `![The kenny fleet console](assets/screenshots/overview.png)` | Hero shot must be re-captured as the **Today** page (see §2); filename can stay `overview.png` or be renamed `today.png` — pick one and update this embed either way. |
| 12 | `<figcaption>The fleet console — a high-level overview with drill-down...` | "fleet console" here actually means the old Overview tab, not the Fleet tab — rewrite as "The Today page" to avoid colliding with the real Fleet destination. |
| 55 | `**[Parental controls](parental-controls.md)** — web activity,` | NOOP — `web activity` is the telemetry section name, not the Activity tab. |

### docs/setup.md

| Line | Excerpt | Becomes |
|---|---|---|
| 139 | `**Discord bot & tickets** (see **[Tickets & the Discord bot](itsm.md)**` | Ticket entity/itsm.md concept survives; only the *tab* that surfaces it changes (→ Inbox). Heading text can stay. |
| 141–142 | `...dashboard's Tickets tab and \`/api/tickets\`` | "Tickets tab" → **Inbox** (tickets appear grouped under NEEDS YOU/WAITING/WORKING/DONE). |
| 156 | rate-limit row: "opening/driving tickets" | NOOP — describes the ticket entity, not the UI tab. |
| 165 | `editable from the dashboard's **Settings** tab, under the **Discord & Tickets** group` | Settings tab → **Admin**; "Discord & Tickets" becomes an Admin section-nav entry. |
| 168 | `settings on this page.` | Same Settings→Admin rename as context. |
| 211 | `The **Add a PC** panel has an OS selector.` | Add a PC panel → **3-step modal wizard** launched from Fleet. |
| 240 | `**Add a PC** control lets you onboard the very first machine` | Same — modal wizard, not a standalone panel/page. |
| 277 | `In **Add a PC**, pick *Linux*,` | Same — rewrite as "wizard step 1 (target OS)". |
| 370 | `more likely to be flagged by AV and game anti-cheat` | NOOP — generic English "flagged", unrelated to the Flagged view. |
| 392 | `dashboard **Settings → Backup** section, superuser only` | **Settings → Backup** → **Admin → Backup** section. |
| 401 | `**Settings → Updates** section (below) tells you` | **Settings → Updates** → **Admin → Updates** section. |
| 405 | `**Settings → Updates** section to roll a pinned version` | Same rename. |

### docs/user-guide.md

| Line | Excerpt | Becomes |
|---|---|---|
| 9 | `An **Overview dashboard** — the whole fleet at a glance` | Overview → **Today**. |
| 14 | `from **Ask kenny, built into the dashboard**` | Rewrite: Ask kenny is no longer "built into the dashboard" as a page/rail — it's a global **⌘K overlay drawer**, context-scoped to whichever host is open. |
| 16 | `**Parental controls** (web activity + web filter, screen time) and **push alerts**` | NOOP — `web activity` telemetry field, not the Activity tab. |
| 62 | `## The Overview tab` | Heading → `## The Today page`. |
| 71 | `[dashboard reference](dashboard.md#the-overview-tab)` | Anchor → `#the-today-page` (must match dashboard.md's renumbered headings). |
| 79 | `| 🟢 | \`ok\` | nothing flagged |` | NOOP — status legend wording, not the Flagged view. |
| 90 | `fields (no raw JSON). For a *flagged* section, when an Anthropic API key` | NOOP — "flagged section" = a telemetry section in warn/crit, not the Flagged tab. |
| 92 | `**Auto-Remediate** button that hands a fix prompt to Ask kenny.` | Ask kenny reference → describe as opening the ⌘K overlay pre-filled with the fix prompt. |
| 104 | `**update**. Onboarding a *new* PC uses the **Add a PC** panel` | Add a PC panel → 3-step modal wizard. |
| 112 | `Health thresholds are evaluated **server-side**` | NOOP (context only; no nav term). |
| 116 | `The **fleet-wide observability** lives on the **Activity** tab: a searchable, paged **tool-call` | Activity tab → **Log** (unified stream with filter chips). |
| 117 | `audit log** ... and an **events & logs** stream (server + agent` | "events & logs" sub-view folds into Log's filter chips (e.g. an "audit" chip, an "events" chip). |
| 118 | `log lines and emitted [alerts](alerting.md)). The **Flagged** view — reached from the *warnings* /` | Flagged view → **Inbox**, specifically its NEEDS YOU/WAITING groups (a flagged section is what feeds a ticket/approval row there now, not a standalone tab). |
| 158 | `for Ask kenny in detail.` | Rewrite section pointer for the ⌘K overlay, not a dedicated page. |
| 165 | `1. In Claude Desktop, open **Settings → Connectors → Add custom connector**.` | NOOP — this is *Claude Desktop's own* Settings, not kenny's dashboard Settings tab. Leave as-is; flag for the rewriter so it isn't mistakenly renamed to Admin. |
| 194 | `| Server-only | \`list_agents\` · \`select_agent\` · \`fleet_overview\` · \`agent_health\` · \`agent_snapshot\` | read-only |` | NOOP — MCP tool names (contract), not the Overview tab. |
| 196 | `Parental-controls tools (\`webfilter_apply/clear\`, \`webfilter_get/set/push\`, \`web_activity_query\`)` | NOOP — tool/field names. |
| 213 | `From the **Add a PC** panel (left of the console), **installer** gives you a ZIP` | Add a PC panel → modal wizard step. |

### docs/dashboard.md — the primary rewrite target (22 screenshot embeds, most of the nav prose)

This file's title/structure ("four top-level tabs") is obsolete top to bottom;
below are every line the grep swept, not just the screenshot embeds.

| Line | Excerpt | Becomes |
|---|---|---|
| 13 | `kenny has **four top-level tabs** — **Overview**, **Fleet**, **Activity**, and` | Rewrite: kenny has **five destinations** — Today, Fleet, Inbox, Log, Admin — plus the ⌘K Ask-kenny overlay. |
| 14–15 | `**Tickets** — plus a **Flagged** view you reach from the header. Each tab is a URL you can bookmark (\`#/overview\`, \`#/fleet\`, \`#/activity/audit\`, \`#/activity/events\`,` | Rewrite the whole bookmark list to the new hashes: `#/today`, `#/fleet`, `#/fleet/{host}`, `#/inbox[/{group}]`, `#/log`, `#/admin/{section}`, `#/profile`. Must also document that the old hashes **redirect** (list them as "old → new" per the CONTEXT table). |
| 16 | `` `#/flagged/warn`, `#/flagged/crit`, `#/tickets`, `#/tickets/{id}`). The examples below `` | Same rewrite — these become `#/inbox/warn`, `#/inbox/crit` (or whatever Inbox's group slugs are) and `#/inbox/ticket/{id}`. |
| 24 | `![The console header](assets/screenshots/header.png)` | Re-shoot against the new shell header (Today/Fleet/Inbox/Log/Admin tabs, no separate approval badge — folds into Inbox). |
| 31 | `**Tab navigation** — **Overview · Fleet · Activity · Tickets**. The active tab is` | Rewrite to the five destinations. |
| 32 | `highlighted; **Tickets** carries a live count badge (operator+ only) when something needs` | Ticket badge concept moves to **Inbox**'s NEEDS YOU count. |
| 34 | `ticket. Clicking the tab always opens [the Tickets tab](#the-tickets-tab) itself; the badge` | Link/anchor → `#the-inbox-page`. |
| 39 | `and when either is above zero it becomes a **link into the [Flagged view](#the-flagged-view)**.` | Flagged view anchor → Inbox anchor/group. |
| 41–42 | `**✨ Ask kenny toggle** *(Fleet tab only)* — show/hide the chat rail (see [Ask kenny](#ask-kenny-chat-rail)).` | Rewrite: Ask kenny is a global **⌘K** trigger (not Fleet-tab-only, not a togglable rail) — remove "Fleet tab only" scoping language, describe the overlay drawer and its host-context-scoping instead. |
| 54 | `- **Settings** *(superuser only)* — opens the [\`#/settings\`](#the-settings-page) sidebar,` | Settings → **Admin**, section-nav instead of sidebar-page; hash → `#/admin/{section}`. |
| 56–57 | `- **Updates** *(operator+, shown instead of Settings for an operator...)* — opens [\`#/settings/updates\`](#updates) directly.` | Rewrite: Updates is now an Admin section for everyone with access (`#/admin/updates`); the operator/superuser menu-swap behavior needs re-describing under the new Admin section-nav model. |
| 64 | `A shield icon lives in the header for **operator+** accounts, next to the Ask kenny toggle,` | Approval badge folds into Inbox — rewrite as "next to the ⌘K trigger" or remove if the badge itself is retired per the CONTEXT (badge → Today). |
| 85–89 | Role descriptions mentioning "Settings" | Settings → Admin throughout. |
| 117 | `## The Overview tab` | Heading → `## The Today page`. |
| 124–125 | `![The Overview dashboard](assets/screenshots/overview.png)` + caption | Re-shoot as Today; caption should mention the approval badge is now part of Today. |
| 167 | `many hosts are flagged in each section — worst at the top. Click a bar segment to drill into` | NOOP — describes a chart interaction, not the Flagged tab. |
| 179 | `link is the number of flagged sections. Click a **link** to see the host/section pairs behind` | NOOP — same as above (KPI drilldown mechanics survive under Today). |
| 186 | `the event count, and a cell is flagged **crit** if any of its events is agent-reported` | NOOP — reliability heatmap coloring language. |
| 202 | `![A drill-down table](assets/screenshots/drilldown.png)` | Re-shoot under Today; mechanism (modal table) likely unchanged. |
| 206 | `Every Overview widget shares the same drill-down popup` | Overview → Today. |
| 215 | `detail**, and the docked **Ask kenny** chat rail.` | "docked...chat rail" → the ⌘K overlay drawer; it is no longer docked to the Fleet tab. |
| 218–219 | `![The Fleet console](assets/screenshots/fleet-console.png)` + caption "the Ask kenny rail (right)" | Re-shoot: Fleet becomes a **card grid → host full page** flow, not a three-pane console with a docked chat rail. Caption needs a full rewrite. |
| 228 | `Below the list, the **Add a PC** panel onboards a *new* machine` | Add a PC panel → 3-step modal wizard (no longer "below the list"). |
| 238 | `![The agent drill-down](assets/screenshots/agent-detail.png)` | Re-shoot as the host **full page** (was a side panel/modal in the three-pane console). |
| 256 | `summary, and — when flagged — the server's **rule reason**` | NOOP — per-section status language. |
| 265 | `![The enlarged screenshot](assets/screenshots/screenshot-modal.png)` | Re-shoot on the new host full page. |
| 273 | `objects become sub-lists. The header carries the section's status pill and, when flagged, the` | NOOP. |
| 277 | `![A section detail with an AI recommendation](assets/screenshots/ai-recommendation.png)` | Re-shoot on host full page; "Auto-Remediate" button now opens the ⌘K overlay, not a docked rail. |
| 281–284 | `**AI Recommendation** ... suggested prompt to Ask kenny (state-changing steps still hit the confirm-gate).` | Ask kenny reference → ⌘K overlay. |
| 300 | `![The reliability section detail](assets/screenshots/reliability.png)` | Re-shoot on host full page. |
| 304–309 | web_activity / parental-controls section + `![...](assets/screenshots/parental-controls.png)` | Re-shoot on host full page; caption text otherwise stable (parental-controls section itself is unaffected by IA, only its container is). |
| 331–335 | `### Ask kenny (chat rail)` heading + `![Ask kenny with a confirm-gate](assets/screenshots/copilot-confirm.png)` + caption | Heading → `### Ask kenny (⌘K overlay)`; re-shoot as an overlay drawer, not an inline rail; caption rewrite. |
| 352 | `Suggestion chips ("Why is this PC flagged?"...)` | NOOP — chip copy, unaffected. |
| 358 | `![The chat history panel](assets/screenshots/chat-history.png)` | Re-shoot inside the ⌘K overlay's history view. |
| 368–371 | `## The Activity tab` ... `**events & logs**.` | Heading → `## The Log page`; unify audit + events + server logs into one stream description with filter chips. |
| 376 | `![The tool-call audit log](assets/screenshots/activity-audit.png)` | Re-shoot as Log filtered to the "audit" chip. |
| 384–388 | `### Events & logs` heading + `![Events & logs](assets/screenshots/activity-events.png)` + caption | Fold into the single Log page description; re-shoot as Log with a different chip selection (or the unfiltered "all" state). |
| 391 | `A unified stream of server + agent **log lines**, emitted **alerts**, and audit events: time,` | This sentence is already close to the *new* Log model's description — good source text to reuse/adapt. |
| 397–400 | `## The Tickets tab` heading + intro | Heading → part of `## The Inbox page`; describe grouping (NEEDS YOU/WAITING/WORKING/NEW/DONE) replacing the old flat Tickets list. |
| 409–410 | `![The Tickets list...](assets/screenshots/tickets.png)` + caption | Re-shoot as Inbox's grouped queue. |
| 417–419 | `"needs you" is a ticket blocked on your... shows (see [the header](#the-shell-header-global-controls)).` | "needs you" language survives almost verbatim as Inbox's NEEDS YOU group — good reuse. |
| 430 | `tickets opens a **bulk-action bar** above the table` | Bulk-action bar concept likely persists inside Inbox; re-verify against the actual redesign, but no vocabulary change needed here beyond context. |
| 433 | `lifecycle in [Tickets & the Discord bot](itsm.md)) is skipped rather than failing the whole` | itsm.md concept survives; anchor context only. |
| 440 | `![A ticket's detail view...](assets/screenshots/ticket-detail.png)` | Re-shoot at the new hash `#/inbox/ticket/{id}`. |
| 444 | `Reached by clicking a row, or \`#/tickets/{id}\` directly` | Hash → `#/inbox/ticket/{id}`. |
| 449–483 | Composer / confirm-gate / "Ask kenny" toggle-in-composer prose | "Ask kenny" mode inside a ticket's composer needs re-describing against the global overlay model — clarify whether the ticket-scoped chat is now itself a context-scoped instance of the same ⌘K overlay, or a separate embedded composer (this is a design question the rewriter must resolve, not purely mechanical). |
| 512, 515 | `"Ask kenny" side has nothing to run against...second, equally-gated chat surface rather than an extension of Ask kenny.` | Same — needs a design decision, flag prominently for the rewrite. |
| 519–522 | `## The Flagged view` heading + `![The Flagged view](assets/screenshots/flagged.png)` | Heading and screenshot fold entirely into **Inbox** (its NEEDS YOU/WAITING grouping supersedes severity-only grouping) — likely the single biggest structural rewrite in this file. |
| 527 | `flagged section of that severity, **grouped by PC** (busiest first). Each tile opens that` | Rewrite grouping description for Inbox. |
| 536, 540 | `![The installer share-link dialog](assets/screenshots/share-link.png)` / `**Add a PC** (Fleet list) onboards a *new* machine. Pick the target **OS** first.` | Share-link dialog re-shoot inside the new Add-a-PC wizard; wizard step language. |
| 552 | inventory purge description | NOOP — unaffected by IA. |
| 565 | `![The About box](assets/screenshots/about.png)` | Re-shoot; About modal's launch point may move (was reachable from Overview's user menu — confirm it now lives under Admin or stays global). |
| 576–578 | `## The Settings page` heading + `*([\`#/settings/{section}\`](#the-shell-header-global-controls)...)` | Heading → `## The Admin page`; hash → `#/admin/{section}`. |
| 582–583 | `![The Settings page...](assets/screenshots/settings.png)` + caption | Re-shoot as Admin's section-nav layout. |
| 588–589 | `Digest, Web filter, Chat & AI, Logging, Backup, Updates, Discord & Tickets, plus [Auto-ticket rules]` | These become Admin's section-nav entries — list survives, container renamed. |
| 594 | `every section at once, so \`#/settings\` still doubles as the one place to Ctrl-F the whole` | `#/settings` → `#/admin`. |
| 600 | `restart — a **restart** pill. Editable settings apply immediately` | NOOP. |
| 605–606 | `` `#/settings` (bare), `#/backup`, and `#/updates` all resolve into this page — old bookmarks `` | This is *already* a redirect-compat sentence — good template for documenting the *new* redirect set (old hashes → `#/admin/...`) the rewriter needs to add. |
| 611 | `![The Backup section of Settings.](assets/screenshots/settings-backup.png)` | Re-shoot under Admin. |
| 694–701 | `### Discord & Tickets` heading + `![The Discord panel in Settings.](assets/screenshots/discord-settings.png)` | Section survives inside Admin; re-shoot; heading context ("in Settings") → "in Admin". |
| 712 | `*(operator+ — like Updates, this section has no settings-catalog group...)*` | Settings-catalog terminology → Admin. |
| 717 | cross-ref to alerting.md auto-ticket rules | NOOP (anchor stable). |
| 736–737 | `![The Overview dashboard in the light theme](assets/screenshots/overview-light.png)` + caption | Re-shoot as Today; also: Nullthrone is **light-by-default**, so "light theme" as a secondary/toggle state may need reframing — dark becomes the *alternate* theme, not vice versa. |
| 743–746 | `**Deep links** — every view is a URL hash you can bookmark or share (\`#/overview\`, \`#/fleet\`, \`#/activity/audit\`, \`#/activity/events\`, \`#/flagged/warn\`, \`#/flagged/crit\`, \`#/tickets\`, \`#/tickets/{id}\`, \`#/settings/{section}\`). \`#/backup\` and \`#/updates\` still resolve too, straight into the matching Settings section, for old bookmarks and the` | Full rewrite: enumerate the *new* five-destination hash set and explicitly state the old→new redirect table from the task CONTEXT. This is the single most important paragraph to get right — it's the documented contract for bookmark compatibility. |
| 748 | `**Keyboard & motion** — Escape closes modals and the Ask kenny drawer; animations respect` | "Ask kenny drawer" language is already forward-looking — good, minimal rewrite (confirm ⌘K opens it, not just a toggle). |
| 758 | `[Tickets & the Discord bot](itsm.md)` | NOOP — itsm.md concept and link target survive. |

### docs/itsm.md

| Line | Excerpt | Becomes |
|---|---|---|
| 34 | `with **New ticket** on the [Tickets tab](dashboard.md).` | Link text/anchor → Inbox. |
| 40–44 | Auto-ticket rules cross-ref to `[Settings](dashboard.md#auto-ticket-rules)` | Settings anchor → Admin anchor. |
| 54–55 | `![The Tickets list...](assets/screenshots/tickets.png)` + caption "The Tickets tab: every ticket you can see..." | Re-shoot as Inbox's grouped queue; caption rewrite. |
| 108 | `![A ticket's detail view...](assets/screenshots/ticket-detail.png)` | Re-shoot at `#/inbox/ticket/{id}`. |
| 132 | `distinct from the dashboard's separate **Ask kenny** chat (the operator-only assistant rail, not` | "assistant rail" → ⌘K overlay; re-verify the ticket-composer-vs-global-overlay relationship (same open design question flagged in dashboard.md). |
| 142 | `and the header's **approvals badge** (a shield icon next to the Ask kenny toggle, with a` | Approval badge folds into Today per CONTEXT — rewrite; "Ask kenny toggle" → ⌘K trigger. |
| 158 | `web_activity_query`. Only the **ticket's requester**...` | NOOP — tool name. |
| 177–194 | Several `**Settings → Discord**` references (linking accounts, unlinking) | Settings → Discord → **Admin → Discord** (or wherever the Discord & Tickets group lands in Admin's section nav). |
| 186–187 | `![The Discord panel in Settings.](assets/screenshots/discord-settings.png)` + caption | Re-shoot under Admin; caption "Settings → Discord" → "Admin → Discord". |
| 261 | `record, the same way an unbounded Ask kenny chat history already is` | Ask kenny reference → ⌘K overlay's history. |
| 315 | `to kenny's database and never editable in the Settings UI` | Settings UI → Admin UI. |
| 368 | `Turn on **User Settings → Advanced → Developer Mode**` | NOOP — this is *Discord's own* user settings, not kenny's. Flag so the rewriter doesn't misfire a Settings→Admin rename here. |
| 392 | `**Settings → Discord** shows the gateway status.` | → Admin → Discord. |
| 408 | `store, the lifecycle, the dashboard's Tickets tab and API all work` | Tickets tab → Inbox. |
| 415–416 | `[\`dashboard.md\`](dashboard.md) — the Tickets tab, the approvals badge, and the Discord Settings panel` | Tickets tab → Inbox; approvals badge → folded into Today; "Discord Settings panel" → "Discord Admin panel". |
| 428 | alerting.md cross-ref | NOOP. |

### docs/alerting.md

| Line | Excerpt | Becomes |
|---|---|---|
| 43–45 | `...shows up in the dashboard's **Activity → events & logs** view with no extra UI plumbing.` | Activity → events & logs → **Log** page (with its filter chips). |
| 48–49 | `![Emitted alerts and server/agent events in the Activity events and logs view.](assets/screenshots/activity-events.png)` + caption | Re-shoot as Log; caption rewrite ("the Activity events and logs view" → "the Log page"). |
| 63 | `is operator-only in the [Tickets tab](dashboard.md#the-tickets-tab)` | Tickets tab anchor → Inbox anchor. |
| 67–72 | `### Which events open a ticket is configurable` + `Settings` cross-ref | Settings → Admin (Auto-ticket rules section). |
| 81 | `tickets without silencing the offline *alert* itself — delivery and the events-table audit` | NOOP — "events-table" is a DB concept, not the Activity tab. |
| 122 | `alert (re-firing at most every 24 h); under **~30 days** shows as an Overview KPI and in` | Overview KPI → Today KPI. |
| 180 | `[\`dashboard.md\`](dashboard.md) — the Overview KPIs and the per-agent AI Forecast card` | Overview KPIs → Today KPIs. |
| 182 | `[\`itsm.md\`](itsm.md) — tickets, the Discord bot, and what an alert-opened ticket looks like` | NOOP — link target stable. |

### docs/telemetry.md

| Line | Excerpt | Becomes |
|---|---|---|
| 33 | `Green circle | \`ok\` | nothing flagged` | NOOP — status legend. |
| 47 | `several of them still feed the Overview dashboard (noted in the` | Overview → Today. |
| 51 | `![The per-agent drill-down...](assets/screenshots/agent-detail.png)` | Re-shoot as the host full page. |
| 71–72, 74 | reliability / web_activity / local_accounts rule descriptions | NOOP — telemetry section semantics, unrelated to IA. |
| 93–94, 100 | `!!! note "What feeds the Overview dashboard"` / `drive the fleet-wide Overview panels` / `reliability` events → heatmap | Overview → Today (93–94); line 100 is NOOP (telemetry wording). |
| 131 | `![The reliability section detail...](assets/screenshots/reliability.png)` | Re-shoot on host full page. |
| 135 | ADR-0026 cross-ref | NOOP. |
| 152 | `heatmap, and the fleet Overview heatmap — suppression never hides volume;` | Overview → Today. |
| 207 | ADR-0026 cross-ref | NOOP. |

### docs/tools.md

| Line | Excerpt | Becomes |
|---|---|---|
| 21 | `\`fleet_overview\`, \`agent_health\`, \`agent_snapshot\`` | NOOP — MCP tool name, part of the wire contract; unaffected by dashboard IA. |
| 77 | `![The confirm-gate...](assets/screenshots/copilot-confirm.png)` | Re-shoot inside the ⌘K overlay. |
| 87, 91 | `web_activity_query` / redacted-output tool list | NOOP. |
| 101 | Role & host scope table row (`fleet_overview`, `list_agents`) | NOOP — tool names. |
| 253 | `\`fleet_overview\` | — | Per-agent rolled-up health for the whole fleet.` | NOOP — tool table. |
| 264, 274 | tool classification rows | NOOP. |
| 302 | `Read it in the dashboard under **Activity → tool-call audit**.` | Activity → tool-call audit → **Log** (audit chip). |
| 305 | `![The tool-call audit log...](assets/screenshots/activity-audit.png)` | Re-shoot as Log. |
| 319 | `[\`dashboard.md\`](dashboard.md) — the fleet view, drill-down, and Activity tab.` | Activity tab → Log. |

### docs/protocol.md — no IA changes needed

Every grep hit here (lines 127, 311–316, 376, 424, 569, 575, 606, 611, 620,
635–638, 651–676, 703, 794, 841, 899, 913, 943, 956) is a wire-contract field
or tool name (`web_activity` telemetry section, `events` log frames,
`fleet_overview` tool, etc.). None reference the dashboard's page/tab
structure. **This file needs no edits for the IA consolidation.**

### docs/parental-controls.md

| Line | Excerpt | Becomes |
|---|---|---|
| 7, 15, 30 | intro + ADR-0024 links | NOOP. |
| 56, 60, 62, 64 | web_activity health-rule table | NOOP — telemetry semantics. |
| 69 | `Open a PC, then click the \`web_activity\` section tile to open its detail popup.` | "Open a PC" now means navigating to the host's **full page** (was: opening a modal from the Fleet card grid) — rewrite the entry point, the popup mechanism inside the host page may be unchanged. |
| 71 | `![The web_activity section detail...](assets/screenshots/parental-controls.png)` | Re-shoot on host full page. |
| 75, 83, 101 | list/editor description | NOOP. |
| 130 | `## Driving it from Ask kenny` | Heading survives ("Ask kenny" name unchanged) but body text should describe the ⌘K overlay, host-context-scoped, instead of an inline rail. |
| 137 | `web_activity_query` tool row | NOOP. |
| 147 | `[\`dashboard.md\`](dashboard.md) — the fleet view, drill-down, and Activity tab.` | Activity tab → Log. |
| 150 | ADR-0024 link | NOOP. |

### docs/account-governance.md

| Line | Excerpt | Becomes |
|---|---|---|
| 19 | `web activity stay whole-machine` | NOOP. |
| 90 | `two-factor settings, and recovery options.` | NOOP — generic English "settings", not the tab. |
| 124 | `**Every call is written to the audit log** — visible under *Activity → Tool-call audit` | Activity → Tool-call audit → **Log** (audit chip). |
| 150 | `one of the two settings is stale).` | NOOP. |
| 179 | `[Parental controls](parental-controls.md) — web activity, filtering, screen time` | NOOP. |

### docs/adr/* and docs/policy/*

`grep -rn -iE '#/(overview|activity|tickets|flagged|settings)|Ask kenny|Add a PC' docs/adr/ docs/policy/`
returns only **`docs/adr/0015-agent-binary-auto-fetch.md:33`**,
**`docs/adr/0036-report-cpu-arch-via-telemetry.md:26,65,100`** — all four are
"Add a PC" mentioned as background/rationale for a *historical* decision
(auto-fetch, OS+Arch dropdown). ADRs record the decision as of when it was
made and are explicitly **not** supposed to be kept current with the live UI
(root `CLAUDE.md`: "What changed and why it moved is the commit message's
job"). **Recommend leaving the ADR text alone** — do not rewrite history —
and let the doc-drift hook's `_SCAN_SKIP_FILES`/citation-only checks (§5)
continue to police only structural ADR-set integrity, not UI wording. No
other ADR or the one `docs/policy/deny_rules.json` file references the old
nav at all.

### Summary count

**10 of 11** `docs/*.md` top-level pages need rewriting for the IA/nav
change: `index.md`, `setup.md`, `user-guide.md`, `dashboard.md` (heaviest —
effectively a full rewrite), `itsm.md`, `alerting.md`, `telemetry.md`,
`tools.md`, `parental-controls.md`, `account-governance.md`. **`protocol.md`
needs none** — it is the wire contract and correctly has zero UI-nav
coupling. ADR files need no rewriting (by design/policy).

---

## 2. Screenshot inventory

### PNG → doc(s) → new view mapping

`docs/assets/screenshots/` has **23 PNGs**. One (`reliability-modal.png`) is
already orphaned — no doc embeds it (confirmed via
`grep -rl "screenshots/reliability-modal.png" docs/*.md` returning nothing),
even though `scripts/screenshots/shots.py` still generates it (a `Shot` named
`"reliability-modal"`, note: *"element crop of the reliability section detail
(companion to full-page)"*). Treat it as dead weight to prune during the
rewrite, or wire it into a doc if it's meant to replace `reliability.png`.

| PNG | Embedded in | Maps to (new IA) |
|---|---|---|
| `about.png` | dashboard.md | About modal — survives, entry point may move under Admin. |
| `activity-audit.png` | dashboard.md, tools.md | **Log** (audit filter chip). |
| `activity-events.png` | alerting.md, dashboard.md | **Log** (events/alerts chip, or the unfiltered stream). |
| `agent-detail.png` | dashboard.md, telemetry.md | **Fleet → host full page**. |
| `ai-recommendation.png` | dashboard.md | Fleet host full page section detail; "Auto-Remediate" now opens the ⌘K overlay. |
| `chat-history.png` | dashboard.md | ⌘K overlay's history view. |
| `copilot-confirm.png` | dashboard.md, tools.md | ⌘K overlay confirm-gate (no longer a docked rail). |
| `discord-settings.png` | dashboard.md, itsm.md | **Admin** → Discord & Tickets section. |
| `drilldown.png` | dashboard.md | **Today** KPI drill-down modal. |
| `flagged.png` | dashboard.md | **Obsolete** — the Flagged view itself is retired; its content is absorbed into Inbox's grouping. No 1:1 replacement shot; a new "Inbox — NEEDS YOU/WAITING groups" shot replaces it conceptually. |
| `fleet-console.png` | dashboard.md | **Obsolete as captured** — the three-pane console (list + detail + docked chat rail) goes away; replace with a **Fleet card grid** shot (list only, no docked rail, no detail pane) plus a separate host-full-page shot (`agent-detail.png`'s replacement). |
| `header.png` | dashboard.md | Shell header — re-shoot with the five-destination tab bar. |
| `overview-light.png` | dashboard.md | **Today**, and becomes the *default*-theme shot under Nullthrone (light-by-default) rather than a secondary "light mode" demo. |
| `overview.png` | dashboard.md, index.md | **Today**. |
| `parental-controls.png` | dashboard.md, parental-controls.md | Fleet host full page → web_activity section. |
| `reliability-modal.png` | *(none — orphaned)* | Dead asset; see above. |
| `reliability.png` | dashboard.md, telemetry.md | Fleet host full page → reliability section. |
| `screenshot-modal.png` | dashboard.md | Fleet host full page → screenshot card modal. |
| `settings-backup.png` | dashboard.md | **Admin** → Backup section. |
| `settings.png` | dashboard.md | **Admin** (section-nav layout). |
| `share-link.png` | dashboard.md | Add-a-PC wizard step (installer share-link). |
| `ticket-detail.png` | dashboard.md, itsm.md | **Inbox** → `#/inbox/ticket/{id}`. |
| `tickets.png` | dashboard.md, itsm.md | **Inbox** grouped queue (replaces the flat list). |

**Net effect:** of 23 current shots, ~2 (`flagged.png`, `fleet-console.png`
as currently framed) become fully obsolete/need reframing rather than a
like-for-like re-shoot, 1 (`reliability-modal.png`) is already orphaned
dead weight, and the rest need re-capturing against the new chrome (new
header, new hash routes, host full page instead of a docked three-pane
console, ⌘K overlay instead of an inline chat rail) even where the
underlying widget content is unchanged. Expect the manifest to also need
**new** shots for things the old IA never had as first-class views: the
Inbox grouped-queue empty/populated states, the Add-a-PC 3-step wizard
(one shot per step is likely, replacing the single `share-link.png` crop),
and the ⌘K overlay's closed→open transition/trigger affordance.

### The screenshot pipeline (`scripts/screenshots/`)

Read in full: `capture.py`, `shots.py`, `seed.py`, `demo_fleet.py`,
`desktop_image.py`, `README.md`.

**Manifest schema** (`shots.py`), quoted verbatim:

```python
@dataclass
class Shot:
    name: str
    hash: str
    mode: str = "full_page"  # "full_page" | "element"
    selector: str | None = None
    theme: str = "dark"  # "dark" | "light"
    actions: list[dict[str, Any]] = field(default_factory=list)
    # Optional per-shot note surfaced in the run report.
    note: str = ""
```

One real entry, verbatim (a `full_page` shot with a `wait_for` action):

```python
Shot(
    name="flagged",
    hash="#/flagged/warn",
    mode="full_page",
    actions=[{"wait_for": ".kc-flagged"}, {"sleep": SETTLE_MS}],
),
```

And an `element`-mode entry showing the crop selector + a multi-step action
chain (`_select()` navigates the Fleet tab to an agent first):

```python
Shot(
    name="reliability",
    hash="#/fleet",
    mode="full_page",
    actions=[
        *_select("grandpa-pc"),
        {"eval": "openSectionDetail('reliability')"},
        {"wait_for": "#modal-overlay #k-reliab-heat"},
        {"wait_for": "#modal-overlay #k-relsup-panel .kwf-list, #modal-overlay #k-relsup-panel .kwf-row"},
        {"sleep": SETTLE_MS},
    ],
),
```

**How `actions` work** (interpreted by `capture.py::_run_actions`, a tiny
fixed vocabulary):
- `{"eval": "<js>"}` — runs arbitrary JS in the page context (e.g. calling a
  dashboard global like `selectAgent('study-pc')` or `openSectionDetail('disk')`),
  wrapped in an async IIFE and awaited.
- `{"wait_for": "<css selector>"}` — `page.wait_for_selector(..., state="visible", timeout=15000)`.
- `{"wait_charts": true}` — a dedicated helper (`_wait_charts`) that polls
  until every `.kc-chart` element's inner `<svg>` has a nonzero
  `getBoundingClientRect().width`, i.e. ECharts has finished laying out.
- `{"sleep": <ms>}` — `page.wait_for_timeout(ms)`, a fixed settle delay for
  chart animation / SSE stream settling. Module-level `SETTLE_MS = 600`.

**Theme selection**: per-shot `theme` field (`"dark"` default, `"light"`
opt-in). Before each shot's page navigation, `capture.py::_capture_shot`
injects an init script — `localStorage.setItem('kenny-theme', <theme>)` —
so the dashboard's own theme-persistence key is pre-seeded before first
paint; this matters because "localStorage is shared across pages in one
context, so a prior light shot would otherwise leak into the dark shots that
follow" (their comment). **For Nullthrone (light-by-default)**: the default
`theme` on most `Shot` entries will need to flip from `"dark"` to `"light"`,
and `overview-light.png`'s special-case status (currently the *only*
light-themed shot) goes away since light becomes the norm — a `*-dark.png`
naming convention for the now-secondary dark shots may be worth adopting
instead.

**Viewport / scale**: module constants in `capture.py` —
`VIEWPORT = {"width": 1500, "height": 950}`, `DEVICE_SCALE = 2` (crisp 2×
PNGs), passed to `browser.new_context(viewport=VIEWPORT,
device_scale_factor=DEVICE_SCALE, ignore_https_errors=True)`.

**Font assertion**: `_assert_fonts(page)` awaits `document.fonts.ready` then
checks `document.fonts.check("16px 'Hanken Grotesk'")` and `"16px
'JetBrains Mono'"`; if either is false it raises `SystemExit` with an
explicit "FONT CHECK FAILED — refusing to ship fallback-font PNGs" message
rather than emitting a shot with the wrong typeface. This runs once as a
"preflight" against `#/overview` before any shot, and again per-shot inside
`_capture_shot`. **For Nullthrone**: the font stack changes to Jost + Public
Sans + JetBrains Mono (JetBrains Mono survives unchanged; Hanken Grotesk is
replaced by Jost for headings/caps and Public Sans for body) — this
assertion's hard-coded family strings must be updated in lockstep with the
CSS, or the pipeline will hard-fail (by design) the moment the dashboard's
fonts change but this check doesn't.

**Server start & seeding** (`capture.py::run`): all in one asyncio event
loop so seeded in-memory state is visible to the same server the browser
hits —
1. `_configure_env(db_path)` sets `KENNY_DB_PATH` (a tempdir sqlite file),
   `KENNY_OPERATOR_TOKEN=demo-operator-token`, disables the alert loop
   (`KENNY_ALERT_INTERVAL_SECS=0`) and webfilter refresh
   (`KENNY_WEBFILTER_REFRESH_SECS=0`), and unsets `KENNY_TLS`.
2. `kenny_server.main.build_app(db_path=db_path)` builds the real ASGI app.
3. An in-process `uvicorn.Server` (`_serve`) is started on a free localhost
   port and polled (`server.started`) up to 200×50ms before proceeding.
4. `seed.seed_app(app)` (from `scripts/screenshots/seed.py`) writes the demo
   fleet directly into `app.state` — this is the part that *must* run
   in-process, since the `ScreenshotStore` and the `AgentRegistry`'s online
   flags are in-memory-only, not SQLite-backed, so a "seed the DB, then
   start a fresh server" approach would silently miss them.
5. Playwright launches headless Chromium (resolved via
   `PLAYWRIGHT_BROWSERS_PATH`, default `/opt/pw-browsers`, globbing for
   `chromium-*/chrome-linux/chrome`; **never runs `playwright install`**),
   with the environment's `HTTPS_PROXY` wired in (bypassing
   `127.0.0.1,localhost`) so Google Fonts can be fetched, and
   `ignore_https_errors=True` for the proxy's intercepting cert.
6. Routes `**/api/changelog` and `**/api/agent-binary` are stubbed via
   `context.route(...).fulfill(...)` so the About modal's changelog and the
   Fleet tab's "agent binary available" state don't depend on a live GitHub
   call.
7. Iterates `shots.MANIFEST` (or a `--only`-filtered subset), capturing each
   into `<out>/<name>.png`, printing a per-shot `[ok]`/`[FAIL]` line; exits
   non-zero if any shot failed.

**Demo fleet** (`demo_fleet.py` + `seed.py`): 6 hosts (`papa-pc` all-green,
`mama-laptop` with a battery, `kid-pc` flagged web_activity, `study-pc` disk
critical + <30-day forecast, `living-room-pc` reboot-pending + failed
update, `grandpa-pc` Defender-off + EOL OS), built by deep-copying
`docs/fixtures/telemetry_snapshot.json` and mutating specific sections, plus
30 days of interpolated history per host, 4 demo tickets driven through the
real `TicketService` API (not raw DB writes), 2 Discord identities + 1
pending claim, reliability-categorization cache pre-seeding (so the LLM
categorizer's output is deterministic without an API key), and a mock
desktop PNG (`desktop_image.py`, pure `zlib`/`struct` PNG encoder, no Pillow
dependency) for the screenshot card/modal.

**Exact regeneration command** (from `README.md` and `capture.py`'s own
docstring):

```bash
cd kenny-server
pip install -e ".[dev,screenshots]"      # server deps + Playwright
# Chromium is provided by the environment — do NOT run `playwright install`.

cd ..
python scripts/screenshots/capture.py                 # -> docs/assets/screenshots/
python scripts/screenshots/capture.py --only overview,fleet-console   # subset
python scripts/screenshots/capture.py --out /tmp/shots                # alt output dir
```

**Can it regenerate everything unattended, once the IA/theme land?** Yes,
mechanically — the pipeline builds the real app, seeds real state through
real service APIs, and drives the real rendered DOM, so it needs no manual
step beyond `capture.py`'s single command. But three things gate a clean
unattended run on the new IA and must be updated *before* re-running it, or
it will fail loudly (by the design of `_assert_fonts`) or silently capture
the wrong thing:
1. Every `Shot.hash` in `shots.py` targeting an old route (`#/overview`,
   `#/activity/audit`, `#/activity/events`, `#/flagged/warn`, `#/tickets`,
   `#/tickets/{id}`, `#/settings`, `#/settings/{section}`) needs updating to
   the new hash, or it will silently ride the redirect (if implemented) to
   the new page and may capture a mismatched viewport/state versus what the
   shot's `actions`/`selector` expect.
2. Selectors like `.kc-flagged`, `.kc-copilot`, `#detail .kc-tiles`,
   `.k-audit__row`, `.k-events__row`, `table.kacc-tbl` are CSS-class
   couplings to the *current* dashboard markup; a redesign (Fleet
   card-grid→full-page, Inbox's new grouping, the ⌘K overlay replacing the
   docked rail) will very likely rename or restructure these elements, so
   every `Shot`'s `selector`/`wait_for` needs re-auditing against the new
   DOM, not just its `hash`.
3. The font assertion's hard-coded `'Hanken Grotesk'` / family strings must
   track the Nullthrone font swap (Jost + Public Sans), or the whole run
   hard-fails at the very first preflight page load.

None of this requires new external dependencies or environment changes —
just editing `shots.py` (and possibly `seed.py`'s selector-dependent
`_select()` helper) to match the redesigned dashboard.

---

## 3. MkDocs theming

**`mkdocs.yml`** (full file read, 81 lines) — key blocks:

```yaml
theme:
  name: material
  logo: assets/kenny-mark-64.png
  favicon: assets/kenny-favicon.png
  font:
    text: Hanken Grotesk
    code: JetBrains Mono
  palette:
    - media: "(prefers-color-scheme: dark)"
      scheme: slate
      primary: amber
      accent: amber
      toggle: {icon: material/weather-sunny, name: Switch to light mode}
    - media: "(prefers-color-scheme: light)"
      scheme: default
      primary: amber
      accent: amber
      toggle: {icon: material/weather-night, name: Switch to dark mode}
  features:
    - navigation.sections
    - navigation.top
    - navigation.tracking
    - content.code.copy
    - content.action.edit
    - search.suggest
    - search.highlight
extra_css:
  - css/kenny-theme.css
```

The palette block is explicitly **dark-first**: the first list entry matches
`prefers-color-scheme: dark` and is what MkDocs Material picks by default
when the media query is ambiguous/unset — comment at line 29 says so
outright ("Dark first (matches the dashboard's warm 'border-collie'
theme)"). For Nullthrone's light-by-default requirement, this ordering must
flip (light entry first) and the `scheme`/`primary`/`accent` values need to
move off Material's built-in `amber` palette token to a **custom** scheme
carrying the brass `#A87E2F` accent and paper `#F4F2EC` / ink `#141317`
pair, since Material's named palettes (`amber`, `slate`, `default`) are
fixed swatches, not the exact hex values this project needs.

**`docs/css/kenny-theme.css`** (full file, 37 lines) — every override point
currently defined, quoted:

```css
:root {
  --md-primary-fg-color: #E8A33D;       /* amber */
  --md-primary-fg-color--light: #F0B65C;
  --md-primary-fg-color--dark: #C9852A;
  --md-accent-fg-color: #E8A33D;
}

[data-md-color-scheme="slate"] {
  --md-hue: 30;
  --md-default-bg-color: #1A1917;       /* warm-900 */
  --md-default-bg-color--light: #201E1B;
  --md-default-fg-color: #ECE6DA;       /* warm-100 */
  --md-default-fg-color--light: #A89F8E;
  --md-code-bg-color: #141311;          /* warm-950 */
  --md-code-fg-color: #ECE6DA;
  --md-typeset-a-color: #F0B65C;
  --md-footer-bg-color: #141311;
  --md-accent-fg-color: #F0B65C;
}

[data-md-color-scheme="default"] {
  --md-default-bg-color: #F3EEE3;       /* warm-50 */
  --md-code-bg-color: #FBF7EF;          /* warm-0 */
  --md-typeset-a-color: #92610F;        /* amber-text */
}

.md-header,
.md-tabs {
  background-color: #23221E;            /* surface */
  color: #ECE6DA;
}
```

This is a small, targeted override set — only `--md-primary-fg-color*`,
`--md-accent-fg-color`, `--md-hue`, `--md-default-bg-color*`,
`--md-default-fg-color*`, `--md-code-bg-color`/`--md-code-fg-color`,
`--md-typeset-a-color`, `--md-footer-bg-color`, and a hardcoded
`.md-header`/`.md-tabs` background/color pair. **What has to change for
Nullthrone:**
- Every hex above is the current warm-amber/slate "border-collie" palette
  and must be replaced: `--md-default-bg-color` → `#F4F2EC` (paper) for the
  default/light scheme (currently it's `#F3EEE3`, close but not the spec'd
  value), `--md-default-fg-color`/link colors → ink `#141317`, and both
  `--md-primary-fg-color` and `--md-accent-fg-color` → the single brass
  `#A87E2F` accent (currently split across `--md-primary-fg-color` =
  `#E8A33D` amber and a *different* `--md-typeset-a-color` = `#92610F` in
  light mode — Nullthrone's "one brass accent" spec means these should
  probably collapse to one value instead of two).
- `theme.font.text` in `mkdocs.yml` (Hanken Grotesk) needs to become a
  **two-font** split kenny's Material config doesn't currently support out
  of the box — Material's `theme.font` block only takes one `text` family
  for body+headings combined and one `code` family. Jost for
  headings/all-caps vs Public Sans for body text needs either (a) CSS rules
  in `kenny-theme.css` targeting `h1`–`h6`/`.md-nav` etc. to layer Jost on
  top of Material's single `text` font (set to Public Sans), or (b) giving
  up on `theme.font` entirely and declaring both `@font-face`/Google-Fonts
  links + custom CSS. `theme.font.code` (JetBrains Mono) is already correct
  and needs no change.
- The `.md-header`/`.md-tabs` hardcoded `#23221E` surface color is a warm
  dark neutral baked in regardless of scheme — this **fights** a
  light-default design outright (the header/tabs would stay dark even in
  the paper-and-ink light mode) and must be removed or made
  scheme-conditional.
- `--md-hue: 30` (warm/amber hue rotation) applies to every HSL-derived
  Material color that isn't explicitly overridden above — worth an explicit
  audit against Nullthrone's ink/paper/brass triad rather than trusting the
  hue rotation to land correctly.

**Material components that will fight square-corners / no-gradients /
no-shadows / hairline Nullthrone:**
- **Cards & admonitions** (`admonition` extension is enabled in
  `markdown_extensions`) — Material's default admonition boxes have rounded
  corners, a colored left border, and a subtle box-shadow; all three
  contradict "square corners, 1px hairlines, no gradients or shadows" and
  need CSS resets (`border-radius: 0`, replace box-shadow with a hairline
  border).
- **Code blocks** (`pymdownx.highlight`, `pymdownx.inlinehilite`,
  `pymdownx.superfences` with a custom `mermaid` fence) — Material renders
  fenced code in a rounded, shadowed panel with a colored copy-button
  overlay (`content.code.copy` feature is enabled); needs the same
  radius/shadow reset, and the copy-button hover state (currently a
  colored fill) should move to a hairline/ink treatment.
- **Search modal** (`plugins: [search]`, `search.suggest`/`search.highlight`
  features) — Material's search overlay is a rounded, shadowed floating
  panel with animated open/close; "no shadows" plus kenny's own new ⌘K
  overlay convention (drawer, not modal-with-shadow) suggests restyling
  this to match rather than leaving Material's stock look, which would now
  visually clash with the dashboard's own overlay pattern.
- **Tabs** (`navigation.top`/`navigation.sections` features, plus the
  hardcoded `.md-tabs` background above) — Material's top-level nav tabs
  use a rounded/underline active-state indicator with transition
  animation; square-corners + no-gradients likely means swapping the
  underline-slide animation for a flat hairline-border active state.
- **`navigation.sections`** — Material auto-generates a collapsible
  rounded/indented tree in the left sidebar; less a visual fight than a
  structural one worth flagging, since the sidebar tree still uses
  Material's own spacing/indent conventions that read differently from a
  flat, hairline-ruled admin-style nav.

No `mkdocs.yml` or CSS edits were made — this section is inventory only.

---

## 4. CI and packaging

### Python package build (`kenny-server/pyproject.toml`)

- Backend: `setuptools>=68`, `build-backend = "setuptools.build_meta"`.
- `[tool.setuptools.packages.find] include = ["kenny_server*"]` — pure
  auto-discovery by package name prefix.
- **Package data** (`[tool.setuptools.package-data]`):
  ```toml
  "kenny_server" = ["data/*.json"]
  "kenny_server.webui" = ["*.html", "*.css", "*.js", "assets/*.png", "assets/*.ico", "assets/*.svg", "assets/*.js"]
  ```
  This is the exact seam a future Node-built frontend must land inside: only
  files already present under `kenny_server/webui/` at build time (matching
  those globs) get bundled into the wheel/sdist. There is **no build step**
  in this `pyproject.toml` today — the dashboard is a single hand-written
  6,757-line `kenny_server/webui/index.html` plus a small `webui/assets/`
  directory (icons, a vendored `echarts.min.js`, dog avatar PNGs) — no
  `package.json`, no bundler, nothing to invoke.
- Optional dependency groups: `discord` (discord.py, lazily imported),
  `dev` (pytest/ruff), `docs` (mkdocs-material + awesome-pages), `screenshots`
  (playwright — explicitly scoped "Not needed for the server itself, tests,
  or CI").
- `[project.scripts] kenny-server = "kenny_server.main:run"` — the installed
  console entrypoint.

### Docker image (`kenny-server/Dockerfile`, single Dockerfile in the repo)

```dockerfile
FROM python:3.14-slim
WORKDIR /app
COPY kenny-server/ /app/
COPY docs/policy/ /app/docs/policy/
RUN pip install --no-cache-dir ".[discord]" && mkdir -p /data
ARG KENNY_SERVER_VERSION=""
ENV KENNY_HOST=0.0.0.0 KENNY_PORT=8000 KENNY_DB_PATH=/data/kenny.sqlite KENNY_SERVER_VERSION=${KENNY_SERVER_VERSION}
EXPOSE 8000
VOLUME ["/data"]
CMD ["kenny-server"]
```

Single-stage: it `COPY`s the whole `kenny-server/` tree verbatim (including
`kenny_server/webui/index.html` as a plain file) then `pip install`s it —
there is no build/compile step for the frontend, because today's frontend
needs none. Built from the **repo root** as context
(`docker build -f kenny-server/Dockerfile -t kenny-server .`, per the header
comment and `compose.yaml`'s `context: .` / `dockerfile:
kenny-server/Dockerfile`), which matters for a future Node stage: it would
need `kenny-web/` (or wherever the new frontend source lives) to also be
inside that same build context, since Docker cannot `COPY` from outside its
context root.

### `.dockerignore` / `.gitignore`

`.dockerignore` excludes `docs/*` from the build context except
`docs/policy` (re-included via `!docs/policy`) — i.e. the Dockerfile
deliberately keeps the whole rest of `docs/` (including
`docs/assets/screenshots/`, all ~9 MB of it) **out** of the image entirely,
only the shared deny-rule catalog crosses in. `.gitignore` excludes
`kenny-server/build/`, `kenny-server/dist/` (setuptools build artifacts) and
`site/` (mkdocs build output) — no Node-related ignores (`node_modules/`,
`dist/` at repo root, etc.) exist yet anywhere in either file; both will
need entries once `kenny-web/` exists.

### Where the Node build step must go

There is currently **zero** Node/npm tooling anywhere in the repo (`find`
for `package.json` under the repo turned up nothing). For a new
`kenny-web/` frontend with a real build step (bundler output, not a
hand-written HTML file) to reach end users **without requiring Node at
`pip install` time**, the build has to run and produce static output
*before* either of these two packaging paths consumes it:

1. **`kenny-server` wheel/sdist** (`pyproject.toml`'s
   `setuptools.build_meta`) — setuptools has no hook here that runs
   arbitrary build commands; it just globs files that already exist
   (`package-data`). So the Node build must happen **upstream** of
   `pip install`/`python -m build`, with its output already sitting inside
   `kenny_server/webui/` (or wherever the new package-data glob points)
   before setuptools ever runs. In CI (`ci.yml`'s `server` job, and any job
   that runs `pip install -e ".[dev]"`) this means adding a
   `kenny-web` build job/step (checkout → `actions/setup-node` → `npm ci &&
   npm run build` → copy `kenny-web/dist/*` into
   `kenny-server/kenny_server/webui/`) that runs **before** the Python
   install/test steps, or committing the built output into the repo/webui
   directory as a build artifact so a plain `pip install -e .` never needs
   Node at all. The latter is what keeps the CLAUDE.md-stated end-user
   contract intact — "end users installing the wheel never need Node" reads
   as: Node is a repo-maintainer/CI-time dependency, never a runtime or
   install-time one for a wheel consumer.
2. **Docker image** (`kenny-server/Dockerfile`) — this is the more natural
   place for the actual build step: convert the Dockerfile to
   **multi-stage**, adding a `FROM node:XX-slim AS web-build` stage that
   `COPY`s `kenny-web/` (from the same repo-root build context
   `compose.yaml`/`_release-artifacts.yml` already use), runs `npm ci &&
   npm run build`, and then the existing `FROM python:3.14-slim` stage
   `COPY --from=web-build /web/dist kenny_server/webui/` in place of (or
   alongside) today's plain `COPY kenny-server/ /app/`. This keeps the
   published GHCR image self-contained (`docker/build-push-action` in
   `_release-artifacts.yml`'s `server-image` job needs no changes beyond
   the Dockerfile itself — it already just builds whatever `file:
   kenny-server/Dockerfile` says) and keeps Node **out** of the final
   runtime image (multi-stage discards the `node` stage's layers).
3. **CI** (`.github/workflows/ci.yml`) — the `changes` job's `paths-filter`
   (`server`/`server_code` filters) and the `server` job
   (`pip install -e ".[dev]"` + `ruff check` + `pytest`) will both need a
   new `kenny-web`-aware filter and a preceding Node build job/step, since
   `server_code` today is explicitly defined as `kenny-server/**` *minus*
   `kenny_server/webui/**` specifically so pure-UI PRs skip the
   agent-tunnel e2e jobs (line 39-43: "Pure UI changes don't affect the
   agent<->server wire behaviour, so they skip the e2e jobs") — that
   filter's assumption (webui changes are UI-only, contract-irrelevant)
   still holds for a Node-built frontend and should be preserved, just
   pointed at `kenny-web/**` as well.
4. **`docs.yml`** (MkDocs → GitHub Pages) is independent of the dashboard
   frontend entirely — it only ever installs `mkdocs-material` +
   `mkdocs-awesome-pages-plugin` and runs `mkdocs build --strict`; a
   `kenny-web` Node build has no reason to touch this workflow.
5. **`release.yml`/`release-dev.yml`/`_release-artifacts.yml`** — both
   release channels funnel through `_release-artifacts.yml`'s
   `server-image` job, which builds `kenny-server/Dockerfile` directly via
   `docker/build-push-action`; if the Node build is folded into the
   Dockerfile as a build stage (option 2 above), these workflows need
   **no changes** at all — Docker Buildx already handles the multi-stage
   build and its own cache (`cache-to: type=gha,mode=max` /
   `cache-from: type=gha`) transparently. This is the cleanest single
   insertion point: **put the Node build stage inside the Dockerfile**,
   and separately mirror the same `npm ci && npm run build` step into
   `ci.yml`'s `server` job (or a new parallel job) purely so PR-time
   `pytest`/`ruff` runs against a freshly built `webui/` too, without
   duplicating release-time logic.

---

## 5. Repo policy that constrains doc work

### Root `CLAUDE.md` (already in context above)

Two invariants bind this rewrite directly:
- *"Every document states what holds now... A line that can go stale when
  code or architecture changes belongs at its source (ADR or contract), not
  in a CLAUDE.md."* — reinforces that `docs/*.md` (not ADRs, not
  CLAUDE.md) is exactly where the *current* dashboard IA must be described,
  and that old-IA language is a correctness bug, not a style nit.
- ADR discipline: *"Write one when the change moves a structural
  boundary... If you cannot name one it is not an ADR: a... UI layout... belong
  in the code and the commit message."* — **the IA/nav consolidation and the
  Nullthrone visual system are, by this rule, explicitly UI-layout /
  presentation changes, not architecture** — they do not get their own ADR
  entries (no new ADR file is implied by this rewrite), which also means
  `.claude/hooks/doc-drift.py`'s ADR record-set checks (§ below) are
  orthogonal to this work and won't be triggered by it unless a rewrite
  session also happens to edit an ADR file or add an `ADR-NNNN` citation
  somewhere.

### `CONTRIBUTING.md`

Restates the same contract-first/ADR-minimal posture and the build/test
commands (`cd kenny-server && pip install -e ".[dev]" && pytest -q && ruff
check .`; `cd kenny-agent && cargo test && cargo build`) — CI runs exactly
these (confirmed against `ci.yml` above). Nothing here is IA-specific beyond
"Update docs and add tests for new behavior" in the PR checklist, which is
the human-facing mirror of the doc-drift hook below.

### `.claude/hooks/doc-drift.py` + `.claude/doc-drift-map.json`

This is a Stop-hook (registered for Claude Code sessions in this repo, per
its own docstring and `.claude/settings.json`) that **will fire on doc
rewrite sessions** in specific, predictable ways other agents need to plan
around:

**What it enforces**, precisely:
1. **Source→doc drift.** `doc-drift-map.json` defines `rules`, each with a
   `sources` glob list and a `docs` list. A rule is "satisfied" for a
   session if *at least one* of its `docs` files is also in the session's
   changed-file set. If a session touches a rule's `sources` but none of
   its `docs`, the Stop hook blocks with a message naming the rule, what
   was touched, which docs to update, and which named `Shot`s
   (`scripts/screenshots/capture.py --only <names>`) may need
   regenerating. Concretely relevant rules for this consolidation work:
   - `dashboard-ui`: source `kenny_server/webui/index.html` →
     `docs/dashboard.md`, screenshots `header, overview, overview-light,
     fleet-console, agent-detail, drilldown, flagged, reliability,
     screenshot-modal, share-link, about, chat-history`. **This rule's own
     screenshot list is itself now stale against the new IA** (e.g. it
     still names `fleet-console`/`flagged`/`overview` rather than the
     renamed/obsoleted shots this report identifies in §2) — whoever
     rewrites `kenny_server/webui/index.html` for the new IA should also
     update this map entry, or the hook will keep pointing agents at
     obsolete screenshot names.
   - `dashboard-api` (`webui/__init__.py` → `dashboard.md` + `telemetry.md`),
     `health-rules`, `fleet-analytics`, `parental-controls`,
     `account-governance`, `alerting`, `tool-catalog`, `runtime-settings` —
     all map various server-side Python modules to the same doc set this
     report covers; a session that both redesigns the frontend *and*
     touches any of these Python modules (likely, since Inbox/Log
     consolidation probably needs new/renamed `/api/*` routes) will need
     to satisfy multiple rules at once.
   - **A pure doc-content rewrite session that touches no source files
     under any rule's `sources` glob will not trip this hook at all** — it
     only fires on the reverse direction (code changed, doc didn't), never
     "doc changed without code." So agents doing this task's actual
     deliverable (rewriting `docs/dashboard.md` prose to match the new IA)
     face **no doc-drift hook resistance** as long as they aren't also
     shipping the frontend code change in the same session. The hook
     *will* fire, though, the moment the actual `kenny_server/webui/
     index.html` IA rewrite lands without a matching `docs/dashboard.md`
     edit in the same session — i.e. code-first, then docs, in the same
     Claude Code session, or expect a Stop block.
2. **ADR record-set integrity** (`adr_rule`), independent of the doc-content
   rules above: whenever a session touches `docs/adr/0*.md`, the ADR index
   (`docs/adr/README.md`), or any file containing an `ADR-NNNN` citation, it
   checks that the ADR numbering is a gap-free `0001..N` sequence, that the
   index and the directory agree in both directions (every record has an
   index row pointing at its real filename and vice versa), and that every
   `ADR-NNNN` citation or `adr/NNNN-...` link anywhere in the *whole repo*
   (scanned by suffix: `.md .py .rs .json .html .yml .yaml .toml`, skipping
   `.git target node_modules __pycache__ .pytest_cache .venv` and one
   explicitly whitelisted vendored file,
   `kenny-server/kenny_server/webui/assets/echarts.min.js`) resolves to a
   real record. **Since this rewrite is UI-layout, not architecture (per
   CLAUDE.md above), it should touch no ADR files** — but if a rewriter is
   tempted to cite an ADR number in a new/edited doc paragraph, that
   citation now has to resolve or this check fires on any session that
   also happens to touch `docs/adr/0*.md`/the index in the same pass. Note
   too: this file-suffix scanner **will** walk a future `kenny-web/`
   TypeScript/JS source tree only if such files end up under one of its
   scanned suffixes and aren't under `node_modules` (which is skipped) —
   worth keeping in mind if `kenny-web/` grows its own citations.
3. **Escape hatches** (fail-open by design, so a hiccup here never wedges a
   session): `KENNY_SKIP_DOC_DRIFT=1` env var, or a `[skip-doc-drift]`
   marker in the tip commit message, suppresses the whole check. Also
   `stop_hook_active` (a loop guard — never blocks twice in a row) and any
   internal exception (git not available, bad JSON, etc.) all fail open to
   `_allow()`.

**Practical guidance for other agents touching docs for this task:**
- A docs-only session (this report's own deliverable, and likely the
  eventual `docs/dashboard.md` rewrite pass) will not be blocked by
  doc-drift.py by itself.
- A session that *also* redesigns `kenny_server/webui/index.html` (or any
  of the Python modules named in the rules) in the same pass **must**
  either also touch the mapped `docs/*.md` file(s) in that same session, or
  use the `[skip-doc-drift]`/`KENNY_SKIP_DOC_DRIFT` escape hatch with a
  clear reason.
- Do not add new ADR files for the IA/Nullthrone work unless a genuinely
  architectural boundary is being moved alongside it (e.g. if the Inbox
  redesign changes the ticket/approval *data model*, not just its
  presentation) — per CLAUDE.md's own test ("if you cannot name [a moved
  boundary] it is not an ADR").
- `.claude/doc-drift-map.json`'s `dashboard-ui` rule's `screenshots` array
  should be updated alongside `docs/dashboard.md` once the new shot names
  from §2 exist, so future drift messages point at real filenames.

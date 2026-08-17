# Backend map for the ten new/changed dashboard endpoints

Repo: `/home/claude/kenny`. Server: `kenny-server/kenny_server/` (Python 3.11,
FastMCP + Starlette, one ASGI app, one port — see `main.py::build_app`).
This note is a map for implementers, not a spec: it says which module/function
to extend, what already exists, what the response looks like today, which
tests cover the area, and the gotchas. File/line references are current as of
this read; re-check line numbers before editing (the files move).

Route builders (all called from `main.py::build_app`, all mounted before the
MCP catch-all `Mount("/", app=mcp_app)`):

- `webui/__init__.py::build_api_routes` — `/api/fleet*`, `/api/agent/*`,
  `/api/events`, `/api/audit`, `/api/settings*`, `/api/backups*`,
  `/api/updates*`, `/api/policy/*`, `/api/reliability/*`, webfilter, account
  governance.
- `webui/__init__.py::build_chat_routes` — `/api/chat*`,
  `/api/recommendation/stream`, `/api/forecast/stream`. Wrapped a second time
  in `main.py` with `guard(r.endpoint, min_role="operator")` for every route —
  the copilot is operator+ only.
- `webui/tickets.py::build_ticket_routes` — `/api/tickets*`, `/api/approvals*`,
  `/api/discord/*`, `/api/tool-classes`, `/api/ticket-rules*`,
  `/api/users/{uid}/profile` (capability profile).
- `webui/users.py::build_user_routes` — `/api/me*`, `/api/users*`,
  `/api/avatars`.
- `distribution.py::build_download_routes` — `/api/agents/{id}/installer`,
  `/api/agents/{id}/share-link`, `/api/agents/{id}/update`,
  `/api/agent-binary*`, and the public `/d/*` nonce-gated downloads.

---

## 1. `GET /api/today`

**Status: does not exist.** No `api_today`/`/api/today` anywhere in the repo.

**Where to add it:** a new handler in `webui/__init__.py::build_api_routes`
(same module as `api_fleet`/`api_fleet_overview`), registered `guard(api_today)`
(no `min_role`/`host_param` — same floor as `/api/fleet`, then filter by
`principal`).

**Data sources, already built, to compose from:**
- Crit/warn sections across the fleet: `fleet_stats._section_severity` (used by
  `/api/fleet/overview`) already returns `{section, warn, crit, members_warn,
  members_crit}` rows sorted worst-first — `fleet_stats.py:276-300`. Each
  `members_*` entry is `{agent_id, value, detail}` (`_member`,
  `fleet_stats.py:26-27`).
- Held approvals: `TicketStore.list_open_approvals()` (`ticketstore.py:1237`)
  or, for a merge that also carries the ticket, `TicketService` has no direct
  "list open approvals across tickets" convenience — go through the store.
  Note `ticketstore.py`'s partial-unique-index comment: at most one open
  approval per ticket.
- Stale tickets: `TicketStore.list(blocked_on_in=..., blocked_before=...)`
  (`ticketstore.py:635-697`) is exactly the stall-sweep's own query
  (`tickets.py::TicketService.nudge_stalled`, `tickets.py:1123-1193`) — reuse
  its cutoff logic rather than re-deriving "stale."
- `donut`: `fleet_stats._health_mix` (`fleet_stats.py:258-273`).
- `trend_30d`: `fleet_stats.aggregate_trend` — already wired at
  `GET /api/fleet/trend` (`webui/__init__.py::api_fleet_trend`,
  lines 210-240); its output shape (`{"days": [...]}`, one bucket per day with
  ok/warn/crit/unknown counts + `members`) is what `trend_30d` should just be.
- `kpis`: `fleet_stats._kpis` (`fleet_stats.py:163-255`) — reboot pending,
  open app updates, failed updates, quarantined threats, OS EOL, disks filling
  <30d. This is already exactly a KPI row list; `/api/fleet/overview` returns
  it verbatim as `.kpis`.
- Per-host AI forecast text (if `verdict_sentence` should read like the
  existing panel) is in `forecast.py::deterministic_summary` /
  `forecast_events` — but that's per-agent, not fleet-wide; for a fleet-wide
  one-sentence verdict you'd write new prose logic, there is no fleet-level
  equivalent today.

**Response shape today:** none — this is a new aggregate endpoint. The closest
existing shape is `/api/fleet/overview`'s `{generated_at, agent_count,
online_count, kpis, health, sections, os, device, posture, top, sankey,
reliability_categories}` (`fleet_stats.aggregate_overview`,
`fleet_stats.py:61-90`) — `/api/today` should probably be a thin re-packaging
of `aggregate_overview` + `aggregate_trend` + tickets/approvals, not a new
computation from scratch.

**Tests covering the area:** `tests/test_fleet_stats.py` (pure functions, no
HTTP), `tests/test_dashboard_api.py` (route-level, `_fleet_summary`,
`/api/audit` shape), `tests/test_forecast.py`, `tests/test_trends.py`.

**Gotchas:**
- Host scoping: `api_fleet`/`api_fleet_overview`/`api_fleet_trend` all call
  `_known_ids` then `visible_ids(principal, ids)` before touching per-agent
  data (`webui/__init__.py:151-160, 174-177, 224-227`) — `/api/today` must do
  the same for its "items" ranking and its ticket/approval reads (tickets
  already self-scope via `TicketStore.list(requester_user_id=...)` for a
  `user`, but approvals list (`/api/approvals`) has **no** scoping today — see
  item 2's gotcha).
- "capped at 3" ranking crit > warn > held approvals > stale tickets is new
  logic; nothing in the repo currently merges these four sources into one
  ranked list.
- `_daily_latest_cached` (`webui/__init__.py:135-149`) is a per-app-instance,
  15s TTL memo over `store.daily_latest` keyed by `(agent_id, since)` — reuse
  it (don't re-query) if `/api/today` also needs the 30-day daily history for
  `trend_30d`.

---

## 2. `GET /api/inbox?group=needs_you|waiting|working|new|done`

**Status: the grouping vocabulary already exists for tickets only; the merge
with sections/approvals does not.**

- `TicketStore.counts()` (`ticketstore.py:699-735`) already buckets tickets
  into exactly these five groups server-side, and documents the rule:
  `needs_you` = blocked on `approval`/`operator`, or a `new` alert-origin
  ticket; `waiting` = blocked on `user`; `working` = `in_progress` unblocked;
  `new` = not started, has a requester; `done` = resolved/closed/cancelled
  collapsed.
- `GET /api/tickets/summary` (`webui/tickets.py::api_tickets_summary`,
  lines 301-311) already serves these counts, narrowed to
  `requester_user_id=principal.user_id` for a scoped `user`.
- `GET /api/tickets?state=&states=` (`api_tickets_list`, lines 243-281) lists
  tickets with computed affordances (`_affordances`, lines 181-200:
  `allowed_transitions`, `allowed_blocks`, `can_unblock`) but does **not**
  itself filter by the `needs_you`/`waiting`/... bucket — the frontend
  (`index.html:2093-2099`) re-derives the bucket client-side from
  `state`/`blocked_on`/`requester_user_id` using the identical rule.
- `GET /api/approvals?ticket_id=` (`api_approvals_list`, line 614) lists open
  approvals, unscoped by host/user (see gotcha below).

**Where to add `/api/inbox`:** `webui/tickets.py` is the natural home (it
already imports `TicketStore`/`TicketService` and owns the bucket vocabulary),
registered alongside the other ticket routes in
`build_ticket_routes`/`webui/tickets.py:947-1043`. Alternative: a new function
in `webui/__init__.py` that pulls from both `TicketStore` and
`fleet_stats`/`health_rules` for the flagged-sections half — but then it needs
`tickets`/`store` (TicketStore) threaded into `build_api_routes`, which today
it is not (tickets are wired separately via `build_ticket_routes` in
`main.py:649-657`). Simplest: add it to `build_ticket_routes` and pass in
whatever health/fleet inputs it needs as extra keyword args (mirrors how
`build_api_routes` already takes a dozen optional collaborators).

**`waits_on` discriminator:** no existing field named this. The closest analog
is `Ticket.blocked_on` (`""`/`"user"`/`"approval"`/`"operator"`,
`ticketstore.py:70-90` in `tickets.py`) — for a flagged section there is no
"waits on" concept at all (it's just a health finding), so the new endpoint's
merge logic has to invent a `waits_on` value per source (e.g.
`"operator"`/`"user"`/`""` for tickets from `blocked_on`, something synthetic
like `"attention"` for flagged sections, `"approval"` for held approvals).

**Approve/deny addressable from this list:** already exists as a *separate*
call — `POST /api/approvals/{aid}` (`api_approval_decide`,
`webui/tickets.py:619-695`, `min_role="operator"`). It both decides the gate
*and* resumes the ticket via `TicketAssistant.resume` (not just a flag flip —
see the long docstring at 619-643: the decision is durable before the resume
starts, and a resume failure is logged/reported but never raised, since the
decision already happened). `/api/inbox` doesn't need to reimplement approve —
it just needs to surface enough (`approval.id`, `approval.ticket_id`) for the
frontend to call the existing route.

**Tests:** `tests/test_ticketstore.py` (counts/list/blocked filters),
`tests/test_tickets_api.py`, `tests/test_tickets.py` (lifecycle),
`tests/test_approval_persistence.py` (approvals survive a restart, decide →
resume flow, exercised over the *real* HTTP routes + real
`OperatorAuthMiddleware` + a real PAT — good template, see idiom section
below).

**Gotchas:**
- `GET /api/approvals` has **no** role/host guard beyond
  `guard(api_approvals_list, min_role="operator")` — it is already
  operator-only (`webui/tickets.py:997`), so a merged `/api/inbox` that
  includes approvals must stay operator-gated for that slice, or filter
  approvals by the requesting user's tickets when `min_role="user"`.
- `TicketService.decide_approval` enforces **who** may approve at the service
  layer, not just the route: `operator_approval` requires an operator role,
  `user_consent` requires the ticket's own requester (`tickets.py:970-1032`,
  "Approving is enforced here"). Don't re-derive this in the new route —
  delegate to `tickets.decide_approval`/`TicketAssistant.resume`.
- SQLite write serialization (ADR-0051, `store.py:76-157`): any write path the
  new endpoint's approve/deny action touches must go through
  `store.write_lock()` (already true inside `ticketstore.py`) — a merged
  inbox is read-mostly so this mainly matters if you add new write helpers.

---

## 3. `GET /api/log?kind=tools|alerts|events&q=&cursor=`

**Status: the underlying merged table already exists; cursor pagination and
free-text search do not.**

- `store.py::EventStore` (lines 392-579) is **already** one SQLite table
  (`events`) holding server log lines (`kind='log'`), forwarded agent log
  events (`kind='log', source='agent'`), tool-call audit (`kind='audit'`,
  written by `EventStore.insert_audit`, called from `CallLog.record` in
  `tools.py`), and alert history (`kind='alert'`, written by
  `EventStore.insert_alert`, called from `alerting.py`/backup/restore staging
  events). This is exactly ADR-0017's design (`docs/adr/0017-...md`).
- `EventStore.query(*, agent_id=None, level=None, kind=None, limit=200)`
  (`store.py:503-532`) already does most of what `/api/log` needs, minus a
  text search and minus cursor pagination (it's `LIMIT` only, no
  keyset/offset).
- `GET /api/events` (`webui/__init__.py::api_events`, lines 456-486) already
  exposes this with `agent`/`level`/`kind`/`limit` query params and host
  scoping (see gotcha).
- `GET /api/audit` (`webui/__init__.py::api_audit`, lines 424-454) is a
  *separate*, `CallLog`-backed (not `EventStore`-backed) audit view that
  additionally annotates `state_changing`/`tool_class` per entry
  (`tool_classes.classify`) — note this is **not** the same audit data as
  `EventStore`'s `kind='audit'` rows; `CallLog` (`tools.py`) is its own
  in-memory-plus-`EventStore`-mirrored structure. Check `tools.py::CallLog`
  before assuming `/api/log?kind=tools` should read `EventStore` alone — it
  may need to read `CallLog.list()` for the `tool_class` annotation the way
  `/api/audit` does.

**Response shape today (`/api/events`):** `{"entries": [...]}`, each entry
`EventStore._row_to_event` shape: `{at, agent_id, source, level, kind, tool,
ok, error, target, message, fields}` (`store.py:564-578`). This is close to
but not identical to the target `{ts, kind, tag, host, actor, message, meta}`
— `at`→`ts`, `agent_id`→`host`, no `tag`/`actor` field exists yet (would need
to be derived from `source`/`tool` or added), `fields`→`meta`.

**Where to add it:** `webui/__init__.py::build_api_routes`, next to
`api_events`/`api_audit`. Cursor pagination needs a new `EventStore` method
(keyset on `(at, id)` — the table already has `id INTEGER PRIMARY KEY
AUTOINCREMENT` and `ORDER BY at DESC, id DESC` — cursor = last-seen
`(at, id)` pair, same pattern as most of this codebase's `ORDER BY x DESC, id
DESC LIMIT ?` queries). Free-text `q` needs a new `WHERE message LIKE ?`
clause (no FTS table exists — a `LIKE '%...%'` scan is consistent with this
codebase's scale, see `EventStore.query`'s existing clause-building style).

**Tests:** `tests/test_eventstore.py`, `tests/test_dashboard_api.py`
(`test_audit_endpoint_shape_and_classification`), `tests/test_call_log.py`,
`tests/test_logging_config.py` (log→EventStore drain).

**Gotchas:**
- Host scoping on `/api/events` is already non-trivial and asymmetric
  (`webui/__init__.py:472-486`): if the caller names a specific `agent` they
  must be allowed to see it (403 otherwise); if they don't, the **whole
  result set** is filtered post-query to `principal.may_see`. A merged
  `/api/log` needs the same two-step check, and cursor pagination interacts
  with post-filtering — filtering after `LIMIT` can return short pages for a
  scoped user. Consider filtering in SQL (`agent_id IN (...)` for a scoped
  user) instead of post-filtering once cursor pagination is added.
- `kind='tools'` in the new endpoint's vocabulary maps to `EventStore`'s
  `kind='audit'`, not literally `'tools'` — don't assume the query params are
  a 1:1 passthrough to the stored `kind` column.
- Alert history (`kind='alert'`) rows are also read by
  `digest.py::build_digest` — don't change `EventStore.insert_alert`'s stored
  shape without checking the digest.

---

## 4. `GET /api/fleet` — add `summary` and `severity_label`

**Status: `summary` already exists. `severity_label` does not.**

`webui/__init__.py::api_fleet` (lines 151-160) calls `_overview()`
(lines 1712-1741) per agent, which **already** returns a `summary` field via
`_fleet_summary(health, snapshot)` (lines 1744-1757): worst crit section's
`reason`/`summary` (or "+N more" if several), else worst warn, else `"all
green"`, else `"no telemetry yet"` if there's no snapshot at all. This is
tested directly in `tests/test_dashboard_api.py`
(`test_fleet_summary_all_green`, `_worst_section_with_reason`, `_warn_when_no_crit`,
`_no_telemetry`) — these tests call `_fleet_summary` as a plain function, no
HTTP.

Task 4 says *"or 'all quiet' + a notable stat"* — current wording is `"all
green"`, not `"all quiet"`; check with the requester whether the copy is meant
to change or whether task 4's example is just illustrative.

**`severity_label` (e.g. `CRITICAL · DISK`) does not exist.** Nearest existing
pattern: `fleet_stats.py`'s `_section_severity` names the worst-per-section,
and `health_rules.worst()` (`health_rules.py:24-27`) is the ok/warn/crit
comparator already used to compute `overall` in `_overview`
(`webui/__init__.py:1719`, via `build_health`). A `severity_label` would be a
new one-line formatter: `f"{overall.upper()} · {worst_section_name.upper()}"`,
built from the same `sections` dict `_fleet_summary` already walks (the worst
crit/warn section's `name`, currently discarded — `_fleet_summary` only keeps
`text`/`reason`, not the section `name`, at line 1753).

**Where to change it:** `webui/__init__.py::_overview` (line 1730-1741, the
dict literal) and `_fleet_summary`/a new sibling helper, same file.

**Response shape today:** `api_fleet` returns `{"overall": ..., "agents":
[...]}`, each agent dict from `_overview`:
```python
{
    "agent_id": ..., "online": ..., "os": ..., "meta": ...,
    "overall": health["overall"],
    "flagged_sections": [...], "warn_sections": [...], "crit_sections": [...],
    "summary": _fleet_summary(health, snapshot),
    "collected_at": ...,
}
```
(`webui/__init__.py:1730-1741`).

**Gotchas:**
- `_overview` is also called from `tests` and possibly other internal code —
  check call sites before changing its signature; adding a new key is
  additive and safe, changing an existing key's meaning is not.
- Host scoping for `/api/fleet` is already handled at the route level
  (`ids = visible_ids(principal, ids)`, line 154-156) before `_overview` runs
  per id, so nothing new is needed there.

---

## 5. Host telemetry — per-section `attention: true|false`

**Status: does not exist as a field; the underlying status already carries
the same information under a different name.**

`health_rules.py::evaluate_section` (lines 606-628) returns `{"status": "ok"|
"warn"|"crit", "summary": ..., "reason"?: ...}` per section —
`health_rules.py` is the sole owner of thresholds (per `kenny-server/CLAUDE.md`:
*"health thresholds live only in `health_rules.py`"*). `attention` is a
one-line derivation: `status != "ok"` (or `status in ("warn", "crit")`,
equivalently `status != "ok"` given `_ORDER = {"ok": 0, "warn": 1, "crit": 2}`,
`health_rules.py:21`).

**Where to add it:** `health_rules.py::evaluate_section` itself (append
`"attention": final != "ok"` to the returned dict at line 628) is the single
place this needs to be computed, since `evaluate_snapshot` and every consumer
(`tools.py::build_health`, `fleet_stats.py`, `webui/__init__.py::_overview`,
`toolloop.py::_agent_health`, `chat.py`/MCP `agent_health` tool) all flow
through it. Adding a key to this dict is additive — no consumer destructures
positionally.

**Tests:** `tests/test_health_rules.py` covers every rule function directly;
add `attention` assertions there. `tests/test_fleet_stats.py` and
`tests/test_dashboard_api.py` exercise the downstream shape.

**Gotchas:**
- `evaluate_snapshot` skips `WINDOWS_ONLY_SECTIONS` for non-Windows agents
  (`health_rules.py:591-593, 653`) — those sections never appear in `sections`
  at all for a Linux/macOS agent, so there's no `attention` to compute for
  them; don't backfill a stub.
- The MCP tool `agent_health` (`toolloop.py::_agent_health`, lines 487-496)
  spreads `**health` directly into its return payload — adding `attention` to
  each section flows through to the MCP surface too, which is fine (additive)
  but means this "dashboard" field is also visible to Claude over MCP. If
  that's undesired, `attention` would need stripping at the MCP boundary
  instead of at the source.

---

## 6. Chat — `scope: host_id|fleet`; distinguish `auto_run` from `needs_confirmation`

**Status: partially exists.** Per-session agent scoping already exists under
the name `agent_id`, not `scope`. The auto-run/confirm distinction exists
structurally (different event types) but not under those literal names.

**Scope / context injection (already exists, different field name):**
- `FleetSession.agent_id` (`chat.py:169`) is the session's selected host;
  `None` means fleet-wide. `POST /api/chat`/`/api/chat/stream` set it from the
  request body's `agent_id` on **every** call, including clearing it back to
  `None` (`webui/__init__.py:1453-1454, 1511-1512` — the comment explicitly
  warns against only syncing when non-empty, "otherwise the session would
  keep pointing ... at a stale agent").
- The model is told about the selection via `chat.py::_context_note`
  (lines 131-156), an *uncached* system block appended after the cached
  `_SYSTEM_PROMPT` (`FleetPolicy.system_blocks`, line 260-261) — kept separate
  from the cached block specifically so per-session state doesn't bust the
  Anthropic prompt cache prefix.
- If task 6 literally wants a `scope` field (`"host_id"` vs `"fleet"`) instead
  of/alongside `agent_id`, the cleanest change is additive: keep `agent_id` as
  the host id when scoped, and derive `scope` as `"host_id" if session.agent_id
  else "fleet"` — don't replace the existing field, `run_turn`/`_drive`/
  `toolloop.resolve_target` all key off `session.agent_id` directly
  (`chat.py:266-270`, `toolloop.py:517-535` `_resolve_chat_target`).

**auto_run vs needs_confirmation (structurally present, not named that):**
- `toolloop.py::drive_events` (lines 567-741) already yields distinct event
  types for the two cases: a read-only/allowed tool executes inline and
  yields `{"type": "tool_result", ...}` (line 682); a held state-changing
  tool yields `{"type": "pending", "tool", "args", "agent_id"}` (line 668)
  **before** pausing the whole loop (`yield {"type": "done", ..., "pending":
  ..., "done": False}` then `return`, lines 670-677) — the "frozen args"
  requirement in task 6 is already true: `PendingCall.args` is captured at
  hold time (`toolloop.py:133-154`) and `_resolve_chat_target` already pops
  any `agent_id` override from `args` and freezes the resolved `target`
  before the gate runs (`toolloop.py:611-617`, "so a dashboard agent switch
  ... can't retarget it" — this is ADR-0038).
- The gate decision itself: `chat.py::FleetPolicy.gate` (lines 272-277) is a
  two-line function — `Allow()` if `classify(tool) == READ_ONLY` else
  `Hold("operator_approval")`. Both change tiers (`standard_change` and
  `normal_change`, `tool_classes.py`) are held identically on this surface
  (ADR-0045's "the dashboard holds both change tiers").
- Renaming the wire event types from `tool_result`/`pending` to
  `auto_run`/`needs_confirmation` would be a **frontend-visible contract
  change** — `index.html`'s `handleChatEvent` (and `webui/tickets.py`'s
  `api_ticket_chat_stream`, which explicitly documents reusing "exactly
  `toolloop.drive_events`'s" vocabulary, `webui/tickets.py:454-458`) both key
  off these literal type strings. Prefer **adding** an `auto_run: bool` field
  to the existing `tool_result` event (`true` when the tier was `read_only`)
  rather than renaming the event type — additive, no frontend breakage.

**Where to change it:** `chat.py::FleetPolicy` (scope field, if literally
required) and `toolloop.py::drive_events`'s event-construction sites (lines
668, 682-693) for the `auto_run`/`needs_confirmation` field, if adding it.

**Response/event shapes today:** see `toolloop.py:567-593` docstring for the
full enumerated event vocabulary (`text_delta`, `tool_result`, `pending`,
`denied`, `done`).

**Tests:** `tests/test_chat.py`, `tests/test_chat_stream.py`,
`tests/test_toolloop.py`, `tests/test_tool_classes.py` (classification +
"agent-parity" exhaustiveness, see ADR-0045).

**Gotchas:**
- **ADR-0045 invariant:** "the tier is a property of the tool; the gate is a
  property of the calling surface." Do not let a `scope=fleet` vs
  `scope=host_id` distinction change *which tier* a tool is classified as —
  only which surface/policy decides what to do with the tier. If a future
  need is "fleet scope should hold more things than host scope," that's a new
  `LoopPolicy.gate` branch on `session`/context, not a change to
  `tool_classes.TOOL_CLASSES`.
- **ADR-0023 invariant (gate parity):** `remotehelp_start`/`remotehelp_stop`
  must stay in the state-changing set because the agent's own
  `control.rs::is_mutating` considers them mutating — `tests/test_tool_classes.py`
  holds a literal copy of the agent's list and fails the build on drift. Don't
  reclassify anything without checking that test.
- `_context_note` is per-session, uncached — adding more session state to the
  system prompt (e.g. a fleet-wide `scope` sentence) is safe to bolt on here,
  but don't move it into `_cached_system()` (that block must stay identical
  across sessions for prompt caching to work).
- `run_turn`/`confirm_pending` both raise `RuntimeError` if
  `session.pending is not None` (`chat.py:508-509`, `560-561`) — a pending
  hold must be resolved via `/api/chat/confirm` before another message can be
  sent. This is existing, unrelated-to-scope behavior worth knowing before
  touching the turn functions.

---

## 7. Config catalog — `{value, source, help}` per key, env-derived read-only

**Status: already fully implemented.** This item as specified already exists
end-to-end; verify with the requester whether anything beyond this is wanted.

- `config.py::CATALOG` (`_SPECS`, lines 165-448) is the single source of
  truth: one `SettingSpec` per key with `label`, `help`, `lifecycle`
  (`"live"|"restart"|"env_only"`), `type`, `choices`, `min`/`max`,
  `sensitive`.
- `Settings.describe()` (`config.py:550-584`) returns **exactly** the shape
  requested: grouped by `GROUP_ORDER`, each row `{key, group, type, label,
  help, lifecycle, source, choices, min, max, sensitive, value, default}` (or
  `value: None, is_set: bool` for `sensitive` specs — secrets never echo).
  `source` is one of `"db"|"env"|"default"` (`Settings._resolve_raw`,
  `config.py:510-516`).
- `SettingSpec.writable` (`config.py:95-96`) is `lifecycle != "env_only"` —
  env-derived keys are already read-only: `PUT /api/settings/{key}` and
  `DELETE /api/settings/{key}` both check `spec.writable` and return `403`
  otherwise (`webui/__init__.py::api_settings_set`, lines 675-701;
  `api_settings_reset`, lines 703-717).
- Routes already exist: `GET /api/settings` (`guard(..., min_role="superuser")`,
  `webui/__init__.py:1264`), `PUT /api/settings/{key}`, `DELETE
  /api/settings/{key}` (lines 1265-1268).

See ADR-0032 (`docs/adr/0032-runtime-settings-in-the-dashboard.md`) for the
full rationale, including the important caveat: only settings whose consumer
actually reads through `Settings.get()` (marked `"live"`) take effect without
a restart — `lifecycle` is "the honesty mechanism," not decorative.

**Tests:** `tests/test_config.py` (catalog shape, `describe`/`describe_one`,
writable/env_only enforcement, group slugs).

**Gotcha:** this whole surface is `min_role="superuser"` — if task 7 means to
expose it (read-only) to `operator`/`user` too, that's a `guard()` change on
the three routes, not a `config.py` change.

---

## 8. `POST /api/tickets {host, description, start_immediately}`

**Status: `POST /api/tickets` already exists with a different, richer body
shape; `start_immediately` does not exist as a concept.**

`webui/tickets.py::api_tickets_create` (lines 313-341), registered at
`POST /api/tickets` (`min_role="user"`, line 951). Current body:
`{title, priority, category, requester_user_id (operator-only), agent_id,
summary, origin}` — `origin` defaults to `"dashboard"` (line 333). A ticket is
always created in state `"new"` (`TicketService.create`, `tickets.py:476-526`)
— there is **no** "start immediately" flag; `new -> in_progress` is a
`system`/`operator`-only transition (`_ACTORS[("new", "in_progress")]`,
`tickets.py:103`), deliberately *not* available to `requester` ("opening a
ticket does not entitle its author to drive its lifecycle").

**Mapping task 8's shape onto today's:**
- `host` → `agent_id` (already a field, already frozen at creation —
  `TicketService.create`'s docstring: "`agent_id` is frozen here: it is the
  routing target every later tool call is checked against").
- `description` → `summary` (already a field).
- `start_immediately` → not supported. To add it: after `tickets.create(...)`
  succeeds, call `tickets.transition(ticket.id, "in_progress", actor="system",
  reason=...)` — but note the actor must be `system`/`operator`
  (`_ACTORS[("new","in_progress")]`), so a `user`-role caller's own request
  can't legally drive that second call as themselves; the route would issue
  it as `actor="system"` on the caller's behalf, which is a policy decision
  worth flagging to the requester (is "start immediately" really "let the
  requester force this into `in_progress`," bypassing the deliberate
  new→in_progress gate?).

**Where to change it:** `webui/tickets.py::api_tickets_create`, additive body
fields, plus an explicit `tickets.transition(...)` call after `tickets.create(...)`.

**Tests:** `tests/test_tickets_api.py`, `tests/test_tickets.py`,
`tests/test_ticket_assistant.py`.

**Gotchas:**
- `origin="dashboard"` already exists and is exactly what "dashboard-
  originated" tickets use today — no new origin value needed.
- A scoped `user` may only create a ticket for themselves
  (`requester_user_id = principal.user_id`, line 330) — `host` is unrestricted
  today (any `agent_id` string is accepted, even one outside the caller's host
  scope!). Check whether `api_tickets_create` should also enforce
  `principal.may_see(agent_id)` for a scoped `user` — currently it does not,
  which looks like a pre-existing gap worth flagging (a `user` could open a
  ticket routing to a host they can't otherwise see; ticket routing target is
  security-relevant per `tickets.py`'s own docstring about `agent_id` being
  "frozen ... the routing target every later tool call is checked against").

---

## 9. `POST /api/agents/share-link {name, os}`

**Status: 90% already exists** as `POST /api/agents/{id}/share-link?os=`
(path param instead of body `name`), with a 1-hour TTL instead of 24h. There
is no separate "new agent" creation step — `agent_id` (task 9's `name`) is
just a free-text string the operator types into the dashboard's "Add a PC"
field (`index.html:4821`, `placeholder="agent id (e.g. study-pc)"`) and
share-linking an id that has never enrolled is exactly the existing,
supported first-time-onboarding flow.

`distribution.py::build_download_routes::share_link` (lines 432-462):
- `POST /api/agents/{id}/share-link` (Windows, default): mints a nonce via
  `ShareLinks.create(agent_id, "installer", INSTALLER_TTL_S)`
  (`INSTALLER_TTL_S = 3600`, line 47), returns `{url:
  "<public>/d/installer/{nonce}", expires_in: 3600}`.
- `POST /api/agents/{id}/share-link?os=linux[&arch=]`: mints **two** nonces (a
  paired `binary` nonce + an `install` nonce carrying it), returns
  `{url, oneliner: "curl -fsSL <url> | sudo sh", expires_in: 3600}`.

**Token minting — exactly where and when, precisely:**
- The share-link mint (`share_link` handler) does **not** mint a token. It
  only mints a `ShareLinks` nonce.
- The **token** is minted lazily, on first fetch of the link:
  - Windows: `GET /d/installer/{nonce}` → `public_installer` (line 488-502)
    consumes the nonce (`share_links.resolve(nonce, "installer",
    consume=True)`) then calls `token_store.create_or_rotate(agent_id)`
    (line 496) and bakes the plaintext token into the ZIP's
    `kenny-agent.setup.json` (never returned to the browser/API caller).
  - Linux: `GET /d/install/{nonce}` → `public_install` (line 464-486)
    consumes the *install* nonce, then similarly calls
    `token_store.create_or_rotate(agent_id)` (line 472) and bakes the token
    into the generated `install.sh`'s `--enroll-token` argv (never written to
    disk on the target box, per `_install_sh`'s docstring).
- `AgentTokenStore.create_or_rotate` (`tokenstore.py:165-191`) is the actual
  mint: `secrets.token_urlsafe(32)`, only the sha256 hash is persisted, the
  plaintext is returned exactly once. A prior token is demoted to a
  **grace-period** token (`KENNY_TOKEN_GRACE_SECS`, default 7 days) rather
  than instantly invalidated (ADR-0014) — re-sharing a link for an agent that
  is still live on its old token doesn't brick it mid-rotation.
- The actual first-contact key exchange is separate again:
  `POST /api/agents/{id}/enroll` (`distribution.py:504-541`) is where the
  agent binds its Ed25519 public key, authenticated by the enroll token as a
  bearer/JSON field (ADR-0022) — `KeyStore.enroll` (not read in this pass;
  see `keystore.py`).

**Existing single-use / expiry semantics (`ShareLinks`, `distribution.py:137-204`):**
- `_Nonce.used: bool` + `resolve_entry(..., consume=True)` marks it used on
  first read — a second fetch of the same `installer`/`install` nonce 404s
  (`"link invalid or expired"`). This is task 9's "single-use," already
  correct, just at a 1-hour TTL not 24h.
- `binary` nonces are deliberately **not** consumed on read (`consume=False`,
  `public_binary`, line 579) — "the agent's updater/installer may retry
  within the TTL." Don't consume-on-read a `binary` nonce if extending this.
- `ShareLinks` is explicitly **in-memory, dev-grade** ("like CallLog",
  `distribution.py:163`) — it does not survive a restart. A 24h-expiring
  share link that must survive a server restart mid-window needs `ShareLinks`
  (or a new store) backed by SQLite instead — this is the single biggest gap
  between what exists and what task 9 literally asks for ("24h-expiring URL,"
  implicitly durable).

**Where to change it:** `distribution.py::share_link` (body vs path param,
`name`→`agent_id`; `INSTALLER_TTL_S`→24h for this specific call, or a new TTL
constant/param), and, if durability across restarts matters,
`ShareLinks`/`_Nonce` → a new SQLite-backed store analogous to
`AgentTokenStore`.

**Docs/screenshot reference:** `docs/assets/screenshots/share-link.png` exists
and almost certainly documents the *current* `shareInstaller`/
`shareLinuxInstaller` UI flow (`index.html:6090-6103`,
`shareLinkModal`/`shareLinuxModal`) — read it before assuming task 9 wants a
net-new UI; it may just want the existing flow's endpoint reshaped to
`{name, os}` and a longer TTL.

**Tests:** `tests/test_distribution.py` (`test_share_link_then_public_download_once`
and neighbors — good template for single-use assertions).

**Gotchas:**
- **Auth gap:** none of `distribution.py`'s routes are wrapped in
  `webui.authz.guard()` — they rely solely on the blanket
  `OperatorAuthMiddleware` (any authenticated principal, any role, passes;
  `main.py:671-682` mounts `*download_routes` unwrapped). Concretely, a
  scoped `user` principal today **can** call `POST /api/agents/{id}/share-link`
  for *any* `agent_id`, including hosts outside their scope — there is no
  `min_role`/`host_param` check anywhere in `distribution.py`. This is a
  pre-existing gap, not something task 9 introduces, but a rewritten `{name,
  os}` endpoint is a natural place to fix it (`guard(share_link,
  min_role="operator")` at minimum, since minting an installer/enrollment
  path is provisioning, matching `/api/agents/{id}/token`'s existing
  `op`-gated pattern at `webui/__init__.py:1367`).
- `/d/*` paths are unauthenticated by design (`auth.py::_is_public`, line
  216, `"/d/"` prefix) — the nonce *is* the credential (ADR-0012/ADR-0030).
  Don't add operator auth to the `/d/*` GET routes; the security boundary is
  the nonce's unguessability + single-use + TTL, not a bearer token (the
  browser downloading the installer is not authenticated as an operator).
- `_public_url()`/`_wss_url()` (`distribution.py:89-103`) derive from
  `KENNY_PUBLIC_URL` (`urls.py::public_base_url`) — if `name`/`os` create a
  link before an agent has ever connected, nothing here depends on the agent
  existing yet, consistent with "serving the installer... minting the token
  on first use."

---

## 10. Persist per-user `theme: light|dark`

**Status: does not exist.** `UserStore`'s `users` table has no `theme`
column (`userstore.py:36-49`); `_public_user` doesn't project one
(`userstore.py:84-98`). The `capability_profile` column is the closest
existing "extra per-user setting" precedent — same file, same table, same
patch style.

**Where to add it:**
1. `userstore.py`: add `theme TEXT` to `_SCHEMA`'s `users` table (line 44,
   alongside `capability_profile`), and to `_MIGRATE_COLUMNS`
   (`userstore.py:77`, currently `("capability_profile",)`) so existing DBs
   get an `ALTER TABLE users ADD COLUMN theme TEXT` on next connect — follow
   `_migrate`'s existing pattern exactly (`userstore.py:121-128`).
2. Add `theme` to `_public_user`'s projection (line 84-98) and to
   `update_user`'s patchable-fields list (`userstore.py:260-303`, follow the
   `avatar`/`email` pattern — simple `if theme is not None: sets.append(...)`).
   A dedicated `set_theme(user_id, theme)` method (mirroring
   `set_capability_profile`, lines 325-347) is also reasonable if theme needs
   its own validation (`theme in ("light", "dark")`) independent of the
   generic `update_user` patch.
3. Route: `PATCH /api/me` already exists
   (`webui/users.py::api_me_update`, lines 99-112) and is the exact right
   place — it already validates `avatar` against a closed set
   (`AVATARS`) the same way `theme` would need `{"light", "dark"}` validated.
   Add `theme = body.get("theme")`, validate, pass through to
   `user_store.update_user(..., theme=theme)`.
4. `GET /api/me` (`api_me`, lines 76-97) already returns the full user row
   including whatever `_public_user` projects — `theme` flows through for
   free once added there. Note the **shared-token principal branch**
   (`principal.user_id is None`, lines 79-91) returns a hand-built dict with
   no backing row — add `"theme": None` there too so the response shape stays
   consistent for the legacy env-token superuser (who has no account to
   persist a theme against).

**Response shape today:** `GET /api/me` → `_public_user(row)` dict, `+hosts`,
`+is_shared_token` (`webui/users.py:92-97`); see `_public_user`,
`userstore.py:84-98`, for the exact key list to extend.

**Tests:** `tests/test_userstore.py`, `tests/test_user_profile.py` (the
capability-profile PUT/validation test is the closest existing template —
same shape of "closed vocabulary, superuser-only vs self-service" test),
`tests/test_auth.py`.

**Gotchas:**
- ADR-0033 (multi-user auth) is the authority for this whole area — read it
  before touching `userstore.py`/`auth.py`. Key invariant: the **synthetic
  env-token principal has no backing row** (`auth.py::_env_principal`,
  lines 100-108) — it is a superuser with `user_id=None`. Every `/api/me*`
  handler already special-cases `principal.user_id is None` as "shared-token
  session, no editable profile" (see `api_me_update`'s early return,
  `webui/users.py:101-102`) — a theme write for this principal has nowhere to
  persist to; keep returning the same `_err("shared-token session has no
  editable profile")` shape for `theme`, don't add a special exception.
- `avatar` validation happens **in the route handler**
  (`webui/users.py:105-106`, `if avatar is not None and avatar not in
  AVATARS: return _err(...)`), not in `userstore.py` — follow that placement
  for `theme` too, for consistency (validation lives at the API boundary in
  this module, unlike `set_capability_profile`'s in-store validation, which
  is the one inconsistency already in the codebase — pick whichever precedent
  you're extending and say so in the commit).
- If `theme` should also be settable by a superuser for *another* user (the
  `/api/users/{uid}` family, `webui/users.py::api_user_update`,
  lines 271-307), that's a second call site to update — decide whether theme
  is self-service-only (`/api/me` only) or also superuser-editable
  (`/api/users/{uid}` too, like `avatar`/`role`/`disabled` are today).

---

## Cross-cutting idioms

### The route-definition idiom (real example)

Every `/api/*` route is a plain `async def handler(request: Request) ->
JSONResponse`, wrapped in `webui.authz.guard(...)`, registered as a Starlette
`Route`. From `webui/__init__.py:1320-1330`:

```python
op = {"min_role": "operator"}
scoped = {"host_param": "id"}
op_scoped = {"min_role": "operator", "host_param": "id"}
return [
    ...
    Route("/api/fleet", guard(api_fleet)),
    Route("/api/fleet/overview", guard(api_fleet_overview)),
    Route("/api/fleet/trend", guard(api_fleet_trend)),
    Route("/api/digest/preview", guard(api_digest_preview, **op)),
    Route("/api/audit", guard(api_audit)),
    Route("/api/agent/{id}", guard(api_agent, **scoped)),
    Route("/api/agent/{id}", guard(api_remove_host, **op), methods=["DELETE"]),
    ...
]
```

The JSON response helper is Starlette's own `JSONResponse` — no project
wrapper. A representative handler (`webui/__init__.py:151-160`):

```python
async def api_fleet(request: Request) -> JSONResponse:
    ids = await _known_ids(registry, store)
    principal = principal_of(request)
    if principal is not None:
        ids = visible_ids(principal, ids)
    agents = [await _overview(i, registry, store) for i in ids]
    from .. import health_rules
    overall = health_rules.worst(*(a["overall"] for a in agents if a["overall"] != "unknown"))
    return JSONResponse({"overall": overall or "unknown", "agents": agents})
```

`guard()` itself (`webui/authz.py:71-93`):

```python
def guard(handler, *, min_role="user", host_param=None):
    @wraps(handler)
    async def wrapped(request: Request):
        try:
            principal = require_user(request, min_role)
            if host_param is not None:
                require_host(principal, request.path_params[host_param])
        except Forbidden as exc:
            return exc.response
        return await handler(request)
    return wrapped
```

`webui/tickets.py` composes an extra layer,
`g = lambda handler, **kw: guard(_catches_ticket_errors(handler), **kw)`
(line 945), so ticket routes get lifecycle-exception→JSON translation on top
of the role/host guard — a second `_catches_ticket_errors`-style wrapper is
the right template for any new module whose handlers raise typed
domain errors (`TicketError`/`TransitionError`/`BlockError` and friends,
`tickets.py:234-370`).

### Role/host-scoping enforcement (how new endpoints avoid leaking data)

Three layers, from `auth.py`/`webui/authz.py`, all documented in
`kenny-server/CLAUDE.md`:

1. **`OperatorAuthMiddleware`** (`auth.py:240-384`) resolves every HTTP
   request to a `Principal` (PAT → session cookie → legacy shared token, in
   that order) and stashes it on `scope["kenny_principal"]`; unauthenticated
   `/api`/`/mcp` requests 401, unauthenticated UI requests redirect to
   `/login`. `/agent/ws` and `/d/*` are exempt (their own credential model).
2. **`guard(handler, min_role=..., host_param=...)`**
   (`webui/authz.py:71-93`) is the per-route floor: `min_role` checks
   `principal.at_least(role)` (role hierarchy `superuser > operator > user`,
   `security.py`, not read in this pass but referenced from `auth.py:92`);
   `host_param` checks `principal.may_see(request.path_params[host_param])`
   for a **path-parameter** agent id.
3. **Inside the handler**, for anything that lists/aggregates across hosts
   rather than addressing one by path param: `visible_ids(principal, ids)`
   (`webui/authz.py:63-68`) filters a fleet-wide id list down to
   `principal.hosts` for a scoped `user` (no-op for operator+); or manual
   `principal.may_see(agent_id)` checks (e.g. `api_events`,
   `webui/__init__.py:472-486`).

**A `user`-role `Principal.hosts`** is a `frozenset[str]` populated from
`UserStore.get_user_hosts` at auth-resolution time
(`auth.py::_principal_from_row`, lines 267-291) — it is **not** re-read per
request beyond that; a host-scope change takes effect on the *next* login/PAT
resolution, not mid-session. `Principal.scoped` is `role == "user"`
(`auth.py:74-77`) — operator/superuser are always unrestricted.

**New endpoint checklist** (derived from the pattern above, apply to all ten
items): (a) always wrap in `guard()` with an explicit `min_role`; (b) for a
single-host route, use `host_param` so `guard` does the check; (c) for a
fleet-wide list/aggregate, call `visible_ids`/`principal.may_see` **inside**
the handler on every id it touches, including ids that arrive nested inside
another object (tickets' `agent_id`, approvals' `agent_id`, log rows'
`agent_id`) — `distribution.py` (item 9) is the counterexample to avoid:
routes with no `guard()` wrapping at all.

### The test idiom (real example)

The standard pattern builds the real `Starlette` app via
`kenny_server.main.build_app(db_path=...)` against a temp SQLite file, drives
it with Starlette's `TestClient`, and authenticates with the app's own
operator token (`app.state.operator_token`, set in `build_app`,
`main.py:738`). From `tests/test_dashboard_api.py:13-20, 66-89`:

```python
def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}

def test_audit_endpoint_shape_and_classification(tmp_path):
    app = build_app(db_path=str(tmp_path / "audit.sqlite"))
    with TestClient(app) as c:
        from functools import partial
        es = app.state.event_store
        c.portal.call(partial(es.insert_audit, agent_id="example-pc", tool="telemetry_collect", ok=True))
        c.portal.call(partial(es.insert_audit, agent_id="example-pc", tool="winget_update", ok=True))
        r = c.get("/api/audit", headers=_bearer(app))
        assert r.status_code == 200
        entries = r.json()["entries"]
        ...
```

`c.portal.call(...)` is `TestClient`'s bridge for calling an `async` store
method directly from a sync test body (seeding data the HTTP surface itself
has no write route for). `app.state.*` exposes essentially every singleton
(`registry`, `store`, `event_store`, `ticket_store`, `user_store`, `settings`,
...) for exactly this kind of direct-seed setup — see the full list at
`main.py:698-741`.

For **multi-user/role tests**, `tests/test_approval_persistence.py` builds a
`Dashboard` helper around `build_ticket_routes(...)` directly (not the whole
app) plus a hand-assembled `Starlette` + `OperatorAuthMiddleware`, when a test
needs to construct two independent "boots" over the same SQLite file to prove
persistence across a literal restart (`tests/test_approval_persistence.py:73-90`
and the module docstring, lines 1-19) — reach for `build_app` normally, and
only for this narrower "prove it's not just in-memory" class of test
should a new suite compose the ticket routes by hand the way this file does.

For fake-Anthropic-client tests (chat, forecast, recommendation, event
categorization), `build_app(client_factory=...)` accepts an injected factory
— see `webui._anthropic_client`'s docstring and `tests/test_chat.py`/
`tests/test_chat_stream.py` for the fake-client shape.

---

## `CLAUDE.md` / `kenny-server/CLAUDE.md` invariants that constrain this work

From `/home/claude/kenny/CLAUDE.md`:

- **The contract is authoritative.** `docs/protocol.md` + `docs/fixtures/`
  define the agent⇄server wire shape; change it there first, then both
  languages. None of the ten items touch the agent wire contract directly
  (they're all dashboard-side aggregation/reshaping of data the server
  already has), but double-check before adding any new field that would need
  to reach the agent side.
- **Python and Rust must not drift** — run `/contract-check` after touching
  the protocol. Not expected to apply to these ten items unless one of them
  needs a new agent-reported field.
- **Every seam two places must agree on gets a test that fails when they
  diverge** — directly relevant to items 5 and 6: `health_rules.py`'s
  thresholds vs every consumer that reads `sections[...]status`, and
  `tool_classes.TOOL_CLASSES` vs the agent's `control.rs::is_mutating`
  (already enforced by `test_tool_classes.py`'s literal-copy test per
  ADR-0023/0045 — extend, don't bypass, that test if item 6 touches
  classification).
- **Record architectural decisions as an ADR** when a change moves a
  structural boundary (language/runtime, wire contract shape,
  network/trust topology, auth model, storage/observability model,
  deployment/distribution shape, agent/session model). Candidates among the
  ten: item 9 (agent distribution shape — new nonce durability, new token-
  minting entry point) and item 6 (session model — a `scope` field) are the
  most likely to cross that bar; the others (1-5, 7, 8, 10) read as additive
  API/storage changes that stay inside existing boundaries and belong in code
  + commit message only.
- **Build & test:** `cd kenny-server && pytest` — not `python -m pytest`
  (the latter adds cwd to `sys.path` and hides import errors CI would catch).

From `/home/claude/kenny/kenny-server/CLAUDE.md`:

- **Telemetry read-paths** (`fleet_overview`, `agent_health`,
  `agent_snapshot`) **read from `store.py`; health thresholds live only in
  `health_rules.py`.** Directly binds item 5 (`attention` must be computed in
  `health_rules.py`, nowhere else) and constrains item 1/4 (any new "worst
  section" logic should call `health_rules.worst`/read `evaluate_snapshot`'s
  output, never re-derive a threshold).
- **Operator auth** (`auth.py::OperatorAuthMiddleware`) resolves every
  request to a `Principal`; **roles/host-scope are enforced by
  `webui/authz.py` guards and `tools.py`. "Don't open these surfaces without
  going through that middleware."** Item 9's `distribution.py` routes are the
  one place in the current codebase that already violates the spirit of this
  (mounted without `guard()`) — don't repeat that pattern for any of the ten
  new endpoints, and consider fixing it while touching item 9.
- **Type-hint everything; keep I/O async.** All ten items are async Starlette
  handlers over `aiosqlite` stores — consistent with every existing handler
  read in this pass.
- **SQLite write serialization (ADR-0051, `store.py:76-157`):** any new write
  path must acquire `store.write_lock()` immediately around the statements it
  protects — **never** around a loop, and **never** around an `await` that
  reaches the tunnel, an LLM call, or a caller-supplied callback (a write held
  across I/O stalls every other writer in the process). Relevant to items 2
  (approve/deny from the inbox), 8 (ticket creation + optional
  transition), and 10 (theme write) — all go through existing store methods
  that already take the lock correctly; don't add a *new* multi-statement
  write without wrapping it the same way `ticketstore.py`'s `set_state`/
  `set_blocked`/`set_agent_id` do (`async with write_lock(): ... await
  self._conn.commit()`).
- **Testing without the real agent:** a mock agent connects to `/agent/ws`
  and replays `docs/fixtures/` responses — not needed for any of these ten
  items (none require a live/mock agent round-trip; they're all reads over
  already-stored data or ticket/user-store writes), but keep in mind if item
  6's scope work ends up touching `toolloop.ToolExecutor.run_capability`.
- **Don't put architecture rationale in CLAUDE.md** — it lives in
  `docs/adr/`; **don't copy tool/frame schemas** into CLAUDE.md — that's
  `docs/protocol.md` + `docs/fixtures/`. (Process note for whoever documents
  the eventual implementation, not a constraint on the code itself.)

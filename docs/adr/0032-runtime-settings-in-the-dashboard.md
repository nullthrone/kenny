# 0032. Runtime settings resolved DB-over-env, editable in the dashboard

- Status: accepted
- Date: 2026-07-04

## Context and Problem Statement

kenny is configured almost entirely through ~50 `KENNY_*` environment variables, read via
scattered `os.environ.get(...)` calls at their point of use. There is no central config
layer: some values are read once at import time (`tunnel.py` framing limits), some once at
startup (`build_app`/`run`: host/port, operator tokens, alert cadence baked into
`AlertEngine`), and some per call (the chat model). Changing any of them means editing the
environment and restarting the process — awkward for the family-scale, single-operator
deployment this project targets, where the operator lives in the web dashboard and rarely
touches the shell.

We want the operator to change the runtime-safe knobs (alert cadence and cooldown, the
weekly-digest schedule, the web-filter lists and refresh interval, the chat model, the log
level) directly from the dashboard, structured into groups, and to *see* every setting's
effective value and where it came from — without turning the settings surface into a
foot-gun for values that cannot safely change at runtime.

## Considered Options

- **Keep everything in the environment (status quo).** Rejected — the problem this ADR
  exists to fix.
- **A per-value config file the operator edits and the server reloads.** Rejected: it
  diverges from every other piece of kenny state, which lives in `kenny.sqlite`; needs a
  file-watch/reload story; and gives no natural precedence relationship with the existing
  env vars deployments already rely on (Docker/Compose set them in `compose.yaml`).
- **A new SQLite settings store + a central resolver with a fixed precedence, exposed
  through the existing `/api` + web UI.** Chosen. Follows the established multi-store
  pattern (`PolicyStore`→ADR-0020, `WebFilterStore`, `ChatHistoryStore`→ADR-0025) and
  keeps env vars working as a floor so nothing about existing deployments breaks.

## Decision Outcome

A new `Settings` resolver (`kenny-server/kenny_server/config.py`) resolves every value in
the order **DB override > environment variable > coded default**. The environment stays a
valid, first-class way to configure the server (bootstrap, secrets, Docker) — a UI change
simply layers on top of it. A new `SettingsStore` (`store.py`, a `key/value/updated_at`
table) is the durable mirror; the resolver's in-memory override map is the authoritative
copy. Because the server is a single process on one asyncio event loop, `Settings.get()` is
a synchronous, lock-free dict lookup (as cheap as the old `os.environ.get`) and there is no
cross-process cache-invalidation problem — the DB is touched only on write and once at
startup in `Settings.load()`. Validation runs *before* persist, so an invalid value can
never reach the DB and poison later reads.

A declarative `CATALOG` in the same module is the single source of truth: one `SettingSpec`
per setting (type, default, label, help, validation, and a `lifecycle` flag). The catalog
drives both API validation and UI rendering. `lifecycle` is load-bearing and is the honesty
mechanism — a DB override only takes effect if the consumer actually reads through
`Settings`, so the catalog only advertises a setting as writable when its consumer does:

- **`live`** — the consumer reads `Settings.get()` on every use, or an apply-hook re-applies
  on write. This ADR converts the four subsystems that make this true: `AlertEngine` (reads
  cadence/cooldown/offline/digest live each pass instead of freezing them at construction),
  the web-filter refresh loop and `ExternalListCache` (interval, source URLs, block cap), the
  copilot chat model (resolved at the route boundary), and the log level (a
  `logging.getLogger().setLevel` apply-hook, re-run at startup for any stored override).
- **`restart`** — read once inside the app lifespan *after* `Settings.load()`, so an override
  applies on the next restart (e.g. the loop initial-delays).
- **`env_only`** — never writable from the UI, shown read-only for transparency. This covers
  secrets (`sensitive` specs never serialise their value — the UI shows only set/not-set),
  the process-bind values read before settings load (host/port/DB path/TLS), and everything
  touching the agent wire contract.

The API adds three routes under the existing `OperatorAuthMiddleware` — no new auth surface:
`GET /api/settings` (grouped catalog with each key's effective value + source badge),
`PUT /api/settings/{key}` (400 on unknown/invalid, 403 on `env_only`, else apply), and
`DELETE /api/settings/{key}` (reset to env/default). The dashboard gains a top-level
**Settings** tab rendering the groups, with a control per type, a `db`/`env`/`default`
source badge, a "restart required" marker, and a per-override reset — reusing the existing
`.k-*` component classes and the web-filter panel's form idiom (no framework, no build step).

**Anything that touches the agent wire contract is deliberately excluded and stays
`env_only`, deferred to a future ADR:** `KENNY_ALLOW_TOKEN_AUTH`, the Ed25519 server/agent
key material and rotation grace windows, and `KENNY_TELEMETRY_INTERVAL_SECS` (agent-facing;
changing it on the wire would require `docs/protocol.md` + `docs/fixtures/` + a
`PROTOCOL_VERSION` bump + the Rust agent + `/contract-check`). This ADR is strictly
server-side: the contract is untouched.

### Consequences

- Good, because the operator can retune alerting, the digest schedule, the web-filter lists,
  the chat model, and log verbosity from the dashboard, live, and can always see the
  effective value and its source instead of guessing what the environment holds.
- Good, because env vars still work exactly as before (as the floor), so Docker/Compose and
  existing deployments need no change; a fresh DB behaves identically to today.
- Good, because it reuses the `store.py` multi-store/WAL pattern and the existing operator
  auth — no new infrastructure, no new dependency, no new auth surface.
- Good, because the `lifecycle` taxonomy keeps the UI honest: a setting is only presented as
  editable when its consumer truly reads through the resolver, so a "custom" badge never
  lies about a value that would silently do nothing.
- Bad / accepted, because only the four converted subsystems are `live` in this iteration;
  many operationally-real vars (agent distribution, login lockout, telemetry framing limits)
  are shown read-only for now. Making them editable is a mechanical follow-up — convert the
  consumer to read `Settings` and flip the `lifecycle` flag — but until then the UI is a
  transparent read-only window onto them, not a control.
- Bad / accepted, because settings values live as raw strings in `kenny.sqlite`; the catalog
  keeps the DB honest via validate-before-persist, and secrets are never written there (they
  stay `env_only`).

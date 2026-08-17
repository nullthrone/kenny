# Dashboard screenshot generation

Regenerate the figures in `docs/assets/screenshots/` by rendering the **real**
web dashboard (`kenny-web/`, the React/TypeScript console) against a **mock**
demo fleet of ~6 family PCs, using kenny's real fonts (Jost + Public Sans +
JetBrains Mono — Nullthrone's display/body/mono stack). One command seeds an
in-process server, drives headless Chromium, and writes the PNGs.

## Quick start

```bash
cd kenny-server
pip install -e ".[dev,screenshots]"      # server deps + Playwright
# Chromium is provided by the environment — do NOT run `playwright install`.

cd ..
python scripts/screenshots/capture.py                 # -> docs/assets/screenshots/
python scripts/screenshots/capture.py --only today,fleet
python scripts/screenshots/capture.py --out /tmp/shots # render elsewhere first
```

The tool prints a per-shot `[ok]`/`[FAIL]` line and a final summary; it exits
non-zero if any shot failed. It never runs `playwright install`.

## How it works

`capture.py` does everything in one event loop so the state it seeds is the
state the browser sees:

1. **build** — `kenny_server.main.build_app(db_path=<tempfile>)` with the demo
   env applied first (see *Env knobs*). The prebuilt `kenny-web` output must
   already exist at `kenny_server/webui/dist/` (`npm --prefix kenny-web run
   build`) — the server has no build step of its own.
2. **serve** — an in-process `uvicorn.Server` on `127.0.0.1:<free port>`.
3. **seed** — `seed.seed_app(app)` writes the demo fleet into `app.state`, and
   also creates the "thomas" superuser account + session the browser signs in
   as. In-memory state (the `ScreenshotStore`, registry online flags) *must*
   be seeded in-process — a "write SQLite then start server" approach would
   miss it. See `seed.py`.
4. **drive** — Playwright Chromium loads each shot's view, runs its actions,
   asserts the fonts, and captures.

### Modules

| file | role |
|------|------|
| `demo_fleet.py` | Builds ~6 hosts by deep-copying/varying `docs/fixtures/telemetry_snapshot.json`. Pure data. |
| `desktop_image.py` | Pure-Python PNG of a mock desktop for the screenshot card (no Pillow). |
| `seed.py` | Seeds a *running* app's stores in-process (telemetry, registry, webfilter, screenshots, activity, chat history, tickets, Discord identities, reliability category cache, the browser's own login session). |
| `shots.py` | The **manifest** — one `Shot` per figure. |
| `capture.py` | Entrypoint: seed → serve → drive → write PNGs. |

### The demo fleet (documented health mix)

`papa-pc` (all green) · `mama-laptop` (laptop/battery) · `kid-pc` (flagged
`web_activity` → parental controls, visible in the Inbox) · `study-pc` (disk
critical + <30-day forecast) · `living-room-pc` (reboot pending + failed
update) · `grandpa-pc` (Defender real-time OFF + end-of-life OS + a suppressed
noisy reliability pattern). Plus a held approval (a printer-driver install on
`living-room-pc`) so the Inbox's gate renders, and a resolved ticket with a
full lifecycle (`demo-tkt-flush`) so the ticket timeline isn't empty.

All timestamps derive from one base clock captured per run, so the daily trend,
scan ages, and "last seen" stay internally consistent. Each host gets a ~30-point
daily series (drives the fleet trend, disk-fill and battery forecasts) plus one
latest snapshot.

## The manifest (`shots.py`)

Each `Shot` declares:

- `name` — output filename (`<name>.png`).
- `hash` — the view to open (`#/today`, `#/fleet`, `#/fleet/study-pc`,
  `#/inbox`, `#/inbox/ticket/{id}`, `#/log`, `#/admin/{section}`, `#/profile`, …).
- `mode` — `full_page` (`page.screenshot(full_page=True)`) or `element`
  (crop `selector` via `locator(...).screenshot()`).
- `selector` — the element/modal to crop in `element` mode.
- `theme` — `light` (default — Nullthrone is light-by-default) or `dark`.
- `actions` — an ordered list run before capture, from a tiny vocabulary
  interpreted by `capture.py`:
  - `{"eval": "<js>"}` — run JS in the page. Used to click a button matched by
    its visible text (`_click_button`, since Nullthrone is CSS Modules — there
    are no hand-written class names to hook a selector to) or to set a
    controlled input's value.
  - `{"wait_for": "<sel>"}` — wait for a selector to be visible. Both a plain
    CSS selector and Playwright's `text=`/attribute-selector syntax work here.
  - `{"sleep": <ms>}` — fixed settle delay (a 200ms view fade-up, a modal's
    200ms scale-in, a stream settling).

To add a figure: append a `Shot`. To adjust one: edit its `actions`/`selector`.
See `shots.py`'s module docstring for the full selector strategy (the global
`kc-*` hooks, `Modal`'s `role="dialog"`, and the handful of `data-shot`
attributes added to `kenny-web/src/**` for anchors nothing else provides).

Two figures from the pre-redesign manifest — a live confirm-gate mid-turn, and
an AI-generated Diagnosis/Action/Urgency recommendation — are not reproduced:
both need a live Anthropic API key this offline harness doesn't have, and
reconstructing their DOM by hand (as the old manifest did against the old
hand-written HTML) is not a reasonable thing to do against React internals.
Two other figures — Discord's "linked accounts" panel and an enlarged
screenshot modal — were dropped for the same reason (no bot token configured;
no such modal exists in the redesign) rather than faked.

## Env knobs

Set automatically by `capture.py`, but override-able:

| var | value | why |
|-----|-------|-----|
| `KENNY_OPERATOR_TOKEN` | `demo-operator-token` | legacy back-compat token, still set so Admin → Operator & Agent Auth shows it configured; the browser itself signs in with a real seeded session, not this token (see `seed.SeedResult.session_id`) |
| `KENNY_ALERT_INTERVAL_SECS` | `0` | disable the alert loop |
| `KENNY_WEBFILTER_REFRESH_SECS` | `0` | disable external-list fetches |
| `KENNY_DB_PATH` | tempfile | throwaway SQLite (removed after the run) |
| `PLAYWRIGHT_BROWSERS_PATH` | env-provided | where Chromium lives |

**Fonts / proxy.** Chromium fetches Google Fonts through `HTTPS_PROXY`; the
browser is launched with that proxy (bypassing `127.0.0.1`) and the context uses
`ignore_https_errors=True` for the proxy's intercepting cert. After each
navigation the tool asserts `document.fonts.check(...)` for Jost, Public Sans,
and JetBrains Mono, and **fails loudly** rather than shipping fallback-font
PNGs. If fonts fail, check `HTTPS_PROXY` and the proxy CA (see
`/root/.ccr/README.md`).

## Viewport

`1500×950`, `deviceScaleFactor: 2` (crisp 2× PNGs).

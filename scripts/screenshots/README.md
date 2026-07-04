# Dashboard screenshot generation

Regenerate the figures in `docs/assets/screenshots/` by rendering the **real**
web dashboard against a **mock** demo fleet of ~6 family PCs, using kenny's
original fonts (Hanken Grotesk + JetBrains Mono). One command seeds an
in-process server, drives headless Chromium, and writes the PNGs.

## Quick start

```bash
cd kenny-server
pip install -e ".[dev,screenshots]"      # server deps + Playwright
# Chromium is provided by the environment — do NOT run `playwright install`.

cd ..
python scripts/screenshots/capture.py                 # -> docs/assets/screenshots/
python scripts/screenshots/capture.py --only overview,fleet-console
python scripts/screenshots/capture.py --out /tmp/shots # render elsewhere first
```

The tool prints a per-shot `[ok]`/`[FAIL]` line and a final summary; it exits
non-zero if any shot failed. It never runs `playwright install`.

## How it works

`capture.py` does everything in one event loop so the state it seeds is the
state the browser sees:

1. **build** — `kenny_server.main.build_app(db_path=<tempfile>)` with the demo
   env applied first (see *Env knobs*).
2. **serve** — an in-process `uvicorn.Server` on `127.0.0.1:<free port>`.
3. **seed** — `seed.seed_app(app)` writes the demo fleet into `app.state`.
   In-memory state (the `ScreenshotStore`, registry online flags) *must* be
   seeded in-process — a "write SQLite then start server" approach would miss
   it. See `seed.py`.
4. **drive** — Playwright Chromium loads each shot's view, runs its actions,
   asserts the fonts, and captures.

### Modules

| file | role |
|------|------|
| `demo_fleet.py` | Builds ~6 hosts by deep-copying/varying `docs/fixtures/telemetry_snapshot.json`. Pure data. |
| `desktop_image.py` | Pure-Python PNG of a mock desktop for the screenshot card/modal (no Pillow). |
| `seed.py` | Seeds a *running* app's stores in-process (telemetry, registry, webfilter, screenshots, activity, chat history, reliability category cache). |
| `shots.py` | The **manifest** — one `Shot` per figure. |
| `capture.py` | Entrypoint: seed → serve → drive → write PNGs. |

### The demo fleet (documented health mix)

`papa-pc` (all green) · `mama-laptop` (laptop/battery) · `kid-pc` (flagged
`web_activity` → parental controls + Flagged) · `study-pc` (disk critical +
<30-day forecast) · `living-room-pc` (reboot pending + failed update) ·
`grandpa-pc` (Defender real-time OFF + end-of-life OS).

All timestamps derive from one base clock captured per run, so the daily trend,
scan ages, and "last seen" stay internally consistent. Each host gets a ~30-point
daily series (drives the fleet trend, disk-fill and battery forecasts) plus one
latest snapshot.

## The manifest (`shots.py`)

Each `Shot` declares:

- `name` — output filename (`<name>.png`).
- `hash` — the view to open (`#/overview`, `#/fleet`, `#/activity/audit`, …).
- `mode` — `full_page` (`page.screenshot(full_page=True)`) or `element`
  (crop `selector` via `locator(...).screenshot()`).
- `selector` — the element/modal to crop in `element` mode.
- `theme` — `dark` (default) or `light`.
- `actions` — an ordered list run before capture, from a tiny vocabulary
  interpreted by `capture.py`:
  - `{"eval": "<js>"}` — run JS in the page (call a dashboard global, e.g.
    `selectAgent('study-pc')`, `openSectionDetail('disk')`).
  - `{"wait_for": "<css>"}` — wait for a selector to be visible.
  - `{"wait_charts": true}` — wait until every Overview ECharts SVG has size.
  - `{"sleep": <ms>}` — fixed settle delay (chart animation / stream).

To add a figure: append a `Shot`. To adjust one: edit its `actions`/`selector`.

Two figures are **reconstructed from the real render code** because they need an
Anthropic API key and/or a live agent that this offline harness lacks:

- `copilot-confirm` — rebuilt from the real transcript renderers
  (`bubble` / `toolRun` / `renderPending`) — the same DOM a live turn produces.
- `ai-recommendation` — the Diagnosis/Action/Urgency block is injected (with a
  real API key the dashboard streams it live).

## Env knobs

Set automatically by `capture.py`, but override-able:

| var | value | why |
|-----|-------|-----|
| `KENNY_OPERATOR_TOKEN` | `demo-operator-token` | operator cookie (`kenny_op`) auth |
| `KENNY_ALERT_INTERVAL_SECS` | `0` | disable the alert loop |
| `KENNY_WEBFILTER_REFRESH_SECS` | `0` | disable external-list fetches |
| `KENNY_DB_PATH` | tempfile | throwaway SQLite (removed after the run) |
| `PLAYWRIGHT_BROWSERS_PATH` | env-provided | where Chromium lives |

**Fonts / proxy.** Chromium fetches Google Fonts through `HTTPS_PROXY`; the
browser is launched with that proxy (bypassing `127.0.0.1`) and the context uses
`ignore_https_errors=True` for the proxy's intercepting cert. After each
navigation the tool asserts `document.fonts.check(...)` for both families and
**fails loudly** rather than shipping fallback-font PNGs. If fonts fail, check
`HTTPS_PROXY` and the proxy CA (see `/root/.ccr/README.md`).

## Viewport

`1500×950`, `deviceScaleFactor: 2` (crisp 2× PNGs).

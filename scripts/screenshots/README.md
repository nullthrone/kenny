# Rendering the dashboard headlessly

Three tools that look at the same thing: the **real** web dashboard (`kenny-web/`,
the React/TypeScript console) rendered against a **mock** demo fleet of ~6
family PCs, in kenny's real fonts (Jost + Public Sans + JetBrains Mono —
Nullthrone's display/body/mono stack), served in-process and driven by headless
Chromium.

- `capture.py` writes the figures in `docs/assets/screenshots/`.
- `overflow_audit.py` asserts that no box paints outside the box containing it.
- `zoom_audit.py` asserts that focusing a control never zooms the page.

They share `harness.py` — the same env, server, seed and browser — so a figure
and an audit are statements about the same dashboard.

## Quick start

```bash
cd kenny-server
pip install -e ".[dev,screenshots]"      # server deps + Playwright
# Chromium is provided by the environment — do NOT run `playwright install`.

cd ..
python scripts/screenshots/capture.py                 # -> docs/assets/screenshots/
python scripts/screenshots/capture.py --only today,fleet
python scripts/screenshots/capture.py --out /tmp/shots # render elsewhere first

python scripts/screenshots/overflow_audit.py          # every view x 3 widths
python scripts/screenshots/overflow_audit.py --route '#/fleet/grandpa-pc' --width 402

python scripts/screenshots/zoom_audit.py                # every view, one phone
python scripts/screenshots/zoom_audit.py --route '#/log'
```

The tool prints a per-shot `[ok]`/`[FAIL]` line and a final summary; it exits
non-zero if any shot failed. It never runs `playwright install`.

## How it works

`harness.demo_dashboard()` does steps 1-3 in one event loop, so the state it
seeds is the state the browser sees:

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
4. **drive** — Playwright Chromium loads each view and then either captures it
   (`capture.py`) or measures it (`overflow_audit.py`, `zoom_audit.py`). The
   first two assert the fonts first; `zoom_audit.py` does not need to, because
   a computed font-size is the same number in a fallback face.

### Modules

| file | role |
|------|------|
| `demo_fleet.py` | Builds ~6 hosts by deep-copying/varying `docs/fixtures/telemetry_snapshot.json`. Pure data. |
| `desktop_image.py` | Pure-Python PNG of a mock desktop for the screenshot card (no Pillow). |
| `seed.py` | Seeds a *running* app's stores in-process (telemetry, registry, webfilter, screenshots, activity, chat history, tickets, Discord identities, reliability category cache, the browser's own login session). |
| `shots.py` | The **manifest** — one `Shot` per figure. |
| `harness.py` | Env, in-process server, demo seed, Chromium launch, font assertion — everything both entrypoints share. |
| `capture.py` | Entrypoint: seed → serve → drive → write PNGs. |
| `overflow_audit.py` + `.js` | Entrypoint: seed → serve → measure every box against the box containing it. |
| `zoom_audit.py` + `.js` | Entrypoint: seed → serve → measure every control WebKit zooms for against the 16px floor, on a touch-emulated phone viewport. |

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
| `KENNY_OPERATOR_TOKEN` | `demo-operator-token` | legacy back-compat token, set as a deployment would; the browser itself signs in with a real seeded session, not this token (see `seed.SeedResult.session_id`) |
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

`1500×950`, `deviceScaleFactor: 2` (crisp 2× PNGs). The overflow audit
re-renders at `1500`, `900` and `402` — the capture width, the awkward middle
where the sidebar is still shown, and the narrowest phone the 760px mobile
breakpoint is written for. The zoom audit renders once, at `402×874` with
touch emulation on, for the reason given below.

## The zoom audit

`zoom_audit.py` asserts one rule: every control WebKit zooms for — text entry
of any kind, and `<select>` — computes at least **16px**. Below that, Safari on
iOS zooms the whole page in when the control is focused and does not zoom back
out. It reports the element, the route, the size it computed to and the label
it carries, and exits non-zero.

It renders once, at `402×874` with `is_mobile` and `has_touch` on, because the
rule holding the floor (`kenny-web/src/styles/global.css`) is gated on
`(hover: none) and (pointer: coarse)` — the pointer, not the viewport, since an
iPhone in landscape is wider than the 760px breakpoint and still zooms. The
audit checks that Chromium actually reports those two media features before it
measures anything; if it ever stops, every measurement would be taken with the
rule inactive, so that is a hard failure with the fix in its message.

It is the half of the invariant that `kenny-web/src/styles/noZoom.test.ts`
cannot see. That test reads the stylesheets and checks that the rule exists and
that nothing outranks it, and runs in CI with the rest of the frontend suite.
This one resolves inheritance, specificity and load order in a real browser.

It opens the Ask Kenny drawer on every route (through the same
`kenny:ask-kenny-open` event the app uses), because that is the field it was
written for. Controls inside modals that are not open are not in the DOM and
are not measured — the rule under test is a selector on element types and knows
nothing about modals, so the rendered surface is evidence about all of them,
but it is evidence, not proof.

## The overflow audit

`overflow_audit.py` asserts one rule: an element's border box never crosses the
border box of the nearest ancestor that draws one (a border or a background) —
the box an operator reads as "the card". It reports the element, the box it
broke out of, the pixels, and the text it was carrying, and exits non-zero.

It is the half of the invariant that `kenny-web/src/styles/containment.test.ts`
cannot see. That test reads the stylesheets and catches the *generator* of these
bugs — an unshrinkable, unwrappable label with no width bound — and runs in CI
with the rest of the frontend suite. This one needs a browser, so it runs on
demand: after a layout change, and whenever a string that comes from a host
(a health `reason`, a path, a hostname) starts being rendered somewhere new.

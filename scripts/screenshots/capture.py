"""Render the real dashboard against the mock demo fleet and write PNGs.

Single entrypoint that, in one event loop:

1. sets the demo env + builds the app (``kenny_server.main.build_app``),
2. serves it with an in-process ``uvicorn.Server`` (so the same ``app.state`` we
   seed is the one the browser hits — in-memory state like the screenshot store
   and registry online flags survive),
3. seeds ``app.state`` with the demo fleet (``seed.seed_app``), including the
   "thomas" superuser session the browser signs in as,
4. drives headless Chromium (Playwright) over the shot manifest, asserting the
   real fonts loaded, and
5. writes ``<name>.png`` per shot into ``--out``.

Usage::

    python scripts/screenshots/capture.py [--only name1,name2] [--out DIR]

Chromium is provided by the environment (``PLAYWRIGHT_BROWSERS_PATH``); never run
``playwright install``. Google Fonts are fetched through the environment HTTPS
proxy — the font assertion fails loudly rather than shipping fallback-font PNGs.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

# Allow both ``python scripts/screenshots/capture.py`` and ``-m`` invocation.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.screenshots import demo_fleet, harness, shots  # type: ignore
else:
    from . import demo_fleet, harness, shots  # noqa: F401  (demo_fleet re-exported)

DEFAULT_OUT = "docs/assets/screenshots"


async def _run_actions(page: Any, actions: list[dict[str, Any]]) -> None:
    for action in actions:
        if "eval" in action:
            await page.evaluate(f"(async () => {{ {action['eval']}; }})()")
        elif "wait_for" in action:
            await page.wait_for_selector(action["wait_for"], state="visible", timeout=15000)
        elif "sleep" in action:
            await page.wait_for_timeout(action["sleep"])


async def _capture_shot(context: Any, base_url: str, shot: shots.Shot, out_dir: Path) -> None:
    page = await context.new_page()
    # Set the theme explicitly before first paint. localStorage is shared across
    # pages in one context, so a prior light shot would otherwise leak into the
    # dark shots that follow — pin it per shot instead of relying on the default.
    await page.add_init_script(
        f"try {{ localStorage.setItem('kenny-theme', {shot.theme!r}); }} catch (e) {{}}"
    )
    try:
        await page.goto(base_url + "/" + shot.hash, wait_until="networkidle", timeout=30000)
        # kenny-web mounts React at #root (index.html), not the old hand-written
        # app's #app.
        await page.wait_for_selector("#root", state="attached", timeout=15000)
        await harness.assert_fonts(page)
        await _run_actions(page, shot.actions)
        out_path = out_dir / f"{shot.name}.png"
        if shot.mode == "full_page":
            await page.screenshot(path=str(out_path), full_page=True)
        elif shot.mode == "viewport":
            await page.screenshot(path=str(out_path), full_page=False)
        else:
            locator = page.locator(shot.selector).first
            await locator.wait_for(state="visible", timeout=15000)
            await locator.screenshot(path=str(out_path))
    finally:
        await page.close()


async def run(only: list[str] | None, out: str) -> int:
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = shots.by_names(only) if only else shots.MANIFEST

    from playwright.async_api import async_playwright

    results: list[tuple[str, str]] = []

    async with harness.demo_dashboard() as dashboard:
        print(f"seeded {len(dashboard.agent_ids)} hosts: {', '.join(dashboard.agent_ids)}")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(**harness.launch_kwargs())
            context = await browser.new_context(
                viewport=harness.VIEWPORT,
                device_scale_factor=harness.DEVICE_SCALE,
                ignore_https_errors=True,
            )
            # A real "thomas" superuser session, not the legacy shared-token
            # cookie — see harness.OPERATOR_TOKEN.
            await context.add_cookies(dashboard.cookies())

            # Font preflight on the first real page — fail loudly before doing work.
            preflight = await context.new_page()
            await preflight.goto(
                dashboard.base_url + "/#/today", wait_until="networkidle", timeout=30000
            )
            await harness.assert_fonts(preflight)
            print("font check: Jost + Public Sans + JetBrains Mono loaded OK")
            await preflight.close()

            for shot in manifest:
                try:
                    await _capture_shot(context, dashboard.base_url, shot, out_dir)
                    results.append((shot.name, "ok"))
                    print(f"  [ok]   {shot.name}.png")
                except SystemExit:
                    raise
                except Exception as exc:  # noqa: BLE001 - report per-shot, keep going
                    results.append((shot.name, f"FAIL: {exc}"))
                    print(f"  [FAIL] {shot.name}: {exc}")

            await context.close()
            await browser.close()

    ok = [n for n, r in results if r == "ok"]
    bad = [(n, r) for n, r in results if r != "ok"]
    print(f"\nwrote {len(ok)}/{len(results)} shots to {out_dir}")
    if bad:
        print("failed shots:")
        for name, reason in bad:
            print(f"  - {name}: {reason}")
    return 0 if not bad else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate kenny dashboard screenshots.")
    parser.add_argument("--only", help="comma-separated shot names to render", default=None)
    parser.add_argument("--out", help=f"output directory (default {DEFAULT_OUT})", default=DEFAULT_OUT)
    args = parser.parse_args()
    only = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else None
    raise SystemExit(asyncio.run(run(only, args.out)))


if __name__ == "__main__":
    main()

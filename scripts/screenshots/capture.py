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
import contextlib
import os
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

# Allow both ``python scripts/screenshots/capture.py`` and ``-m`` invocation.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.screenshots import demo_fleet, seed, shots  # type: ignore
else:
    from . import demo_fleet, seed, shots  # noqa: F401  (demo_fleet re-exported)

DEFAULT_OUT = "docs/assets/screenshots"
# The legacy back-compat token — still set as KENNY_OPERATOR_TOKEN so the demo
# Admin → Operator & Agent Auth section shows it as a real configured secret,
# but the browser itself signs in with a real "thomas" session (see
# seed.SeedResult.session_id), not this cookie: the shared-token identity has
# no user row and would make profile.png show its empty "no editable account"
# state instead of the real one.
OPERATOR_TOKEN = "demo-operator-token"
VIEWPORT = {"width": 1500, "height": 950}
DEVICE_SCALE = 2


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _configure_env(db_path: str) -> None:
    """Env that must be set before ``build_app`` (background loops off, token fixed)."""

    os.environ["KENNY_DB_PATH"] = db_path
    os.environ["KENNY_OPERATOR_TOKEN"] = OPERATOR_TOKEN
    os.environ["KENNY_ALERT_INTERVAL_SECS"] = "0"
    os.environ["KENNY_WEBFILTER_REFRESH_SECS"] = "0"
    # No TLS locally; the operator cookie is accepted over plain http.
    os.environ.pop("KENNY_TLS", None)


async def _serve(app: Any, port: int) -> tuple[Any, asyncio.Task[None]]:
    """Start an in-process uvicorn server; return it and its serve() task."""

    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    # Wait for startup (lifespan connects the stores) before seeding.
    for _ in range(200):
        if server.started:
            return server, task
        await asyncio.sleep(0.05)
    raise RuntimeError("uvicorn did not start in time")


def _chromium_executable() -> str | None:
    """Best-effort path to the pre-installed Chromium, else let Playwright resolve."""

    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    for pat in ("chromium-*/chrome-linux/chrome", "chromium_headless_shell-*/chrome-linux/*"):
        hits = sorted(root.glob(pat))
        if hits:
            return str(hits[0])
    return None


async def _assert_fonts(page: Any) -> None:
    """Fail loudly unless the real Jost + Public Sans + JetBrains Mono webfonts loaded.

    Nullthrone's font stack: Jost for display/caps labels, Public Sans for body
    text, JetBrains Mono for code/mono values (see tokens/fonts.css).
    """

    await page.evaluate("document.fonts.ready")
    ok = await page.evaluate(
        "({jost: document.fonts.check(\"16px 'Jost'\"),"
        " publicSans: document.fonts.check(\"16px 'Public Sans'\"),"
        " mono: document.fonts.check(\"16px 'JetBrains Mono'\")})"
    )
    if not (ok.get("jost") and ok.get("publicSans") and ok.get("mono")):
        raise SystemExit(
            "FONT CHECK FAILED — refusing to ship fallback-font PNGs. "
            f"Jost loaded={ok.get('jost')}, Public Sans loaded={ok.get('publicSans')}, "
            f"JetBrains Mono loaded={ok.get('mono')}. "
            "Chromium could not fetch Google Fonts (check HTTPS_PROXY / cert handling)."
        )


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
        await _assert_fonts(page)
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

    tmp = tempfile.TemporaryDirectory(prefix="kenny-shots-")
    db_path = str(Path(tmp.name) / "demo.sqlite")
    _configure_env(db_path)

    from kenny_server.main import build_app

    app = build_app(db_path=db_path)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    manifest = shots.by_names(only) if only else shots.MANIFEST

    from playwright.async_api import async_playwright

    server, serve_task = await _serve(app, port)
    seeded = await seed.seed_app(app)
    print(f"seeded {len(seeded.agent_ids)} hosts: {', '.join(seeded.agent_ids)}")

    results: list[tuple[str, str]] = []
    proxy_server = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    launch_kwargs: dict[str, Any] = {"headless": True}
    exe = _chromium_executable()
    if exe:
        launch_kwargs["executable_path"] = exe
    if proxy_server:
        launch_kwargs["proxy"] = {"server": proxy_server, "bypass": "127.0.0.1,localhost"}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=DEVICE_SCALE,
            ignore_https_errors=True,
        )
        # A real "thomas" superuser session (seed.SeedResult.session_id), not the
        # legacy shared-token cookie — see OPERATOR_TOKEN's comment above.
        await context.add_cookies(
            [{"name": "kenny_op", "value": seeded.session_id, "url": base_url}]
        )

        # Font preflight on the first real page — fail loudly before doing work.
        preflight = await context.new_page()
        await preflight.goto(base_url + "/#/today", wait_until="networkidle", timeout=30000)
        await _assert_fonts(preflight)
        print("font check: Jost + Public Sans + JetBrains Mono loaded OK")
        await preflight.close()

        for shot in manifest:
            try:
                await _capture_shot(context, base_url, shot, out_dir)
                results.append((shot.name, "ok"))
                print(f"  [ok]   {shot.name}.png")
            except SystemExit:
                raise
            except Exception as exc:  # noqa: BLE001 - report per-shot, keep going
                results.append((shot.name, f"FAIL: {exc}"))
                print(f"  [FAIL] {shot.name}: {exc}")

        await context.close()
        await browser.close()

    server.should_exit = True
    with contextlib.suppress(Exception):
        await serve_task
    tmp.cleanup()

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

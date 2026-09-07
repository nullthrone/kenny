"""Assert that no box in the dashboard paints outside the box that contains it.

The rule this enforces: an element's border box never crosses the border box of
the nearest ancestor that actually draws one (a border or a background). That
ancestor is what an operator reads as "the card"; a child sticking out of it is
the glitch this audit exists to catch, whatever produced it.

It is the half of the invariant that `kenny-web/src/styles/containment.test.ts`
cannot see. That test reads the stylesheets and catches the generator of these
bugs — an unshrinkable, unwrappable label with no width bound. This one runs
the real thing in a real browser at three widths, so it also catches the cases
no stylesheet rule predicts: several unshrinkable siblings that only overflow
together, a fixed `min-width` under a narrow parent, a negative margin.

Usage::

    python scripts/screenshots/overflow_audit.py [--width 1500,900,402] [--route "#/today"]

Exits non-zero listing every escape: the element, the box it broke out of, by
how many pixels, and the text it was carrying. Chromium comes from the
environment (``PLAYWRIGHT_BROWSERS_PATH``); never run ``playwright install``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.screenshots import harness  # type: ignore
else:
    from . import harness

AUDIT_JS = (Path(__file__).parent / "overflow_audit.js").read_text()

# Every view, plus the two hosts whose health text is longest: study-pc's disk
# reason and grandpa-pc's reliability reason (a full sentence — the shape that
# used to break out of its card).
ROUTES = [
    "#/today",
    "#/fleet",
    "#/fleet/study-pc",
    "#/fleet/grandpa-pc",
    "#/fleet/living-room-pc",
    "#/fleet/kid-pc",
    "#/fleet/papa-pc",
    "#/inbox",
    "#/log",
    "#/admin",
    "#/profile",
]

# 1500 is the capture viewport, 402 the narrowest phone the mobile breakpoint
# is written for, 900 the awkward middle where the sidebar is still shown.
WIDTHS = [1500, 900, 402]

SETTLE_MS = 700


def _format(escape: dict[str, Any]) -> str:
    by = ", ".join(f"{side} +{px}px" for side, px in escape["by"].items())
    text = escape["text"]
    return f"{escape['el']} escapes {escape['box']} ({by})" + (f" — {text!r}" if text else "")


async def run(widths: list[int], routes: list[str]) -> int:
    from playwright.async_api import async_playwright

    findings: list[str] = []
    checked = 0

    async with harness.demo_dashboard() as dashboard, async_playwright() as pw:
        browser = await pw.chromium.launch(**harness.launch_kwargs())
        for width in widths:
            context = await browser.new_context(
                viewport={"width": width, "height": 950}, ignore_https_errors=True
            )
            await context.add_cookies(dashboard.cookies())
            page = await context.new_page()
            for route in routes:
                await page.goto(
                    f"{dashboard.base_url}/{route}", wait_until="networkidle", timeout=30000
                )
                await page.wait_for_selector("#root", state="attached", timeout=15000)
                # Layout measured in a fallback font is not the layout an
                # operator sees — a wider face is exactly what overflows.
                await harness.assert_fonts(page)
                await page.wait_for_timeout(SETTLE_MS)
                result = await page.evaluate(AUDIT_JS)
                checked += 1
                for escape in result["escapes"]:
                    findings.append(f"[{width}px] {route}: {_format(escape)}")
                if result["pageOverflow"] > 0:
                    findings.append(
                        f"[{width}px] {route}: the page itself scrolls sideways by "
                        f"{result['pageOverflow']}px"
                    )
            await context.close()
        await browser.close()

    print(f"audited {checked} page renders ({len(routes)} routes x {len(widths)} widths)")
    if not findings:
        print("no box escapes its container")
        return 0
    print(f"\n{len(findings)} escape(s):")
    for finding in findings:
        print(f"  - {finding}")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit the dashboard for boxes that overflow.")
    parser.add_argument("--width", help="comma-separated viewport widths", default=None)
    parser.add_argument("--route", help="comma-separated routes (e.g. '#/today')", default=None)
    args = parser.parse_args()
    widths = [int(w) for w in args.width.split(",")] if args.width else WIDTHS
    routes = [r.strip() for r in args.route.split(",") if r.strip()] if args.route else ROUTES
    raise SystemExit(asyncio.run(run(widths, routes)))


if __name__ == "__main__":
    main()

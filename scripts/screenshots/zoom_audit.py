"""Assert that focusing a control in the dashboard never zooms the page.

The rule this enforces: every control WebKit zooms for -- text entry of any
kind, and ``<select>`` -- computes at least 16px on a touch-primary device.
Below that, Safari on iOS zooms the whole page in on focus and does not zoom
back out, which is what an operator reports as "the Ask Kenny panel jumps when
I tap the box".

It is the half of the invariant that ``kenny-web/src/styles/noZoom.test.ts``
cannot see. That test reads the stylesheets and checks that the rule holding
the floor exists and that nothing outranks it. This one runs the real thing in
a real browser and measures what each control actually computes to, with
inheritance, specificity and load order resolved -- the only place the claim is
settled rather than argued.

Fonts are not asserted here (unlike ``capture.py`` and ``overflow_audit.py``):
a computed font-size is the same number in a fallback face, so a missing
webfont cannot make this audit lie.

Known gap, stated rather than hidden: a control only reaches the DOM when its
view renders it. The Ask Kenny drawer is opened explicitly below because it is
the field this audit was written for, but controls inside modals that are not
open (``CreateUserModal``, ``PatModal``, ``TotpModal``, ...) are not measured.
The rule under test is a selector on element types and knows nothing about
modals, so covering the rendered surface is evidence about all of them -- but
it is evidence, not proof.

Usage::

    python scripts/screenshots/zoom_audit.py [--route "#/today"]

Exits non-zero listing every control below the floor: the element, the route,
the size it computed to and the label it carries. Chromium comes from the
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
    from scripts.screenshots import harness, overflow_audit  # type: ignore
else:
    from . import harness, overflow_audit

AUDIT_JS = (Path(__file__).parent / "zoom_audit.js").read_text()

# The same views the overflow audit walks, read from it rather than copied, so
# a route added there is covered here too.
ROUTES = overflow_audit.ROUTES

# An iPhone-shaped viewport with touch emulation on. The width is not what
# arms the rule -- it is gated on the pointer, because an iPhone in landscape
# is wider than any width breakpoint and still zooms -- but it is the shape an
# operator holds, and it is what puts the drawer in its full-width state.
DEVICE = {
    "viewport": {"width": 402, "height": 874},
    "device_scale_factor": 3,
    "is_mobile": True,
    "has_touch": True,
}

# The rule is gated on these two media features. If Chromium's device emulation
# does not actually flip them, every measurement below would be taken with the
# rule inactive and the audit would report the desktop sizes as failures -- or,
# worse, a future desktop-only fix would look green here. So this is checked
# first and hard-fails.
TOUCH_QUERY = "(hover: none) and (pointer: coarse)"

# The drawer's visible state lives in Shell; this is the event "Fix via Ask
# kenny" already uses to raise it (Shell.tsx's `kenny:ask-kenny-open`
# listener), so the audit opens it the same way the app does instead of
# depending on the trigger button's markup.
OPEN_DRAWER_JS = "() => window.dispatchEvent(new Event('kenny:ask-kenny-open'))"

SETTLE_MS = 700
FLOOR_PX = 16


def _format(route: str, finding: dict[str, Any]) -> str:
    label = finding["label"]
    return (
        f"{route}: {finding['el']} computes {finding['px']:g}px "
        f"(floor {FLOOR_PX}px)" + (f" — {label!r}" if label else "")
    )


async def run(routes: list[str]) -> int:
    from playwright.async_api import async_playwright

    findings: list[str] = []
    measured = 0

    async with harness.demo_dashboard() as dashboard, async_playwright() as pw:
        browser = await pw.chromium.launch(**harness.launch_kwargs())
        context = await browser.new_context(ignore_https_errors=True, **DEVICE)
        await context.add_cookies(dashboard.cookies())
        page = await context.new_page()

        for route in routes:
            await page.goto(
                f"{dashboard.base_url}/{route}", wait_until="networkidle", timeout=30000
            )
            await page.wait_for_selector("#root", state="attached", timeout=15000)

            if not await page.evaluate(f"matchMedia({TOUCH_QUERY!r}).matches"):
                raise SystemExit(
                    f"EMULATION CHECK FAILED — Chromium does not report {TOUCH_QUERY} under "
                    "is_mobile/has_touch, so the rule under test is not even active and every "
                    "measurement would be meaningless. Add "
                    "'--blink-settings=primaryPointerType=coarse,primaryHoverType=none' to "
                    "harness.launch_kwargs() and run again."
                )

            # The field this audit exists for is behind a drawer.
            await page.evaluate(OPEN_DRAWER_JS)
            await page.wait_for_timeout(SETTLE_MS)

            result = await page.evaluate(AUDIT_JS)
            measured += result["measured"]
            findings.extend(_format(route, f) for f in result["findings"])

        await context.close()
        await browser.close()

    print(f"measured {measured} controls across {len(routes)} routes")
    if not measured:
        # A selector that matched nothing would report "no findings" and mean
        # nothing by it.
        print("no control was measured at all — the audit proved nothing")
        return 1
    if not findings:
        print("no control zooms on focus")
        return 0
    print(f"\n{len(findings)} control(s) below the floor:")
    for finding in findings:
        print(f"  - {finding}")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit the dashboard for controls that zoom on focus."
    )
    parser.add_argument(
        "--route", help="comma-separated routes (e.g. '#/today')", default=None
    )
    args = parser.parse_args()
    routes = (
        [r.strip() for r in args.route.split(",") if r.strip()]
        if args.route
        else ROUTES
    )
    raise SystemExit(asyncio.run(run(routes)))


if __name__ == "__main__":
    main()

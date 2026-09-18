"""The legacy dashboard page must not zoom when a control is focused.

WebKit on iOS zooms the whole page in when a text-entry control is focused
whose computed font-size is below 16px, and does not zoom back out. The active
dashboard holds a 16px floor in ``kenny-web/src/styles/global.css``, guarded
there by ``src/styles/noZoom.test.ts``. This module guards the other page the
server can serve: ``_LEGACY_INDEX``, the hand-written prototype that
``_entry_point()`` falls back to when no ``kenny-web`` build is present
(ADR-0052). Nobody edits that file any more, which is exactly why nothing
would notice if the block were dropped from it.

The two pages are checked separately and never read each other's source: each
guard owns the file it ships with, and the comment in each block names the
other.
"""

from __future__ import annotations

import re

from kenny_server.webui import _LEGACY_INDEX

# The block, prelude through its closing brace. Gated on the pointer and not
# the viewport: an iPhone in landscape is wider than any width breakpoint and
# still zooms.
_NO_ZOOM_BLOCK = re.compile(
    r"@media\s*\(hover:\s*none\)\s*and\s*\(pointer:\s*coarse\)\s*\{(.*?)\n    \}",
    re.DOTALL,
)

# Every type WebKit does NOT zoom for, and therefore every type the rule has to
# subtract -- an <input> with no type attribute is a text field, so the
# selector cannot be written as a list of the types that do zoom.
_NOT_ZOOMED = ("checkbox", "radio", "button", "submit", "reset", "range", "color", "file")


def test_legacy_page_holds_the_16px_floor() -> None:
    assert _LEGACY_INDEX.is_file(), "the legacy entry point moved -- update this guard"
    match = _NO_ZOOM_BLOCK.search(_LEGACY_INDEX.read_text(encoding="utf-8"))
    assert match is not None, "the legacy page has no (hover: none) and (pointer: coarse) block"

    body = match.group(1)
    # A literal, not a var(): 16px is WebKit's threshold, not a step on that
    # page's type scale, and must not move with it.
    assert re.search(r"font-size:\s*16px\s*!important", body)
    assert "var(" not in body

    assert re.search(r"(^|,)\s*textarea\s*[,{]", body, re.MULTILINE)
    assert re.search(r"(^|,)\s*select\s*[,{]", body, re.MULTILINE)
    assert re.search(r"(^|,)\s*input:not\(", body, re.MULTILINE)
    for type_ in _NOT_ZOOMED:
        assert re.search(rf":not\(\[type='{type_}'\]\)", body), f"input[type={type_}] not excluded"

#!/usr/bin/env python3
"""Generate the tray-icon assets for kenny-agent.

This is a **developer tool**, not part of the cargo build. It turns the source
illustration of Kenny (the dog) into the two multi-resolution `.ico` files the tray
embeds via `include_bytes!`:

* ``kenny-on.ico``  — remote control **on**  (Kenny on a light rounded badge)
* ``kenny-off.ico`` — remote control **off** (greyed badge + red diagonal slash)

The "off" variant is derived automatically from the source, so only one image needs to
be supplied. Drop the artwork next to this script as ``kenny-source.png`` (square-ish,
transparent or white background, ideally >= 256 px) and run::

    python3 kenny-agent/assets/gen_icons.py

If ``kenny-source.png`` is absent a clearly-marked placeholder badge is generated so the
crate still builds; replace it with the real artwork and re-run before shipping.

Requires Pillow (``pip install pillow``).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "kenny-source.png"
ICON_ON = HERE / "kenny-on.ico"
ICON_OFF = HERE / "kenny-off.ico"

# Icon sizes embedded in each .ico (Windows picks the best fit per surface).
SIZES = [16, 24, 32, 48, 256]
# Master canvas the badge is composed at, then downscaled to each size.
CANVAS = 256
# Light badge so Kenny's mostly-black silhouette stays visible on dark taskbars.
BADGE_RGBA = (230, 237, 243, 255)  # matches the dashboard --fg light chip
BADGE_RADIUS = 52
PADDING = 18


def _trim_white_to_alpha(img: Image.Image, threshold: int = 244) -> Image.Image:
    """Make near-white pixels transparent so the subject sits on the badge cleanly."""
    img = img.convert("RGBA")
    px = img.getdata()
    out = [
        (r, g, b, 0) if (r >= threshold and g >= threshold and b >= threshold) else (r, g, b, a)
        for (r, g, b, a) in px
    ]
    img.putdata(out)
    return img


def _crop_to_content(img: Image.Image) -> Image.Image:
    bbox = img.getbbox()
    return img.crop(bbox) if bbox else img


def _rounded_badge(size: int, radius: int, color: tuple[int, int, int, int]) -> Image.Image:
    badge = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(badge)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=color)
    return badge


def _placeholder_subject() -> Image.Image:
    """A clearly-temporary mark used only when kenny-source.png is missing."""
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # A simple dog face: head + two drop ears + snout, in near-black.
    ink = (24, 26, 30, 255)
    draw.ellipse([70, 70, 186, 200], fill=ink)            # head
    draw.polygon([(70, 96), (44, 60), (96, 88)], fill=ink)  # left ear
    draw.polygon([(186, 96), (212, 60), (160, 88)], fill=ink)  # right ear
    draw.ellipse([108, 150, 148, 188], fill=(235, 232, 228, 255))  # muzzle
    draw.ellipse([120, 162, 136, 176], fill=ink)          # nose
    return img


def _compose(subject: Image.Image) -> Image.Image:
    """Center the subject on the light rounded badge at the master resolution."""
    badge = _rounded_badge(CANVAS, BADGE_RADIUS, BADGE_RGBA)
    inner = CANVAS - 2 * PADDING
    subj = subject.copy()
    subj.thumbnail((inner, inner), Image.LANCZOS)
    x = (CANVAS - subj.width) // 2
    y = (CANVAS - subj.height) // 2
    badge.alpha_composite(subj, (x, y))
    return badge


def _to_off(on: Image.Image) -> Image.Image:
    """Derive the disabled look: desaturate the badge and stamp a red diagonal slash."""
    off = on.convert("RGBA")
    grey = off.convert("L").convert("RGBA")
    # Keep the badge alpha (rounded corners), drop the color.
    grey.putalpha(off.getchannel("A"))
    draw = ImageDraw.Draw(grey)
    m = 26
    draw.line([(m, CANVAS - m), (CANVAS - m, m)], fill=(200, 32, 36, 255), width=26)
    return grey


def main() -> None:
    if SOURCE.exists():
        subject = _crop_to_content(_trim_white_to_alpha(Image.open(SOURCE)))
        print(f"using source artwork: {SOURCE.name}")
    else:
        subject = _placeholder_subject()
        print("WARNING: kenny-source.png not found — generating PLACEHOLDER icons.")

    on = _compose(subject)
    off = _to_off(on)

    on.save(ICON_ON, format="ICO", sizes=[(s, s) for s in SIZES])
    off.save(ICON_OFF, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"wrote {ICON_ON.name} and {ICON_OFF.name} ({', '.join(map(str, SIZES))} px)")


if __name__ == "__main__":
    main()

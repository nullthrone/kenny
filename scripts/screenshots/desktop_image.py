"""Generate a mock Windows-desktop PNG for the seeded screenshot card/modal.

Pure-Python (``zlib`` + ``struct``) so the tooling needs no image dependency
(Pillow etc.). Produces a warm gradient "wallpaper" with a bottom taskbar and a
start button — enough to stand in for a real desktop capture in the dashboard's
screenshot thumbnail and near-fullscreen modal.
"""

from __future__ import annotations

import base64
import struct
import zlib

_WIDTH = 1280
_HEIGHT = 800
_TASKBAR_H = 44


def _png(width: int, height: int, rgb: bytes) -> bytes:
    """Encode raw RGB bytes (row-major, 3 bytes/px) as a PNG."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    # Prepend the (no-op) filter byte to each scanline.
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw.extend(rgb[y * stride : (y + 1) * stride])

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _render() -> bytes:
    """Build the desktop RGB buffer."""

    buf = bytearray(_WIDTH * _HEIGHT * 3)
    taskbar_top = _HEIGHT - _TASKBAR_H
    for y in range(_HEIGHT):
        if y >= taskbar_top:
            # Flat dark taskbar.
            row = (26, 25, 23)
        else:
            # Warm diagonal gradient wallpaper (border-collie amber/warm tones).
            t = y / max(1, taskbar_top)
            r = int(46 + t * 30)
            g = int(38 + t * 22)
            b = int(30 + t * 16)
            row = (r, g, b)
        base = y * _WIDTH * 3
        for x in range(_WIDTH):
            off = base + x * 3
            if y >= taskbar_top and 12 <= x <= 44 and taskbar_top + 8 <= y <= _HEIGHT - 8:
                # Amber "start" square in the taskbar.
                buf[off], buf[off + 1], buf[off + 2] = 232, 163, 61
            else:
                buf[off], buf[off + 1], buf[off + 2] = row
    return bytes(buf)


_CACHE: str | None = None


def demo_desktop_png_b64() -> str:
    """Return the mock desktop as a base64 PNG string (built once, cached)."""

    global _CACHE
    if _CACHE is None:
        _CACHE = base64.b64encode(_png(_WIDTH, _HEIGHT, _render())).decode("ascii")
    return _CACHE


if __name__ == "__main__":  # pragma: no cover
    import pathlib

    out = pathlib.Path("demo_desktop.png")
    out.write_bytes(base64.b64decode(demo_desktop_png_b64()))
    print("wrote", out, out.stat().st_size, "bytes")

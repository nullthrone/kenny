"""Serve the real dashboard against the mock demo fleet, headlessly.

Everything both `capture.py` (writes the doc figures) and `overflow_audit.py`
(asserts the layout holds) need in order to look at the same dashboard: the
env that must be set before ``build_app``, the in-process uvicorn server, the
demo seed, and a Chromium that reaches it.

Keeping this in one place is what makes the two tools comparable — a figure
rendered from one fleet and an audit run against a differently-built one would
say nothing about each other.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sys
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.screenshots import seed  # type: ignore
else:
    from . import seed

# The legacy back-compat token — still set as KENNY_OPERATOR_TOKEN so the demo
# Admin → Operator & Agent Auth section shows it as a real configured secret,
# but the browser itself signs in with a real "thomas" session
# (``Dashboard.session_id``), not this cookie: the shared-token identity has no
# user row and would make profile.png show its empty "no editable account"
# state instead of the real one.
OPERATOR_TOKEN = "demo-operator-token"
VIEWPORT = {"width": 1500, "height": 950}
DEVICE_SCALE = 2


@dataclass
class Dashboard:
    """A seeded dashboard being served, and the session a browser signs in with."""

    base_url: str
    session_id: str
    agent_ids: list[str]

    def cookies(self) -> list[dict[str, str]]:
        return [{"name": "kenny_op", "value": self.session_id, "url": self.base_url}]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def configure_env(db_path: str) -> None:
    """Env that must be set before ``build_app`` (background loops off, token fixed)."""

    os.environ["KENNY_DB_PATH"] = db_path
    os.environ["KENNY_OPERATOR_TOKEN"] = OPERATOR_TOKEN
    os.environ["KENNY_ALERT_INTERVAL_SECS"] = "0"
    os.environ["KENNY_WEBFILTER_REFRESH_SECS"] = "0"
    # No TLS locally; the operator cookie is accepted over plain http.
    os.environ.pop("KENNY_TLS", None)


def chromium_executable() -> str | None:
    """Best-effort path to the pre-installed Chromium, else let Playwright resolve."""

    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    for pat in ("chromium-*/chrome-linux/chrome", "chromium_headless_shell-*/chrome-linux/*"):
        hits = sorted(root.glob(pat))
        if hits:
            return str(hits[0])
    return None


def launch_kwargs() -> dict[str, Any]:
    """Chromium launch options: the pre-installed binary, and the env's proxy
    (bypassed for the loopback server) so Google Fonts still resolve."""

    kwargs: dict[str, Any] = {"headless": True}
    exe = chromium_executable()
    if exe:
        kwargs["executable_path"] = exe
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        kwargs["proxy"] = {"server": proxy, "bypass": "127.0.0.1,localhost"}
    return kwargs


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


@contextlib.asynccontextmanager
async def demo_dashboard() -> AsyncIterator[Dashboard]:
    """Build, serve and seed the demo dashboard for the duration of the block.

    The server runs in this process so the state seeded into ``app.state`` is
    the state the browser hits — in-memory state (the screenshot store,
    registry online flags) would not survive a "write SQLite, then start" split.
    """

    tmp = tempfile.TemporaryDirectory(prefix="kenny-demo-")
    db_path = str(Path(tmp.name) / "demo.sqlite")
    configure_env(db_path)

    from kenny_server.main import build_app

    app = build_app(db_path=db_path)
    port = free_port()
    server, serve_task = await _serve(app, port)
    seeded = await seed.seed_app(app)
    try:
        yield Dashboard(
            base_url=f"http://127.0.0.1:{port}",
            session_id=seeded.session_id,
            agent_ids=list(seeded.agent_ids),
        )
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await serve_task
        tmp.cleanup()


async def assert_fonts(page: Any) -> None:
    """Fail loudly unless the real Jost + Public Sans + JetBrains Mono webfonts loaded.

    Nullthrone's font stack: Jost for display/caps labels, Public Sans for body
    text, JetBrains Mono for code/mono values (see tokens/fonts.css). Layout
    measured in a fallback font is not the layout an operator sees, so both
    tools check this before trusting anything on the page.
    """

    await page.evaluate("document.fonts.ready")
    ok = await page.evaluate(
        "({jost: document.fonts.check(\"16px 'Jost'\"),"
        " publicSans: document.fonts.check(\"16px 'Public Sans'\"),"
        " mono: document.fonts.check(\"16px 'JetBrains Mono'\")})"
    )
    if not (ok.get("jost") and ok.get("publicSans") and ok.get("mono")):
        raise SystemExit(
            "FONT CHECK FAILED — refusing to trust fallback-font layout. "
            f"Jost loaded={ok.get('jost')}, Public Sans loaded={ok.get('publicSans')}, "
            f"JetBrains Mono loaded={ok.get('mono')}. "
            "Chromium could not fetch Google Fonts (check HTTPS_PROXY / cert handling)."
        )

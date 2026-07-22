"""Externally reachable URLs of this server.

Several surfaces need the server's public base URL: the agent-distribution links
(``distribution.py``), the OAuth issuer/metadata/redirect URLs (``oauth.py``),
and the RFC 9728 ``resource_metadata`` pointer in the 401 challenge
(``auth.py``). Keeping the derivation in one place avoids drift and the layering
smell of importing it out of ``distribution.py`` into ``auth.py``.

The base is taken from ``KENNY_PUBLIC_URL`` when set (the externally reachable
origin behind the TLS proxy), else falls back to ``http://localhost:<port>`` for
local/dev use.
"""

from __future__ import annotations

import os

#: Path the FastMCP Streamable HTTP endpoint is mounted at (see ``main.py``).
MCP_PATH = "/mcp"


def public_base_url() -> str:
    """Externally reachable base URL of this server, without a trailing slash."""

    base = os.environ.get("KENNY_PUBLIC_URL", "").strip()
    if base:
        return base.rstrip("/")
    port = os.environ.get("KENNY_PORT", "8000")
    return f"http://localhost:{port}"


def mcp_resource_url() -> str:
    """Canonical MCP resource identifier (RFC 8707 audience / RFC 9728 resource).

    This is the base URL plus the MCP mount path with no trailing slash, e.g.
    ``https://kenny.example.com/mcp`` — the value OAuth access tokens are bound to
    and validated against.
    """

    return f"{public_base_url()}{MCP_PATH}"

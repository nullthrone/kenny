"""kenny-server: MCP endpoint, agent tunnel, telemetry store, and operator dashboard.

See ../docs/protocol.md for the wire contract and ../docs/adr/ for architecture decisions.
"""

import os

PROTOCOL_VERSION = "0.13"

# The release image stamps its real version into KENNY_SERVER_VERSION at build time
# (release.yml passes the git tag as a docker build-arg, mirroring how the agent
# binary version is led by the release tag per ADR-0015). Outside a release build
# (local dev, `pip install -e .`) this falls back to a dev marker instead of a
# hardcoded release number that would silently go stale.
__version__ = os.environ.get("KENNY_SERVER_VERSION", "").strip() or "0.0.0-dev"

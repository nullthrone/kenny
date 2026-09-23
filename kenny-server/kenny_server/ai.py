"""The one door to the Anthropic API: whether AI is available, which features are on,
and the client that talks to it (ADR-0066).

Every AI feature asks here instead of reading ``ANTHROPIC_API_KEY`` itself. The key
resolves through :class:`~kenny_server.config.Settings` (a key saved in the dashboard
wins over the environment), and each feature has its own live switch, so turning a
feature off or rotating the key applies to the next call without a restart.

The server is one process with one ``Settings`` (ADR-0032), so the composition root
binds one :class:`AiAccess` process-wide (:func:`bind`) and module-level callers that
have no object to hold one — the event classifier, the MCP ``agent_health`` path —
read it through :func:`current`. Before anything is bound (a unit test, a tool run
outside the server) :func:`current` answers from the environment alone.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

#: Feature name -> the setting that switches it. The dashboard's status payload,
#: every server gate and the frontend's gating all use these names.
FEATURES: dict[str, str] = {
    "ask": "KENNY_AI_ASK_ENABLED",
    "recommend": "KENNY_AI_RECOMMEND_ENABLED",
    "forecast": "KENNY_AI_FORECAST_ENABLED",
    "classify": "KENNY_AI_CLASSIFY_ENABLED",
    "ticket_assistant": "KENNY_AI_TICKET_ASSISTANT_ENABLED",
    "triage": "KENNY_TRIAGE_ENABLED",
}

KEY_SETTING = "ANTHROPIC_API_KEY"


def _default_factory(api_key: str) -> Any:
    import anthropic

    return anthropic.Anthropic(api_key=api_key or None)


class AiAccess:
    """Key, feature switches and client, resolved on every call.

    ``client_factory`` replaces the real SDK client (tests pass a fake); it is
    called with no arguments, as every injected factory in the server is. The
    real client is built per key and rebuilt when the key changes.
    """

    def __init__(
        self,
        settings: Any = None,
        *,
        client_factory: Callable[[], Any] | None = None,
        env: Any = None,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory
        self._env = env if env is not None else os.environ
        self._client: Any = None
        self._client_key: str | None = None

    # -- key ---------------------------------------------------------------

    def api_key(self) -> str:
        if self._settings is not None:
            return str(self._settings.get(KEY_SETTING) or "").strip()
        return str(self._env.get(KEY_SETTING) or "").strip()

    def key_source(self) -> str:
        """Where the key comes from: ``db`` (the dashboard), ``env``, or ``none``."""

        if not self.api_key():
            return "none"
        if self._settings is not None:
            return self._settings.effective(KEY_SETTING)[1]
        return "env"

    def available(self) -> bool:
        """True if an API key is configured. Nothing AI runs without one."""

        return bool(self.api_key())

    # -- features ----------------------------------------------------------

    def switched_on(self, feature: str) -> bool:
        """The feature's own switch, regardless of the key."""

        key = FEATURES[feature]
        if self._settings is not None:
            return bool(self._settings.get(key))
        raw = (self._env.get(key) or "1").strip().lower()
        return raw not in ("0", "false", "no", "off")

    def enabled(self, feature: str) -> bool:
        """True if the feature may call the model now: a key is set and it is switched on."""

        return self.available() and self.switched_on(feature)

    def status(self) -> dict[str, Any]:
        """What the dashboard gates on (``GET /api/ai/status``)."""

        return {
            "configured": self.available(),
            "source": self.key_source(),
            "features": {name: self.enabled(name) for name in FEATURES},
        }

    # -- client ------------------------------------------------------------

    def client(self) -> Any:
        """A client for the key in force right now."""

        if self._client_factory is not None:
            return self._client_factory()
        key = self.api_key()
        if self._client is None or key != self._client_key:
            self._client = _default_factory(key)
            self._client_key = key
        return self._client

    def probe(self) -> dict[str, Any]:
        """Check the key against the API with the cheapest call there is."""

        if not self.available():
            return {"ok": False, "error": "no API key is set"}
        try:
            self.client().models.list(limit=1)
        except Exception as exc:  # noqa: BLE001 - reported to the operator verbatim
            return {"ok": False, "error": str(exc) or type(exc).__name__}
        return {"ok": True, "error": None}


_current: AiAccess | None = None


def bind(access: AiAccess | None) -> None:
    """Make ``access`` the process-wide AI access (the composition root does this)."""

    global _current
    _current = access


def current() -> AiAccess:
    """The bound AI access, or one that reads the environment alone."""

    return _current if _current is not None else AiAccess()

"""The one door to the Anthropic API: whether AI is available, which features are on,
and the client that talks to it (ADR-0066).

Every AI feature asks here instead of reading ``ANTHROPIC_API_KEY`` itself. The key
resolves through :class:`~kenny_server.config.Settings` (a key saved in the dashboard
wins over the environment), and each feature has its own live switch, so turning a
feature off or rotating the key applies to the next call without a restart.

The client may talk to the API through an operator-configured AI gateway
(ADR-0068): a base URL, extra request headers for the gateway's own
authentication and routing, and the model ids the gateway expects. The gateway
must speak the Anthropic Messages API; nothing here translates to another wire
format.

The server is one process with one ``Settings`` (ADR-0032), so the composition root
binds one :class:`AiAccess` process-wide (:func:`bind`) and module-level callers that
have no object to hold one — the event classifier, the MCP ``agent_health`` path —
read it through :func:`current`. Before anything is bound (a unit test, a tool run
outside the server) :func:`current` answers from the environment alone.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("kenny.ai")

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

#: The gateway (ADR-0068). An empty base URL means the Anthropic API itself.
BASE_URL_SETTING = "ANTHROPIC_BASE_URL"
HEADERS_SETTING = "ANTHROPIC_CUSTOM_HEADERS"

#: The model for the short, cheap calls: recommendations, forecast prose and
#: reliability-event classification. The conversational features use
#: ``KENNY_CHAT_MODEL``.
FAST_MODEL_SETTING = "KENNY_FAST_MODEL"
DEFAULT_FAST_MODEL = "claude-haiku-4-5"

#: Sent as the API key when a gateway is set and no key is: the gateway holds
#: the provider credentials and authenticates kenny by its own headers. The SDK
#: refuses to build a client without some key.
GATEWAY_PLACEHOLDER_KEY = "kenny-via-gateway"

#: Switches every feature off at once, whatever its own switch says.
MASTER_SETTING = "KENNY_AI_ENABLED"


# RFC 9110 field-name token.
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HEADER_VALUE_FORBIDDEN = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
#: Headers the HTTP client owns, or that would let a setting break framing or
#: redirect the request: never taken from the operator.
_RESERVED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "upgrade",
        "te",
        "trailer",
        "proxy-authorization",
        "proxy-connection",
        "x-api-key",
        "anthropic-version",
    }
)


def parse_headers(raw: str) -> dict[str, str]:
    """``Name: Value`` entries, one per line or separated by ``;``, as a dict.

    Raises :class:`ValueError` for anything that must not reach the wire: a
    malformed name, a control character in a value, a header the HTTP client
    owns, or the same name twice. Blank entries are ignored. An error names at
    most the header, never a value: values are credentials, and the message
    reaches the log and the dashboard.
    """

    headers: dict[str, str] = {}
    seen: set[str] = set()
    for entry in re.split(r"[;\n]", raw.replace("\r\n", "\n")):
        if not entry.strip():
            continue
        name, sep, value = entry.partition(":")
        name, value = name.strip(), value.strip()
        if not sep or not name:
            raise ValueError("expected 'Name: Value' entries separated by ';'")
        if not _HEADER_NAME.match(name):
            raise ValueError("a header name may hold only letters, digits and -_.")
        if not value:
            raise ValueError(f"header {name} has no value")
        if _HEADER_VALUE_FORBIDDEN.search(value):
            raise ValueError(f"header {name} contains a control character")
        lowered = name.lower()
        if lowered in _RESERVED_HEADERS:
            raise ValueError(f"header {name} cannot be set here")
        if lowered in seen:
            raise ValueError(f"header {name} is given twice")
        seen.add(lowered)
        headers[name] = value
    return headers


def check_headers(raw: str) -> None:
    """Setting validator for :data:`HEADERS_SETTING`."""

    parse_headers(raw)


def check_base_url(raw: str) -> None:
    """Setting validator for :data:`BASE_URL_SETTING`.

    ``https`` anywhere; plain ``http`` only to a host on the same machine or the
    same container network (loopback, or a name without a dot), because the
    gateway headers are credentials. No user info, query or fragment: the SDK
    appends ``/v1/messages`` to the path, and credentials belong in the headers.
    """

    value = raw.strip()
    if not value:
        return
    parts = urlsplit(value)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("expected an http(s) URL such as https://gateway.example")
    if parts.username is not None or parts.password is not None:
        raise ValueError("put credentials in the gateway headers, not in the URL")
    if parts.query or parts.fragment:
        raise ValueError("the URL must not carry a query or a fragment")
    # A dotless name is a container-network service; an IPv6 literal has no dot
    # either, so only its loopback counts as local.
    local = host in ("localhost", "127.0.0.1", "::1") or ("." not in host and ":" not in host)
    if parts.scheme == "http" and not local:
        raise ValueError("plain http is only allowed to a local or container-network host")


def _default_factory(api_key: str, base_url: str, headers: Mapping[str, str]) -> Any:
    import anthropic

    return anthropic.Anthropic(
        api_key=api_key or None,
        base_url=base_url or None,
        default_headers=dict(headers) or None,
    )


class AiAccess:
    """Key, feature switches and client, resolved on every call.

    ``client_factory`` replaces the real SDK client (tests pass a fake); it is
    called with no arguments, as every injected factory in the server is. The
    real client is built per key, base URL and header set, and rebuilt when any
    of them changes.
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
        self._client_config: tuple[str, str, tuple[tuple[str, str], ...]] | None = None
        self._warned_headers: str | None = None

    def _raw(self, key: str) -> str:
        if self._settings is not None:
            return str(self._settings.get(key) or "").strip()
        return str(self._env.get(key) or "").strip()

    # -- key ---------------------------------------------------------------

    def api_key(self) -> str:
        return self._raw(KEY_SETTING)

    def key_source(self) -> str:
        """Where the key comes from: ``db`` (the dashboard), ``env``, or ``none``."""

        if not self.api_key():
            return "none"
        if self._settings is not None:
            return self._settings.effective(KEY_SETTING)[1]
        return "env"

    def available(self) -> bool:
        """True if an API key or a gateway is configured. Nothing AI runs without
        one: a gateway may hold the provider credentials itself."""

        return bool(self.api_key() or self.base_url())

    # -- gateway -----------------------------------------------------------

    def base_url(self) -> str:
        """The gateway's base URL, or ``""`` for the Anthropic API itself."""

        return self._raw(BASE_URL_SETTING).rstrip("/")

    def gateway_host(self) -> str | None:
        """The gateway's host for display; never its path or its headers."""

        url = self.base_url()
        return (urlsplit(url).hostname or url) if url else None

    def config_error(self) -> str | None:
        """Why the gateway configuration in force is unusable, or ``None``.

        A value saved from the dashboard is validated on write; one from the
        environment is not, so it is checked here, where the client is built.
        """

        try:
            check_base_url(self._raw(BASE_URL_SETTING))
        except ValueError as exc:
            return f"{BASE_URL_SETTING}: {exc}"
        try:
            parse_headers(self._raw(HEADERS_SETTING))
        except ValueError as exc:
            return f"{HEADERS_SETTING}: {exc}"
        return None

    def custom_headers(self) -> dict[str, str]:
        """The extra request headers, or none at all if they do not parse."""

        raw = self._raw(HEADERS_SETTING)
        try:
            return parse_headers(raw)
        except ValueError as exc:
            if raw != self._warned_headers:  # once per value, not once per call
                logger.warning("ignoring %s: %s", HEADERS_SETTING, exc)
                self._warned_headers = raw
            return {}

    # -- models ------------------------------------------------------------

    def fast_model(self) -> str:
        """The model id for the short, cheap calls, as the gateway names it."""

        return self._raw(FAST_MODEL_SETTING) or DEFAULT_FAST_MODEL

    # -- features ----------------------------------------------------------

    def _switch(self, key: str) -> bool:
        if self._settings is not None:
            return bool(self._settings.get(key))
        raw = (self._env.get(key) or "1").strip().lower()
        return raw not in ("0", "false", "no", "off")

    def master_on(self) -> bool:
        """The master switch: off means no feature runs."""

        return self._switch(MASTER_SETTING)

    def switched_on(self, feature: str) -> bool:
        """The feature's own switch, regardless of the key and the master switch."""

        return self._switch(FEATURES[feature])

    def enabled(self, feature: str) -> bool:
        """True if the feature may call the model now: AI is on, a key is set and
        the feature's own switch is on."""

        return self.master_on() and self.available() and self.switched_on(feature)

    def status(self) -> dict[str, Any]:
        """What the dashboard gates on (``GET /api/ai/status``)."""

        return {
            "enabled": self.master_on(),
            "configured": self.available(),
            "source": self.key_source(),
            "gateway": self.gateway_host(),
            "features": {name: self.enabled(name) for name in FEATURES},
        }

    # -- client ------------------------------------------------------------

    def client(self) -> Any:
        """A client for the key in force right now."""

        if self._client_factory is not None:
            return self._client_factory()
        base_url = self.base_url()
        key = self.api_key() or (GATEWAY_PLACEHOLDER_KEY if base_url else "")
        headers = self.custom_headers()
        config = (key, base_url, tuple(sorted(headers.items())))
        if self._client is None or config != self._client_config:
            self._client = _default_factory(key, base_url, headers)
            self._client_config = config
        return self._client

    def probe(self) -> dict[str, Any]:
        """Check the connection with the smallest real call there is.

        One single-token message on the fast model: it takes the same path
        every feature takes — key or gateway headers, base URL, model id, and
        whatever the gateway inspects — which listing models would not.
        """

        if not self.available():
            return {"ok": False, "error": "no API key or gateway is set"}
        problem = self.config_error()
        if problem is not None:
            return {"ok": False, "error": problem}
        try:
            self.client().messages.create(
                model=self.fast_model(),
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
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

"""Refuse JSON request bodies whose text cannot be stored.

JSON may carry a lone UTF-16 surrogate, as the escape ``"\\ud800"`` or as the
raw bytes of one, and Python's parser accepts both. Such a string cannot be
encoded as UTF-8, so the first SQLite bind, log line or outbound request that
meets it raises ``UnicodeEncodeError`` — after the handler may already have done
part of its work. :class:`StorableJsonTextMiddleware` answers those bodies with
``400`` before any handler runs, which keeps every route's own validation free
of the case.
"""

from __future__ import annotations

import json
import re

from .urls import MCP_PATH

# A body can only decode to a surrogate if it holds a ``\uD8xx``-``\uDFxx``
# escape or the lead byte (0xED) of a raw three-byte surrogate encoding. Every
# other body is passed on without being parsed here.
_SURROGATE_HINT = re.compile(rb"\\u[dD][89a-fA-F]|\xed[\xa0-\xbf]")

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_FORM_TYPES = (b"application/x-www-form-urlencoded", b"multipart/form-data")

_REJECTION = json.dumps(
    {"error": "request body contains text that is not valid Unicode (a lone surrogate)"}
).encode("utf-8")


def has_unstorable_text(body: bytes) -> bool:
    """True when ``body`` is JSON holding a string that cannot be UTF-8 encoded.

    A body that is not JSON at all is not this function's concern — the route
    that reads it reports that itself — so it answers ``False``.
    """

    if not _SURROGATE_HINT.search(body):
        return False
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return False
    try:
        json.dumps(parsed, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return True
    except (ValueError, RecursionError):
        return False
    return False


class StorableJsonTextMiddleware:
    """Pure-ASGI gate: a JSON body with a lone surrogate gets ``400``.

    Only requests that can carry a JSON body to one of the server's own routes
    are read here; the MCP endpoint, form posts and every other scope pass
    through untouched. The body is buffered (each of those routes reads it whole
    anyway) and replayed to the application unchanged.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if not self._applies(scope):
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # The client went away before the body was complete; there is
                # nobody to answer.
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)

        if has_unstorable_text(body):
            await send(
                {
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(_REJECTION)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _REJECTION})
            return

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    def _applies(scope) -> bool:
        if scope["type"] != "http" or scope.get("method") not in _BODY_METHODS:
            return False
        if scope.get("path", "").startswith(MCP_PATH):
            return False
        for name, value in scope.get("headers", ()):
            if name == b"content-type":
                return not value.lower().startswith(_FORM_TYPES)
        return True

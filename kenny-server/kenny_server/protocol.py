"""Pydantic models for every wire frame.

The wire contract is ``../docs/protocol.md`` and the golden frames in
``../docs/fixtures/``. These models validate against those fixtures (see
``tests/test_fixtures.py``); change a frame shape only when the contract changes.

A discriminated union on ``type`` covers all seven frame kinds. Use
:func:`parse_frame` to turn an inbound JSON object into a model and
:func:`dump_frame` to turn a model back into a JSON-ready dict.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

# ---------------------------------------------------------------------------
# Shared / leaf types
# ---------------------------------------------------------------------------

Status = Literal["ok", "warn", "crit"]
OS = Literal["windows", "linux", "macos"]
ErrorCode = Literal[
    "timeout",
    "not_found",
    "exec_failed",
    "unsupported",
    "bad_args",
    "internal",
    "disabled",
    "blocked",
    # Agent voluntarily stepped back while a protected game is running on the
    # endpoint (anti-cheat coexistence); today only `screen_capture`. See ADR-0039.
    "paused",
]


class Section(BaseModel):
    """Base for every telemetry section payload.

    Each section carries a required ``status``/``summary`` plus arbitrary
    section-specific fields (allowed via ``extra='allow'``), so the server can
    aggregate fleet health without per-section domain logic.
    """

    model_config = ConfigDict(extra="allow")

    status: Status
    summary: str


class ResponseError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str


class RegisterMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    hostname: str
    os: OS
    version: str
    # Normalized CPU arch (``x86_64``/``aarch64``); optional so legacy agents that
    # predate #139 still register. Absent -> `_norm_arch` in distribution.py
    # defaults to x86_64.
    arch: str | None = None
    # Release channel the binary was built as (``stable``/``dev``); optional so
    # legacy/no-channel agents that predate ADR-0052 still register. Absent ->
    # callers treat it as ``stable`` (see `registry.Agent.channel`).
    channel: str | None = None


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


class Register(BaseModel):
    """``register`` frame: agent -> server, right after connect.

    From v0.8 the agent puts ``protocol`` and a fresh ``client_nonce`` on the
    wire to select the mutual-auth handshake; ``token`` is optional/legacy and
    only honoured during the migration window (``KENNY_ALLOW_TOKEN_AUTH``).
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["register"] = "register"
    agent_id: str
    protocol: str | None = None
    client_nonce: str | None = None
    token: str | None = None
    meta: RegisterMeta


class Challenge(BaseModel):
    """``challenge`` frame: server -> agent, the server's signed nonce (auth step 2)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["challenge"] = "challenge"
    server_nonce: str
    server_sig: str


class Auth(BaseModel):
    """``auth`` frame: agent -> server, the agent's signature (auth step 3)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["auth"] = "auth"
    agent_sig: str


class Request(BaseModel):
    """``request`` frame: server -> agent, invoke one capability tool."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["request"] = "request"
    id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class Response(BaseModel):
    """``response`` frame: agent -> server, result/error for a ``request``."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["response"] = "response"
    id: str
    ok: bool
    result: dict[str, Any] | None = None
    error: ResponseError | None = None


class Telemetry(BaseModel):
    """``telemetry`` frame: agent -> server, pushed snapshot."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["telemetry"] = "telemetry"
    agent_id: str
    collected_at: str
    snapshot: dict[str, Section]


class Log(BaseModel):
    """``log`` frame: agent -> server, a structured log line."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["log"] = "log"
    agent_id: str
    at: str
    level: Literal["error", "warn", "info", "debug", "trace"]
    target: str
    message: str
    fields: dict[str, Any] | None = None


class PolicyRule(BaseModel):
    """One deny rule (shared catalog shape; also used by the ``policy`` frame)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    applies_to: Literal["powershell", "posix", "self_protection", "path"]
    pattern: str
    reason: str


class Policy(BaseModel):
    """``policy`` frame: server -> agent, operator's append-only extra deny rules."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["policy"] = "policy"
    rules: list[PolicyRule] = Field(default_factory=list)


class Ping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["ping"] = "ping"


class Pong(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["pong"] = "pong"


Frame = Annotated[
    Union[
        Register,
        Challenge,
        Auth,
        Request,
        Response,
        Telemetry,
        Log,
        Policy,
        Ping,
        Pong,
    ],
    Field(discriminator="type"),
]

_FRAME_ADAPTER: TypeAdapter[Frame] = TypeAdapter(Frame)


def parse_frame(data: dict[str, Any] | str | bytes) -> Frame:
    """Parse a JSON object (dict, str, or bytes) into the matching frame model."""

    if isinstance(data, (str, bytes, bytearray)):
        return _FRAME_ADAPTER.validate_json(data)
    return _FRAME_ADAPTER.validate_python(data)


def dump_frame(model: BaseModel) -> dict[str, Any]:
    """Serialize a frame model into a JSON-ready dict (omitting unset/None)."""

    return model.model_dump(mode="json", exclude_none=True)

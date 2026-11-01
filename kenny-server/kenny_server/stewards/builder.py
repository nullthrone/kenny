"""Stewards defined as data: what a superuser may build in the dashboard (ADR-0074).

A built steward is a :class:`~kenny_server.stewards.spec.StewardSpec` read from a
JSON object, never code: every tool, tier, precondition, event name, the verdict
tool and the gate are the server's, and the data only chooses among them and
narrows them. Three steps, each a function here:

* :func:`spec_from_data` reads the data form under a strict schema: an unknown
  field, a wrong type or a value out of range is refused, and what the server
  decides (``verdict_tool``, ``default_mode``, ``version``) is not the data's to
  say.
* :func:`validate_builder` checks the spec exactly as a shipped one is checked
  (:func:`~kenny_server.stewards.spec.validate` and
  :func:`~kenny_server.stewards.catalog.check_dispatchable`) **and** against the
  builder's own bans (:func:`tool_refusal`, no evidence, registered
  preconditions only, never started by a new ticket).
* :func:`spec_to_data` writes the canonical data form back: what the store
  keeps, what the editor loads, and what :func:`spec_from_data` reads into the
  same spec, so a stored steward hashes the same on every boot.

Errors are addressed to the field the editor shows (``{"field": "tools[3]",
"error": "..."}``); :class:`BuilderError` carries all of them at once.

:func:`clone_data` turns a shipped spec into data under a new id **faithfully**:
it never drops nor rewrites what a built steward may not use (an evidence
constraint, a steward-only tool). Reading the clone back refuses those with
field errors, so a person sees exactly what stands in the way rather than a
silently different steward.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

from ..ticket_assistant import FLEET_WIDE_TOOLS
from ..ticket_rules import KNOWN_SECTIONS
from ..tool_classes import READ_ONLY, SENSITIVE_TOOLS, TOOL_CLASSES
from ..toolloop import (
    COPILOT_STEWARD_TOOLS,
    SERVER_TOOLS,
    STEWARD_ONLY_TOOLS,
    STEWARD_VERDICT_TOOL,
    STEWARD_VERDICTS,
    _capability_schema,
    build_tool_schemas,
)
from ..tools import _OS_SCOPED_TOOLS, CAPABILITY_TOOLS, _is_secret_key
from .authorizations import MAX_EXPIRY_DAYS, NEVER_AUTHORIZED
from .catalog import BUILT_ID_PREFIX, CATALOG, TICKET_SURFACE_TOOLS, check_dispatchable
from .policy import HOST_ARG, host_bound
from .spec import (
    _PARAM_RE,
    ALERT_EVENT_TYPES,
    MAX_TIMEOUT_S,
    PARAM_KINDS,
    TICKET_CREATED,
    TRIGGER_KINDS,
    ArgConstraint,
    Budget,
    SpecError,
    StewardSpec,
    ToolTimeout,
    Trigger,
    TriggerSet,
    is_event_name,
    param_kind,
    validate,
)

__all__ = [
    "BUILT_ID_RE",
    "BUILDER_TOOLS",
    "LIMITS",
    "SOURCE_RE",
    "BuilderError",
    "FieldError",
    "build_from_data",
    "builder_catalog",
    "builder_events",
    "arg_refusal",
    "unbindable_tools",
    "clone_data",
    "spec_from_data",
    "spec_to_data",
    "tool_refusal",
    "validate_builder",
    "valid_source",
]

#: One error the editor shows beside one field: ``{"field", "error"}``.
FieldError = dict[str, str]

#: A built steward's id: :data:`~kenny_server.stewards.catalog.BUILT_ID_PREFIX`
#: and a short name. No shipped id has the prefix, so the two never collide.
BUILT_ID_RE = re.compile(r"^" + BUILT_ID_PREFIX + r"[a-z][a-z0-9_]{1,32}$")

#: Where a built steward came from: an empty form, a clone of a shipped
#: steward, or a draft Ask kenny proposed. A label for people, never a permission.
SOURCE_RE = re.compile(r"^(blank|copilot|clone:[a-z][a-z0-9_]{1,40})$")

#: The bounds of the data form. Shown to the editor by :func:`builder_catalog`.
LIMITS: Mapping[str, int] = {
    "title_max": 80,
    "description_max": 400,
    "prompt_max": 8000,
    "tools_max": 24,
    "max_iterations_min": 1,
    "max_iterations_max": 20,
    "timeout_max_s": MAX_TIMEOUT_S,
    "params_max": 16,
    "preconditions_max": 8,
    "constraints_max": 64,
    "constraint_values_max": 200,
    "value_len_max": 200,
    "events_max": 32,
    "min_interval_days_max": 366,
}

#: The default iteration budget of a built steward that names none.
_DEFAULT_ITERATIONS = 8

#: The fields of the data form. ``verdict_tool``, ``default_mode`` and
#: ``version`` are the server's to set and are refused when sent.
_FIELDS = frozenset(
    {
        "id",
        "title",
        "description",
        "prompt",
        "trigger",
        "triggers_allowed",
        "tools",
        "constraints",
        "timeouts",
        "params",
        "preconditions",
        "budget",
        "sensitive_ok",
    }
)
_SERVER_FIELDS = frozenset({"verdict_tool", "default_mode", "version"})

#: Arguments every capability call accepts that no constraint may bind: the
#: one bounded free argument and the per-call host override the gate refuses.
_UNBINDABLE_ARGS = frozenset({"timeout_s", "agent_id"})

#: The JSON type an argument travels as on the wire where the model-facing
#: schema (:func:`~kenny_server.toolloop._capability_schema`) says string: the
#: contract's booleans, integers and lists. ``tests/test_steward_builder.py``
#: joins every entry to ``CAPABILITY_TOOLS``.
_WIRE_TYPES: Mapping[str, Mapping[str, str]] = {
    "webfilter_apply": {"domains": "array"},
    "account_set_enabled": {"enabled": "boolean"},
    "account_set_admin": {"admin": "boolean"},
    "account_set_logon_rights": {"deny": "array"},
    "account_create": {"admin": "boolean"},
    "account_delete": {"remove_profile": "boolean"},
    "account_session_action": {"warn_seconds": "integer"},
    "password_policy_set": {
        "min_length": "integer",
        "max_age_days": "integer",
        "lockout_threshold": "integer",
    },
}


class BuilderError(ValueError):
    """A built steward that must not be saved; :attr:`errors` says where and why."""

    def __init__(self, errors: Sequence[FieldError]) -> None:
        self.errors: list[FieldError] = [dict(e) for e in errors]
        super().__init__("; ".join(f"{e['field']}: {e['error']}" for e in self.errors))


class _Runner(Protocol):
    """What :func:`validate_builder` asks of the runner: its precondition registry and catalog."""

    catalog: Mapping[str, StewardSpec]

    def preconditions(self) -> frozenset[str]: ...


def _err(field: str, error: str) -> FieldError:
    return {"field": field, "error": error}


def _declared_args(tool: str) -> tuple[set[str], set[str]]:
    """``(declared, required)`` argument names of ``tool``, the host override left out."""

    if tool in CAPABILITY_TOOLS:
        raw = CAPABILITY_TOOLS[tool]
        declared = {a.rstrip("?") for a in raw}
        required = {a for a in raw if not a.endswith("?")}
        return declared, required
    entry = SERVER_TOOLS.get(tool, {})
    return set(entry.get("properties", {})), set(entry.get("required", []))


# -- which tools ---------------------------------------------------------------


def tool_refusal(tool: str) -> str | None:
    """Why a built steward may never name ``tool``, or ``None`` when it may (ADR-0074 rule 2)."""

    return _structural_refusal(tool) or _binding_refusal(tool)


def _structural_refusal(tool: str) -> str | None:
    """The refusal that is about what the tool *is*, before its arguments are asked."""

    if tool not in TOOL_CLASSES:
        return "kenny has no such tool"
    if tool in NEVER_AUTHORIZED:
        return "a shell or the agent's own update; a built steward never names one"
    if tool in COPILOT_STEWARD_TOOLS:
        return "a tool of Ask kenny, not of a run"
    if tool in TICKET_SURFACE_TOOLS:
        return "works on a ticket's own surface, which only triage runs on"
    if tool in FLEET_WIDE_TOOLS or tool == "select_agent":
        return "reads or picks across the fleet; a run is bound to one machine"
    if tool in STEWARD_ONLY_TOOLS:
        return "a server rule tool only a shipped steward uses"
    if tool not in SERVER_TOOLS and tool not in CAPABILITY_TOOLS:
        return "cannot run in a steward's tool loop"
    return None


def _arg_type(tool: str, arg: str) -> str:
    """The JSON type ``arg`` of ``tool`` travels as: ``string`` unless the contract says otherwise."""

    wire = _WIRE_TYPES.get(tool, {}).get(arg)
    if wire is not None:
        return wire
    if tool in CAPABILITY_TOOLS:
        props = _capability_schema(tool, CAPABILITY_TOOLS[tool])["properties"]
    else:
        props = SERVER_TOOLS.get(tool, {}).get("properties", {})
    return str((props.get(arg) or {}).get("type") or "string")


def arg_refusal(tool: str, arg: str) -> str | None:
    """Why no constraint may bind ``tool``'s ``arg``, or ``None`` when one may.

    A constraint admits a non-empty string among its values and nothing else
    (:meth:`~kenny_server.stewards.spec.ArgConstraint.admits`), so a boolean, a
    number or a list can never satisfy one; and a value written into a spec is
    stored in plaintext and shown to every operator, so a secret never is.
    """

    if arg in _UNBINDABLE_ARGS:
        return f"{tool} takes no argument {arg!r} a constraint can bind"
    if _is_secret_key(arg):
        return f"{arg} is a secret; a constraint would store and show it"
    kind = _arg_type(tool, arg)
    if kind != "string":
        return f"{arg} is not a string ({kind}); a constraint admits string values only"
    return None


def _binding_refusal(tool: str) -> str | None:
    """Why a tool that changes something can never be bound by constraints, or ``None``.

    Every argument such a tool requires needs a constraint
    (:func:`validate_builder`), and :func:`arg_refusal` bars some arguments from
    ever having one; a tool left with nothing to bind has no safe use.
    """

    if tool == STEWARD_VERDICT_TOOL or TOOL_CLASSES.get(tool) == READ_ONLY:
        return None
    declared, required = _declared_args(tool)
    stuck = sorted(a for a in required - _UNBINDABLE_ARGS if arg_refusal(tool, a) is not None)
    if stuck:
        why = arg_refusal(tool, stuck[0])
        return f"requires {stuck[0]}, which no constraint can bind ({why})"
    if not any(arg_refusal(tool, a) is None for a in declared):
        return "takes no argument a constraint can bind"
    return None


#: Every tool a built steward may name, sorted.
BUILDER_TOOLS: tuple[str, ...] = tuple(
    sorted(
        t
        for t in set(TOOL_CLASSES) | set(SERVER_TOOLS) | set(CAPABILITY_TOOLS)
        if tool_refusal(t) is None
    )
)


def unbindable_tools() -> list[dict[str, str]]:
    """The tools left out of the builder only because no constraint can bind them, with why."""

    out: list[dict[str, str]] = []
    for name in sorted(set(TOOL_CLASSES) | set(SERVER_TOOLS) | set(CAPABILITY_TOOLS)):
        if name in BUILDER_TOOLS:
            continue
        why = _binding_refusal(name)
        if why is not None and _structural_refusal(name) is None:
            out.append({"name": name, "reason": why})
    return out


def builder_events() -> list[str]:
    """Every event a built steward may be started by: alerts, never a new ticket."""

    alerts = [f"alert:{t}" for t in ALERT_EVENT_TYPES]
    health = [f"alert:health:{s}" for s in sorted(KNOWN_SECTIONS["health"])]
    return [e for e in alerts + health if is_event_name(e)]


def _tool_args(tool: str, schema: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A tool's arguments as the model sees them, minus the host override."""

    input_schema = schema.get("input_schema") or {}
    required = set(input_schema.get("required") or ())
    out: list[dict[str, Any]] = []
    for name, prop in (input_schema.get("properties") or {}).items():
        if name == "agent_id":
            continue
        out.append(
            {
                "name": name,
                "type": _arg_type(tool, name),
                "required": name in required,
                "bindable": arg_refusal(tool, name) is None,
            }
        )
    return out


def builder_catalog(runner: _Runner) -> dict[str, Any]:
    """What a built steward may be made of, as ``GET /api/stewards/builder/catalog`` shows it."""

    schemas = {s["name"]: s for s in build_tool_schemas(frozenset(BUILDER_TOOLS), unattended=True)}
    tools: list[dict[str, Any]] = []
    for name in BUILDER_TOOLS:
        schema = schemas.get(name, {})
        scope = _OS_SCOPED_TOOLS.get(name)
        tools.append(
            {
                "name": name,
                "tier": TOOL_CLASSES[name],
                "description": str(schema.get("description") or ""),
                "args": _tool_args(name, schema),
                "os": sorted(scope) if scope is not None else None,
                "sensitive": name in SENSITIVE_TOOLS,
                "target": "host" if name in CAPABILITY_TOOLS or name in HOST_ARG else "server",
                "verdict": name == STEWARD_VERDICT_TOOL,
            }
        )
    return {
        "tools": tools,
        "unbindable": unbindable_tools(),
        "verdict_tool": STEWARD_VERDICT_TOOL,
        "verdicts": list(STEWARD_VERDICTS),
        "param_kinds": dict(PARAM_KINDS),
        "default_param_kind": "list",
        "preconditions": sorted(runner.preconditions()),
        "trigger_kinds": list(TRIGGER_KINDS),
        "events": builder_events(),
        "limits": dict(LIMITS),
        "id_pattern": BUILT_ID_RE.pattern,
        "never_authorized": sorted(NEVER_AUTHORIZED),
        "max_expiry_days": MAX_EXPIRY_DAYS,
    }


# -- data -> spec ----------------------------------------------------------------


def _string(
    errors: list[FieldError],
    field: str,
    value: Any,
    *,
    max_len: int,
    required: bool = True,
) -> str:
    if not isinstance(value, str):
        errors.append(_err(field, "must be text"))
        return ""
    text = value.strip()
    if required and not text:
        errors.append(_err(field, "is required"))
    elif len(text) > max_len:
        errors.append(_err(field, f"is longer than {max_len} characters"))
    return text


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _string_list(
    errors: list[FieldError],
    field: str,
    value: Any,
    *,
    max_items: int,
    pattern: re.Pattern[str] | None = None,
    max_len: int = 200,
) -> list[str]:
    """A list of distinct non-empty strings, in the order given."""

    if not isinstance(value, list):
        errors.append(_err(field, "must be a list"))
        return []
    if len(value) > max_items:
        errors.append(_err(field, f"takes at most {max_items} entries"))
        return []
    out: list[str] = []
    for i, item in enumerate(value):
        where = f"{field}[{i}]"
        if not isinstance(item, str) or not item.strip():
            errors.append(_err(where, "must be non-empty text"))
            continue
        item = item.strip()
        if len(item) > max_len:
            errors.append(_err(where, f"is longer than {max_len} characters"))
        elif pattern is not None and not pattern.fullmatch(item):
            errors.append(_err(where, f"must match {pattern.pattern}"))
        elif item in out:
            errors.append(_err(where, f"{item} is named twice"))
        else:
            out.append(item)
    return out


def _unknown(errors: list[FieldError], field: str, value: Mapping[str, Any], known: Iterable[str]) -> None:
    allowed = set(known)
    for key in sorted(str(k) for k in value if k not in allowed):
        errors.append(_err(f"{field}.{key}" if field else key, "is not a field of a built steward"))


def _trigger(errors: list[FieldError], raw: Any) -> Trigger | None:
    if not isinstance(raw, Mapping):
        errors.append(_err("trigger", "must be an object with a kind"))
        return None
    _unknown(errors, "trigger", raw, ("kind", "event", "min_interval_days"))
    kind = raw.get("kind")
    if kind not in TRIGGER_KINDS:
        errors.append(_err("trigger.kind", f"must be one of {', '.join(TRIGGER_KINDS)}"))
        return None
    event = raw.get("event")
    interval = raw.get("min_interval_days")
    if kind == "event":
        if event == TICKET_CREATED:
            errors.append(_err("trigger.event", "a new ticket starts triage only"))
            return None
        if not is_event_name(event):
            errors.append(_err("trigger.event", "must be one of the events the builder catalog lists"))
            return None
        if interval is not None:
            errors.append(_err("trigger.min_interval_days", "only a schedule has a minimum interval"))
        return Trigger(kind="event", event=event)
    if event is not None:
        errors.append(_err("trigger.event", "only an event trigger names an event"))
    if interval is not None and (
        not _whole(interval) or not 1 <= interval <= LIMITS["min_interval_days_max"]
    ):
        errors.append(
            _err("trigger.min_interval_days", f"must be null or 1 to {LIMITS['min_interval_days_max']} whole days")
        )
        interval = None
    return Trigger(kind="schedule", min_interval_days=interval)


def _triggers_allowed(errors: list[FieldError], raw: Any, trigger: Trigger | None) -> TriggerSet | None:
    if raw is None:
        if trigger is None:
            return None
        # The baseline alone, editable within it.
        return TriggerSet(
            schedule=trigger.kind == "schedule",
            events=frozenset({trigger.event}) if trigger.event else frozenset(),
        )
    if not isinstance(raw, Mapping):
        errors.append(_err("triggers_allowed", "must be an object with schedule, events and command"))
        return None
    _unknown(errors, "triggers_allowed", raw, ("schedule", "events", "command"))
    schedule = raw.get("schedule", False)
    command = raw.get("command", False)
    if not isinstance(schedule, bool):
        errors.append(_err("triggers_allowed.schedule", "must be true or false"))
    if not isinstance(command, bool):
        errors.append(_err("triggers_allowed.command", "must be true or false"))
    events = _string_list(
        errors, "triggers_allowed.events", raw.get("events", []), max_items=LIMITS["events_max"]
    )
    for i, name in enumerate(events):
        if name == TICKET_CREATED:
            errors.append(_err(f"triggers_allowed.events[{i}]", "a new ticket starts triage only"))
        elif not is_event_name(name):
            errors.append(
                _err(f"triggers_allowed.events[{i}]", "must be one of the events the builder catalog lists")
            )
    # A built steward's triggers are always the install's to configure within this ceiling.
    return TriggerSet(
        schedule=schedule is True,
        events=frozenset(events),
        command=command is True,
        editable=True,
    )


def _constraints(errors: list[FieldError], raw: Any) -> list[ArgConstraint]:
    if not isinstance(raw, list):
        errors.append(_err("constraints", "must be a list"))
        return []
    if len(raw) > LIMITS["constraints_max"]:
        errors.append(_err("constraints", f"takes at most {LIMITS['constraints_max']} entries"))
        return []
    out: list[ArgConstraint] = []
    for i, entry in enumerate(raw):
        where = f"constraints[{i}]"
        if not isinstance(entry, Mapping):
            errors.append(_err(where, "must be an object with tool, arg and allowed or param"))
            continue
        before = len(errors)
        if "evidence" in entry:
            errors.append(
                _err(
                    f"{where}.evidence",
                    "values the server computes need server code; bind the argument to "
                    "literal values or to one of this steward's parameters",
                )
            )
        _unknown(errors, where, entry, ("tool", "arg", "allowed", "param", "evidence"))
        tool = _string(errors, f"{where}.tool", entry.get("tool"), max_len=80)
        arg = _string(errors, f"{where}.arg", entry.get("arg"), max_len=80)
        has_allowed, has_param = "allowed" in entry, "param" in entry
        if has_allowed == has_param and "evidence" not in entry:
            errors.append(_err(where, "takes exactly one of allowed (values) or param (a parameter name)"))
        allowed: list[str] = []
        param: str | None = None
        if has_allowed:
            allowed = _string_list(
                errors,
                f"{where}.allowed",
                entry.get("allowed"),
                max_items=LIMITS["constraint_values_max"],
                max_len=LIMITS["value_len_max"],
            )
            if not allowed and isinstance(entry.get("allowed"), list):
                errors.append(_err(f"{where}.allowed", "names no value; an empty list is never all"))
        if has_param:
            param = _string(errors, f"{where}.param", entry.get("param"), max_len=41)
            if param and not _PARAM_RE.fullmatch(param):
                errors.append(_err(f"{where}.param", f"must match {_PARAM_RE.pattern}"))
        if len(errors) == before:
            if param is not None:
                out.append(ArgConstraint(tool, arg, param=param))
            else:
                out.append(ArgConstraint(tool, arg, allowed=frozenset(allowed)))
    return out


def _timeouts(errors: list[FieldError], raw: Any) -> list[ToolTimeout]:
    if not isinstance(raw, list):
        errors.append(_err("timeouts", "must be a list"))
        return []
    if len(raw) > LIMITS["tools_max"]:
        errors.append(_err("timeouts", f"takes at most {LIMITS['tools_max']} entries"))
        return []
    out: list[ToolTimeout] = []
    for i, entry in enumerate(raw):
        where = f"timeouts[{i}]"
        if not isinstance(entry, Mapping):
            errors.append(_err(where, "must be an object with tool and max_s"))
            continue
        before = len(errors)
        _unknown(errors, where, entry, ("tool", "max_s"))
        tool = _string(errors, f"{where}.tool", entry.get("tool"), max_len=80)
        max_s = entry.get("max_s")
        if not _whole(max_s) or not 1 <= max_s <= MAX_TIMEOUT_S:
            errors.append(_err(f"{where}.max_s", f"must be 1 to {MAX_TIMEOUT_S} whole seconds"))
        if len(errors) == before:
            out.append(ToolTimeout(tool, max_s))
    return out


def spec_from_data(
    data: Any, *, version: int = 1, steward_id: str | None = None
) -> StewardSpec:
    """Read the data form of a built steward; :class:`BuilderError` with every field error.

    ``version`` is the server's (incremented on every save); ``steward_id``,
    when given, is the id the steward is saved under, and an ``id`` in the data
    must equal it. The verdict tool is always ``steward_verdict`` (added to the
    tools when the data leaves it out), the default mode always ``shadow``, and
    the trigger configuration always the install's to edit within
    ``triggers_allowed``. Only the shape is checked here; what the spec may do
    is :func:`validate_builder`'s.
    """

    errors: list[FieldError] = []
    if not isinstance(data, Mapping):
        raise BuilderError([_err("spec", "must be an object")])
    for key in sorted(str(k) for k in data if k not in _FIELDS):
        if key in _SERVER_FIELDS:
            errors.append(_err(key, "is set by the server; leave it out"))
        else:
            errors.append(_err(key, "is not a field of a built steward"))

    raw_id = data.get("id", steward_id)
    if steward_id is not None and raw_id != steward_id:
        errors.append(_err("id", f"must be {steward_id}, the steward being saved"))
    if not isinstance(raw_id, str) or not BUILT_ID_RE.fullmatch(raw_id):
        errors.append(_err("id", f"must match {BUILT_ID_RE.pattern}"))
    sid = raw_id if isinstance(raw_id, str) else ""
    if sid in CATALOG:
        errors.append(_err("id", f"{sid} is a shipped steward's id"))

    title = _string(errors, "title", data.get("title"), max_len=LIMITS["title_max"])
    description = _string(
        errors, "description", data.get("description", ""), max_len=LIMITS["description_max"], required=False
    )
    prompt = _string(errors, "prompt", data.get("prompt"), max_len=LIMITS["prompt_max"])

    trigger = _trigger(errors, data.get("trigger"))
    triggers_allowed = _triggers_allowed(errors, data.get("triggers_allowed"), trigger)

    tools = _string_list(errors, "tools", data.get("tools"), max_items=LIMITS["tools_max"], max_len=80)
    if isinstance(data.get("tools"), list) and STEWARD_VERDICT_TOOL not in tools:
        tools.append(STEWARD_VERDICT_TOOL)
        if len(tools) > LIMITS["tools_max"]:
            errors.append(
                _err("tools", f"takes at most {LIMITS['tools_max']} tools, {STEWARD_VERDICT_TOOL} included")
            )

    constraints = _constraints(errors, data.get("constraints", []))
    timeouts = _timeouts(errors, data.get("timeouts", []))
    params = _string_list(
        errors, "params", data.get("params", []), max_items=LIMITS["params_max"], pattern=_PARAM_RE, max_len=41
    )
    preconditions = _string_list(
        errors,
        "preconditions",
        data.get("preconditions", []),
        max_items=LIMITS["preconditions_max"],
        pattern=_PARAM_RE,
        max_len=41,
    )

    budget_raw = data.get("budget", {"max_iterations": _DEFAULT_ITERATIONS})
    iterations = _DEFAULT_ITERATIONS
    if not isinstance(budget_raw, Mapping):
        errors.append(_err("budget", "must be an object with max_iterations"))
    else:
        _unknown(errors, "budget", budget_raw, ("max_iterations",))
        iterations = budget_raw.get("max_iterations", _DEFAULT_ITERATIONS)
        low, high = LIMITS["max_iterations_min"], LIMITS["max_iterations_max"]
        if not _whole(iterations) or not low <= iterations <= high:
            errors.append(_err("budget.max_iterations", f"must be {low} to {high} whole model round-trips"))
            iterations = _DEFAULT_ITERATIONS

    sensitive_ok = data.get("sensitive_ok", False)
    if not isinstance(sensitive_ok, bool):
        errors.append(_err("sensitive_ok", "must be true or false"))

    if errors or trigger is None:
        raise BuilderError(errors or [_err("trigger", "is required")])
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise BuilderError([_err("version", "must be a whole number of at least 1")])
    return StewardSpec(
        id=sid,
        title=title,
        description=description,
        prompt=prompt,
        trigger=trigger,
        tools=frozenset(tools),
        verdict_tool=STEWARD_VERDICT_TOOL,
        budget=Budget(max_iterations=iterations),
        constraints=tuple(constraints),
        timeouts=tuple(timeouts),
        params=tuple(params),
        sensitive_ok=sensitive_ok is True,
        default_mode="shadow",
        version=version,
        triggers_allowed=triggers_allowed,
        preconditions=tuple(preconditions),
    )


# -- the builder's rules ---------------------------------------------------------


def _code_ids(runner: _Runner | None) -> set[str]:
    ids = set(CATALOG)
    catalog = getattr(runner, "catalog", None) if runner is not None else None
    code = getattr(catalog, "code", None)
    if isinstance(code, Mapping):
        ids |= set(code)
    return ids


def validate_builder(
    spec: StewardSpec, runner: _Runner | None, *, tool_order: Sequence[str] | None = None
) -> list[FieldError]:
    """Every reason ``spec`` must not be a built steward; ``[]`` when it may be.

    The builder's bans (ADR-0074 rule 2) and limits, addressed to the field the
    editor shows, and then — once those pass — the checks every shipped spec
    passes (``check_dispatchable(validate(spec))``), so a built steward is never
    held to less than a shipped one. ``runner`` supplies the registered
    preconditions and the shipped ids; ``tool_order`` the order the editor listed
    the tools in, for ``tools[i]`` (sorted when not given).
    """

    order = [t for t in (tool_order or ()) if t in spec.tools]
    order += sorted(t for t in spec.tools if t not in order)

    def tool_field(name: str) -> str:
        return f"tools[{order.index(name)}]" if name in order else "tools"

    errors: list[FieldError] = []
    # -- identity and what the server decides
    if not BUILT_ID_RE.fullmatch(spec.id):
        errors.append(_err("id", f"must match {BUILT_ID_RE.pattern}"))
    if spec.id in _code_ids(runner):
        errors.append(_err("id", f"{spec.id} is a shipped steward's id"))
    if spec.verdict_tool != STEWARD_VERDICT_TOOL:
        errors.append(_err("verdict_tool", f"a built steward ends with {STEWARD_VERDICT_TOOL}"))
    if spec.default_mode != "shadow":
        errors.append(_err("default_mode", "a built steward starts in shadow"))
    if spec.version < 1:
        errors.append(_err("version", "must be at least 1"))
    allowed = spec.triggers_allowed
    if allowed is not None and not allowed.editable:
        errors.append(_err("triggers_allowed", "a built steward's triggers are the install's to configure"))
    # -- limits
    if not spec.title.strip() or len(spec.title) > LIMITS["title_max"]:
        errors.append(_err("title", f"must be 1 to {LIMITS['title_max']} characters"))
    if len(spec.description) > LIMITS["description_max"]:
        errors.append(_err("description", f"is longer than {LIMITS['description_max']} characters"))
    if not spec.prompt.strip() or len(spec.prompt) > LIMITS["prompt_max"]:
        errors.append(_err("prompt", f"must be 1 to {LIMITS['prompt_max']} characters"))
    if not spec.tools:
        errors.append(_err("tools", "names no tool"))
    elif len(spec.tools) > LIMITS["tools_max"]:
        errors.append(_err("tools", f"takes at most {LIMITS['tools_max']} tools"))
    low, high = LIMITS["max_iterations_min"], LIMITS["max_iterations_max"]
    if not low <= spec.budget.max_iterations <= high:
        errors.append(_err("budget.max_iterations", f"must be {low} to {high}"))
    if len(spec.params) > LIMITS["params_max"]:
        errors.append(_err("params", f"takes at most {LIMITS['params_max']} parameters"))
    # -- triggers: never a new ticket
    if spec.trigger.kind == "event" and spec.trigger.event == TICKET_CREATED:
        errors.append(_err("trigger.event", "a new ticket starts triage only"))
    if allowed is not None and TICKET_CREATED in allowed.events:
        errors.append(_err("triggers_allowed.events", "a new ticket starts triage only"))
    # -- tools
    for name in order:
        why = tool_refusal(name)
        if why is not None:
            errors.append(_err(tool_field(name), f"{name}: {why}"))
        elif name in SENSITIVE_TOOLS and not spec.sensitive_ok:
            errors.append(
                _err(tool_field(name), f"{name} reads private data; set sensitive_ok to name it")
            )
    usable = {t for t in spec.tools if tool_refusal(t) is None}
    # -- constraints: literal values or a parameter, on an argument the tool takes
    seen: set[tuple[str, str]] = set()
    for i, c in enumerate(spec.constraints):
        where = f"constraints[{i}]"
        if c.evidence is not None:
            errors.append(
                _err(f"{where}.evidence", "values the server computes need server code; use values or a parameter")
            )
            continue
        if c.tool in (STEWARD_VERDICT_TOOL, spec.verdict_tool):
            errors.append(
                _err(f"{where}.tool", f"{c.tool} ends the run; the server decides its effect, not a constraint")
            )
            continue
        if c.tool not in spec.tools:
            errors.append(_err(f"{where}.tool", f"{c.tool} is not one of this steward's tools"))
            continue
        if c.tool not in usable:
            continue  # refused under tools already
        if TOOL_CLASSES[c.tool] == READ_ONLY:
            errors.append(_err(f"{where}.tool", f"{c.tool} is read-only; a constraint on it bounds nothing"))
            continue
        declared, _required = _declared_args(c.tool)
        if c.arg not in declared:
            errors.append(_err(f"{where}.arg", f"{c.tool} takes no argument {c.arg!r} a constraint can bind"))
        else:
            why = arg_refusal(c.tool, c.arg)
            if why is not None:
                errors.append(_err(f"{where}.arg", why))
        if (c.tool, c.arg) in seen:
            errors.append(_err(where, f"a second constraint on {c.tool}.{c.arg}"))
        seen.add((c.tool, c.arg))
        if c.param is not None:
            if c.param not in spec.params:
                errors.append(_err(f"{where}.param", f"{c.param} is not one of this steward's parameters"))
            elif param_kind(c.param) != "list":
                errors.append(
                    _err(f"{where}.param", f"{c.param} is a {param_kind(c.param)}, not a list of values")
                )
    for name in order:
        if name not in usable or TOOL_CLASSES[name] == READ_ONLY or name == spec.verdict_tool:
            continue
        bound = {c.arg for c in spec.constraints_for(name)}
        if not bound:
            errors.append(
                _err(tool_field(name), f"{name} changes something; bind its arguments with a constraint")
            )
            continue
        _declared, required = _declared_args(name)
        unbound = sorted(required - bound - _UNBINDABLE_ARGS)
        if unbound:
            errors.append(
                _err(tool_field(name), f"{name}'s required argument(s) {', '.join(unbound)} carry no constraint")
            )
    # -- timeouts
    timed: set[str] = set()
    for i, t in enumerate(spec.timeouts):
        where = f"timeouts[{i}]"
        if t.tool not in spec.tools:
            errors.append(_err(f"{where}.tool", f"{t.tool} is not one of this steward's tools"))
        elif t.tool in usable and TOOL_CLASSES[t.tool] == READ_ONLY:
            errors.append(_err(f"{where}.tool", f"{t.tool} is read-only; a timeout on it bounds nothing"))
        if t.tool in timed:
            errors.append(_err(where, f"a second timeout on {t.tool}"))
        timed.add(t.tool)
    # -- parameters a trigger or a machine needs
    ceiling = allowed if allowed is not None else TriggerSet()
    if (spec.trigger.kind == "schedule" or ceiling.schedule) and "window" not in spec.params:
        errors.append(_err("params", "a schedule needs a window parameter"))
    # Every way a built steward starts — a schedule, an alert, a command — picks
    # its machines from ``hosts``; without it a host tool would have nowhere to
    # run, or run wherever an alert came from.
    if host_bound(spec) and "hosts" not in spec.params:
        errors.append(_err("params", "works on machines; it needs a hosts parameter naming them"))
    # -- preconditions: the registered library only
    registered = runner.preconditions() if runner is not None else frozenset()
    for i, name in enumerate(spec.preconditions):
        if name not in registered:
            errors.append(_err(f"preconditions[{i}]", f"no precondition {name} is registered on this server"))
    if errors:
        return errors
    # -- everything a shipped spec is held to
    try:
        check_dispatchable(validate(spec))
    except SpecError as exc:
        errors.append(_err("spec", str(exc)))
    return errors


def build_from_data(
    data: Any, runner: _Runner | None, *, version: int = 1, steward_id: str | None = None
) -> StewardSpec:
    """:func:`spec_from_data` then :func:`validate_builder`; :class:`BuilderError` with every error."""

    order = data.get("tools") if isinstance(data, Mapping) else None
    try:
        spec = spec_from_data(data, version=version, steward_id=steward_id)
    except BuilderError as exc:
        # The tools a built steward may never name are worth saying at once,
        # with whatever else is malformed: a clone shows every obstacle.
        flagged = {e["field"] for e in exc.errors}
        extra = [
            _err(f"tools[{i}]", f"{name}: {why}")
            for i, name in enumerate(order if isinstance(order, list) else [])
            if isinstance(name, str)
            and (why := tool_refusal(name)) is not None
            and f"tools[{i}]" not in flagged
        ]
        raise BuilderError([*exc.errors, *extra]) from None
    errors = validate_builder(
        spec, runner, tool_order=[t for t in order if isinstance(t, str)] if isinstance(order, list) else None
    )
    if errors:
        raise BuilderError(errors)
    return spec


def valid_source(source: Any) -> bool:
    """Whether ``source`` names where a built steward came from (:data:`SOURCE_RE`)."""

    return isinstance(source, str) and SOURCE_RE.fullmatch(source) is not None


# -- spec -> data ----------------------------------------------------------------


def _constraint_data(c: ArgConstraint) -> dict[str, Any]:
    if c.evidence is not None:
        return {"tool": c.tool, "arg": c.arg, "evidence": c.evidence}
    if c.param is not None:
        return {"tool": c.tool, "arg": c.arg, "param": c.param}
    return {"tool": c.tool, "arg": c.arg, "allowed": sorted(c.allowed)}


def spec_to_data(spec: StewardSpec) -> dict[str, Any]:
    """The canonical data form of ``spec``: what is stored, loaded and edited.

    Reading it back with :func:`spec_from_data` (at the same version) yields a
    spec with the same ``spec_hash``.
    """

    trigger: dict[str, Any] = {"kind": spec.trigger.kind}
    if spec.trigger.kind == "event":
        trigger["event"] = spec.trigger.event
    else:
        trigger["min_interval_days"] = spec.trigger.min_interval_days
    allowed = spec.triggers_allowed
    if allowed is None:
        allowed = TriggerSet(
            schedule=spec.trigger.kind == "schedule",
            events=frozenset({spec.trigger.event}) if spec.trigger.event else frozenset(),
        )
    return {
        "id": spec.id,
        "title": spec.title,
        "description": spec.description,
        "prompt": spec.prompt,
        "trigger": trigger,
        "triggers_allowed": {
            "schedule": allowed.schedule,
            "events": sorted(allowed.events),
            "command": allowed.command,
        },
        "tools": sorted(spec.tools),
        "constraints": [_constraint_data(c) for c in spec.constraints],
        "timeouts": [{"tool": t.tool, "max_s": t.max_s} for t in spec.timeouts],
        "params": list(spec.params),
        "preconditions": list(spec.preconditions),
        "budget": {"max_iterations": spec.budget.max_iterations},
        "sensitive_ok": spec.sensitive_ok,
    }


def clone_data(catalog_spec: StewardSpec, new_id: str) -> dict[str, Any]:
    """``catalog_spec`` as data under ``new_id``, faithfully: nothing dropped, nothing widened.

    What a built steward may not use stays in the data — an evidence
    constraint, a steward-only or ticket tool, the triage verdict tool — so
    reading it back (:func:`build_from_data`) refuses each with its field
    error, and the editor shows the person what to change. The title says it
    is a copy; everything else is the shipped steward's.
    """

    data = spec_to_data(catalog_spec)
    data["id"] = new_id
    suffix = " (copy)"
    data["title"] = catalog_spec.title[: LIMITS["title_max"] - len(suffix)] + suffix
    return data

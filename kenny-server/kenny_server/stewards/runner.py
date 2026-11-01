"""The one place a steward's run is started (ADR-0071).

Every run passes the same doors in the same order, and each door is here rather
than in the steward so no steward can forget one:

1. **A steward's effect never starts another steward** (rule 6). A ticket whose
   origin is :data:`~kenny_server.ticketstore.STEWARD_ORIGIN` starts nothing,
   and neither does any ticket on a host a steward changed within
   :data:`STEWARD_CHANGE_QUIET_PERIOD`, or is changing in a run still in
   flight (:func:`counts_as_change`) — an alert the change raised opens a
   ticket of another origin — which is recorded as a ``skipped`` run naming
   the steward and the run. Each change a generic run makes is put on its row
   when the gate allows it, before it is forwarded
   (``StewardStore.append_action``), so the record never lags the host.
2. **The global switch** (``KENNY_STEWARDS_ENABLED``) and **the steward's mode**.
   ``off`` either way means the run never existed: nothing is recorded.
3. **The global caps**, concurrent runs (``KENNY_STEWARDS_MAX_CONCURRENT``) and
   tokens over the last 24 hours (``KENNY_STEWARDS_DAILY_TOKENS``), checked
   before the model is called. A run a cap refuses *is* recorded, as
   ``skipped`` with the reason, because "kenny would have looked but was over
   budget" is something a person needs to be able to see. The concurrency cap
   bounds :meth:`StewardRunner.run_generic` runs only, counted in memory from
   admission to the run's ``finally`` so a run that fails, is cancelled or
   cannot close its row never keeps its slot. Triage is exempt from it: every
   new ticket is investigated, however many arrive together; the token cap
   applies to it like to every run.
4. **The run row** (``steward_runs``): opened ``running`` before the first model
   call, closed in a ``finally`` (retried once) with how it ended, its verdict,
   what it did and proposed, and what it cost.

Two ways in. :meth:`StewardRunner.on_ticket_created` starts the ``triage`` steward,
which keeps running on its ticket's own surface (``triage.TriageService``);
:meth:`StewardRunner.run_generic` drives any other steward through
:class:`~kenny_server.stewards.policy.StewardPolicy` and the ordinary tool loop.

Triage's mode is not stored here. It is derived from the settings that already
switch it (``KENNY_TRIAGE_ENABLED`` and the AI availability behind it -> ``off``;
``KENNY_TRIAGE_RESOLVE`` off -> ``shadow``, on -> ``act``), so there is one
source of truth and nothing to migrate; :meth:`StewardRunner.set_mode` writes those
settings for it. Every other steward's mode is its ``steward_settings`` row, or the
spec's default while nobody has chosen one.

A run's mode is read when it starts and again before every change it makes:
the runner hands each run a live predicate (the global switch on and the
steward's mode ``act``) — the gate's ``still_acting`` for a generic run,
``TriageService.still_acting`` for triage's verdict — so demoting a steward or
switching every steward off reaches a run already in flight.

**``act`` binds to the effective hash** (ADR-0072 rule 6): the spec hash
combined with the steward's parameters and, when they differ from the spec's
baseline, its triggers (:func:`~kenny_server.stewards.spec.effective_hash`),
always computed from the live configuration by :meth:`StewardRunner._config`
alone. Choosing ``act`` names the
hash the person choosing it was shown, and is refused unless that is still the
live hash; the stored binding is that reviewed hash, never one read at the
moment of the write. A steward is in ``act`` only while that stored hash
equals the live one, computed from the catalog entry and the stored
configuration on every read. The first read that finds them apart drops the
steward to ``shadow`` for good (a code rollback does not promote it again),
records who and why on the event log, and voids every authorization bound to
another hash. A parameter or trigger edit (:meth:`StewardRunner.set_params`,
:meth:`StewardRunner.set_triggers`) does the same at once. A
generic run freezes its parameters, effective hash and evidence at start, and its
``still_acting`` also turns false once the live hash moves away from the run's,
and, for a scheduled run, once its maintenance window has closed
(``act_until``).

Triage is outside that binding: its mode is its settings, it takes no
parameters, and its trigger is fixed by its spec.

**The circuit breaker** (:meth:`StewardRunner.maybe_trip`): after
:data:`BREAKER_THRESHOLD` consecutive runs of one steward in ``act`` that
stopped the canary (:func:`stops_canary`), it is moved to ``shadow`` as
:data:`BREAKER_ACTOR`. Only runs the server started count — scheduled and
event-started ones; a preview or a command run is a person's, and is neutral.

**Preconditions** (:meth:`StewardRunner.register_precondition`): named
server-side checks a spec declares; :meth:`StewardRunner.precondition_refusal`
says why a run must not start on a host, before any model is called.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..ticketstore import STEWARD_ORIGIN, Ticket
from ..tool_classes import READ_ONLY, classify
from ..toolloop import ToolExecutor, UsageMeter, drive_events
from ..tools import CAPABILITY_TOOLS, redact_audit_args
from ..webfilter import DAY_KEYS, format_hhmm, parse_recurrence
from .authorizations import Authorization, AuthorizationStore
from .catalog import CATALOG, check_dispatchable
from .policy import HOST_ARG, StewardPolicy, StewardSession, Authorizer, host_bound
from .spec import (
    MODES,
    StewardSpec,
    allowed_triggers,
    baseline_config,
    effective_hash,
    effective_triggers,
    evidence_names,
    param_kind,
    resolve,
    runs_on_ticket,
    validate,
    validate_triggers,
    with_evidence,
)
from .store import INTERRUPTED_ERROR, StewardRun, StewardStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..triage import TriageService

__all__ = [
    "BENIGN_REFUSALS",
    "BREAKER_ACTOR",
    "BREAKER_THRESHOLD",
    "BREAKER_TRIGGER_PREFIXES",
    "RESULT_ECHO_ARGS",
    "RESULT_SUMMARY_FIELDS",
    "STEWARD_CHANGE_QUIET_PERIOD",
    "DAILY_TOKENS_SETTING",
    "ENABLED_SETTING",
    "MAX_CONCURRENT_SETTING",
    "PREVIEWS_PER_DAY_SETTING",
    "RUN_RETENTION_DAYS",
    "TICKET_CREATED_TRIGGER",
    "TRIAGE_STEWARD_ID",
    "StewardRunner",
    "EvidenceProvider",
    "HashMismatch",
    "Precondition",
    "caused_by_steward",
    "counts_as_change",
    "halts",
    "interrupted",
    "made_change",
    "run_failed",
    "runs_on_ticket",
    "stops_canary",
    "summarize_result",
]

#: Computes, from the server's own records, the values an evidence-fed
#: constraint admits (ADR-0072 rule 6): ``provider() -> iterable of str``.
EvidenceProvider = Callable[[], Awaitable[Iterable[str]]]

#: A named server-side check before a run starts on a host:
#: ``check(host_id, params) -> None`` to proceed, or a reason to skip. The
#: reason is shown to people and compared to tell one skip from another, so it
#: must be stable text, never a measurement that differs from pass to pass.
#: May be a coroutine function.
Precondition = Callable[[str | None, Mapping[str, Any]], "Awaitable[str | None] | str | None"]

logger = logging.getLogger("kenny.stewards")

#: The catalog id of the steward that runs on a newly created ticket.
TRIAGE_STEWARD_ID = "triage"

#: The ``trigger`` a ticket-creation run is recorded with.
TICKET_CREATED_TRIGGER = "event:ticket_created"

#: The ``trigger`` of an on-demand preview run is this followed by the username
#: of whoever asked for it. Nothing scheduled reads it back as a run of its own.
PREVIEW_TRIGGER_PREFIX = "preview:"

#: Longest username kept in a preview run's ``trigger``.
_MAX_PREVIEWER = 64

#: The global switch, and the two caps every run is checked against.
ENABLED_SETTING = "KENNY_STEWARDS_ENABLED"
MAX_CONCURRENT_SETTING = "KENNY_STEWARDS_MAX_CONCURRENT"
DAILY_TOKENS_SETTING = "KENNY_STEWARDS_DAILY_TOKENS"

#: How many previews of one steward may start in any 24 hours.
PREVIEWS_PER_DAY_SETTING = "KENNY_STEWARDS_PREVIEWS_PER_DAY"

#: The settings triage's mode is derived from and written to.
_TRIAGE_ENABLED = "KENNY_TRIAGE_ENABLED"
_TRIAGE_RESOLVE = "KENNY_TRIAGE_RESOLVE"

#: How long a finished run stays on the record.
RUN_RETENTION_DAYS = 90

#: Ceiling on the free text a run row carries (summary, error).
_MAX_TEXT = 2000

#: How long after a steward changed a host a new ticket on that host starts no
#: triage (ADR-0071 rule 6): the ticket may be the change's own effect, raised
#: as an alert rather than opened by the steward.
STEWARD_CHANGE_QUIET_PERIOD = timedelta(hours=6)

#: The actor recorded when the server, not a person, drops a steward to shadow.
_SYSTEM_ACTOR = "system"

#: The parameter a ``schedule`` trigger reads its maintenance window from.
WINDOW_PARAM = "window"

#: Ceiling on one list parameter's length and on one value's length.
_MAX_PARAM_VALUES = 200
_MAX_PARAM_VALUE_LEN = 200

#: Bounds of a ``percent`` parameter.
_PERCENT = (1, 99)

#: Longest precondition skip reason kept.
_MAX_REASON = 200

#: The run triggers the circuit breaker reads: runs the server started on its
#: own, in a window or on an event. A preview (``preview:``) or a command run
#: is a person's, and neither counts toward a streak nor ends one.
BREAKER_TRIGGER_PREFIXES: tuple[str, ...] = ("schedule:", "event:")

#: Consecutive failed runs of one steward that move it back to ``shadow``.
BREAKER_THRESHOLD = 3

#: Who the circuit breaker acts as when it demotes a steward.
BREAKER_ACTOR = "system:circuit-breaker"

#: Error codes of a change that was *refused*, not one that broke: the steward's
#: remote-control kill switch (``disabled``), its deterministic safety guard
#: (``blocked``) and a game-scoped pause (``paused``). An outcome to read, not
#: a failure to count.
BENIGN_REFUSALS: frozenset[str] = frozenset({"disabled", "blocked", "paused"})

#: The verdicts after which a run that changed something lets the next host go
#: ahead: the run says it acted and checked. Any other verdict, or none, leaves
#: the change unconfirmed.
_CONFIRMING_VERDICTS: frozenset[str] = frozenset({"acted"})

#: Per change-tier tool, the fields of its result kept on the run's action as
#: ``result``, and what each must be: ``count`` a whole number >= 0, ``bool``,
#: or ``enum`` an identifier. Numbers and enums from the tool's own JSON result
#: only, never text: a person reads them, and what the model says about a
#: change is not what the change did. A tool not named here keeps no result.
RESULT_SUMMARY_FIELDS: Mapping[str, Mapping[str, str]] = {
    "fs_cleanup": {
        "target": "enum",
        "files_removed": "count",
        "bytes_freed": "count",
        "complete": "bool",
    },
    "service_restart": {"ok": "bool", "running": "bool"},
}

#: Per tool, the ``enum`` fields of its result that must echo an argument of
#: the call itself (field -> argument): a result naming another target than
#: the one asked for is not believed, and the field is dropped.
RESULT_ECHO_ARGS: Mapping[str, Mapping[str, str]] = {
    "fs_cleanup": {"target": "target"},
}

_ENUM_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

#: The largest count a summary keeps (JSON's exact-integer range).
_MAX_COUNT = 2**53

#: "Read it from the store" for :meth:`StewardRunner._config`.
_LIVE: Any = object()


class PreviewRefused(ValueError):
    """A preview request that must not run.

    ``conflict`` marks a state that may pass (a switch, a cap, a preview already
    running), not a bad request; ``code`` names the reason for a tool result.
    """

    def __init__(self, message: str, *, conflict: bool = False, code: str = "refused") -> None:
        super().__init__(message)
        self.conflict = conflict
        self.code = code


class HashMismatch(ValueError):
    """The effective hash a person reviewed is not the steward's live one.

    Consent binds to what the person saw; if the steward changed in between
    (a concurrent parameter edit), there is nothing to bind it to.
    """


def validate_params(spec: StewardSpec, params: Any) -> dict[str, Any]:
    """``params`` checked against what ``spec`` declares, normalised; else :class:`ValueError`.

    Keys must be among ``spec.params``; each is read by its kind
    (:func:`~kenny_server.stewards.spec.param_kind`). ``window`` is a mapping
    ``{days, start, end[, tz]}`` validated by the web filter's own parser
    (:func:`~kenny_server.webfilter.parse_recurrence`) and stored normalised
    (``days`` as weekday keys, times as ``HH:MM``, the zone filled in). A
    ``bool`` parameter is a JSON ``true`` or ``false`` and nothing else — not
    ``"false"``, not ``0``, not a list. A ``percent`` parameter is a whole JSON
    number from 1 to 99 — not ``"80"``, not ``80.0``, not ``true``. Every other
    parameter is a list of non-empty strings, deduplicated and sorted, so the
    same set always yields the same effective hash. An empty list is kept: it
    admits nothing.
    """

    if not isinstance(params, Mapping):
        raise ValueError("params must be an object")
    unknown = sorted(str(k) for k in params if k not in spec.params)
    if unknown:
        raise ValueError(f"steward {spec.id} takes no parameter(s) {', '.join(unknown)}")
    out: dict[str, Any] = {}
    for name, value in params.items():
        if name == WINDOW_PARAM:
            if not isinstance(value, Mapping):
                raise ValueError("window must be an object with days, start and end")
            extra = sorted(str(k) for k in value if k not in ("days", "start", "end", "tz"))
            if extra:
                raise ValueError(f"window takes no field(s) {', '.join(extra)}")
            days, start_min, end_min, zone = parse_recurrence(
                days=value.get("days"),
                start=value.get("start"),
                end=value.get("end"),
                tz=value.get("tz"),
            )
            out[name] = {
                "days": [DAY_KEYS[d] for d in days],
                "start": format_hhmm(start_min),
                "end": format_hhmm(end_min),
                "tz": zone,
            }
            continue
        if param_kind(name) == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
            out[name] = value
            continue
        if param_kind(name) == "percent":
            low, high = _PERCENT
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be a whole number from {low} to {high}")
            out[name] = value
            continue
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{name} must be a list of values")
        if len(value) > _MAX_PARAM_VALUES:
            raise ValueError(f"{name} takes at most {_MAX_PARAM_VALUES} values")
        if any(not isinstance(v, str) or not v.strip() for v in value):
            raise ValueError(f"every value of {name} must be a non-empty string")
        if any(len(v) > _MAX_PARAM_VALUE_LEN for v in value):
            raise ValueError(f"a value of {name} is longer than {_MAX_PARAM_VALUE_LEN}")
        out[name] = sorted({v.strip() for v in value})
    return out


def surface_refusal(spec: StewardSpec) -> str | None:
    """Why ``spec`` can never be previewed (it runs on a ticket's own surface), or ``None``."""

    if spec.id == TRIAGE_STEWARD_ID or runs_on_ticket(spec):
        return f"{spec.id} runs on a ticket's own surface and cannot be previewed"
    return None


def _preview_brief(spec: StewardSpec, host_id: str | None, params: Mapping[str, Any]) -> str:
    """The one message that starts a preview run.

    Says it is a preview, so the steward reports what it finds and what it would
    do; the gate (always ``shadow`` here) is what stops a change, not this text.
    """

    where = f' on the machine "{host_id}"' if host_id else ""
    lines = [
        f"On-demand preview of the {spec.title.lower()} run{where}, requested by a person. "
        "Investigate as you normally would; any change you attempt is recorded as a "
        "recommendation and not made."
    ]
    packages = params.get("packages")
    if "packages" in spec.params and isinstance(packages, (list, tuple, set, frozenset)):
        allowed = sorted({p for p in packages if isinstance(p, str) and p})
        if allowed:
            lines.append("Package ids you may update: " + ", ".join(allowed) + ".")
        else:
            lines.append("No package is allowed to be updated; update nothing.")
    return "\n".join(lines)


def _declared(spec: StewardSpec, params: Mapping[str, Any]) -> dict[str, Any]:
    """The stored parameters ``spec`` still declares; stray keys left behind."""

    return {name: params[name] for name in spec.params if name in params}


def _clip(value: Any, limit: int = _MAX_TEXT) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def caused_by_steward(ticket: Ticket) -> bool:
    """Whether ``ticket`` was opened by a steward's own effect (ADR-0071 rule 6).

    Such a ticket never starts a steward: otherwise a change that breaks a
    service raises an alert, opens a ticket and starts the next run.
    """

    return ticket.origin == STEWARD_ORIGIN


def _evidence_brief(spec: StewardSpec, evidence: Mapping[str, Sequence[str]]) -> str:
    """The run's frozen evidence, told to the model as facts, one line per constraint.

    The gate enforces the same values whatever the model makes of this; saying
    them only spares the run guessing at what the server will accept.
    """

    lines = ["The server computed these values from its own records when this run started:"]
    for c in sorted(spec.constraints, key=lambda c: (c.tool, c.arg)):
        if c.evidence is None:
            continue
        values = list(evidence.get(c.evidence) or ())
        shown = ", ".join(values) if values else "none"
        lines.append(f"- {c.tool} {c.arg} may be: {shown}")
    return "\n".join(lines)


def _redacted(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{**entry, "args": redact_audit_args(entry.get("args") or {})} for entry in entries]


def _with_outcomes(
    actions: Sequence[Mapping[str, Any]], results: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Each allowed change, with whether it then actually ran (``ok``/``code``).

    The gate records an action when it allows a call; the loop's ``tool_result``
    for that call follows it. Paired in order, by tool and arguments.
    """

    out = [dict(a) for a in actions]
    for result in results:
        tool, args = result.get("tool"), result.get("args")
        candidates = [a for a in out if "ok" not in a and a.get("tool") == tool]
        match = next((a for a in candidates if a.get("args") == args), None)
        if match is None and candidates:
            match = candidates[0]
        if match is None:
            continue
        match["ok"] = bool(result.get("ok"))
        error = result.get("error")
        if isinstance(error, Mapping) and error.get("code"):
            match["code"] = str(error["code"])
        summary = result.get("result")
        if match["ok"] and isinstance(summary, Mapping) and summary:
            match["result"] = dict(summary)
    return out


def summarize_result(
    tool: str, result: Any, args: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """The fields :data:`RESULT_SUMMARY_FIELDS` keeps of ``tool``'s ``result``, or ``None``.

    A field that is missing or not of its kind is left out, never coerced. A
    field :data:`RESULT_ECHO_ARGS` ties to an argument is kept only when it
    equals that argument of the call (``args``); without ``args`` it is left
    out.
    """

    fields = RESULT_SUMMARY_FIELDS.get(tool)
    if fields is None or not isinstance(result, Mapping):
        return None
    echoes = RESULT_ECHO_ARGS.get(tool, {})
    out: dict[str, Any] = {}
    for name, kind in fields.items():
        value = result.get(name)
        if kind == "bool" and isinstance(value, bool):
            out[name] = value
        elif (
            kind == "count"
            and isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= _MAX_COUNT
        ):
            out[name] = value
        elif kind == "enum" and isinstance(value, str) and _ENUM_RE.fullmatch(value):
            if name in echoes and (args is None or args.get(echoes[name]) != value):
                continue
            out[name] = value
    return out or None


class _RecordingExecutor:
    """A run's executor, keeping a summary of each change-tier capability result.

    Everything else is the wrapped executor's. Only a call that reached the
    executor is summarised — one the gate allowed — and only what
    :func:`summarize_result` keeps. The summary is matched to the loop's
    ``tool_result`` event for the same call by its arguments object.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._summaries: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def run_capability(
        self, tool: str, args: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        result = await self._inner.run_capability(tool, args, **kwargs)
        if classify(tool) != READ_ONLY:
            summary = summarize_result(tool, result, args)
            if summary is not None:
                self._summaries.append((tool, args, summary))
        return result

    def take(self, tool: str, args: Any) -> dict[str, Any] | None:
        """The summary of the call ``tool`` with ``args`` made, once; ``None`` if none."""

        for i, (name, called, summary) in enumerate(self._summaries):
            if name == tool and called is args:
                del self._summaries[i]
                return summary
        for i, (name, called, summary) in enumerate(self._summaries):
            if name == tool and called == args:
                del self._summaries[i]
                return summary
        return None


# -- reading a finished run ----------------------------------------------------


def interrupted(run: StewardRun) -> bool:
    """Whether ``run`` was cut short by a shutdown or a restart, not by its own fault."""

    return run.status == "failed" and run.error == INTERRUPTED_ERROR


def run_failed(run: StewardRun) -> bool:
    """Whether ``run`` counts as a failure.

    ``failed`` outright, or a change it made that errored for any reason other
    than a refusal that is an outcome (:data:`BENIGN_REFUSALS`). A run a
    shutdown or restart interrupted is not the steward's fault and is not one.
    """

    if run.status == "failed":
        return not interrupted(run)
    if run.status != "completed":
        return False
    return any(
        a.get("ok") is False and a.get("code") not in BENIGN_REFUSALS for a in run.actions
    )


def made_change(run: StewardRun) -> bool:
    """Whether ``run`` changed something: an action the gate allowed that did not fail."""

    return any(a.get("ok") is not False for a in run.actions)


def counts_as_change(run: StewardRun) -> bool:
    """Whether ``run`` changed, or may yet change, what it ran on (ADR-0071 rule 6).

    * still ``running`` in ``act`` (any steward but triage, whose verdict
      changes a ticket, not a machine), or with a change already recorded:
      it may change the host at any moment, and its changes are put on its row
      as they are made (``StewardStore.append_action``);
    * ``failed`` — interrupted or not — with any change recorded, whatever its
      outcome: an attempt cut short may have run;
    * otherwise, an action the gate allowed that did not fail (``ok`` not
      ``False``).

    A ``skipped`` run never ran.
    """

    if run.status == "skipped":
        return False
    if run.status == "running":
        return bool(run.actions) or (run.mode == "act" and run.steward_id != TRIAGE_STEWARD_ID)
    if run.status == "failed":
        return bool(run.actions)
    return any(a.get("ok") is not False for a in run.actions)


def halts(run: StewardRun) -> bool:
    """Whether ``run`` changed something and did not confirm it (any verdict but ``acted``)."""

    return made_change(run) and run.verdict not in _CONFIRMING_VERDICTS


def stops_canary(run: StewardRun) -> bool:
    """Whether the hosts after ``run`` must wait for a person.

    Also what the circuit breaker counts, unless the run was
    :func:`interrupted`.
    """

    return run_failed(run) or halts(run)


class StewardRunner:
    """Starts, bounds and records every run of every steward.

    ``ai_access`` is the :class:`~kenny_server.ai.AiAccess` the rest of the
    server asks whether a model may be called; ``triage`` is the service the
    ``triage`` steward runs through (``None`` while no assistant could be built).
    ``catalog`` defaults to the shipped one; tests pass their own.
    """

    def __init__(
        self,
        *,
        store: StewardStore,
        settings: Any,
        ai_access: Any = None,
        triage: TriageService | None = None,
        catalog: Mapping[str, StewardSpec] = CATALOG,
        event_store: Any = None,
        now: Callable[[], datetime] | None = None,
        authorizations: AuthorizationStore | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.ai_access = ai_access
        self._triage: TriageService | None = None
        self.triage = triage
        self.catalog = catalog
        #: Where a mode change is recorded with who made it.
        self.event_store = event_store
        self._now = now or (lambda: datetime.now(timezone.utc))
        # Held across "count what is running" and "open the row", so two runs
        # starting together cannot both see room under the concurrency cap.
        self._admission = asyncio.Lock()
        #: Generic runs admitted and not yet past their ``finally``. In memory,
        #: not ``steward_runs``: a row a failed close left ``running`` must not
        #: hold a slot until the next restart.
        self._in_flight = 0
        #: Preview runs still going. The event loop keeps only a weak reference
        #: to a task, so a fire-and-forget one can be collected mid-run.
        self._previews: set[asyncio.Task[None]] = set()
        #: ``(steward id, host id)`` of every preview from its admission check to
        #: its end: at most one runs per steward and host.
        self._preview_targets: set[tuple[str, str | None]] = set()
        #: Held from a preview's checks until its row exists, so two requests
        #: cannot both pass the daily limit or the one-at-a-time rule.
        self._preview_admission = asyncio.Lock()
        #: The standing authorizations (ADR-0072) a run's ``normal_change`` is
        #: matched against. Without a store no ``normal_change`` ever runs.
        self.authorizations = authorizations
        #: What :meth:`run_generic` uses when its caller passes none
        #: (:meth:`configure`).
        self._executor: ToolExecutor | None = None
        self._client_factory: Callable[[], Any] | None = None
        self._model: str | Callable[[], str] | None = None
        #: Named evidence providers (:meth:`register_evidence`).
        self._evidence: dict[str, EvidenceProvider] = {}
        #: Named preconditions (:meth:`register_precondition`).
        self._preconditions: dict[str, Precondition] = {}
        #: Held across "check the hash a person was shown" and the write that
        #: binds to it (a grant, ``act``), and across "void what is bound to
        #: another hash" and the configuration write that moves the hash, so
        #: no grant at the old hash can land between a void and its write and
        #: come back to life when the configuration is reverted.
        self._consent = asyncio.Lock()

    def now(self) -> datetime:
        """The runner's clock (injected in tests); what expiry and budgets are judged by."""

        return self._now()

    def configure(
        self,
        *,
        executor: ToolExecutor | None = None,
        client_factory: Callable[[], Any] | None = None,
        model: str | Callable[[], str] | None = None,
    ) -> None:
        """Set what a run started without explicit ``client``/``model``/``executor`` uses.

        ``model`` may be a callable, read at each run start, so a live setting
        (``KENNY_CHAT_MODEL``) reaches the next run. Arguments left ``None``
        keep what was configured before.
        """

        if executor is not None:
            self._executor = executor
        if client_factory is not None:
            self._client_factory = client_factory
        if model is not None:
            self._model = model

    @property
    def executor(self) -> ToolExecutor | None:
        """The executor :meth:`configure` set: what a run calls, and what asks a host if it is idle."""

        return self._executor

    # -- evidence ------------------------------------------------------------

    def register_evidence(self, name: str, provider: EvidenceProvider) -> None:
        """Feed every constraint declaring ``evidence=name`` from ``provider``.

        Registering a name twice is refused: which records feed a constraint
        must not depend on wiring order.
        """

        if name in self._evidence:
            raise ValueError(f"evidence provider {name!r} is already registered")
        self._evidence[name] = provider

    def evidence_providers(self) -> frozenset[str]:
        """The evidence names a provider is registered for."""

        return frozenset(self._evidence)

    async def resolve_evidence(self, spec: StewardSpec) -> dict[str, list[str]]:
        """What each evidence provider ``spec`` names computes now, sorted.

        A provider that is missing, raises or returns anything but strings
        yields the empty list for its name — which admits nothing — and a
        warning: failing evidence must never widen a run (ADR-0072 rule 6).
        """

        out: dict[str, list[str]] = {}
        for name in evidence_names(spec):
            provider = self._evidence.get(name)
            if provider is None:
                logger.warning("steward %s: no evidence provider %r; it admits nothing", spec.id, name)
                out[name] = []
                continue
            try:
                raw = await provider()
                if isinstance(raw, (str, bytes, Mapping)):
                    # One id, or a mapping, is not a collection of ids.
                    raise TypeError(f"evidence must be a collection of ids, not {type(raw).__name__}")
                values = sorted({v for v in raw if isinstance(v, str) and v})
            except Exception:  # noqa: BLE001 - evidence that fails admits nothing
                logger.warning(
                    "steward %s: evidence provider %r failed; it admits nothing",
                    spec.id,
                    name,
                    exc_info=True,
                )
                values = []
            out[name] = values
        return out

    # -- preconditions ---------------------------------------------------------

    def register_precondition(self, name: str, check: Precondition) -> None:
        """Answer every spec declaring precondition ``name`` with ``check``.

        Registering a name twice is refused, as for evidence providers.
        """

        if name in self._preconditions:
            raise ValueError(f"precondition {name!r} is already registered")
        self._preconditions[name] = check

    def preconditions(self) -> frozenset[str]:
        """The precondition names a check is registered for."""

        return frozenset(self._preconditions)

    async def precondition_refusal(
        self, spec: StewardSpec, host_id: str | None, params: Mapping[str, Any]
    ) -> str | None:
        """Why a run of ``spec`` must not start on ``host_id`` now, or ``None``.

        Asks each precondition ``spec`` declares, in order, and returns the
        first reason. Fails closed: a name nothing is registered for, a check
        that raises, and an answer that is neither ``None`` nor text each skip
        the run, with a stable reason of their own.
        """

        for name in spec.preconditions:
            check = self._preconditions.get(name)
            if check is None:
                logger.warning("steward %s: no precondition %r is registered", spec.id, name)
                return f"precondition {name} is not available on this server"
            try:
                answer = check(host_id, params)
                if inspect.isawaitable(answer):
                    answer = await answer
            except Exception:  # noqa: BLE001 - a check that fails is a skip, not a run
                logger.warning(
                    "steward %s: precondition %r failed on %s", spec.id, name, host_id, exc_info=True
                )
                return f"precondition {name} could not be checked"
            if answer is None:
                continue
            if not isinstance(answer, str) or not answer.strip():
                return f"precondition {name} gave no usable answer"
            return _clip(answer, _MAX_REASON)
        return None

    @property
    def triage(self) -> TriageService | None:
        """The service the ``triage`` steward runs through, wired to this runner's mode."""

        return self._triage

    @triage.setter
    def triage(self, service: TriageService | None) -> None:
        self._triage = service
        if service is not None:
            service.still_acting = self._triage_still_acting

    def _triage_still_acting(self) -> bool:
        """Whether a triage verdict may still resolve: switch on, triage in ``act``."""

        return self.enabled() and self._triage_mode() == "act"

    # -- lifecycle -----------------------------------------------------------

    async def startup(self) -> None:
        """Account for runs a dead process left open; drop runs past retention.

        Also unbinds, before anything runs, what was granted to a steward that is
        no longer what it was: an authorization bound to another hash is voided
        and a stale ``act`` dropped; for a steward no longer in the catalog at
        all, every live authorization is voided and a stored ``act`` dropped,
        so removing a steward and shipping an identical spec later revives
        neither (ADR-0072 rule 6).
        """

        interrupted = await self.store.fail_interrupted()
        if interrupted:
            logger.warning("%d steward run(s) were interrupted by a restart", interrupted)
        cutoff = self._now() - timedelta(days=RUN_RETENTION_DAYS)
        await self.store.prune(cutoff.isoformat())
        # A new release can change what a steward is (its spec, a tool's tier).
        # Unbind what was granted against the old one before anything runs,
        # whatever mode the steward is in, so a later rollback revives nothing.
        for spec in self.catalog.values():
            live = await self.live_hash(spec.id)
            if live is not None:
                await self._void_others(spec.id, live, _SYSTEM_ACTOR)
            await self._mode_for(spec)
        await self._unbind_removed()

    async def _unbind_removed(self) -> None:
        """Void the grants of, and drop ``act`` for, every steward the catalog lacks."""

        present = sorted(self.catalog)
        if self.authorizations is not None:
            voided = await self.authorizations.void_absent_stewards(present, _SYSTEM_ACTOR)
            for steward_id, count in sorted(voided.items()):
                await self._record_event(
                    steward_id,
                    f"steward {steward_id}: {count} authorization(s) voided by {_SYSTEM_ACTOR}: "
                    "the steward is no longer in the catalog",
                    {"voided": count, "actor": _SYSTEM_ACTOR},
                    level="warning",
                )
        for steward_id in await self.store.demote_absent(present, actor=_SYSTEM_ACTOR):
            await self._record_event(
                steward_id,
                f"steward {steward_id}: mode set to shadow by {_SYSTEM_ACTOR}: "
                "the steward is no longer in the catalog",
                {"mode": "shadow", "actor": _SYSTEM_ACTOR},
                level="warning",
            )

    # -- modes ---------------------------------------------------------------

    def enabled(self) -> bool:
        """The global switch: off means no steward starts."""

        return bool(self.settings.get(ENABLED_SETTING))

    def spec(self, steward_id: str) -> StewardSpec:
        """The catalog spec for ``steward_id``; :class:`KeyError` if there is none."""

        spec = self.catalog.get(steward_id)
        if spec is None:
            raise KeyError(steward_id)
        return spec

    def _triage_mode(self) -> str:
        if self.ai_access is not None:
            on = bool(self.ai_access.enabled("triage"))
        else:
            on = bool(self.settings.get(_TRIAGE_ENABLED))
        if not on:
            return "off"
        return "act" if self.settings.get(_TRIAGE_RESOLVE) else "shadow"

    async def live_hash(self, steward_id: str) -> str | None:
        """What ``steward_id`` is right now: its catalog spec and stored configuration.

        ``None`` for an id the catalog no longer has.
        """

        spec = self.catalog.get(steward_id)
        if spec is None:
            return None
        return await self._config_hash(spec)

    async def _config(
        self, spec: StewardSpec, *, params: Any = _LIVE, triggers: Any = _LIVE
    ) -> tuple[dict[str, Any], Any, str]:
        """``(params, stored triggers, effective hash)`` of ``spec``: the one place it is computed.

        Each of ``params`` and ``triggers`` is read from the store unless given
        — given, to ask what the hash *would* be with that one replaced — so no
        caller can hash a steward without the half of its configuration it did
        not think of. Triage takes no parameters.
        """

        if params is _LIVE:
            params = (
                {} if spec.id == TRIAGE_STEWARD_ID else _declared(spec, await self.store.get_params(spec.id))
            )
        if triggers is _LIVE:
            triggers = await self.store.get_triggers(spec.id)
        return params, triggers, effective_hash(spec, params, triggers)

    async def _config_hash(
        self, spec: StewardSpec, *, params: Any = _LIVE, triggers: Any = _LIVE
    ) -> str:
        """``spec``'s effective hash from the live configuration (:meth:`_config`)."""

        return (await self._config(spec, params=params, triggers=triggers))[2]

    async def _mode_for(self, spec: StewardSpec) -> str:
        if spec.id == TRIAGE_STEWARD_ID:
            return self._triage_mode()
        stored = await self.store.get_mode(spec.id)
        mode = stored if stored in MODES else spec.default_mode
        if mode != "act":
            return mode
        live = await self.live_hash(spec.id)
        if live is not None and await self.store.get_act_hash(spec.id) == live:
            return "act"
        # ``act`` was chosen for something this steward no longer is. Exactly one
        # caller wins the conditional demotion and records it.
        if await self.store.demote_unbound(spec.id, live or "", actor=_SYSTEM_ACTOR):
            logger.warning(
                "steward %s: its effective hash changed since act was chosen; dropped to shadow",
                spec.id,
            )
            await self._record_event(
                spec.id,
                f"steward {spec.id}: mode set to shadow by {_SYSTEM_ACTOR}: "
                "its effective hash changed since act was chosen",
                {"mode": "shadow", "actor": _SYSTEM_ACTOR, "effective_hash": live},
                level="warning",
            )
            if live is not None:
                await self._void_others(spec.id, live, _SYSTEM_ACTOR)
        return "shadow"

    async def _void_others(self, steward_id: str, live: str, actor: str) -> int:
        """Void ``steward_id``'s authorizations bound to any hash but ``live``; record it."""

        if self.authorizations is None:
            return 0
        voided = await self.authorizations.void_other_hashes(steward_id, live, actor)
        if voided:
            await self._record_event(
                steward_id,
                f"steward {steward_id}: {voided} authorization(s) voided by {actor}: "
                "bound to what the steward no longer is",
                {"voided": voided, "actor": actor, "effective_hash": live},
                level="warning",
            )
        return voided

    async def mode_of(self, steward_id: str) -> str:
        """The mode ``steward_id`` runs in now; :class:`KeyError` for an unknown id."""

        return await self._mode_for(self.spec(steward_id))

    async def _reviewed(self, spec: StewardSpec, reviewed_hash: str | None) -> str:
        """``reviewed_hash`` if it is ``spec``'s live hash; else :class:`ValueError`.

        :class:`HashMismatch` when the person reviewed something the steward no
        longer is; a plain :class:`ValueError` when they named nothing.
        """

        if not isinstance(reviewed_hash, str) or not reviewed_hash:
            raise ValueError(
                "effective_hash is required: the hash of the steward as it was shown "
                "to the person consenting"
            )
        live = await self.live_hash(spec.id)
        if reviewed_hash != live:
            raise HashMismatch(
                f"steward {spec.id} changed since it was shown (effective hash "
                f"{reviewed_hash[:12]} is now {str(live)[:12]}); review it again"
            )
        return reviewed_hash

    async def set_mode(
        self, steward_id: str, mode: str, *, actor: str, effective_hash: str | None = None
    ) -> str:
        """Choose ``steward_id``'s mode; returns the mode now in force.

        Choosing ``act`` needs ``effective_hash``: the hash of the steward as the
        person choosing it was shown it (ADR-0072 rule 6). ``act`` binds to that
        hash, never to whatever the steward is at the moment of the write, so a
        concurrent parameter edit cannot widen what they agreed to;
        :class:`HashMismatch` when it is no longer the live hash. ``off`` and
        ``shadow`` take none: the circuit breaker demotes without one.

        For ``triage`` this writes its settings through the same
        :meth:`~kenny_server.config.Settings.set` the dashboard's settings page
        uses, so their apply-hooks rebind it at once; ``KENNY_TRIAGE_RESOLVE``
        is written before ``KENNY_TRIAGE_ENABLED`` so a ticket created between
        the two writes never runs in the mode being left, and ``off`` writes
        ``KENNY_TRIAGE_RESOLVE`` off as well. The mode in force can
        differ from ``mode``: triage chosen ``shadow`` stays ``off`` while no AI
        key or gateway is configured.

        Raises :class:`KeyError` for an unknown steward, :class:`ValueError` for
        an unknown mode or an ``act`` without a hash.
        """

        spec = self.spec(steward_id)
        if mode not in MODES:
            raise ValueError(f"unknown steward mode {mode!r}; expected one of {', '.join(MODES)}")
        async with self._consent:
            bound = await self._reviewed(spec, effective_hash) if mode == "act" else None
            if spec.id == TRIAGE_STEWARD_ID:
                # Off clears resolve too, so a run already in flight cannot resolve
                # at its verdict, and turning triage back on starts it in shadow.
                await self.settings.set(_TRIAGE_RESOLVE, "1" if mode == "act" else "0")
                await self.settings.set(_TRIAGE_ENABLED, "0" if mode == "off" else "1")
            else:
                # ``act`` is chosen for what the person was shown; it binds to that.
                await self.store.set_mode(spec.id, mode, actor=actor, act_hash=bound)
        await self._record_mode_change(spec.id, mode, actor)
        return await self._mode_for(spec)

    async def _record_mode_change(self, steward_id: str, mode: str, actor: str) -> None:
        """Put who chose which mode on the event log.

        For every steward alike: triage's settings rows carry no author, and an
        ``steward_settings`` row keeps only the latest choice.
        """

        await self._record_event(
            steward_id,
            f"steward {steward_id}: mode set to {mode} by {actor}",
            {"mode": mode, "actor": actor},
        )

    async def _record_event(
        self, steward_id: str, message: str, fields: Mapping[str, Any], *, level: str = "info"
    ) -> None:
        """One row on the event log about ``steward_id``; losing it never undoes the change."""

        if self.event_store is None:
            logger.info(message)
            return
        try:
            await self.event_store.insert_log(
                source="server",
                at=self._now().isoformat(),
                level=level,
                target=logger.name,
                message=message,
                fields={"steward": steward_id, **fields},
            )
        except Exception:  # noqa: BLE001 - the change is made; losing its record must not undo it
            logger.warning("failed to record a change of steward %s", steward_id, exc_info=True)

    # -- parameters ----------------------------------------------------------

    async def get_params(self, steward_id: str) -> dict[str, Any]:
        """``steward_id``'s parameters as its spec declares them; :class:`KeyError` if unknown."""

        spec = self.spec(steward_id)
        if spec.id == TRIAGE_STEWARD_ID:
            return {}
        return _declared(spec, await self.store.get_params(spec.id))

    async def set_params(
        self, steward_id: str, params: Mapping[str, Any], *, actor: str
    ) -> dict[str, Any]:
        """Replace ``steward_id``'s parameters; returns ``{params, effective_hash, mode, voided}``.

        A change of the effective hash drops a steward in ``act`` to ``shadow``
        (in the same write as the parameters) and voids every authorization
        bound to another hash — first, so no attempt can be spent against what
        the steward used to be (ADR-0072 rule 6). The void and the write hold
        the lock a grant holds from its hash check to its insert, so no grant at
        the old hash can land between them. Writing the parameters it
        already has changes nothing. Raises :class:`KeyError` for an unknown
        steward and :class:`ValueError` for parameters its spec does not take.
        """

        spec = self.spec(steward_id)
        clean = validate_params(spec, params)
        async with self._consent:
            before_hash = await self._config_hash(spec)
            after_hash = await self._config_hash(spec, params=clean)
            changed = before_hash != after_hash
            voided = await self._void_others(spec.id, after_hash, actor) if changed else 0
            before_mode = await self.store.set_params(spec.id, clean, actor=actor, demote=changed)
        if changed:
            await self._record_event(
                spec.id,
                f"steward {spec.id}: parameters changed by {actor}",
                {"actor": actor, "params": clean, "effective_hash": after_hash},
            )
            if before_mode == "act":
                await self._record_event(
                    spec.id,
                    f"steward {spec.id}: mode set to shadow by {actor}: its parameters changed",
                    {"mode": "shadow", "actor": actor, "effective_hash": after_hash},
                )
        return {
            "params": clean,
            "effective_hash": after_hash,
            "mode": await self._mode_for(spec),
            "voided": voided,
        }

    # -- triggers --------------------------------------------------------------

    async def trigger_config(self, spec: StewardSpec) -> dict[str, Any]:
        """``spec``'s trigger configuration in force: the stored one within its ceiling."""

        return effective_triggers(spec, await self.store.get_triggers(spec.id))

    async def set_triggers(
        self,
        steward_id: str,
        triggers: Any,
        *,
        actor: str,
        effective_hash: str | None = None,
    ) -> dict[str, Any]:
        """Replace ``steward_id``'s trigger configuration; returns ``{triggers, effective_hash, mode, voided}``.

        What may start a steward is part of what a person consented to, so this
        is :meth:`set_params` for triggers: a change of the effective hash voids
        every authorization bound to another hash first, then writes and drops
        a steward in ``act`` to ``shadow`` in one transaction (ADR-0072 rule 6),
        all under the lock a grant holds from its hash check to its insert;
        reverting later revives neither. A configuration whose effective hash
        is the live one changes nothing and writes nothing.

        ``effective_hash``, when given, is the hash the person was shown;
        :class:`HashMismatch` when it is no longer the live one. Raises
        :class:`KeyError` for an unknown steward and :class:`ValueError` for a
        steward whose triggers its spec fixes or a configuration it does not
        allow (:func:`~kenny_server.stewards.spec.validate_triggers`).
        """

        spec = self.spec(steward_id)
        if not allowed_triggers(spec).editable:
            raise ValueError(f"steward {spec.id}'s triggers are fixed by its spec")
        clean = validate_triggers(spec, triggers)
        async with self._consent:
            if effective_hash is not None:
                await self._reviewed(spec, effective_hash)
            before_hash = await self._config_hash(spec)
            after_hash = await self._config_hash(spec, triggers=clean)
            if before_hash != after_hash:
                voided = await self._void_others(spec.id, after_hash, actor)
                before_mode = await self.store.set_config(
                    spec.id, triggers=clean, actor=actor, demote=True
                )
        if before_hash == after_hash:
            return {
                "triggers": await self.trigger_config(spec),
                "effective_hash": before_hash,
                "mode": await self._mode_for(spec),
                "voided": 0,
            }
        await self._record_event(
            spec.id,
            f"steward {spec.id}: triggers changed by {actor}",
            {"actor": actor, "triggers": clean, "effective_hash": after_hash},
        )
        if before_mode == "act":
            await self._record_event(
                spec.id,
                f"steward {spec.id}: mode set to shadow by {actor}: its triggers changed",
                {"mode": "shadow", "actor": actor, "effective_hash": after_hash},
            )
        return {
            "triggers": effective_triggers(spec, clean),
            "effective_hash": after_hash,
            "mode": await self._mode_for(spec),
            "voided": voided,
        }

    # -- the circuit breaker ---------------------------------------------------

    async def breaker_history(self, steward_id: str) -> list[StewardRun]:
        """Every run of ``steward_id`` the breaker reads, newest first (:data:`BREAKER_TRIGGER_PREFIXES`).

        Selected by trigger in the store, so no number of previews or command
        runs can push one out.
        """

        return await self.store.runs_with_trigger_prefixes(steward_id, BREAKER_TRIGGER_PREFIXES)

    async def maybe_trip(self, steward_id: str, *, threshold: int = BREAKER_THRESHOLD) -> bool:
        """Move ``steward_id`` to ``shadow`` after ``threshold`` consecutive canary stops in ``act``.

        Returns whether it did. Skipped, running and interrupted runs are
        neutral: passed over, never counted, never ending a streak.
        """

        streak = 0
        for run in await self.breaker_history(steward_id):  # newest first
            if run.status in ("skipped", "running") or interrupted(run):
                continue
            if not stops_canary(run):
                break
            streak += 1
        if streak < max(1, int(threshold)):
            return False
        if await self.mode_of(steward_id) != "act":
            return False
        await self.set_mode(steward_id, "shadow", actor=BREAKER_ACTOR)
        message = (
            f"steward {steward_id}: {streak} runs in a row failed or left a change unconfirmed; "
            "moved back to shadow by the circuit breaker"
        )
        logger.warning(message)
        await self._record_event(
            steward_id,
            message,
            {"failed_in_a_row": streak, "actor": BREAKER_ACTOR},
            level="warning",
        )
        return True

    # -- standing authorizations ---------------------------------------------

    def _authorizations(self) -> AuthorizationStore:
        if self.authorizations is None:
            raise RuntimeError("standing authorizations are not configured")
        return self.authorizations

    async def grant(
        self,
        steward_id: str,
        *,
        tool: str,
        scope: Any,
        max_attempts_per_day: Any,
        expires_at: Any,
        actor: str,
        effective_hash: str | None,
        note: str = "",
    ) -> Authorization:
        """Grant ``steward_id`` a standing authorization bound to the hash a person reviewed.

        ``effective_hash`` is the steward's hash as the person granting was shown
        it; the grant binds to it, never to the hash at the moment of the
        write. Raises :class:`KeyError` for an unknown steward,
        :class:`HashMismatch` when it is no longer the live hash, a
        :class:`ValueError` when it is missing, and
        :class:`~kenny_server.stewards.authorizations.AuthorizationError` for a
        grant the store refuses. Who may call this is the API's to enforce.
        """

        spec = self.spec(steward_id)
        store = self._authorizations()
        async with self._consent:
            # The hash is checked and the grant written with no configuration
            # write in between (``_consent``).
            reviewed = await self._reviewed(spec, effective_hash)
            return await store.grant(
                spec=spec,
                effective_hash=reviewed,
                tool=tool,
                scope=scope,
                max_attempts_per_day=max_attempts_per_day,
                expires_at=expires_at,
                granted_by=actor,
                note=note,
                now=self._now(),
            )

    # -- what the API shows --------------------------------------------------

    async def overview(self) -> list[dict[str, Any]]:
        """Every catalog steward: its public spec, its mode and its latest run."""

        out: list[dict[str, Any]] = []
        for spec in self.catalog.values():
            latest = await self.store.list_runs(steward_id=spec.id, limit=1)
            mode = await self._mode_for(spec)
            _params, stored_triggers, live = await self._config(spec)
            if spec.id == TRIAGE_STEWARD_ID:
                # Triage's ``act`` is its settings, not a binding to a hash.
                bound: bool | None = None
            else:
                bound = mode == "act" and await self.store.get_act_hash(spec.id) == live
            out.append(
                {
                    **spec.to_public(),
                    "mode": mode,
                    "params": await self.get_params(spec.id),
                    "effective_hash": live,
                    "act_bound": bound,
                    "triggers": effective_triggers(spec, stored_triggers),
                    "triggers_baseline": baseline_config(spec),
                    "triggers_allowed": allowed_triggers(spec).to_public(),
                    # Where each tool acts: on the run's machine, or on the
                    # server itself. A grant's scope follows it (ADR-0072: a
                    # host list for one, the ``server`` sentinel for the other).
                    "host_bound": host_bound(spec),
                    "tool_targets": {
                        tool: "host"
                        if tool in CAPABILITY_TOOLS or tool in HOST_ARG
                        else "server"
                        for tool in sorted(spec.tools)
                    },
                    "latest_run": latest[0].to_public() if latest else None,
                }
            )
        return out

    # -- admission -----------------------------------------------------------

    async def _cap_reason(self, *, counted: bool) -> str | None:
        """Why a run may not start now under the global caps, or ``None``.

        ``counted`` runs are bounded by the concurrency cap; every run by the
        token cap.
        """

        if counted:
            limit = max(1, int(self.settings.get(MAX_CONCURRENT_SETTING)))
            if self._in_flight >= limit:
                return f"{self._in_flight} steward run(s) already in flight; the limit is {limit}"
        cap = int(self.settings.get(DAILY_TOKENS_SETTING))
        if cap > 0:
            since = self._now() - timedelta(hours=24)
            spent = await self.store.tokens_since(since.isoformat())
            if spent >= cap:
                return f"steward runs spent {spent} tokens in the last 24 hours; the cap is {cap}"
        return None

    async def _admit(
        self,
        spec: StewardSpec,
        mode: str,
        *,
        trigger: str,
        subject: str | None,
        host_id: str | None,
        ticket_id: str | None = None,
        counted: bool,
        params: Mapping[str, Any] | None = None,
        run_hash: str | None = None,
        evidence: Mapping[str, Sequence[str]] | None = None,
        record_refusal: bool = True,
        initiated_by: str | None = None,
    ) -> tuple[StewardRun, bool]:
        """Open the run row; ``(row, False)`` when a cap refused it (row ``skipped``).

        An admitted ``counted`` run holds a concurrency slot; the caller must
        :meth:`_release` it in a ``finally``. Without ``record_refusal`` (a
        preview: the person who asked is told at once) a cap's refusal writes
        no row and raises :class:`PreviewRefused` instead; otherwise every
        refused request would be a free row on the record.
        """

        async with self._admission:
            reason = await self._cap_reason(counted=counted)
            if reason is not None and not record_refusal:
                logger.info("steward %s preview not started: %s", spec.id, reason)
                raise PreviewRefused(reason, conflict=True, code="over_cap")
            run = await self.store.start_run(
                steward_id=spec.id,
                spec_hash=spec.spec_hash,
                trigger=trigger,
                mode=mode,
                subject=subject,
                host_id=host_id,
                ticket_id=ticket_id,
                params=params,
                effective_hash=run_hash,
                evidence=evidence,
                initiated_by=initiated_by,
            )
            if reason is None:
                if counted:
                    self._in_flight += 1
                return run, True
            logger.info("steward %s run not started: %s", spec.id, reason)
            return await self.store.finish_run(run.id, status="skipped", error=reason), False

    def _release(self) -> None:
        """Give back the concurrency slot an admitted ``counted`` run held."""

        self._in_flight = max(0, self._in_flight - 1)

    async def _close(self, run_id: str, **outcome: Any) -> StewardRun | None:
        """Close the run row; one retry, then log and give up (``None``).

        Whatever happens here the run's slot is already free: the slot is held
        in memory, and a row left ``running`` is accounted for at the next
        :meth:`startup`.
        """

        for attempt in (1, 2):
            try:
                return await self.store.finish_run(run_id, **outcome)
            except Exception:  # noqa: BLE001 - the run is over either way
                if attempt == 2:
                    logger.exception("steward run %s could not be closed; left running", run_id)
                else:
                    logger.warning("steward run %s failed to close; retrying", run_id, exc_info=True)
        return None

    # -- triage: a new ticket ------------------------------------------------

    async def on_ticket_created(self, ticket: Ticket) -> None:
        """Start the ``triage`` steward on ``ticket``, best-effort.

        Registered with :meth:`~kenny_server.tickets.TicketService.set_triage`.
        Never raises: the ticket already exists and is the operator's to see,
        so a failure here costs the analysis and nothing else (ADR-0027's
        bargain, as ``TriageService.run`` strikes it).
        """

        try:
            await self._on_ticket_created(ticket)
        except Exception:  # noqa: BLE001 - a failed run must not cost the ticket
            logger.exception("steward run for ticket %s failed to start or finish", ticket.id)

    async def _on_ticket_created(self, ticket: Ticket) -> None:
        if caused_by_steward(ticket):
            logger.debug("ticket %s was opened by a steward; no steward starts on it", ticket.id)
            return
        if self.triage is None or TRIAGE_STEWARD_ID not in self.catalog or not self.enabled():
            return
        spec = self.catalog[TRIAGE_STEWARD_ID]
        mode = await self._mode_for(spec)
        if mode == "off":
            return
        if ticket.agent_id is None:
            # Nowhere to look, so no investigation and no run to record.
            return
        reason = await self.quiet_period_refusal(ticket.agent_id)
        if reason is not None:
            logger.info("ticket %s: no triage: %s", ticket.id, reason)
            skipped = await self.store.start_run(
                steward_id=spec.id,
                spec_hash=spec.spec_hash,
                trigger=TICKET_CREATED_TRIGGER,
                mode=mode,
                subject=f"ticket:{ticket.id}",
                host_id=ticket.agent_id,
                ticket_id=ticket.id,
            )
            await self.store.finish_run(skipped.id, status="skipped", error=reason)
            return
        run, admitted = await self._admit(
            spec,
            mode,
            trigger=TICKET_CREATED_TRIGGER,
            subject=f"ticket:{ticket.id}",
            host_id=ticket.agent_id,
            ticket_id=ticket.id,
            counted=False,
        )
        if not admitted:
            return
        meter = UsageMeter()
        status, error, verdict = "failed", INTERRUPTED_ERROR, None
        try:
            verdict = await self.triage.investigate(
                ticket, run_id=run.id, usage=meter, resolve=mode == "act"
            )
            status, error = "completed", None
        except Exception as exc:  # noqa: BLE001 - recorded on the run, never raised
            logger.exception("triage run %s on ticket %s failed", run.id, ticket.id)
            status, error = "failed", _clip(exc) or type(exc).__name__
        finally:
            await self._close(
                run.id, status=status, verdict=verdict, usage=meter.to_dict(), error=error
            )

    async def quiet_period_refusal(
        self, host_id: str | None, *, server_changes: bool = False
    ) -> str | None:
        """Why nothing may start on ``host_id`` now because a steward changed it, or ``None``.

        ADR-0071 rule 6: an event on a machine a steward changed within
        :data:`STEWARD_CHANGE_QUIET_PERIOD` — or is changing now, in a run
        still in flight (:func:`counts_as_change`) — starts nothing, whatever
        raised it. Shared by every event that starts a steward, so they refuse
        alike. With ``server_changes`` (an alert-started run) a server-only
        run's change counts on every machine, and on none (``host_id``
        ``None``): a change to the server's own records, such as a dropped
        suppression rule, can raise an alert on any machine.
        """

        hours = int(STEWARD_CHANGE_QUIET_PERIOD.total_seconds() // 3600)
        if host_id is not None:
            changed_by = await self._recent_steward_change(host_id)
            if changed_by is not None:
                return (
                    f"steward {changed_by.steward_id} (run {changed_by.id}) changed this "
                    f"machine within the last {hours} hours or is changing it now; a "
                    "steward's effect never starts another steward"
                )
        if server_changes:
            changed_by = await self._recent_steward_change(None)
            if changed_by is not None:
                return (
                    f"steward {changed_by.steward_id} (run {changed_by.id}) changed the "
                    f"server within the last {hours} hours or is changing it now, which can "
                    "raise an alert on any machine; a steward's effect never starts another "
                    "steward"
                )
        return None

    async def _recent_steward_change(self, host_id: str | None) -> StewardRun | None:
        """The newest run that changed ``host_id`` within the quiet period, or ``None``.

        ``host_id`` ``None`` reads the server-only runs. Which runs count is
        :func:`counts_as_change`.
        """

        since = self._now() - STEWARD_CHANGE_QUIET_PERIOD
        for run in await self.store.runs_on_host_since(host_id, since.isoformat()):
            if counts_as_change(run):
                return run
        return None

    # -- any other steward -----------------------------------------------------

    async def run_generic(
        self,
        spec: StewardSpec,
        *,
        host_id: str | None,
        trigger: str,
        brief: str,
        client: Any = None,
        model: str | None = None,
        executor: ToolExecutor | None = None,
        authorizer: Authorizer | None = None,
        act_until: datetime | None = None,
        preview: bool = False,
        on_admitted: Callable[[StewardRun], None] | None = None,
        initiated_by: str | None = None,
        shadow_only: bool = False,
        expected_hash: str | None = None,
    ) -> StewardRun | None:
        """Run ``spec`` once on ``host_id`` through the steward gate.

        ``expected_hash`` is the effective hash a person was shown when they
        started this run (a command run); a run whose effective hash at start
        differs is refused before anything is recorded, with
        :class:`HashMismatch`, so it never acts on what they did not see.

        ``shadow_only`` runs it in ``shadow`` whatever mode is stored, as a
        preview does — recorded so, no change can start in it and it spends no
        authorization — but it is not a preview: its verdict opens a ticket
        for a finding as any run's does. ``initiated_by`` names the person who
        asked for the run, on its row; ``None`` for a run the server started.

        ``preview`` makes this an on-demand preview: the run is **always**
        ``shadow``, whatever mode is stored (a change is refused at the gate and
        kept as a recommendation, a verdict opens no ticket), it never spends an
        authorization, and no change can start in it even if the steward is in
        ``act``. Its ``trigger`` is
        ``preview:<who asked>`` (:data:`PREVIEW_TRIGGER_PREFIX`). Triage cannot be
        previewed (it runs on a ticket's own surface), and a steward whose stored
        mode is ``off`` starts nothing -- a preview is no way round a
        superuser's switch. ``on_admitted`` is called with the run row as soon
        as it is opened (also when a cap skipped it), before the model is called.
        A preview a cap refuses leaves no row: it raises :class:`PreviewRefused`.

        ``act_until`` is the moment from which no change of this run may start
        (the end of the maintenance window a scheduled run belongs to): from
        then on the gate refuses every change and records it as a
        recommendation, as it does once the steward leaves ``act``.

        ``client``, ``model`` and ``executor`` default to what :meth:`configure`
        set; :class:`RuntimeError` if neither supplies one. ``authorizer``
        defaults to spending an attempt of a standing authorization bound to
        this run's effective hash (:meth:`AuthorizationStore.consume`); without
        an authorization store no ``normal_change`` runs.

        The run's parameters are read once at start, resolved into the spec its
        gate enforces (:func:`~kenny_server.stewards.spec.resolve`), and stored on
        the run row with the effective hash they make; a parameter edit during
        the run does not reach its constraints, it ends its ``act``. Its
        evidence-fed constraints are resolved once too, after the mode is read,
        through the registered providers (:meth:`resolve_evidence`), stored on
        the run row and told to the model in the brief; nothing the run sees
        later moves them.

        Returns the finished run row — ``completed``, ``failed`` or a cap's
        ``skipped`` — or ``None`` when the run was never started because the
        global switch, the AI master switch or the steward's mode is off, or when
        its row could not be closed (logged). Never raises for a failure inside
        the run; it is recorded as ``failed``.

        Raises :class:`ValueError` (or :class:`~kenny_server.stewards.spec.SpecError`)
        for a spec that must not run here: an invalid one, one that runs on a
        ticket's surface (that is :meth:`on_ticket_created`'s), one whose id is
        not in the catalog, or one that differs from its catalog entry's
        ``spec_hash``. Only a catalog spec runs: its mode is a superuser's
        choice, and a spec from anywhere else has no such choice behind it.
        """

        check_dispatchable(validate(spec))
        if runs_on_ticket(spec):
            raise ValueError(f"steward {spec.id} runs on a ticket's own surface, not run_generic")
        known = self.catalog.get(spec.id)
        if known is None:
            raise ValueError(f"steward {spec.id} is not in the catalog")
        if known.spec_hash != spec.spec_hash:
            raise ValueError(f"steward {spec.id}: spec differs from the catalog entry")
        if preview and not trigger.startswith(PREVIEW_TRIGGER_PREFIX):
            raise ValueError(f"a preview run's trigger starts with {PREVIEW_TRIGGER_PREFIX!r}")
        executor = executor if executor is not None else self._executor
        if executor is None:
            raise RuntimeError("run_generic needs an executor; call configure() first")
        if model is None:
            model = self._model() if callable(self._model) else self._model
        if not model:
            raise RuntimeError("run_generic needs a model; call configure() first")
        if client is None and self._client_factory is None:
            raise RuntimeError("run_generic needs a client; call configure() first")
        if not self.enabled() or not self._ai_ready():
            return None
        # The configuration before the mode: if it changes in between, the mode
        # read sees the newer hash and ``still_acting`` catches the difference.
        params, _triggers, run_hash = await self._config(spec)
        if expected_hash is not None and expected_hash != run_hash:
            raise HashMismatch(
                f"steward {spec.id} changed since it was shown (effective hash "
                f"{expected_hash[:12]} is now {run_hash[:12]}); review it again"
            )
        mode = await self._mode_for(spec)
        if mode == "off":
            return None
        if preview or shadow_only:
            # Forced, after the ``off`` check above: a preview observes what the
            # steward would do, it is never a way to make it do anything.
            mode = "shadow"
        # Evidence after the mode, so a steward that is off computes nothing.
        evidence = await self.resolve_evidence(spec) if evidence_names(spec) else None
        run, admitted = await self._admit(
            spec,
            mode,
            trigger=trigger,
            subject=f"host:{host_id}" if host_id else None,
            host_id=host_id,
            counted=True,
            params=params,
            run_hash=run_hash,
            evidence=evidence,
            record_refusal=not preview,
            initiated_by=initiated_by,
        )
        if on_admitted is not None:
            on_admitted(run)
        if not admitted:
            return run

        meter = UsageMeter()
        session: StewardSession | None = None
        status, error, summary, verdict = "failed", INTERRUPTED_ERROR, None, None
        change_results: list[dict[str, Any]] = []
        finished: StewardRun | None = None

        async def still_acting() -> bool:
            if preview or shadow_only:
                return False
            if act_until is not None and self._now() >= act_until:
                return False
            if not self.enabled() or await self._mode_for(spec) != "act":
                return False
            # Bound to what the steward was when this run started.
            return await self.live_hash(spec.id) == run_hash

        async def consume(
            _session: StewardSession, tool: str, _args: dict[str, Any], target: str | None
        ) -> Authorization | None:
            store = self.authorizations
            if (
                preview
                or shadow_only
                or store is None
                or await self.live_hash(spec.id) != run_hash
            ):
                return None
            return await store.consume(
                steward_id=spec.id,
                effective_hash=run_hash,
                tool=tool,
                host_id=target,
                run_id=run.id,
                now=self._now(),
            )

        async def record_action(entry: dict[str, Any]) -> None:
            # On the row before the call is forwarded (ADR-0071 rule 6).
            await self.store.append_action(run.id, _redacted([entry])[0])

        recording = _RecordingExecutor(executor)
        try:
            if client is None:
                assert self._client_factory is not None  # checked above
                client = self._client_factory()
            session = StewardSession(
                id=run.id,
                spec=with_evidence(resolve(spec, params), evidence or {}),
                mode=mode,
                agent_id=host_id,
                preview=preview,
            )
            session.usage = meter
            if evidence is not None:
                brief = f"{brief}\n\n{_evidence_brief(spec, evidence)}"
            session.messages.append({"role": "user", "content": brief})
            policy = StewardPolicy(
                session,
                authorizer=authorizer if authorizer is not None else consume,
                still_acting=still_acting,
                on_action=record_action,
            )
            async for event in drive_events(
                session,
                recording,
                client=client,
                model=model,
                policy=policy,
                max_iterations=spec.budget.max_iterations,
            ):
                kind = event.get("type")
                if kind == "tool_result":
                    tool = str(event.get("tool") or "")
                    if tool == spec.verdict_tool:
                        said = (event.get("args") or {}).get("verdict")
                        if event.get("ok") and isinstance(said, str) and said.strip():
                            verdict = said.strip()[:200]
                    elif classify(tool) != READ_ONLY:
                        outcome = dict(event)
                        summary = recording.take(tool, event.get("args"))
                        if summary is not None:
                            outcome["result"] = summary
                        change_results.append(outcome)
                elif kind == "done":
                    summary = _clip(event.get("assistant_text"))
            status, error = "completed", None
        except Exception as exc:  # noqa: BLE001 - recorded on the run, never raised
            logger.exception("steward %s run %s failed", spec.id, run.id)
            status, error = "failed", _clip(exc) or type(exc).__name__
        finally:
            self._release()
            actions = session.actions if session is not None else []
            recommendations = session.recommendations if session is not None else []
            finished = await self._close(
                run.id,
                status=status,
                verdict=verdict,
                summary=summary,
                usage=meter.to_dict(),
                error=error,
                actions=_redacted(_with_outcomes(actions, change_results)),
                recommendations=_redacted(recommendations),
            )
        return finished

    # -- an on-demand preview ------------------------------------------------

    async def preview_refusal(self, spec: StewardSpec, host_id: str | None) -> PreviewRefused | None:
        """Why a preview of ``spec`` on ``host_id`` must not start now, or ``None``.

        The one list of a preview's refusals, asked by the preview route, by the
        copilot before it offers a preview card, and again by
        :meth:`start_preview` itself, so the copilot never offers a run the
        route would refuse. In order: a steward that runs on a ticket's own
        surface (triage), one not in the catalog, one whose mode is ``off``,
        the global switch off, no AI to run it, a preview of this steward on this
        host already running, and :data:`PREVIEWS_PER_DAY_SETTING` previews of
        this steward started in the last 24 hours. Whether ``host_id`` is a valid
        target is the caller's (:func:`~kenny_server.stewards.policy.run_target_problem`);
        the global caps are checked at admission.
        """

        why = surface_refusal(spec)
        if why is not None:
            return PreviewRefused(why, code="not_previewable")
        if spec.id not in self.catalog:
            return PreviewRefused(f"unknown steward {spec.id}", code="unknown_steward")
        if await self._mode_for(spec) == "off":
            return PreviewRefused(
                f"{spec.id} is off; a preview does not override that", code="steward_off"
            )
        if not self.enabled():
            return PreviewRefused("stewards are switched off", conflict=True, code="stewards_off")
        if not self._ai_ready():
            return PreviewRefused(
                "AI is switched off or not configured", conflict=True, code="ai_unavailable"
            )
        if (spec.id, host_id) in self._preview_targets:
            where = f" on {host_id}" if host_id else ""
            return PreviewRefused(
                f"a preview of {spec.id}{where} is already running; wait for it to finish",
                conflict=True,
                code="preview_running",
            )
        limit = max(1, int(self.settings.get(PREVIEWS_PER_DAY_SETTING)))
        since = (self._now() - timedelta(hours=24)).isoformat()
        started = await self.store.count_runs_since(
            spec.id, prefix=PREVIEW_TRIGGER_PREFIX, since_iso=since
        )
        if started >= limit:
            return PreviewRefused(
                f"{spec.id} was previewed {started} times in the last 24 hours; the limit is {limit}",
                conflict=True,
                code="preview_limit",
            )
        return None

    async def start_preview(
        self, spec: StewardSpec, *, host_id: str | None, requested_by: str
    ) -> StewardRun | None:
        """Start one preview run of ``spec`` in the background; return its row.

        Returns as soon as the run is *admitted* -- the row exists and is
        ``running`` -- and never waits for the model. The run itself continues
        in a task this runner holds until it ends; it records its own outcome
        (``failed`` included) and nothing observes it but the run row. ``None``
        when nothing could start (the global switch or the AI master switch
        went off, or the mode turned ``off``, after the checks here).

        Raises :class:`PreviewRefused` for a request that must not run now:
        anything :meth:`preview_refusal` names, or a global cap at admission --
        a refused preview leaves no run row. ``host_id`` is the caller's to
        validate (:func:`~kenny_server.stewards.policy.run_target_problem`).
        """

        key = (spec.id, host_id)
        async with self._preview_admission:
            refused = await self.preview_refusal(spec, host_id)
            if refused is not None:
                raise refused
            trigger = f"{PREVIEW_TRIGGER_PREFIX}{requested_by.strip()[:_MAX_PREVIEWER] or 'unknown'}"
            loop = asyncio.get_running_loop()
            admitted: asyncio.Future[StewardRun | None] = loop.create_future()

            def on_admitted(run: StewardRun) -> None:
                if not admitted.done():
                    admitted.set_result(run)

            async def go() -> None:
                try:
                    await self.run_generic(
                        spec,
                        host_id=host_id,
                        trigger=trigger,
                        brief=_preview_brief(spec, host_id, await self.get_params(spec.id)),
                        preview=True,
                        on_admitted=on_admitted,
                        initiated_by=requested_by.strip()[:_MAX_PREVIEWER] or None,
                    )
                except PreviewRefused as exc:
                    if not admitted.done():
                        admitted.set_exception(exc)
                except Exception as exc:  # noqa: BLE001 - reported to the caller if still waiting
                    logger.exception("steward %s preview failed to start", spec.id)
                    if not admitted.done():
                        admitted.set_exception(exc)
                finally:
                    self._preview_targets.discard(key)
                    if not admitted.done():
                        admitted.set_result(None)

            # Held from here to the run's end, so a second request for the same
            # target is refused however soon it comes.
            self._preview_targets.add(key)
            task = asyncio.create_task(go(), name=f"steward-preview-{spec.id}")
            self._previews.add(task)
            task.add_done_callback(self._previews.discard)
            # The lock is held until the row exists: the next request's daily
            # count sees it.
            return await admitted

    async def wait_previews(self) -> None:
        """Wait for every preview run still going (shutdown and tests).

        Waits on the tasks not yet done: a finished task stays in the set until
        its done-callback runs, and gathering only finished tasks need not yield
        to the loop that would run it.
        """

        while pending := [task for task in self._previews if not task.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def cancel_previews(self) -> None:
        """Stop every preview run still going (server shutdown).

        Each closes its own row as ``failed`` / interrupted on the way out, the
        same as a scheduled run cut off by a restart.
        """

        tasks = list(self._previews)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def ai_ready(self) -> bool:
        """Whether a model may be called now (the AI master switch and an available client)."""

        return self._ai_ready()

    def _ai_ready(self) -> bool:
        if self.ai_access is None:
            return True
        return bool(self.ai_access.master_on() and self.ai_access.available())

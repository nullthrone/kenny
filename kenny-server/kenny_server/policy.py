"""Shared deny-rule catalog loader + best-effort server-side mirror.

ADR-0020: the agent embeds ``docs/policy/deny_rules.json`` at build time and is
the authoritative enforcement point; the server loads the same file for an
optional best-effort mirror that refuses an obviously dangerous call *before*
forwarding it (earlier feedback for Claude/the operator).

The mirror is UX, not the boundary. If the catalog file is absent at runtime the
engine has no built-in rules, logs a single warning, and degrades to enforcing
only the operator's append-only rules (a no-op when there are none). The loader
NEVER raises on a missing/unreadable catalog.

ADR-0064 adds a second decision axis on the same frame: the fleet's **shell
execution mode**. Deny rules say what must never run; the mode says what may run
at all. Deny is evaluated first and always, so an ``allow`` entry can never lift a
rule from the shared catalog.

The mirror's status is not uniform. For an agent that enforces the mode itself it
stays UX. For an agent predating v0.18 — which parses the ``policy`` frame and
ignores ``shell`` — it is the only place the mode is enforced.

Dependency-free: stdlib ``re``/``json``/``pathlib``/``logging`` only. Patterns
use the portable regex subset common to Rust ``regex`` and Python ``re``.

The verdicts here are pinned against the agent's by the shared decision vectors in
``docs/fixtures/vectors/policy_decisions.json`` — the test that fails when the two
hand-written mirrors drift.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from .protocol import PolicyRule

logger = logging.getLogger("kenny.policy")

#: Valid values of ``policy.shell.mode`` (ADR-0064), loosest first.
SHELL_MODES = ("unrestricted", "allowlist", "off")

#: The rule-group name each shell tool's command string is matched against. The two
#: shell tools are the only ones the execution mode governs.
_SHELL_TOOL_GROUP = {"powershell_exec": "powershell", "shell_exec": "posix"}

#: The arg key carrying each shell tool's command string.
_SHELL_TOOL_ARG = {"powershell_exec": "script", "shell_exec": "command"}

# Tools whose args are concatenated and matched against ``self_protection``.
_SELF_PROTECTION_TOOLS = {
    "winget_install",
    "winget_uninstall",
    "winget_update",
    "net_dns_flush",
    "net_adapter_reset",
}


def _catalog_candidates() -> list[Path]:
    """Ordered candidate paths for the shared deny-rule catalog."""

    env = os.environ.get("KENNY_POLICY_CATALOG", "").strip()
    if env:
        return [Path(env)]
    return [
        # dev/repo: kenny-server/kenny_server/policy.py -> repo root / docs / policy
        Path(__file__).resolve().parents[2] / "docs" / "policy" / "deny_rules.json",
        # container: see Dockerfile COPY docs/policy/ -> /app/docs/policy/
        Path("/app/docs/policy/deny_rules.json"),
    ]


def _resolve_catalog_path() -> Path | None:
    for candidate in _catalog_candidates():
        if candidate.is_file():
            return candidate
    return None


def _load_catalog_rules() -> list[dict[str, Any]]:
    """Load the built-in rules from the shared catalog. Never raises."""

    path = _resolve_catalog_path()
    if path is None:
        logger.warning(
            "policy catalog not found; server mirror disabled (operator rules only). "
            "Set KENNY_POLICY_CATALOG or ship docs/policy/deny_rules.json."
        )
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:  # unreadable / malformed JSON
        logger.warning("policy catalog %s could not be read (%s); mirror disabled", path, exc)
        return []
    rules = data.get("rules", [])
    if not isinstance(rules, list):
        logger.warning("policy catalog %s has no usable rules; mirror disabled", path)
        return []
    return rules


def _compile_group(rules: list[Any]) -> dict[str, list[tuple[str, re.Pattern[str]]]]:
    """Group rules by ``applies_to`` -> list of (reason, compiled pattern).

    Accepts dicts or :class:`PolicyRule`. A rule that fails to compile (or is
    malformed) is skipped and logged, never fatal.
    """

    groups: dict[str, list[tuple[str, re.Pattern[str]]]] = {
        "powershell": [],
        "posix": [],
        "self_protection": [],
        "path": [],
    }
    for raw in rules:
        if isinstance(raw, PolicyRule):
            applies_to, pattern, reason, rid = (
                raw.applies_to,
                raw.pattern,
                raw.reason,
                raw.id,
            )
        elif isinstance(raw, dict):
            applies_to = raw.get("applies_to")
            pattern = raw.get("pattern")
            reason = raw.get("reason", "")
            rid = raw.get("id", "<unknown>")
        else:
            continue
        if applies_to not in groups or not isinstance(pattern, str):
            logger.warning("policy rule %s skipped: bad applies_to/pattern", rid)
            continue
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            logger.warning("policy rule %s skipped: pattern failed to compile (%s)", rid, exc)
            continue
        groups[applies_to].append((reason, compiled))
    return groups


def _iter_strings(value: Any) -> list[str]:
    """Recursively collect all string values from a nested args structure."""

    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_iter_strings(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(_iter_strings(v))
    return out


class PolicyEngine:
    """Compiles the shared built-in rules plus operator rules; mirrors the agent.

    Built-ins come from the shared catalog; operator rules are settable and
    re-compiled on change. Built-ins are never weakened by operator rules — the
    two sets are simply additive (a hit in either blocks).
    """

    def __init__(self) -> None:
        self._builtin_raw: list[dict[str, Any]] = _load_catalog_rules()
        self._builtin = _compile_group(self._builtin_raw)
        self._operator = _compile_group([])
        # ADR-0064. Default ``unrestricted``: the mirror must not refuse calls the
        # fleet has not been configured to refuse.
        self._shell_mode: str = "unrestricted"
        self._shell_allow: dict[str, list[tuple[str, re.Pattern[str]]]] = _compile_group([])

    # -- rule management ---------------------------------------------------

    def builtin_rules(self) -> list[dict[str, Any]]:
        """Expose the catalog's built-in rules (for operator visibility)."""

        return [dict(r) for r in self._builtin_raw]

    def set_operator_rules(self, rules: list[dict[str, Any] | PolicyRule]) -> None:
        """Replace + recompile the operator rule set."""

        normalised: list[dict[str, Any]] = []
        for r in rules:
            if isinstance(r, PolicyRule):
                normalised.append(r.model_dump())
            else:
                normalised.append(dict(r))
        self._operator = _compile_group(normalised)

    def set_shell_policy(
        self, mode: str, allow: list[dict[str, Any] | PolicyRule] | None = None
    ) -> None:
        """Replace the fleet shell execution mode and its allow rules (ADR-0064).

        An unrecognised mode is refused rather than guessed at: it falls back to
        ``unrestricted`` and logs, because silently picking a stricter mode would
        take down fleet administration over a typo, and silently picking a looser
        one would be a policy the operator never set.
        """

        if mode not in SHELL_MODES:
            logger.warning("unknown shell policy mode %r; falling back to unrestricted", mode)
            mode = "unrestricted"
        normalised: list[dict[str, Any]] = []
        for r in allow or []:
            entry = r.model_dump() if isinstance(r, PolicyRule) else dict(r)
            # An allow entry for a surface with no command string means nothing.
            if entry.get("applies_to") not in ("powershell", "posix"):
                logger.warning(
                    "shell allow rule %s ignored: applies_to must be powershell or posix",
                    entry.get("id", "<unknown>"),
                )
                continue
            normalised.append(entry)
        self._shell_mode = mode
        self._shell_allow = _compile_group(normalised)

    # POSSIBLY DEAD: no webui route or MCP tool reads this back today — only
    # tests call it directly. Kept as the one getter paired with
    # `set_shell_policy` for whoever needs to expose the current mode later.
    def shell_mode(self) -> str:
        """The current fleet shell execution mode."""

        return self._shell_mode

    # -- mirror ------------------------------------------------------------

    def _match(self, group: str, text: str) -> tuple[str, str] | None:
        """Match built-in then operator rules of ``group`` against ``text``."""

        for source in (self._builtin, self._operator):
            for reason, pattern in source.get(group, []):
                if pattern.search(text):
                    return ("blocked", reason)
        return None

    def check(self, tool: str, args: dict[str, Any]) -> tuple[str, str] | None:
        """Return ``("blocked", reason)`` on a hit, else ``None``.

        Mirrors the agent's matching exactly per ADR-0020.
        """

        if tool in _SHELL_TOOL_GROUP:
            group = _SHELL_TOOL_GROUP[tool]
            raw = args.get(_SHELL_TOOL_ARG[tool], "")
            # A missing or non-string arg is an empty haystack. Under ``unrestricted``
            # that lets the call through to the handler, whose job it is to answer
            # ``bad_args``; under ``allowlist`` an empty string matches no entry, so
            # the gate below refuses it.
            text = raw if isinstance(raw, str) else ""
            return (
                self._match(group, text)
                or self._match("self_protection", text)
                or self._shell_gate(group, text)
            )

        if tool in _SELF_PROTECTION_TOOLS:
            # These tools forward their string args into a shell/exec on the agent
            # (e.g. net_adapter_reset interpolates the adapter name into a PowerShell
            # command), so scan the args against the full powershell catalog as well
            # as self_protection — mirroring the agent guard — so a destructive command
            # cannot be smuggled through an argument
            # (kenny-sec:handlers/net-adapter-reset-powershell-injection).
            blob = " ".join(_iter_strings(args))
            return self._match("powershell", blob) or self._match("self_protection", blob)

        if tool in ("fs_read", "fs_list", "fs_search"):
            key = "root" if tool == "fs_search" else "path"
            raw = args.get(key, "")
            path = raw if isinstance(raw, str) else ""
            normalised = path.replace("/", "\\")
            return self._match("path", normalised)

        if tool == "agent_update":
            # Host allowlist is agent-only; not mirrored (server cannot self-determine host).
            return None

        return None

    def _shell_gate(self, group: str, text: str) -> tuple[str, str] | None:
        """Apply the fleet shell execution mode (ADR-0064).

        Runs *after* the deny groups, so deny always outranks allow. ``allow``
        entries match the whole trimmed command: a substring match would turn every
        entry into a prefix onto which anything can be appended.
        """

        if self._shell_mode == "unrestricted":
            return None
        if self._shell_mode == "off":
            return ("blocked", "shell execution is off by fleet policy")
        candidate = text.strip()
        for _reason, pattern in self._shell_allow.get(group, []):
            if pattern.fullmatch(candidate):
                return None
        return ("blocked", "not permitted by the fleet shell allowlist")

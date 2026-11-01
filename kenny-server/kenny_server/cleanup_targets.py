"""The agent's disk-cleanup target catalog, as the server needs to know it.

``fs_cleanup_scan`` and ``fs_cleanup`` free disk space from a fixed catalog the
agent owns (``docs/protocol.md``, "Cleanup targets"). The server never sends a
path, only a target name, and it must know the names to refuse a typo in a
steward's ``targets`` allowlist when a person edits it, not when a run silently
admits nothing.

:data:`TARGETS` is a Python literal equal to
``docs/fixtures/vectors/fs_cleanup_targets.json`` (the server ships without the
repository docs, so it cannot read the file at runtime);
``tests/test_cleanup_targets.py`` asserts the two are equal, and the agent tests
the same file against its own catalog, so a name accepted here is a name the
agent serves.

Stdlib only: :mod:`kenny_server.stewards.spec` and the runner import this.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

__all__ = [
    "ALL_TARGETS",
    "SUGGESTED_TARGETS",
    "TARGETS",
    "brief_line",
    "targets_for",
    "unknown_targets",
]

#: OS -> the target names its agent serves, in catalog order.
TARGETS: Mapping[str, tuple[str, ...]] = {
    "windows": ("temp_system", "temp_users", "windows_update_cache", "recycle_bin", "logs_old"),
    "linux": ("tmp", "package_cache", "journal", "logs_old"),
}

#: Every target name on any OS.
ALL_TARGETS: frozenset[str] = frozenset(name for names in TARGETS.values() for name in names)

#: What a household can reasonably start the disk-hygiene allowlist with. A
#: suggestion only: the ``targets`` parameter starts empty, and an empty
#: allowlist admits nothing. ``recycle_bin`` (irreversible, and it may hold what
#: somebody means to restore) and ``logs_old`` (what a person debugging wants) are
#: left out on purpose; adding either is a choice.
SUGGESTED_TARGETS: tuple[str, ...] = (
    "temp_system",
    "temp_users",
    "windows_update_cache",
    "tmp",
    "package_cache",
    "journal",
)


def targets_for(os_name: str | None) -> tuple[str, ...]:
    """The target names a host running ``os_name`` serves; none for an unknown OS."""

    return TARGETS.get((os_name or "").lower(), ())


def unknown_targets(values: Iterable[str]) -> list[str]:
    """The names among ``values`` that no OS's catalog has, sorted."""

    return sorted({v for v in values if v not in ALL_TARGETS})


def brief_line(allowed: Iterable[str], os_name: str | None) -> str:
    """The brief line naming the allowed targets this host serves, or saying there are none.

    ``allowed`` is the ``targets`` parameter; a name another OS serves is left
    out, so the model is not handed a target the agent would refuse as
    ``unsupported``.
    """

    served = set(targets_for(os_name))
    usable = [t for t in sorted({a for a in allowed if isinstance(a, str)}) if t in served]
    if usable:
        return "Cleanup targets you may scan and clean on this machine: " + ", ".join(usable) + "."
    return "No cleanup target is allowed on this machine; scan and clean nothing."

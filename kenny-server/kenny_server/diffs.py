"""Server-side snapshot diffing (ADR-0030).

Compares two telemetry snapshots and reports what appeared, disappeared or
changed in inventory-style sections (autostart entries, services, USB devices,
installed software, ...). Collectors stay stateless (ADR-0007), so noticing
"something is new since yesterday" is exclusively the server's job: this module
is pure and I/O-free (like ``fleet_stats``); the alert loop and the dashboard
feed it snapshots from the store.

The per-section table below is data-driven like ``health_rules.RULES``: adding
a diffable section is one entry naming the list field, the identity key and
which fields count as a change. Sections absent from either snapshot are
skipped entirely, so a collector rollout never floods the diff with "added"
rows for a whole section.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SectionSpec:
    """How to diff one section's inventory list."""

    list_field: str
    key_fields: tuple[str, ...]
    changed_fields: tuple[str, ...] = ()
    detail_fields: tuple[str, ...] = ()


# Section name -> spec. Sections added later (e.g. Phase-3 collectors) are
# picked up automatically once their entry lands here.
SPECS: dict[str, SectionSpec] = {
    "autostart": SectionSpec(
        list_field="entries",
        key_fields=("name", "location"),
        changed_fields=("command",),
        detail_fields=("command",),
    ),
    "services": SectionSpec(
        list_field="services",
        key_fields=("name",),
        changed_fields=("start",),
        detail_fields=("display", "start"),
    ),
    "peripherals": SectionSpec(
        list_field="devices",
        key_fields=("name",),
        detail_fields=("class",),
    ),
    "installed_software": SectionSpec(
        list_field="apps",
        key_fields=("name",),
        changed_fields=("version",),
        detail_fields=("publisher", "version"),
    ),
    "browser_extensions": SectionSpec(
        list_field="extensions",
        key_fields=("browser", "id"),
        detail_fields=("name",),
    ),
    "listening_ports": SectionSpec(
        list_field="ports",
        key_fields=("proto", "port", "process"),
        detail_fields=("address",),
    ),
    "scheduled_tasks": SectionSpec(
        list_field="tasks",
        key_fields=("path", "name"),
        changed_fields=("action",),
        detail_fields=("action", "run_as"),
    ),
    "local_accounts": SectionSpec(
        list_field="accounts",
        key_fields=("name",),
        changed_fields=("is_admin", "enabled"),
        detail_fields=("is_admin", "enabled"),
    ),
}


def _key(item: dict[str, Any], spec: SectionSpec) -> str:
    return " | ".join(str(item.get(f, "")) for f in spec.key_fields)


def _describe(item: dict[str, Any], spec: SectionSpec) -> str:
    parts = [
        f"{f}={item.get(f)}" for f in spec.detail_fields if item.get(f) not in (None, "")
    ]
    return ", ".join(parts)


def diff_section(
    name: str, old_payload: dict[str, Any], new_payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """Diff one section; returns ``{section, kind, key, detail}`` rows."""

    spec = SPECS.get(name)
    if spec is None:
        return []
    old_items = old_payload.get(spec.list_field)
    new_items = new_payload.get(spec.list_field)
    if not isinstance(old_items, list) or not isinstance(new_items, list):
        return []
    old_map = {_key(i, spec): i for i in old_items if isinstance(i, dict)}
    new_map = {_key(i, spec): i for i in new_items if isinstance(i, dict)}

    changes: list[dict[str, Any]] = []
    for key in sorted(new_map.keys() - old_map.keys()):
        changes.append(
            {"section": name, "kind": "added", "key": key, "detail": _describe(new_map[key], spec)}
        )
    for key in sorted(old_map.keys() - new_map.keys()):
        changes.append(
            {"section": name, "kind": "removed", "key": key, "detail": _describe(old_map[key], spec)}
        )
    for key in sorted(new_map.keys() & old_map.keys()):
        old_item, new_item = old_map[key], new_map[key]
        deltas = [
            f"{f}: {old_item.get(f)} -> {new_item.get(f)}"
            for f in spec.changed_fields
            if old_item.get(f) != new_item.get(f)
        ]
        if deltas:
            changes.append(
                {"section": name, "kind": "changed", "key": key, "detail": "; ".join(deltas)}
            )
    return changes


def diff_snapshots(
    old_snapshot: dict[str, Any], new_snapshot: dict[str, Any]
) -> list[dict[str, Any]]:
    """Diff every section present in *both* snapshots that has a spec."""

    changes: list[dict[str, Any]] = []
    for name in SPECS:
        old_payload = old_snapshot.get(name)
        new_payload = new_snapshot.get(name)
        if isinstance(old_payload, dict) and isinstance(new_payload, dict):
            changes.extend(diff_section(name, old_payload, new_payload))
    return changes

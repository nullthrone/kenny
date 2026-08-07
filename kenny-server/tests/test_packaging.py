"""The shipped image must carry every extra the running server can need.

``discord.py`` is an optional extra on purpose: a source install without it
still runs, because the gateway reports itself unavailable and the Discord
surface simply stays off. That degradation is a feature for a source install and
a trap for the image — the container is kenny's deployment shape (ADR-0010), and
an operator who configures a bot token there has no pip invocation to add the
extra at. Shipping the image without it made the surface unreachable however it
was configured, and the graceful degradation turned that into one log line
instead of an error.

The defect was a coupling between ``pyproject.toml`` and the ``Dockerfile`` that
nothing checked. This module checks it, and fails closed: a new extra counts as
runtime-required unless it is listed here as build- or docs-only.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _ROOT / "pyproject.toml"
_DOCKERFILE = _ROOT / "Dockerfile"

#: Extras that deliberately do NOT belong in the runtime image. Everything else
#: is treated as runtime-required, so forgetting to classify a new extra breaks
#: the build rather than silently shipping without it.
_NON_RUNTIME_EXTRAS = frozenset({"dev", "docs", "screenshots"})


def _declared_extras() -> frozenset[str]:
    data = tomllib.loads(_PYPROJECT.read_text())
    return frozenset(data["project"].get("optional-dependencies", {}))


def _extras_installed_by_the_image() -> frozenset[str]:
    """Extras named in the Dockerfile's ``pip install`` of the local project."""

    text = _DOCKERFILE.read_text()
    found: set[str] = set()
    # Matches `pip install ... ".[a,b]"` / `.[a]` / `'.[a]'`, ignoring flags.
    for match in re.finditer(r"pip install[^\n]*?[\"']?\.\[([^\]]+)\][\"']?", text):
        found.update(part.strip() for part in match.group(1).split(","))
    return frozenset(found)


def test_the_image_installs_every_runtime_extra() -> None:
    required = _declared_extras() - _NON_RUNTIME_EXTRAS
    installed = _extras_installed_by_the_image()
    missing = required - installed
    assert not missing, (
        f"Dockerfile does not install runtime extra(s) {sorted(missing)}. "
        "The published image would start, accept the configuration, and leave "
        "the feature silently unreachable."
    )


def test_every_extra_is_classified() -> None:
    """A new extra must be sorted into runtime or non-runtime deliberately."""

    unknown = _NON_RUNTIME_EXTRAS - _declared_extras()
    assert not unknown, (
        f"{sorted(unknown)} is listed as non-runtime but no longer exists in "
        "pyproject.toml; drop it here so the guard keeps meaning something."
    )


def test_the_image_does_not_ship_development_extras() -> None:
    """Test and docs tooling has no business in the runtime image."""

    shipped = _extras_installed_by_the_image() & _NON_RUNTIME_EXTRAS
    assert not shipped, f"Dockerfile installs development extra(s) {sorted(shipped)}"

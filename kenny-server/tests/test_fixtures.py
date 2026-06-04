"""Round-trip every golden fixture through protocol models.

CI / ``/contract-check`` greps for a test with "fixtures" in the name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kenny_server.protocol import dump_frame, parse_frame

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "docs" / "fixtures"
FIXTURE_FILES = sorted(FIXTURES_DIR.glob("*.json"))


def test_fixtures_dir_present() -> None:
    assert FIXTURE_FILES, f"no fixtures found in {FIXTURES_DIR}"


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_fixtures_round_trip(path: Path) -> None:
    original = json.loads(path.read_text())
    model = parse_frame(original)
    dumped = dump_frame(model)
    assert dumped == original, f"{path.name} did not round-trip:\n{dumped}\n!=\n{original}"

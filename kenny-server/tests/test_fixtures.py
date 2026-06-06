"""Round-trip every golden fixture through protocol models.

CI / ``/contract-check`` greps for a test with "fixtures" in the name.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from kenny_server.keystore import build_transcript
from kenny_server.protocol import dump_frame, parse_frame

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "docs" / "fixtures"
FIXTURE_FILES = sorted(FIXTURES_DIR.glob("*.json"))
VECTORS_FILE = FIXTURES_DIR / "vectors" / "mutual_auth.json"


def test_fixtures_dir_present() -> None:
    assert FIXTURE_FILES, f"no fixtures found in {FIXTURES_DIR}"


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_fixtures_round_trip(path: Path) -> None:
    original = json.loads(path.read_text())
    model = parse_frame(original)
    dumped = dump_frame(model)
    assert dumped == original, f"{path.name} did not round-trip:\n{dumped}\n!=\n{original}"


def test_mutual_auth_vectors_byte_exact() -> None:
    """Guard byte-exactness of the transcript + signatures against the Rust side.

    Rebuild the transcript from the golden vector inputs, assert it matches the
    recorded ``transcript_hex``, then assert the server signature verifies under
    the server public key and the agent signature under the agent public key.
    """

    v = json.loads(VECTORS_FILE.read_text())
    transcript = build_transcript(
        v["agent_id"],
        base64.b64decode(v["client_nonce_b64"]),
        base64.b64decode(v["server_nonce_b64"]),
    )
    assert transcript.hex() == v["transcript_hex"]

    server_pub = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(v["server_public_key_b64"])
    )
    agent_pub = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(v["agent_public_key_b64"])
    )
    # .verify raises on mismatch; absence of an exception is the assertion.
    server_pub.verify(base64.b64decode(v["server_sig_b64"]), transcript)
    agent_pub.verify(base64.b64decode(v["agent_sig_b64"]), transcript)

"""The register-frame version comparison must be numeric, not lexicographic.

``"0.10" >= "0.8"`` is False as a string comparison; an agent at protocol 0.10
must still select the v0.8 signature handshake.
"""

from __future__ import annotations

from kenny_server.protocol import Register, RegisterMeta
from kenny_server.tunnel import _parse_version, _signature_path


def _register(protocol: str | None, nonce: str | None = "n" * 32) -> Register:
    return Register(
        agent_id="example-pc",
        protocol=protocol,
        client_nonce=nonce,
        meta=RegisterMeta(hostname="example-pc", os="windows", version="1.0.0"),
    )


def test_parse_version_orders_numerically() -> None:
    assert _parse_version("0.10") > _parse_version("0.9") > _parse_version("0.8")
    assert _parse_version("garbage") == (0,)


def test_signature_path_selected_for_0_10() -> None:
    assert _signature_path(_register("0.10")) is True
    assert _signature_path(_register("0.9")) is True
    assert _signature_path(_register("0.8")) is True


def test_signature_path_not_selected_below_0_8_or_without_nonce() -> None:
    assert _signature_path(_register("0.7")) is False
    assert _signature_path(_register(None)) is False
    assert _signature_path(_register("0.10", nonce=None)) is False

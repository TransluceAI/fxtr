"""Unit tests for :class:`TransportConnectionServer`.

Pure-Python tests of the token-table API: register, consume, revoke.
The token table doesn't drive any side-channel protocol itself; the
container handshake / eryx callback paths are tested at the runner
integration level.
"""

from __future__ import annotations

from typing import Any

import pytest
from fxtr_sandboxing.transport_server import BadTransportTokenError, TransportConnectionServer


def _noop_handler(_stream: Any) -> None:
    """Handler that ignores the stream (used when we only test consumption)."""


def test_register_and_consume_round_trip() -> None:
    server = TransportConnectionServer()
    server.register_token("tok-a", _noop_handler)
    handler = server.consume_token("tok-a")
    assert handler is _noop_handler


def test_consume_missing_token_raises() -> None:
    server = TransportConnectionServer()
    with pytest.raises(BadTransportTokenError):
        server.consume_token("never-minted")


def test_consume_is_single_use() -> None:
    server = TransportConnectionServer()
    server.register_token("tok-b", _noop_handler)
    server.consume_token("tok-b")
    with pytest.raises(BadTransportTokenError):
        server.consume_token("tok-b")


def test_revoke_removes_token() -> None:
    server = TransportConnectionServer()
    server.register_token("tok-c", _noop_handler)
    server.revoke_token("tok-c")
    with pytest.raises(BadTransportTokenError):
        server.consume_token("tok-c")


def test_revoke_idempotent_on_missing() -> None:
    server = TransportConnectionServer()
    # No exception when revoking something that was never registered, or
    # was already consumed.
    server.revoke_token("never-minted")
    server.register_token("tok-d", _noop_handler)
    server.consume_token("tok-d")
    server.revoke_token("tok-d")  # already consumed — no-op


def test_register_collision_raises() -> None:
    server = TransportConnectionServer()
    server.register_token("tok-e", _noop_handler)
    with pytest.raises(ValueError):
        server.register_token("tok-e", _noop_handler)

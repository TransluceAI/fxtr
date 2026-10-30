"""Codec unit tests for :class:`fxtr_sandbox_types.BindingConnectRequest`.

The request is the standard connect shape bound workflow-service
implementations receive (see ``binding_request.py``'s module doc). It
crosses service boundaries via the typed-service codec, so this pins
the mashumaro-level behaviors that codec relies on: the bare-recursive
``JSONValue`` alias resolves, and its union member order round-trips
every JSON shape faithfully (the regression this guards: a dict decoded
through a ``Sequence`` arm comes back as its key list). The full
wire-transit test (dialect strategies for the ``FxtrID`` key and the
``ServiceRef`` handles over a real RPC pair) lives with the consumer,
in ``projects/fxtr_experiment/tests/test_task_service.py``.
"""

from __future__ import annotations

import dataclasses

import mashumaro.codecs.basic as basic_codec
import pytest
from fxtr2.data.entity_core import FxtrID
from fxtr_sandbox_types.binding_request import BindingConnectRequest, JSONValue


@dataclasses.dataclass(frozen=True)
class _ConfigOnly:
    """A JSONValue-only twin: the request's key/handles fields need the
    typed-service dialect's strategies (exercised in the consumer's wire
    test); this pins the plain-mashumaro half."""

    config: JSONValue = None


_ENCODER = basic_codec.BasicEncoder(_ConfigOnly)
_DECODER = basic_codec.BasicDecoder(_ConfigOnly)


@pytest.mark.parametrize(
    "config",
    [
        None,
        True,
        False,
        3,
        2.5,
        "s",
        [],
        {},
        {"a": [1, "x", None, {"b": True}], "c": {}},
        [1, [2, 3], {"k": "v"}],
        ["a", "b"],
    ],
)
def test_json_value_round_trips(config: JSONValue) -> None:
    value = _ConfigOnly(config=config)
    assert _DECODER.decode(_ENCODER.encode(value)) == value


def test_request_defaults_are_dormant() -> None:
    """The one-arg constructor is the shape ``BindingRegistry.spawn``
    assembles today: key only, no config, no handles."""
    key = FxtrID.from_tag_and_payload("d", b"\x00" * 8)
    request = BindingConnectRequest(key=key)
    assert request.key == key
    assert request.config is None
    assert dict(request.handles) == {}

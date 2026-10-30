"""Integration tests for the fxtr HTTP server wrapper."""

from __future__ import annotations

import dataclasses

import anyio
import httpx
import pytest

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.experimental.http.server import serve_until, serving_context


@dataclasses.dataclass(frozen=True)
class SimpleEntity(DataclassEntity, fxtr_type=FxtrID.builtin("test_srv_simple")):
    name: str


def _track_builtins(scope: FxtrScope) -> None:
    scope.track_builtin(FxtrID.builtin("test_srv_simple"))


class TestServingContext:
    @pytest.mark.anyio
    async def test_serving_context_starts_and_stops(self):
        """serving_context starts a server and stops it on exit."""
        async with FxtrScope(name="test_server") as scope:
            _track_builtins(scope)
            ref = scope.store(SimpleEntity(name="test"))
            pins = {"test": ref.entity_id}

            async with serving_context(scope, pins, port=18923):
                async with httpx.AsyncClient(base_url="http://127.0.0.1:18923") as client:
                    resp = await client.get("/pins")
                    assert resp.status_code == 200
                    assert "test" in resp.json()

            # Server should be stopped now — connection should fail
            async with httpx.AsyncClient(base_url="http://127.0.0.1:18923") as client:
                with pytest.raises(httpx.ConnectError):
                    await client.get("/pins")

    @pytest.mark.anyio
    async def test_serve_until_stops_on_event(self):
        """serve_until stops when the event is set."""
        async with FxtrScope(name="test_server") as scope:
            _track_builtins(scope)
            ref = scope.store(SimpleEntity(name="test"))
            pins = {"test": ref.entity_id}

            done = anyio.Event()

            async with anyio.create_task_group() as tg:
                tg.start_soon(serve_until, scope, pins, done, 18924)
                await anyio.sleep(0.2)

                async with httpx.AsyncClient(base_url="http://127.0.0.1:18924") as client:
                    resp = await client.get("/pins")
                    assert resp.status_code == 200

                done.set()

"""Tests for the stateless REST service endpoint (``service_rest_endpoint``)."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.experimental.http.service_codec import CallbackAddress
from fxtr2.experimental.http.service_rest_endpoint import rest_service_route

_TYPE_ID = FxtrID.builtin("test_rest_service_type")


@dataclasses.dataclass(frozen=True)
class _Thing(DataclassEntity, fxtr_type=_TYPE_ID):
    label: str


async def _echo(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
    return args[0]


async def _combine(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
    return f"{args[0]}-{kwargs.get('suffix', 'default')}"


async def _fail(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
    raise RuntimeError(args[0])


async def _with_callback(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
    # Callback args are inert over http; the impl just confirms it
    # decoded to an opaque address and ignores it.
    assert isinstance(args[0], CallbackAddress)
    return "ignored-callback"


@pytest.fixture
async def rest_ctx():
    async with FxtrScope(name="test_rest_service") as scope:
        scope.track_builtin(_TYPE_ID)
        thing = scope.store(_Thing(label="hello"))
        thing_id = thing.entity_id

        async def _get_thing(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
            return thing_id

        async def _fxtr_type(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
            return _TYPE_ID

        get_impl = {"echo": _echo, "combine": _combine, "$fxtr.type": _fxtr_type}
        post_impl = {
            "get_thing": _get_thing,
            "fail": _fail,
            "with_callback": _with_callback,
        }
        routes = [
            rest_service_route("/svc/echo", get_impl, scope=scope, method="GET"),
            rest_service_route("/svc/post", post_impl, scope=scope, method="POST"),
        ]
        app = Starlette(routes=routes)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, thing_id


def _payload(args: list | None = None, kwargs: dict | None = None) -> str:
    return json.dumps({"args": args or [], "kwargs": kwargs or {}})


class TestGet:
    @pytest.mark.anyio
    async def test_echo_primitive(self, rest_ctx):
        client, _ = rest_ctx
        resp = await client.get("/svc/echo", params={"method": "echo", "payload": _payload([42])})
        assert resp.status_code == 200
        assert resp.json() == 42

    @pytest.mark.anyio
    async def test_echo_entity_round_trips(self, rest_ctx):
        client, thing_id = rest_ctx
        arg = ["entity", thing_id.id_string()]
        resp = await client.get("/svc/echo", params={"method": "echo", "payload": _payload([arg])})
        assert resp.status_code == 200
        assert resp.json() == ["entity", thing_id.id_string()]

    @pytest.mark.anyio
    async def test_kwargs(self, rest_ctx):
        client, _ = rest_ctx
        resp = await client.get(
            "/svc/echo",
            params={"method": "combine", "payload": _payload(["a"], {"suffix": "b"})},
        )
        assert resp.json() == "a-b"

    @pytest.mark.anyio
    async def test_fxtr_type_returns_entity_tag(self, rest_ctx):
        client, _ = rest_ctx
        resp = await client.get("/svc/echo", params={"method": "$fxtr.type"})
        assert resp.status_code == 200
        assert resp.json() == ["entity", _TYPE_ID.id_string()]

    @pytest.mark.anyio
    async def test_missing_method_is_400(self, rest_ctx):
        client, _ = rest_ctx
        resp = await client.get("/svc/echo")
        assert resp.status_code == 400
        assert "method" in resp.json()["message"]

    @pytest.mark.anyio
    async def test_unknown_method_is_400(self, rest_ctx):
        client, _ = rest_ctx
        resp = await client.get("/svc/echo", params={"method": "nope"})
        assert resp.status_code == 400
        assert "unknown method" in resp.json()["message"]

    @pytest.mark.anyio
    async def test_bad_payload_json_is_400(self, rest_ctx):
        client, _ = rest_ctx
        resp = await client.get("/svc/echo", params={"method": "echo", "payload": "{not json"})
        assert resp.status_code == 400


class TestPost:
    @pytest.mark.anyio
    async def test_returns_entity(self, rest_ctx):
        client, thing_id = rest_ctx
        resp = await client.post("/svc/post", json={"method": "get_thing"})
        assert resp.status_code == 200
        assert resp.json() == ["entity", thing_id.id_string()]

    @pytest.mark.anyio
    async def test_impl_failure_is_500_with_message(self, rest_ctx):
        client, _ = rest_ctx
        resp = await client.post("/svc/post", json={"method": "fail", "args": ["boom"]})
        assert resp.status_code == 500
        assert resp.json()["message"] == "boom"

    @pytest.mark.anyio
    async def test_callback_arg_is_inert(self, rest_ctx):
        client, _ = rest_ctx
        resp = await client.post(
            "/svc/post",
            json={"method": "with_callback", "args": [["callback", "inbox-1"]]},
        )
        assert resp.status_code == 200
        assert resp.json() == "ignored-callback"

    @pytest.mark.anyio
    async def test_non_object_body_is_400(self, rest_ctx):
        client, _ = rest_ctx
        resp = await client.post("/svc/post", json=[1, 2, 3])
        assert resp.status_code == 400

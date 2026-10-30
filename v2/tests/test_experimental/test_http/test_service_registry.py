"""Tests for the stateless service registry + discovery (``service_registry``)."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route, WebSocketRoute

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.experimental.http.service_registry import ServiceRegistry

_TYPE_ID = FxtrID.builtin("test_registry_service_type")


@dataclasses.dataclass(frozen=True)
class _Thing(DataclassEntity, fxtr_type=_TYPE_ID):
    label: str


async def _echo(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
    return args[0]


async def _ping(args: list[Any], kwargs: dict[str, Any], ctx: Any) -> Any:
    return "pong"


@pytest.fixture
async def reg_ctx():
    async with FxtrScope(name="test_registry") as scope:
        scope.track_builtin(_TYPE_ID)
        thing = scope.store(_Thing(label="x"))
        reg = ServiceRegistry(scope)
        reg.add_entity("demo/root", thing.entity_id)
        reg.add_rest_service(
            path="/svc/echo",
            impl={"echo": _echo},
            method="GET",
            type_id=_TYPE_ID,
            discovery_name="echo",
        )
        reg.add_ws_service(
            "scheduler", ws_path="/ws", impl={"ping": _ping}, discovery_name="scheduler"
        )
        yield scope, reg, thing.entity_id


@pytest.mark.anyio
async def test_discovery_shape(reg_ctx):
    _scope, reg, thing_id = reg_ctx
    disco = reg.discovery()
    assert disco["entities"] == {"demo/root": thing_id.id_string()}
    assert disco["services"]["echo"] == {
        "kind": "http",
        "url": "/svc/echo",
        "method": "GET",
        "service_type_id": _TYPE_ID.id_string(),
    }
    sched = disco["services"]["scheduler"]
    assert sched["kind"] == "ws"
    assert sched["url"] == "/ws"  # relative ws path unchanged
    assert "service_type_id" not in sched  # none supplied → omitted


@pytest.mark.anyio
async def test_discovery_entities_p2_when_provenanced():
    """A layered entity in a provenanced registry lists under the [data, prov] form;
    bare entities and a non-provenanced registry stay bare strings."""
    async with FxtrScope(name="test_registry_prov") as scope:
        scope.track_builtin(_TYPE_ID)
        thing = scope.store(_Thing(label="x"))
        # Discovery emits no precomp — it just reads `_provenance_parent` for the
        # encoding, which is identical for any provenance-node tag. So a
        # trivial u-wrap is sufficient here; genuine n-node provenance (and the
        # prov-node precomp) is exercised in test_asgi_app's /pin tests.
        u_node = scope.create_u_node(thing.entity_id)
        layered = scope.embed_as_provenance_parent(u_node)

        # provenanced=False (default): layered entity still listed bare.
        bare_reg = ServiceRegistry(scope)
        bare_reg.add_entity("demo/root", layered)
        assert bare_reg.discovery()["entities"] == {"demo/root": thing.entity_id.id_string()}

        # provenanced=True: layered → [data, prov], bare → string.
        reg = ServiceRegistry(scope, provenanced=True)
        reg.add_entity("demo/root", layered)
        reg.add_entity("demo/bare", thing.entity_id)
        assert reg.discovery()["entities"] == {
            "demo/root": [thing.entity_id.id_string(), u_node.id_string()],
            "demo/bare": thing.entity_id.id_string(),
        }


@pytest.mark.anyio
async def test_routes_emitted(reg_ctx):
    _scope, reg, _thing_id = reg_ctx
    routes = reg.routes()
    rest = [r for r in routes if isinstance(r, Route)]
    ws = [r for r in routes if isinstance(r, WebSocketRoute)]
    assert len(rest) == 1
    assert len(ws) == 1
    assert rest[0].path == "/svc/echo"
    assert ws[0].path == "/ws"


@pytest.mark.anyio
async def test_duplicate_names_rejected(reg_ctx):
    scope, _reg, _thing_id = reg_ctx
    reg = ServiceRegistry(scope)
    reg.add_rest_service(path="/a", impl={"m": _echo}, discovery_name="dup")
    # A ws service reusing that discovery name collides...
    with pytest.raises(ValueError):
        reg.add_ws_service("sched", ws_path="/ws", impl={"m": _ping}, discovery_name="dup")
    # ...but its routing name lives in a separate, per-ws_path namespace,
    # so a duplicate routing name on the same path is its own error.
    reg.add_ws_service("sched", ws_path="/ws", impl={"m": _ping})
    with pytest.raises(ValueError):
        reg.add_ws_service("sched", ws_path="/ws", impl={"m": _ping})
    reg.add_entity("e", _TYPE_ID)
    with pytest.raises(ValueError):
        reg.add_entity("e", _TYPE_ID)


@pytest.mark.anyio
async def test_absolute_base_url():
    async with FxtrScope(name="test_registry_abs") as scope:
        reg = ServiceRegistry(scope, base_url="http://host:8000")
        reg.add_rest_service(
            path="/svc/echo", impl={"echo": _echo}, method="GET", discovery_name="echo"
        )
        reg.add_ws_service("sched", ws_path="/ws", impl={"ping": _ping}, discovery_name="sched")
        disco = reg.discovery()
        assert disco["services"]["echo"]["url"] == "http://host:8000/svc/echo"
        assert disco["services"]["sched"]["url"] == "ws://host:8000/ws"


@pytest.mark.anyio
async def test_undiscoverable_services_omitted_from_discovery():
    """A service registered without a discovery name is mounted but
    omitted from discovery; its returned descriptor is how an impl hands
    it out. A ws service still needs an explicit routing name."""
    async with FxtrScope(name="test_registry_undiscoverable") as scope:
        reg = ServiceRegistry(scope)
        rest_desc = reg.add_rest_service(path="/svc/hidden", impl={"echo": _echo}, method="GET")
        ws_desc = reg.add_ws_service("hidden_ws", ws_path="/ws", impl={"ping": _ping})

        # Returned descriptors are usable references to the (mounted) services.
        assert rest_desc.kind == "http"
        assert rest_desc.url == "/svc/hidden"
        assert rest_desc.method == "GET"
        assert ws_desc.kind == "ws"
        assert ws_desc.url == "/ws"
        assert ws_desc.name == "hidden_ws"  # the explicit routing key

        # Neither appears in discovery.
        assert reg.discovery()["services"] == {}

        # But both routes are mounted.
        routes = reg.routes()
        assert [r.path for r in routes if isinstance(r, Route)] == ["/svc/hidden"]
        assert [r.path for r in routes if isinstance(r, WebSocketRoute)] == ["/ws"]


@pytest.mark.anyio
async def test_ws_routing_and_discovery_names_independent():
    """A ws service's routing name and discovery name may differ; the
    descriptor carries the routing name, discovery lists it under the
    discovery name."""
    async with FxtrScope(name="test_registry_ws_names") as scope:
        reg = ServiceRegistry(scope)
        desc = reg.add_ws_service(
            "wire_key", ws_path="/ws", impl={"ping": _ping}, discovery_name="Scheduler"
        )
        assert desc.name == "wire_key"
        listed = reg.discovery()["services"]
        assert set(listed) == {"Scheduler"}
        assert listed["Scheduler"]["name"] == "wire_key"


@pytest.mark.anyio
async def test_unnamed_rest_route_serves():
    """A non-discoverable REST route still serves at its path."""
    async with FxtrScope(name="test_registry_unnamed_serve") as scope:
        reg = ServiceRegistry(scope)
        reg.add_rest_service(path="/svc/hidden", impl={"echo": _echo}, method="GET")
        app = Starlette(routes=reg.routes())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/svc/hidden", params={"method": "echo", "payload": json.dumps({"args": [7]})}
            )
            assert resp.status_code == 200
            assert resp.json() == 7


@pytest.mark.anyio
async def test_named_service_returns_descriptor():
    """Named registrations also return a descriptor (matching discovery)."""
    async with FxtrScope(name="test_registry_named_desc") as scope:
        reg = ServiceRegistry(scope)
        desc = reg.add_rest_service(
            path="/svc/echo",
            impl={"echo": _echo},
            method="GET",
            type_id=_TYPE_ID,
            discovery_name="echo",
        )
        assert desc.to_wire() == reg.discovery()["services"]["echo"]


@pytest.mark.anyio
async def test_discovery_endpoint_and_rest_invoke(reg_ctx):
    _scope, reg, thing_id = reg_ctx
    app = Starlette(routes=[*reg.routes(), Route("/discovery", reg.discovery_handler)])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/discovery")
        assert resp.status_code == 200
        body = resp.json()
        assert body["entities"]["demo/root"] == thing_id.id_string()
        assert body["services"]["echo"]["url"] == "/svc/echo"

        # The mounted REST route actually serves.
        resp = await client.get(
            "/svc/echo", params={"method": "echo", "payload": json.dumps({"args": [99]})}
        )
        assert resp.status_code == 200
        assert resp.json() == 99

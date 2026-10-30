"""Tests for WorkspaceNamespace (FxtrNamespace over HTTP).

Uses a minimal in-process workspace server built on Starlette, so these
tests are self-contained and don't require the hub server package.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import cbor2
import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from fxtr2.conversion.converters.mashumaro_entities import Child, DataclassEntity
from fxtr2.data.abstract_arena import FxtrEntityFlatData
from fxtr2.data.arena_util import compute_precomp_closure
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.remote.remote_client import PushResult
from fxtr2.data.remote.transfer_format import decode_entity_stream, encode_entity_stream
from fxtr2.experimental.http.client import HttpFxtrReadNamespace
from fxtr2.experimental.http.workspace_namespace import WorkspaceNamespace

# =============================================================================
# Test Entity Definitions
# =============================================================================


@dataclasses.dataclass(frozen=True)
class SimpleModel(DataclassEntity, fxtr_type=FxtrID.builtin("test_ws_simple")):
    name: str
    value: int


@dataclasses.dataclass(frozen=True)
class ModelWithChild(DataclassEntity, fxtr_type=FxtrID.builtin("test_ws_with_child")):
    label: str
    child: Child[SimpleModel]


def _track_builtins(scope: FxtrScope) -> None:
    scope.track_builtin(FxtrID.builtin("test_ws_simple"))
    scope.track_builtin(FxtrID.builtin("test_ws_with_child"))


# =============================================================================
# Minimal in-process workspace server
# =============================================================================


class _InProcessWorkspaceServer:
    """A minimal workspace + entity transfer server for testing.

    Stores entities in an FxtrScope and pins in a dict. Exposes both the
    workspace API and the entity transfer endpoints that the client needs.
    """

    def __init__(self, scope: FxtrScope):
        self._scope = scope
        self._pins: dict[str, FxtrID] = {}
        # Entity flat data stored by ID (simulating entity push/pull)
        self._entity_flat: dict[FxtrID, FxtrEntityFlatData] = {}

    def create_app(self) -> Starlette:
        server = self

        async def ws_set_pin(request: Request) -> Response:
            key = request.path_params["key"]
            body = await request.json()
            entity_id = FxtrID.from_id_string(body["entity_id"])
            overwrite = body.get("overwrite", False)

            # Verify entity exists
            arena = await server._scope._deep_fetch(entity_id)
            if arena is None:
                return Response("Entity not found", status_code=400)
            arena.close()

            if key in server._pins and not overwrite:
                return Response(
                    json.dumps({"detail": f"Pin '{key}' already exists"}),
                    status_code=409,
                    media_type="application/json",
                )
            server._pins[key] = entity_id
            return Response(json.dumps({"status": "ok"}), media_type="application/json")

        async def ws_delete_pin(request: Request) -> Response:
            key = request.path_params["key"]
            if key not in server._pins:
                return Response("Not found", status_code=404)
            del server._pins[key]
            return Response(json.dumps({"status": "deleted"}), media_type="application/json")

        async def ws_get_pin(request: Request) -> Response:
            name = request.path_params["name"]
            fxtr_id = server._pins.get(name)
            if fxtr_id is None:
                return Response("Not found", status_code=404)

            precomp: dict[str, dict[str, str]] = {}
            arena = await server._scope._deep_fetch(fxtr_id)
            if arena is not None:
                try:
                    precomp = compute_precomp_closure(
                        arena, server._scope.clone_authorization(), [fxtr_id]
                    )
                finally:
                    arena.close()

            result: dict[str, Any] = {"id": fxtr_id.id_string()}
            if precomp:
                result["precomp"] = precomp
            return Response(cbor2.dumps(result), media_type="application/cbor")

        async def ws_list_pins(request: Request) -> Response:
            prefix = request.query_params.get("prefix", "")
            filtered = {k: v.id_string() for k, v in server._pins.items() if k.startswith(prefix)}
            return Response(json.dumps(filtered), media_type="application/json")

        async def ws_data(request: Request) -> Response:
            id_string = request.path_params["id_string"]
            try:
                fxtr_id = FxtrID.from_id_string(id_string)
            except (ValueError, KeyError):
                return Response("Invalid ID", status_code=400)

            arena = await server._scope._deep_fetch(fxtr_id)
            if arena is None:
                return Response("Not found", status_code=404)

            try:
                ml = server._scope.clone_authorization()
                flat = await arena.load_flat(ml, fxtr_id)
                dep_ids = [dep_id for dep_id, _kind in flat.deps]
                dep_ids.append(fxtr_id)
                precomp = compute_precomp_closure(
                    arena, server._scope.clone_authorization(), dep_ids
                )
            finally:
                arena.close()

            result: dict[str, Any] = {}
            if flat.entity_bytes is not None:
                result["bytes"] = flat.entity_bytes
            if precomp:
                result["precomp"] = precomp
            return Response(cbor2.dumps(result), media_type="application/cbor")

        # Entity transfer endpoints (simplified, no batching)
        async def entities_check(request: Request) -> Response:
            body = await request.json()
            ids = [FxtrID.from_id_string(s) for s in body["entity_ids"]]
            known = []
            missing = []
            for eid in ids:
                arena = await server._scope._deep_fetch(eid)
                if arena is not None:
                    arena.close()
                    known.append(eid.id_string())
                else:
                    missing.append(eid.id_string())
            return Response(
                json.dumps({"known": known, "missing": missing}),
                media_type="application/json",
            )

        async def entities_push(request: Request) -> Response:
            raw = await request.body()
            entities = decode_entity_stream(raw)
            stored = 0
            for entity in entities:
                tag = entity.entity_id.get_tag()
                if tag in (".", "!"):
                    continue
                if entity.entity_bytes is None:
                    continue

                # Store via store_cbor which parses the CBOR and tracks
                # the entity and its children in the scope.
                from fxtr2.data.entity_core import fxtr_cbor_loads

                parsed = fxtr_cbor_loads(entity.entity_bytes)
                server._scope.store_cbor(parsed)
                stored += 1

            return Response(
                json.dumps({"entities_stored": stored}),
                media_type="application/json",
            )

        async def entities_pull(request: Request) -> Response:
            body = await request.json()
            ids = [FxtrID.from_id_string(s) for s in body["entity_ids"]]
            result: list[FxtrEntityFlatData] = []
            for eid in ids:
                arena = await server._scope._deep_fetch(eid)
                if arena is not None:
                    try:
                        result.append(
                            await arena.load_flat(server._scope.clone_authorization(), eid)
                        )
                    finally:
                        arena.close()
            encoded = encode_entity_stream(result)
            return Response(content=encoded, media_type="application/octet-stream")

        # Workspace routes use /workspace/ prefix, entity transfer at root
        routes = [
            # Workspace API
            Route("/workspace/pin/{key:path}", ws_set_pin, methods=["PUT"]),
            Route("/workspace/pin/{key:path}", ws_delete_pin, methods=["DELETE"]),
            Route("/workspace/pin/{name:path}", ws_get_pin),
            Route("/workspace/pins", ws_list_pins),
            Route("/workspace/data/{id_string:path}", ws_data),
            # Entity transfer
            Route("/entities/check", entities_check, methods=["POST"]),
            Route("/entities/push", entities_push, methods=["POST"]),
            Route("/entities/pull", entities_pull, methods=["POST"]),
        ]

        return Starlette(routes=routes)


class _FakeHubClient:
    """Minimal HubClient-compatible object for WorkspaceNamespace.

    Satisfies RemoteClient protocol and exposes the attributes
    WorkspaceNamespace reads.
    """

    def __init__(self, base_url: str, repo: str, transport: httpx.AsyncBaseTransport):
        self._base_url = base_url
        self._repo = repo
        self._api_key = "fake-token"
        self._transport = transport

    def _repo_url(self, path: str) -> str:
        return f"{self._base_url}/{path}"

    async def negotiate(self, entity_ids: list[FxtrID]) -> tuple[list[FxtrID], list[FxtrID]]:
        payload = {"entity_ids": [eid.id_string() for eid in entity_ids]}
        async with httpx.AsyncClient(timeout=120, transport=self._transport) as client:
            resp = await client.post(self._repo_url("entities/check"), json=payload)
            resp.raise_for_status()
            data = resp.json()
            known = [FxtrID.from_id_string(s) for s in data["known"]]
            missing = [FxtrID.from_id_string(s) for s in data["missing"]]
            return known, missing

    async def push_entities(self, entities: list[FxtrEntityFlatData]) -> PushResult:
        entity_bytes = encode_entity_stream(entities)
        async with httpx.AsyncClient(timeout=120, transport=self._transport) as client:
            resp = await client.post(
                self._repo_url("entities/push"),
                content=entity_bytes,
                headers={"Content-Type": "application/octet-stream"},
            )
            resp.raise_for_status()
            return PushResult(entities_stored=resp.json().get("entities_stored", 0))

    async def pull_entities(self, entity_ids: list[FxtrID]) -> list[FxtrEntityFlatData]:
        payload = {"entity_ids": [eid.id_string() for eid in entity_ids]}
        async with httpx.AsyncClient(timeout=120, transport=self._transport) as client:
            resp = await client.post(self._repo_url("entities/pull"), json=payload)
            resp.raise_for_status()
            return decode_entity_stream(resp.content)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def workspace_client():
    """Create an in-process workspace server + fake hub client."""
    async with FxtrScope(name="server") as server_scope:
        _track_builtins(server_scope)

        ws_server = _InProcessWorkspaceServer(server_scope)
        app = ws_server.create_app()
        transport = httpx.ASGITransport(app=app)

        # The workspace URL is base_url + /api/v1/repos/{repo}/workspace
        # but our simple server serves at /workspace/ directly.
        # We adjust the _FakeHubClient so WorkspaceNamespace builds the right URL.
        # WorkspaceNamespace calls: {_base_url}/api/v1/repos/{_repo}/workspace
        # We want it to hit: http://test/workspace
        # So set _base_url="" and _repo="" and override _build_workspace_url
        fake_client = _FakeHubClient(
            base_url="http://test",
            repo="owner/repo",
            transport=transport,
        )

        yield fake_client, transport


class _TestableWorkspaceNamespace(WorkspaceNamespace):
    """WorkspaceNamespace subclass that overrides URL building for tests."""

    def _build_workspace_url(self) -> str:
        return f"{self._hub_client._base_url}/workspace"


# =============================================================================
# Tests
# =============================================================================


class TestWorkspaceNamespacePinAndRetrieve:
    @pytest.mark.anyio
    async def test_pin_and_retrieve_simple_entity(self, workspace_client) -> None:
        fake_client, transport = workspace_client

        async with FxtrScope(name="outer") as outer_scope:
            _track_builtins(outer_scope)

            async with _TestableWorkspaceNamespace(fake_client) as ws:
                entity = SimpleModel(name="test", value=42)
                await ws.pin("my-model", entity)

                ref = await ws.retrieve("my-model", type=SimpleModel, copy_into_scope=True)
                loaded = await ref.load()
                assert loaded.name == "test"
                assert loaded.value == 42

    @pytest.mark.anyio
    async def test_pin_and_retrieve_entity_with_child(self, workspace_client) -> None:
        fake_client, transport = workspace_client

        async with FxtrScope(name="outer") as outer_scope:
            _track_builtins(outer_scope)

            async with _TestableWorkspaceNamespace(fake_client) as ws:
                child = SimpleModel(name="child", value=7)
                parent = ModelWithChild(label="parent", child=child)
                await ws.pin("experiment", parent)

                ref = await ws.retrieve("experiment", type=ModelWithChild, copy_into_scope=True)
                loaded_parent = await ref.load()
                assert loaded_parent.label == "parent"

                # Child is auto-loaded (Child[] is inline)
                assert loaded_parent.child.name == "child"
                assert loaded_parent.child.value == 7


class TestWorkspaceNamespaceListAndUnpin:
    @pytest.mark.anyio
    async def test_list_pins(self, workspace_client) -> None:
        fake_client, transport = workspace_client

        async with FxtrScope(name="outer") as outer_scope:
            _track_builtins(outer_scope)

            async with _TestableWorkspaceNamespace(fake_client) as ws:
                e1 = SimpleModel(name="a", value=1)
                e2 = SimpleModel(name="b", value=2)
                await ws.pin("models:encoder", e1)
                await ws.pin("models:decoder", e2)

                pins = await ws.list_pins()
                assert len(pins) == 2
                assert "models:encoder" in pins
                assert "models:decoder" in pins

                filtered = await ws.list_pins(prefix="models:enc")
                assert len(filtered) == 1
                assert "models:encoder" in filtered

    @pytest.mark.anyio
    async def test_unpin(self, workspace_client) -> None:
        fake_client, transport = workspace_client

        async with FxtrScope(name="outer") as outer_scope:
            _track_builtins(outer_scope)

            async with _TestableWorkspaceNamespace(fake_client) as ws:
                entity = SimpleModel(name="temp", value=0)
                await ws.pin("temp-pin", entity)

                pins = await ws.list_pins()
                assert "temp-pin" in pins

                await ws.unpin("temp-pin")

                pins = await ws.list_pins()
                assert "temp-pin" not in pins

    @pytest.mark.anyio
    async def test_unpin_missing_raises(self, workspace_client) -> None:
        fake_client, transport = workspace_client

        async with FxtrScope(name="outer") as outer_scope:
            _track_builtins(outer_scope)

            async with _TestableWorkspaceNamespace(fake_client) as ws:
                with pytest.raises(KeyError):
                    await ws.unpin("nonexistent")


class TestWorkspaceNamespaceOverwrite:
    @pytest.mark.anyio
    async def test_overwrite_false_raises(self, workspace_client) -> None:
        fake_client, transport = workspace_client

        async with FxtrScope(name="outer") as outer_scope:
            _track_builtins(outer_scope)

            async with _TestableWorkspaceNamespace(fake_client) as ws:
                e1 = SimpleModel(name="v1", value=1)
                e2 = SimpleModel(name="v2", value=2)
                await ws.pin("result", e1)

                with pytest.raises(KeyError):
                    await ws.pin("result", e2)

    @pytest.mark.anyio
    async def test_overwrite_true_succeeds(self, workspace_client) -> None:
        fake_client, transport = workspace_client

        async with FxtrScope(name="outer") as outer_scope:
            _track_builtins(outer_scope)

            async with _TestableWorkspaceNamespace(fake_client) as ws:
                e1 = SimpleModel(name="v1", value=1)
                e2 = SimpleModel(name="v2", value=2)
                await ws.pin("result", e1)
                await ws.pin("result", e2, overwrite=True)

                ref = await ws.retrieve("result", type=SimpleModel, copy_into_scope=True)
                loaded = await ref.load()
                assert loaded.name == "v2"
                assert loaded.value == 2


class TestHttpFxtrReadNamespaceWithWorkspace:
    """Verify that HttpFxtrReadNamespace can read from workspace endpoints.

    Pins entities through WorkspaceNamespace, then reads them back through
    HttpFxtrReadNamespace pointing at the same workspace URL. This confirms
    the response format compatibility claimed in the workspace module docstring.
    """

    @pytest.mark.anyio
    async def test_read_namespace_retrieves_workspace_pin(self, workspace_client) -> None:
        fake_client, transport = workspace_client

        async with FxtrScope(name="outer") as outer_scope:
            _track_builtins(outer_scope)

            # Write a pin through WorkspaceNamespace
            async with _TestableWorkspaceNamespace(fake_client) as ws:
                entity = SimpleModel(name="shared", value=99)
                await ws.pin("models:shared", entity)

            # Read it back through HttpFxtrReadNamespace
            http_client = httpx.AsyncClient(
                base_url=f"{fake_client._base_url}/workspace",
                transport=transport,
                timeout=120,
            )
            try:
                async with HttpFxtrReadNamespace(http_client) as read_ns:
                    ref = await read_ns.retrieve(
                        "models:shared", type=SimpleModel, copy_into_scope=True
                    )
                    loaded = await ref.load()
                    assert loaded.name == "shared"
                    assert loaded.value == 99
            finally:
                await http_client.aclose()

    @pytest.mark.anyio
    async def test_read_namespace_lists_workspace_pins(self, workspace_client) -> None:
        fake_client, transport = workspace_client

        async with FxtrScope(name="outer") as outer_scope:
            _track_builtins(outer_scope)

            async with _TestableWorkspaceNamespace(fake_client) as ws:
                await ws.pin("data:alpha", SimpleModel(name="a", value=1))
                await ws.pin("data:beta", SimpleModel(name="b", value=2))
                await ws.pin("models:gamma", SimpleModel(name="c", value=3))

            http_client = httpx.AsyncClient(
                base_url=f"{fake_client._base_url}/workspace",
                transport=transport,
                timeout=120,
            )
            try:
                async with HttpFxtrReadNamespace(http_client) as read_ns:
                    all_pins = await read_ns.list_pins()
                    assert len(all_pins) == 3
                    assert "data:alpha" in all_pins
                    assert "models:gamma" in all_pins

                    filtered = await read_ns.list_pins(prefix="data:")
                    assert len(filtered) == 2
                    assert "data:alpha" in filtered
                    assert "data:beta" in filtered
            finally:
                await http_client.aclose()

    @pytest.mark.anyio
    async def test_read_namespace_retrieves_entity_with_child(self, workspace_client) -> None:
        fake_client, transport = workspace_client

        async with FxtrScope(name="outer") as outer_scope:
            _track_builtins(outer_scope)

            async with _TestableWorkspaceNamespace(fake_client) as ws:
                child = SimpleModel(name="inner", value=7)
                parent = ModelWithChild(label="outer", child=child)
                await ws.pin("experiment", parent)

            http_client = httpx.AsyncClient(
                base_url=f"{fake_client._base_url}/workspace",
                transport=transport,
                timeout=120,
            )
            try:
                async with HttpFxtrReadNamespace(http_client) as read_ns:
                    ref = await read_ns.retrieve(
                        "experiment", type=ModelWithChild, copy_into_scope=True
                    )
                    loaded = await ref.load()
                    assert loaded.label == "outer"
                    assert loaded.child.name == "inner"
                    assert loaded.child.value == 7
            finally:
                await http_client.aclose()

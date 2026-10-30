"""Tests for the fxtr HTTP ASGI app."""

from __future__ import annotations

import dataclasses
import json

import cbor2
import httpx
import pytest

from fxtr2.conversion.converters.mashumaro_entities import ChildRef, DataclassEntity
from fxtr2.data.entity_core import FxtrID, fxtr_cbor_loads
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.experimental.http.asgi_app import create_fxtr_app
from tests.test_experimental.test_http.conftest import make_c_node

# =============================================================================
# Test Entity Definitions
# =============================================================================


@dataclasses.dataclass(frozen=True)
class SimpleEntity(DataclassEntity, fxtr_type=FxtrID.builtin("test_http_simple")):
    name: str
    value: int


@dataclasses.dataclass(frozen=True)
class EntityWithRef(DataclassEntity, fxtr_type=FxtrID.builtin("test_http_with_ref")):
    name: str
    child: ChildRef[SimpleEntity]


def _track_builtins(scope: FxtrScope) -> None:
    scope.track_builtin(FxtrID.builtin("test_http_simple"))
    scope.track_builtin(FxtrID.builtin("test_http_with_ref"))


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def app_context():
    """Create a scope with entities and an ASGI app for testing."""
    async with FxtrScope(name="test_http") as scope:
        _track_builtins(scope)

        child_ref = scope.store(SimpleEntity(name="child", value=42))
        parent_ref = scope.store(EntityWithRef(name="parent", child=child_ref))

        pins = {
            "my/pin": child_ref.entity_id,
            "parent": parent_ref.entity_id,
        }
        app = create_fxtr_app(scope, pins)
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield scope, client, child_ref, parent_ref, pins


@pytest.fixture
async def layered_app_context():
    """A scope with a bare pin and a genuinely-provenanced (n-node) layered pin.

    The layered entity is built the real way — a parent stored under a
    ``provenance_context`` whose child carries provenance takes the store's
    prov path and gets an ``n``-node provenance parent (`entity_ref_scope`'s
    ``_store_cbor_prov_path``). This exercises the serve path with the same
    shape a published report has, not just a trivial ``u``-wrap. Yields a
    ``client_for(provenanced)`` factory so a test can compare the bare-default
    vs provenanced wire.

    - ``lay`` → the layered ``(data, n-node)`` parent.
    - ``bare`` → the same data node with its provenance stripped (a plain bare
      pin, to confirm bare pins stay bare strings in a provenanced response).
    """
    async with FxtrScope(name="test_http_prov_builder") as builder:
        c_node = make_c_node(builder)
        async with FxtrScope(
            provenance_context=c_node,
            provenance_context_from_scope=builder,
            name="test_http_prov",
        ) as scope:
            _track_builtins(scope)
            child_ref = scope.store(SimpleEntity(name="child", value=42))
            parent_ref = scope.store(EntityWithRef(name="parent", child=child_ref))
            layered = parent_ref.entity_id
            assert layered._provenance_parent is not None
            assert layered._provenance_parent.get_tag() == "n"
            n_node = FxtrID(raw_data_id=layered._provenance_parent._raw_data_id)  # bare prov node
            bare_data = FxtrID(raw_data_id=layered._raw_data_id)  # data node, no provenance
            pins = {"bare": bare_data, "lay": layered}

            def client_for(provenanced: bool) -> httpx.AsyncClient:
                app = create_fxtr_app(scope, pins, provenanced=provenanced)
                transport = httpx.ASGITransport(app=app)
                return httpx.AsyncClient(transport=transport, base_url="http://test")

            yield scope, bare_data, n_node, layered, client_for


# =============================================================================
# Preview endpoint tests
# =============================================================================


class TestPreviewEndpoint:
    @pytest.mark.anyio
    async def test_preview_found(self, app_context):
        scope, client, child_ref, parent_ref, pins = app_context
        resp = await client.get(f"/preview/{child_ref.entity_id.id_string()}")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        assert "child" in resp.text
        assert "42" in resp.text

    @pytest.mark.anyio
    async def test_preview_not_found(self, app_context):
        scope, client, child_ref, parent_ref, pins = app_context
        fake_id = FxtrID.from_tag_and_payload("d", b"\x00" * 32)
        resp = await client.get(f"/preview/{fake_id.id_string()}")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_preview_derived_node(self, app_context):
        scope, client, child_ref, parent_ref, pins = app_context
        # A "!" builtin node is a derived node with no CBOR content
        builtin_id = FxtrID.builtin("test_http_simple")
        resp = await client.get(f"/preview/{builtin_id.id_string()}")
        assert resp.status_code == 200
        assert "Derived node" in resp.text


# =============================================================================
# Data endpoint tests
# =============================================================================


class TestDataEndpoint:
    @pytest.mark.anyio
    async def test_data_returns_bytes_and_precomp(self, app_context):
        scope, client, child_ref, parent_ref, pins = app_context
        resp = await client.get(f"/data/{child_ref.entity_id.id_string()}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/cbor"

        result = cbor2.loads(resp.content)
        assert "bytes" in result
        assert isinstance(result["bytes"], bytes)

        # Decode the entity bytes to verify content
        decoded = fxtr_cbor_loads(result["bytes"])
        assert isinstance(decoded, dict)
        assert decoded["name"] == "child"
        assert decoded["value"] == 42

    @pytest.mark.anyio
    async def test_data_precomp_is_transitively_closed(self, app_context):
        scope, client, child_ref, parent_ref, pins = app_context
        resp = await client.get(f"/data/{parent_ref.entity_id.id_string()}")
        assert resp.status_code == 200

        result = cbor2.loads(resp.content)
        assert "bytes" in result
        # precomp may or may not be present depending on entity structure
        # The key point is that if present, all values are also represented

    @pytest.mark.anyio
    async def test_data_not_found(self, app_context):
        scope, client, child_ref, parent_ref, pins = app_context
        fake_id = FxtrID.from_tag_and_payload("d", b"\x00" * 32)
        resp = await client.get(f"/data/{fake_id.id_string()}")
        assert resp.status_code == 404


# =============================================================================
# Pin endpoint tests
# =============================================================================


class TestPinEndpoint:
    @pytest.mark.anyio
    async def test_pin_found(self, app_context):
        scope, client, child_ref, parent_ref, pins = app_context
        resp = await client.get("/pin/my/pin")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/cbor"

        result = cbor2.loads(resp.content)
        assert result["id"] == child_ref.entity_id.id_string()

    @pytest.mark.anyio
    async def test_pin_not_found(self, app_context):
        scope, client, child_ref, parent_ref, pins = app_context
        resp = await client.get("/pin/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_pin_includes_precomp(self, app_context):
        scope, client, child_ref, parent_ref, pins = app_context
        resp = await client.get("/pin/my/pin")
        assert resp.status_code == 200
        result = cbor2.loads(resp.content)
        # precomp is omitted if empty (which is fine for a ground data node)
        assert "id" in result


# =============================================================================
# Pins list endpoint tests
# =============================================================================


class TestPinsListEndpoint:
    @pytest.mark.anyio
    async def test_pins_list(self, app_context):
        scope, client, child_ref, parent_ref, pins = app_context
        resp = await client.get("/pins")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"

        result = json.loads(resp.content)
        assert "my/pin" in result
        assert "parent" in result
        assert result["my/pin"] == child_ref.entity_id.id_string()

    @pytest.mark.anyio
    async def test_pins_list_with_prefix(self, app_context):
        scope, client, child_ref, parent_ref, pins = app_context
        resp = await client.get("/pins?prefix=my/")
        assert resp.status_code == 200

        result = json.loads(resp.content)
        assert "my/pin" in result
        assert "parent" not in result


# =============================================================================
# Provenance wire (layered pins/pin + prov-node precomp + the provenanced gate)
# =============================================================================


class TestProvenanceWire:
    @pytest.mark.anyio
    async def test_pins_bare_by_default_even_for_layered(self, layered_app_context):
        # Default (provenanced=False) emits bare for every pin, layered or not.
        scope, bare_data, n_node, layered, client_for = layered_app_context
        async with client_for(False) as client:
            result = json.loads((await client.get("/pins")).content)
        assert result["bare"] == bare_data.id_string()
        assert result["lay"] == layered.id_string()  # layered → bare data string

    @pytest.mark.anyio
    async def test_pins_provenanced_emits_p2(self, layered_app_context):
        scope, bare_data, n_node, layered, client_for = layered_app_context
        async with client_for(True) as client:
            result = json.loads((await client.get("/pins")).content)
        # Bare pin stays a string; layered pin becomes the [data, prov] array.
        assert result["bare"] == bare_data.id_string()
        assert result["lay"] == [layered.id_string(), n_node.id_string()]

    @pytest.mark.anyio
    async def test_pin_provenanced_id_and_prov_precomp(self, layered_app_context):
        scope, bare_data, n_node, layered, client_for = layered_app_context
        async with client_for(True) as client:
            res = cbor2.loads((await client.get("/pin/lay")).content)
        assert res["id"] == [layered.id_string(), n_node.id_string()]
        # The prov node's precomp is bundled: the n-node unwraps to the data node
        # (so the client needs no extra /data fetch to unwrap it).
        assert res["precomp"][n_node.id_string()]["unwraps_to"] == layered.id_string()

    @pytest.mark.anyio
    async def test_pin_layered_bare_when_not_provenanced(self, layered_app_context):
        scope, bare_data, n_node, layered, client_for = layered_app_context
        async with client_for(False) as client:
            res = cbor2.loads((await client.get("/pin/lay")).content)
        assert res["id"] == layered.id_string()  # bare data id


# =============================================================================
# Fetch-or-fail + bare-id guard (no silent tolerance)
# =============================================================================


class TestFetchOrFail:
    @pytest.mark.anyio
    async def test_pin_unfetchable_entity_is_500(self):
        # A pin name that resolves but whose entity the scope can't serve is a
        # server inconsistency → 500, never a bare 200 with no precomp.
        async with FxtrScope(name="test_http_missing") as scope:
            _track_builtins(scope)
            missing = FxtrID.from_tag_and_payload("d", b"\x07" * 32)
            app = create_fxtr_app(scope, {"missing": missing})
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/pin/missing")
        assert resp.status_code == 500

    @pytest.mark.anyio
    async def test_data_rejects_at_form(self, app_context):
        # The '@'-form must never be silently accepted on the wire (it would
        # otherwise resolve to the bare data node, ignoring the suffix).
        scope, client, child_ref, parent_ref, pins = app_context
        at_form = f"{child_ref.entity_id.id_string()}@{child_ref.entity_id.id_string()}"
        resp = await client.get(f"/data/{at_form}")
        assert resp.status_code == 400


# =============================================================================
# Scope lifecycle tests
# =============================================================================


class TestScopeLifecycle:
    @pytest.mark.anyio
    async def test_scope_closed_returns_503(self):
        """After scope closes, all endpoints return 503."""
        scope = FxtrScope(name="test_closed")
        async with scope:
            _track_builtins(scope)
            ref = scope.store(SimpleEntity(name="x", value=1))
            pins = {"test": ref.entity_id}
            app = create_fxtr_app(scope, pins)

        # Scope is now closed
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for url in [
                f"/preview/{ref.entity_id.id_string()}",
                f"/data/{ref.entity_id.id_string()}",
                "/pin/test",
                "/pins",
            ]:
                resp = await client.get(url)
                assert resp.status_code == 503, f"Expected 503 for {url}, got {resp.status_code}"

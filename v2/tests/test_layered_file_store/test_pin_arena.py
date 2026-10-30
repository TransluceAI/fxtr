"""Tests for the pin-arena seam + LayeredStoreDeepFetchable.

Covers the lifetime-critical case: a layer holding a **local** entity `A` that
references a **remote-only** entity `B`. Mounting `A` via the seam into a
long-lived scope and closing the per-retrieve arena must leave `A` loadable and
`A -> B` resolvable through the `source` deep-fetchable threaded into the seam —
i.e. lazy resolution survives the transient arena (the property the flake
manager relies on once a build span is gone).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope
from fxtr2.data.layered_file_store import layer_format as fmt
from fxtr2.data.layered_file_store.pin_arena import (
    LayeredStoreDeepFetchable,
    open_seed_arena,
    resolve_pin,
)
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from fxtr2.leases.lease_bridges import IndependentLocalBridge
from tests.test_layered_file_store.conftest import LfsLeaf, LfsParent, StubDeepFetchable


async def _build_local_a_remote_b_layer(
    layer: Path, store_scope: FxtrScope, source: StubDeepFetchable
) -> tuple[FxtrID, FxtrID]:
    """Build a layer with A local (data) and B remote-only (precomp marker).

    Returns ``(a_id, b_id)``. A = ``LfsParent(child=B)``; B = ``LfsLeaf``.
    """
    leaf_ref = store_scope.store(LfsLeaf(name="B", value=7))
    parent_ref = store_scope.store(LfsParent(name="A", child=leaf_ref))
    async with LayeredFileStoreIsolatedWriter(
        layer, parents=[], remote=(source, source.lease())
    ) as w:
        # Mark B remote first; the A walk then stops at B (already covered) and
        # writes only A's data locally.
        await w.mark_as_remote(leaf_ref.entity_id)
        await w.pin_to_this_layer("A", parent_ref.entity_id, from_scope=store_scope)
    # Sanity: A is local data, B is precomp-only (remote).
    assert fmt.data_file_exists(layer, parent_ref.entity_id)
    assert not fmt.data_file_exists(layer, leaf_ref.entity_id)
    assert fmt.precomp_file_exists(layer, leaf_ref.entity_id)
    return parent_ref.entity_id, leaf_ref.entity_id


@pytest.mark.anyio
async def test_seam_local_a_resolves_remote_b_after_temp_arena_close(tmp_path: Path):
    layer = tmp_path / "L"
    async with FxtrScope(name="store") as store_scope:
        source = StubDeepFetchable(store_scope)
        a_id, _b_id = await _build_local_a_remote_b_layer(layer, store_scope, source)

        # The consumer scope stands in for the flake manager's long-lived scope.
        async with FxtrScope(name="consumer") as consumer:
            brand_lease = source.lease()
            fxtr_id = resolve_pin(layer, "A")
            arena = open_seed_arena(layer, fxtr_id, grant=brand_lease.grant, source=source)
            assert fxtr_id == a_id
            consumer._main_arena.lazily_attach_from_arena(
                consumer._current_auth_lease().clone(),
                fxtr_id,
                arena,
                IndependentLocalBridge(lease=source.lease()),
            )
            arena.close()  # the per-retrieve arena is gone

            # A is local (disk); loading it + following A -> B forces B's fetch
            # through the source deep-fetchable (B has no local data).
            loaded_a = await consumer.load(EntityRef(a_id, LfsParent))
            assert loaded_a.name == "A"
            loaded_b = await loaded_a.child.load(consumer)
            assert loaded_b.name == "B"
            assert loaded_b.value == 7
            brand_lease.discard()


@pytest.mark.anyio
async def test_resolve_pin_absent_pin_raises_keyerror(tmp_path: Path):
    layer = tmp_path / "L"
    async with FxtrScope(name="store") as store_scope:
        source = StubDeepFetchable(store_scope)
        await _build_local_a_remote_b_layer(layer, store_scope, source)
        with pytest.raises(KeyError):
            resolve_pin(layer, "nope")


@pytest.mark.anyio
async def test_deep_fetchable_lookup_hit_and_miss(tmp_path: Path):
    layer = tmp_path / "L"
    async with FxtrScope(name="store") as store_scope:
        source = StubDeepFetchable(store_scope)
        a_id, b_id = await _build_local_a_remote_b_layer(layer, store_scope, source)

        brand_lease = source.lease()
        df = LayeredStoreDeepFetchable([layer], grant=brand_lease.grant, source=source)

        arena_a = await df.try_lookup(a_id)  # local data -> hit
        assert arena_a is not None
        arena_a.close()

        arena_b = await df.try_lookup(b_id)  # precomp (remote) -> hit
        assert arena_b is not None
        arena_b.close()

        # An id no layer holds (no data, no precomp) -> miss.
        unknown = FxtrID(raw_data_id=b"d" + b"\x00" * 32)
        assert await df.try_lookup(unknown) is None
        brand_lease.discard()


@pytest.mark.anyio
async def test_deep_fetchable_registered_resolves_closure_by_id(tmp_path: Path):
    """Register the deep-fetch view on a serving scope; loading A by id pulls it
    from the layer set and following A -> B falls through to the source."""
    layer = tmp_path / "L"
    async with FxtrScope(name="store") as store_scope:
        source = StubDeepFetchable(store_scope)
        a_id, _b_id = await _build_local_a_remote_b_layer(layer, store_scope, source)

        brand_lease = source.lease()
        df = LayeredStoreDeepFetchable([layer], grant=brand_lease.grant, source=source)
        async with FxtrScope(name="serving") as serving:
            serving.register_deep_fetch_source(df, IndependentLocalBridge(lease=source.lease()))
            loaded_a = await serving.load(EntityRef(a_id, LfsParent))
            assert loaded_a.name == "A"
            loaded_b = await loaded_a.child.load(serving)
            assert loaded_b.value == 7
        brand_lease.discard()

"""Unit tests for the wire-encoding helpers in `fxtr2/services/cap_ir_arena.py`.

Covers `_encode_fxtr_id` / `_decode_fxtr_id` round-trips, the precomp-closure
helper's BFS shape, the symmetric `ingest_precomp` ingestion, and the
`_decode_flat_data` reply parser. The acceptance tests for the full
adapter / arena live in `test_cap_ir_arena.py`.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

import pytest

from fxtr2.data.abstract_arena import DepKind, FxtrEntityFlatData, ReadableArena
from fxtr2.data.entity_core import FxtrCbor, FxtrID
from fxtr2.leases.lease_types import Lease, LifetimeToken
from fxtr2.leases.leased_cap_ir import IRException, IRTagged, IRValue
from fxtr2.leases.local_lease import UnconditionalLocalLifetime
from fxtr2.services.cap_ir_arena import (
    FXTR_ID_TAG,
    FXTR_ID_WITH_PROV_TAG,
    _decode_flat_data,
    _decode_fxtr_id,
    _encode_fxtr_id,
    compute_and_track_precomp_closure,
    decode_fxtr_id_with_prov,
    encode_fxtr_id_with_prov,
    ingest_precomp,
)

# =============================================================================
# _encode_fxtr_id / _decode_fxtr_id
# =============================================================================


class TestFxtrIDRoundTrip:
    """Each node tag round-trips correctly through the tagged-bytes encoding."""

    @pytest.mark.parametrize(
        "id_obj",
        [
            FxtrID.from_tag_and_payload("d", b"\x01" * 32),
            FxtrID.from_tag_and_payload("c", b"\x02" * 32),
            FxtrID.from_tag_and_payload("n", b"\x03" * 32),
            FxtrID.from_tag_and_payload("u", b"d" + b"\x04" * 32),
            FxtrID.builtin("foo"),
            FxtrID.from_tag_and_payload(".", b""),
        ],
        ids=["d", "c", "n", "u(d)", "!builtin", ".ground"],
    )
    def test_round_trip(self, id_obj: FxtrID) -> None:
        encoded = _encode_fxtr_id(id_obj)
        assert isinstance(encoded, IRTagged)
        assert encoded.tag == FXTR_ID_TAG
        assert encoded.value == bytes(id_obj)
        decoded = _decode_fxtr_id(encoded)
        assert decoded == id_obj
        assert hash(decoded) == hash(id_obj)


class TestFxtrIDWithProvRoundTrip:
    """The service-path encoder/decoder pair handles bare and prov-bearing IDs.

    Bare IDs round-trip through ``FXTR_ID_TAG`` (same as the
    arena-internal encoding); prov-bearing IDs round-trip through
    ``FXTR_ID_WITH_PROV_TAG`` and rely on the receiver-side arena's
    ``unwrap_provenance`` for sync data-ID recovery.
    """

    def test_bare_id_uses_plain_tag(self) -> None:
        bare = FxtrID.from_tag_and_payload("d", b"\x01" * 32)
        encoded = encode_fxtr_id_with_prov(bare)
        assert encoded.tag == FXTR_ID_TAG
        assert encoded.value == bytes(bare)

    def test_prov_bearing_id_uses_prov_tag(self) -> None:
        prov_parent = FxtrID.from_tag_and_payload("n", b"\xaa" * 32)
        data = FxtrID.from_tag_and_payload("d", b"\xbb" * 32)
        layered = FxtrID(raw_data_id=bytes(data), provenance_parent=prov_parent)
        encoded = encode_fxtr_id_with_prov(layered)
        assert encoded.tag == FXTR_ID_WITH_PROV_TAG
        # The wire payload is the prov parent's bytes only — the data ID
        # is recovered on the receiver via sync unwrap.
        assert encoded.value == bytes(prov_parent)

    def test_decode_uses_embed_prov_for_prov_tag(self) -> None:
        prov_parent = FxtrID.from_tag_and_payload("n", b"\xaa" * 32)
        data = FxtrID.from_tag_and_payload("d", b"\xbb" * 32)
        encoded = IRTagged(FXTR_ID_WITH_PROV_TAG, bytes(prov_parent))

        def embed(prov: FxtrID) -> FxtrID:
            assert prov == prov_parent
            return FxtrID(raw_data_id=bytes(data), provenance_parent=prov)

        decoded = decode_fxtr_id_with_prov(encoded, embed_prov=embed)
        assert decoded == data
        assert decoded._provenance_parent == prov_parent

    def test_decode_propagates_embed_prov_errors(self) -> None:
        unknown_prov = FxtrID.from_tag_and_payload("n", b"\xcc" * 32)
        encoded = IRTagged(FXTR_ID_WITH_PROV_TAG, bytes(unknown_prov))

        def embed(prov: FxtrID) -> FxtrID:
            raise ValueError(f"unknown prov {prov}")

        with pytest.raises(ValueError, match="unknown prov"):
            decode_fxtr_id_with_prov(encoded, embed_prov=embed)


class TestFxtrIDDecodeErrors:
    def test_wrong_tag_type(self) -> None:
        with pytest.raises(IRException) as excinfo:
            _decode_fxtr_id("not a tagged value")
        assert excinfo.value.category == "MalformedArenaPacket"

    def test_wrong_tag_label(self) -> None:
        with pytest.raises(IRException) as excinfo:
            _decode_fxtr_id(IRTagged("not_fxtr_id", b"some bytes"))
        assert excinfo.value.category == "MalformedArenaPacket"

    def test_inner_value_not_bytes(self) -> None:
        with pytest.raises(IRException) as excinfo:
            _decode_fxtr_id(IRTagged(FXTR_ID_TAG, "not bytes"))
        assert excinfo.value.category == "MalformedArenaPacket"


# =============================================================================
# compute_and_track_precomp_closure
# =============================================================================


class _StubArena(ReadableArena[Any]):
    """In-memory stub `ReadableArena` for testing `compute_and_track_precomp_closure`.

    Implements only the four sync provenance methods the helper actually
    calls (`unwrap_provenance`, `c_node_to_context_data`,
    `p_node_to_pathroot`, `step_back_p_ctx`); every other inherited
    abstract method raises. Each implemented method consumes its lease
    (matching the real contract) and returns the next ID from the
    stub's tables.
    """

    def __init__(
        self,
        token: LifetimeToken[Any],
        unwraps_to: dict[FxtrID, FxtrID] | None = None,
        ctx_node: dict[FxtrID, FxtrID] | None = None,
        step_back: dict[FxtrID, FxtrID] | None = None,
    ) -> None:
        self._token = token
        self._unwraps_to = unwraps_to or {}
        self._ctx_node = ctx_node or {}
        self._step_back = step_back or {}
        self.calls: list[tuple[str, FxtrID]] = []

    @property
    def token(self) -> LifetimeToken[Any]:
        return self._token

    def unwrap_provenance(self, lease: Lease[Any], node_id: FxtrID) -> FxtrID | None:
        lease.discard()
        self.calls.append(("unwrap_provenance", node_id))
        return self._unwraps_to.get(node_id)

    def c_node_to_context_data(self, lease: Lease[Any], c_node_id: FxtrID) -> FxtrID:
        lease.discard()
        self.calls.append(("c_node_to_context_data", c_node_id))
        result = self._ctx_node.get(c_node_id)
        if result is None:
            raise KeyError(c_node_id)
        return result

    def p_node_to_pathroot(self, lease: Lease[Any], p_node_id: FxtrID) -> FxtrID:
        lease.discard()
        self.calls.append(("p_node_to_pathroot", p_node_id))
        result = self._ctx_node.get(p_node_id)
        if result is None:
            raise KeyError(p_node_id)
        return result

    def step_back_p_ctx(self, lease: Lease[Any], p_node_id: FxtrID) -> FxtrID | None:
        lease.discard()
        self.calls.append(("step_back_p_ctx", p_node_id))
        return self._step_back.get(p_node_id)

    # The rest of the ReadableArena interface — unused by `compute_and_track_precomp_closure`.
    def is_tracked(self, lease: Lease[Any], node_id: FxtrID) -> bool:
        raise NotImplementedError

    async def load(self, lease: Lease[Any], node_id: FxtrID) -> FxtrCbor:
        raise NotImplementedError

    async def load_flat(self, lease: Lease[Any], node_id: FxtrID) -> FxtrEntityFlatData:
        raise NotImplementedError

    def abandon(self, lease: Lease[Any], node_id: FxtrID) -> None:
        raise NotImplementedError

    def copy_to_new_arena(
        self, lease: Lease[Any], node_ids: Collection[FxtrID]
    ) -> ReadableArena[Any]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class TestComputePrecompClosure:
    def test_d_node_seed_only_no_entries(self) -> None:
        """A `d`-node with no precomp produces no entries."""
        ull = UnconditionalLocalLifetime("test")
        d_id = FxtrID.from_tag_and_payload("d", b"\x01" * 32)
        arena = _StubArena(ull.token)

        entries = compute_and_track_precomp_closure(arena, ull.root_lease.take(), seed_ids=[d_id])

        assert entries == []
        assert arena.calls == []  # `d`-tag not in (c,p,u,n) → no provenance probe
        ull.revoke("test done")

    def test_c_node_chain(self) -> None:
        """c-node → ctx_node → d-node closure."""
        ull = UnconditionalLocalLifetime("test")
        d_id = FxtrID.from_tag_and_payload("d", b"\x10" * 32)
        c_id = FxtrID.from_tag_and_payload("c", b"\x20" * 32)
        # c-node unwraps to the ground (".") node, which is a `d`-style leaf.
        ground = FxtrID.from_tag_and_payload(".", b"")

        arena = _StubArena(
            ull.token,
            unwraps_to={c_id: ground},
            ctx_node={c_id: d_id},
        )

        entries = compute_and_track_precomp_closure(arena, ull.root_lease.take(), seed_ids=[c_id])

        # Should have exactly one entry, for c_id, with both unwraps_to and ctx_node.
        assert len(entries) == 1
        e = entries[0]
        assert _decode_fxtr_id(e["id"]) == c_id
        assert _decode_fxtr_id(e["unwraps_to"]) == ground
        assert _decode_fxtr_id(e["ctx_node"]) == d_id

        # ground is a "." (builtin-ish leaf), d_id is a `d`-node — neither generates
        # further provenance probes. Only c-node was probed.
        unwrap_calls = [c for c in arena.calls if c[0] == "unwrap_provenance"]
        assert unwrap_calls == [("unwrap_provenance", c_id)]
        ull.revoke("test done")

    def test_p_node_with_step_back(self) -> None:
        """p-node yields entries with both ctx_node and step_back_p_ctx."""
        ull = UnconditionalLocalLifetime("test")
        p_id = FxtrID.from_tag_and_payload("p", b"\xab" * 32)
        pathroot = FxtrID.from_tag_and_payload("d", b"\xcd" * 32)
        prev_p = FxtrID.from_tag_and_payload("p", b"\xef" * 32)
        prev_pathroot = FxtrID.from_tag_and_payload("d", b"\x11" * 32)

        # Set up: p_id unwraps somewhere, has pathroot, has step-back to prev_p.
        # prev_p is a sibling p-node with its own pathroot but no further
        # step-back / unwrap (so the BFS stops at prev_p's pathroot, a `d`-leaf).
        arena = _StubArena(
            ull.token,
            unwraps_to={p_id: pathroot},
            ctx_node={p_id: pathroot, prev_p: prev_pathroot},
            step_back={p_id: prev_p},
        )

        entries = compute_and_track_precomp_closure(arena, ull.root_lease.take(), seed_ids=[p_id])

        # Both p-nodes get entries. p_id has all three relationships; prev_p
        # has just ctx_node (no unwrap, no step-back). The `d` pathroots are
        # leaves and don't generate entries.
        decoded = {_decode_fxtr_id(e["id"]): e for e in entries}
        assert set(decoded.keys()) == {p_id, prev_p}
        e = decoded[p_id]
        assert _decode_fxtr_id(e["unwraps_to"]) == pathroot
        assert _decode_fxtr_id(e["ctx_node"]) == pathroot
        assert _decode_fxtr_id(e["step_back_p_ctx"]) == prev_p
        prev_e = decoded[prev_p]
        assert _decode_fxtr_id(prev_e["ctx_node"]) == prev_pathroot
        assert "unwraps_to" not in prev_e
        assert "step_back_p_ctx" not in prev_e
        ull.revoke("test done")

    def test_visited_set_avoids_revisits(self) -> None:
        """Cycles or shared targets are visited once."""
        ull = UnconditionalLocalLifetime("test")
        c1 = FxtrID.from_tag_and_payload("c", b"\x01" * 32)
        c2 = FxtrID.from_tag_and_payload("c", b"\x02" * 32)
        d_target = FxtrID.from_tag_and_payload("d", b"\x03" * 32)

        # c1 and c2 both have ctx_node = d_target, so d_target shows up via both.
        arena = _StubArena(
            ull.token,
            ctx_node={c1: d_target, c2: d_target},
        )

        entries = compute_and_track_precomp_closure(arena, ull.root_lease.take(), seed_ids=[c1, c2])

        assert len(entries) == 2  # c1 and c2; d_target is a leaf
        # d_target should not have been probed at all — `d`-tag bypasses both
        # the (c,p,u,n) unwrap branch and the c/p tag-specific branches.
        d_target_calls = [c for c in arena.calls if c[1] == d_target]
        assert d_target_calls == []
        ull.revoke("test done")

    def test_lease_consumed(self) -> None:
        """The supplied lease is taken on entry and discarded on exit."""
        ull = UnconditionalLocalLifetime("test")
        arena = _StubArena(ull.token)

        lease = ull.root_lease.take()
        _ = compute_and_track_precomp_closure(arena, lease, seed_ids=[])
        assert not lease.is_active()  # taken into the helper
        ull.revoke("test done")


# =============================================================================
# ingest_precomp
# =============================================================================


class TestIngestPrecomp:
    def test_basic_ingestion(self) -> None:
        """Round-trip from `compute_and_track_precomp_closure` through `ingest_precomp`
        produces the expected three dicts."""
        ull = UnconditionalLocalLifetime("test")
        c_id = FxtrID.from_tag_and_payload("c", b"\x01" * 32)
        ctx = FxtrID.from_tag_and_payload("d", b"\x02" * 32)
        ground = FxtrID.from_tag_and_payload(".", b"")
        arena = _StubArena(
            ull.token,
            unwraps_to={c_id: ground},
            ctx_node={c_id: ctx},
        )
        entries = compute_and_track_precomp_closure(arena, ull.root_lease.take(), seed_ids=[c_id])

        unwraps_to: dict[FxtrID, FxtrID] = {}
        ctx_node: dict[FxtrID, FxtrID] = {}
        step_back_p_ctx: dict[FxtrID, FxtrID] = {}
        ingest_precomp(
            entries,
            unwraps_to=unwraps_to,
            ctx_node=ctx_node,
            step_back_p_ctx=step_back_p_ctx,
        )

        assert unwraps_to == {c_id: ground}
        assert ctx_node == {c_id: ctx}
        assert step_back_p_ctx == {}
        ull.revoke("test done")

    def test_missing_id_raises(self) -> None:
        with pytest.raises(IRException) as excinfo:
            ingest_precomp(
                [{}],
                unwraps_to={},
                ctx_node={},
                step_back_p_ctx={},
            )
        assert excinfo.value.category == "MalformedArenaPacket"

    def test_non_mapping_entry_raises(self) -> None:
        with pytest.raises(IRException) as excinfo:
            ingest_precomp(
                ["not a dict"],  # type: ignore[list-item]
                unwraps_to={},
                ctx_node={},
                step_back_p_ctx={},
            )
        assert excinfo.value.category == "MalformedArenaPacket"


# =============================================================================
# _decode_flat_data
# =============================================================================


class TestDecodeFlatData:
    def test_round_trip_d_node(self) -> None:
        node_id = FxtrID.from_tag_and_payload("d", b"\x01" * 32)
        dep_id = FxtrID.from_tag_and_payload("d", b"\x02" * 32)
        reply: IRValue = {
            "entity_id": _encode_fxtr_id(node_id),
            "entity_bytes": b"some cbor here",
            "deps": [{"id": _encode_fxtr_id(dep_id), "kind": "strong_ref"}],
            "precomp": [],
        }

        flat, precomp = _decode_flat_data(reply)

        assert flat == FxtrEntityFlatData(
            entity_id=node_id,
            entity_bytes=b"some cbor here",
            deps=[(dep_id, DepKind.STRONG_REF)],
        )
        assert list(precomp) == []

    def test_derived_node_no_bytes(self) -> None:
        c_id = FxtrID.from_tag_and_payload("c", b"\x01" * 32)
        reply: IRValue = {
            "entity_id": _encode_fxtr_id(c_id),
            "entity_bytes": None,  # derived node
            "deps": [],
            "precomp": [],
        }

        flat, _ = _decode_flat_data(reply)
        assert flat.entity_bytes is None

    def test_missing_field_raises(self) -> None:
        with pytest.raises(IRException) as excinfo:
            _decode_flat_data({"entity_id": _encode_fxtr_id(FxtrID.builtin("x"))})  # type: ignore[arg-type]
        assert excinfo.value.category == "MalformedArenaPacket"

    def test_unknown_dep_kind_raises(self) -> None:
        node_id = FxtrID.from_tag_and_payload("d", b"\x01" * 32)
        reply: IRValue = {
            "entity_id": _encode_fxtr_id(node_id),
            "entity_bytes": b"",
            "deps": [{"id": _encode_fxtr_id(node_id), "kind": "bogus_kind"}],
            "precomp": [],
        }
        with pytest.raises(IRException) as excinfo:
            _decode_flat_data(reply)
        assert excinfo.value.category == "MalformedArenaPacket"

    def test_non_mapping_reply_raises(self) -> None:
        with pytest.raises(IRException) as excinfo:
            _decode_flat_data("not a dict")
        assert excinfo.value.category == "MalformedArenaPacket"

    def test_bad_entity_bytes_type(self) -> None:
        node_id = FxtrID.from_tag_and_payload("d", b"\x01" * 32)
        reply: IRValue = {
            "entity_id": _encode_fxtr_id(node_id),
            "entity_bytes": "not bytes",  # should be bytes or None
            "deps": [],
            "precomp": [],
        }
        with pytest.raises(IRException) as excinfo:
            _decode_flat_data(reply)
        assert excinfo.value.category == "MalformedArenaPacket"

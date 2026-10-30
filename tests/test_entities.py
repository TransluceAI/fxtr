"""Tests for entities."""

import math
import pickle

from fxtr.object_model import core, dag_util, locator


class SomeTestEntity(
    core.DataEntityBase, namespace_id="!testing.test_entities", type_id="SomeTestEntity^1"
):
    """A test entity."""

    name: str
    number: int


def test_entity_pickle():
    """Test that entities can be pickled and unpickled."""
    entity = SomeTestEntity(name="test", number=10)
    print(pickle.dumps(entity))
    assert entity == pickle.loads(pickle.dumps(entity))


class SomeGenericEntity[T: core.AnyEntity](
    core.DataEntityBase, namespace_id="!testing.test_entities", type_id="SomeGenericEntity^1"
):
    """A test entity."""

    value: T


def test_typespec_of_generic_entities_is_ok():
    """Test that a union of generic entities is ok."""
    typespec = core.python_type_to_hub_type_spec(SomeGenericEntity[SomeTestEntity])
    assert isinstance(typespec, core.TypeSpec_DataEntity)


def test_union_of_generic_entities_is_ok():
    """Test that a union of generic entities is ok."""
    typespec = core.python_type_to_hub_type_spec(
        SomeGenericEntity[SomeTestEntity] | SomeGenericEntity[core.AdHocMetadata]
    )
    assert isinstance(typespec, core.TypeSpec_UnionOfEntities)


class InnerEntity(
    core.DataEntityBase, namespace_id="!testing.test_entities", type_id="InnerEntity^1"
):
    """An inner test entity for nesting tests."""

    value: int


class OuterEntity(
    core.DataEntityBase, namespace_id="!testing.test_entities", type_id="OuterEntity^1"
):
    """An outer test entity that contains an inner entity."""

    inner: InnerEntity
    name: str


def test_nested_entity_pickle():
    """Test that nested entities can be pickled and unpickled."""
    outer = OuterEntity(inner=InnerEntity(value=42), name="test")
    print(f"Outer: {outer}")
    pickled = pickle.dumps(outer)
    print(f"Pickled size: {len(pickled)}")
    unpickled = pickle.loads(pickled)
    assert outer == unpickled
    assert unpickled.inner.value == 42
    assert unpickled.name == "test"


def test_run_pickle():
    """Test that Run entities can be pickled and unpickled."""
    from frozendict import frozendict

    # Create a minimal Run object
    run = core.Run(
        task=core.entity_cid(SomeTestEntity(name="task", number=1)),
        provenance=core.entity_cid(core.ImplProvenanceSet(provenances=frozendict({}))),
        run_data=core.entity_cid(SomeTestEntity(name="result", number=2)),
        run_info=core.BasicRunInfo(
            author="test",
            author_email="test@example.com",
            timestamp="2024-01-01T00:00:00Z",
            message="test run",
            metadata=core.AdHocMetadata(metadata=frozendict({})),
        ),
    )
    print(f"Run: {run}")
    pickled = pickle.dumps(run)
    print(f"Pickled size: {len(pickled)}")
    unpickled = pickle.loads(pickled)
    assert run == unpickled


class FloatEntity(
    core.DataEntityBase, namespace_id="!testing.test_entities", type_id="FloatEntity^1"
):
    """A test entity."""

    value: float
    nested: SomeTestEntity


def test_extremal_float_values():
    """Test that extremal float values can be stored and retrieved."""
    # Test positive infinity
    entity = FloatEntity(value=float("inf"), nested=SomeTestEntity(name="test", number=1))
    # Build subgraph from the entity
    subgraph = dag_util.entity_subgraph_to_ipld_blocks(named_roots={}, sequence_roots=[entity])
    # Create locator from subgraph
    in_memory_locator = locator.InMemoryCompleteSubgraphLocator(subgraph=subgraph)
    # Use locator to reconstruct entity
    loader = locator.CachingEntityLoader.from_locator(in_memory_locator)
    reconstructed = loader(core.entity_cid(entity))
    assert isinstance(reconstructed, FloatEntity)
    assert reconstructed.value == float("inf")

    # Test negative infinity
    entity = FloatEntity(value=float("-inf"), nested=SomeTestEntity(name="test", number=2))
    subgraph = dag_util.entity_subgraph_to_ipld_blocks(named_roots={}, sequence_roots=[entity])
    in_memory_locator = locator.InMemoryCompleteSubgraphLocator(subgraph=subgraph)
    loader = locator.CachingEntityLoader.from_locator(in_memory_locator)
    reconstructed = loader(core.entity_cid(entity))
    assert isinstance(reconstructed, FloatEntity)
    assert reconstructed.value == float("-inf")

    # Test NaN
    entity = FloatEntity(value=float("nan"), nested=SomeTestEntity(name="test", number=3))
    subgraph = dag_util.entity_subgraph_to_ipld_blocks(named_roots={}, sequence_roots=[entity])
    in_memory_locator = locator.InMemoryCompleteSubgraphLocator(subgraph=subgraph)
    loader = locator.CachingEntityLoader.from_locator(in_memory_locator)
    reconstructed = loader(core.entity_cid(entity))
    assert isinstance(reconstructed, FloatEntity)
    assert math.isnan(reconstructed.value)


def test_cbrrr_cbor2_encoding_equivalence():
    """Test that cbrrr and cbor2 produce identical CBOR bytes for FloatEntity with regular float."""
    import cbor2
    import cbrrr

    from fxtr.object_model import cbor_util

    # Create a FloatEntity with a regular float value (not inf, -inf, or nan) and a nested entity
    entity = FloatEntity(value=3.14159, nested=SomeTestEntity(name="test", number=42))

    # Convert entity to IPLD format
    ipld_data = entity._fxtr_typespec.entity_data_to_ipld(entity)

    # Encode using cbrrr
    cbrrr_encoded = cbrrr.encode_dag_cbor(ipld_data)

    # Encode using cbor2 with canonical=True and default encoder
    cbor2_encoded = cbor2.dumps(ipld_data, canonical=True, default=cbor_util._default_encoder)

    # Assert they produce identical bytes
    assert cbrrr_encoded == cbor2_encoded, (
        f"Encodings differ:\n"
        f"  cbrrr:   {cbrrr_encoded.hex()}\n"
        f"  cbor2:   {cbor2_encoded.hex()}\n"
        f"  lengths: {len(cbrrr_encoded)} vs {len(cbor2_encoded)}"
    )

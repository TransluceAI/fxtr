"""Smoke tests to verify test infrastructure works."""

import pytest

from fxtr2.data.entity_core import FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.memory_arena import MemoryArena, MemoryStore


def test_memory_store_fixture(memory_store: MemoryStore):
    """Test that the memory_store fixture provides a working store."""
    assert memory_store is not None
    assert isinstance(memory_store, MemoryStore)


def test_memory_arena_fixture(memory_arena: MemoryArena):
    """Test that the memory_arena fixture provides a working arena."""
    assert memory_arena is not None
    assert isinstance(memory_arena, MemoryArena)


def test_ground_context_fixture(ground_context: FxtrID):
    """Test that the ground_context fixture returns the '.' node."""
    assert ground_context is not None
    tag, payload = ground_context.split_tag_and_payload()
    assert tag == "."
    assert payload == b""


def test_fxtr_id_basics():
    """Basic sanity check for FxtrID creation."""
    # Ground node
    ground = FxtrID.from_tag_and_payload(".", b"")
    assert ground.get_tag() == "."
    assert ground.local_id_string() == "."

    # Builtin node
    builtin = FxtrID.builtin("test_type")
    assert builtin.get_tag() == "!"
    assert builtin.local_id_string() == "!test_type"


@pytest.mark.anyio
async def test_async_store_and_load(memory_arena: MemoryArena):
    """Test async store and load operations work."""
    ground = FxtrID.from_tag_and_payload(".", b"")
    type_id = FxtrID.builtin("test")

    # Track the builtin type
    memory_arena.track_builtin(type_id)

    # Create a simple entity
    entity_data = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
        "$fxtr.type": RawWeakFxtrID(inner_id=type_id),
        "name": "test_entity",
        "value": 42,
    }

    # Store it
    entity_id = memory_arena.store(entity_data)
    assert entity_id is not None
    assert entity_id.get_tag() == "d"  # Ground data node

    # Load it back
    loaded = await memory_arena.load(entity_id)
    assert loaded["name"] == "test_entity"
    assert loaded["value"] == 42

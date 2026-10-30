"""Tests for fxtr2/data/fxtr_context_api.py - the user-facing namespace API."""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import pytest

from fxtr2.data import entity_serdes, system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr2.data.fxtr_scope import ACTIVE_SCOPE, FxtrScope

pytestmark = pytest.mark.anyio

# =============================================================================
# Test Entity Types
# =============================================================================

tydef_uuid = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


@dataclasses.dataclass(frozen=True)
class SampleEntity(
    entity_serdes.DataclassEntity,
    fxtr_type=system_entities.define_type(tydef_uuid, "context_api_test_entity"),
):
    """Simple test entity for namespace tests."""

    value: str


tydef_uuid2 = uuid.UUID("b2c3d4e5-f678-90ab-cdef-123456789012")


@dataclasses.dataclass(frozen=True)
class SampleEntityWithRef(
    entity_serdes.DataclassEntity,
    fxtr_type=system_entities.define_type(tydef_uuid2, "context_api_test_entity_with_ref"),
):
    """Test entity that contains a reference to another entity."""

    name: str
    child: entity_serdes.EntityRef[SampleEntity]


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tmp_db_url(tmp_path: Path) -> str:
    """Create a SQLite URL for the temporary database."""
    return f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite'}"


# =============================================================================
# Basic Context Manager Tests
# =============================================================================


class TestSQLiteFxtrNamespaceContextManager:
    """Tests for SQLiteFxtrNamespace context manager behavior."""

    async def test_opens_and_closes(self, tmp_db_url: str):
        """Namespace opens and closes properly as context manager."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            # Should be usable inside context
            assert ns.scope is not None
            pins = await ns.list_pins()
            assert pins == {}

    async def test_raises_outside_context(self, tmp_db_url: str):
        """Accessing backend outside context manager raises error."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        with pytest.raises(RuntimeError, match="must be used as a context manager"):
            ns._get_backend()

    async def test_scope_becomes_active(self, tmp_db_url: str):
        """Namespace scope becomes the active scope when entered."""
        outer_scope = ACTIVE_SCOPE.get()
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            # The namespace's scope should be the active scope
            assert ACTIVE_SCOPE.get() is ns.scope
            assert ACTIVE_SCOPE.get() is not outer_scope
        # After exit, should restore outer scope
        assert ACTIVE_SCOPE.get() is outer_scope

    async def test_isolated_scope_not_active(self, tmp_db_url: str):
        """With isolate_scope=True, namespace scope doesn't become active."""
        outer_scope = ACTIVE_SCOPE.get()
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns", isolate_scope=True)
        async with ns:
            # Active scope should still be outer, not the namespace's scope
            assert ACTIVE_SCOPE.get() is outer_scope
            assert ACTIVE_SCOPE.get() is not ns.scope


# =============================================================================
# Pin Tests
# =============================================================================


class TestSQLiteFxtrNamespacePin:
    """Tests for pin operations."""

    async def test_pin_entity_object(self, tmp_db_url: str):
        """Can pin an Entity object directly."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            entity = SampleEntity(value="test_value")
            await ns.pin("mykey", entity)

            pins = await ns.list_pins()
            assert "mykey" in pins

    async def test_pin_entity_ref(self, tmp_db_url: str):
        """Can pin an EntityRef."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            entity = SampleEntity(value="test_value")
            ref = entity.store()
            await ns.pin("mykey", ref)

            pins = await ns.list_pins()
            assert "mykey" in pins
            assert pins["mykey"] == ref.ref

    async def test_pin_fxtr_id(self, tmp_db_url: str):
        """Can pin a raw FxtrID."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            entity = SampleEntity(value="test_value")
            ref = entity.store()
            await ns.pin("mykey", ref.ref)

            pins = await ns.list_pins()
            assert "mykey" in pins
            assert pins["mykey"] == ref.ref

    async def test_pin_overwrite_false_raises(self, tmp_db_url: str):
        """Pin with overwrite=False raises KeyError on duplicate."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            entity1 = SampleEntity(value="first")
            entity2 = SampleEntity(value="second")
            await ns.pin("mykey", entity1)

            with pytest.raises(KeyError):
                await ns.pin("mykey", entity2, overwrite=False)

    async def test_pin_overwrite_true_replaces(self, tmp_db_url: str):
        """Pin with overwrite=True replaces existing pin."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            entity1 = SampleEntity(value="first")
            entity2 = SampleEntity(value="second")
            ref1 = entity1.store()
            ref2 = entity2.store()

            await ns.pin("mykey", ref1)
            await ns.pin("mykey", ref2, overwrite=True)

            pins = await ns.list_pins()
            assert pins["mykey"] == ref2.ref

    async def test_pin_with_explicit_scope(self, tmp_db_url: str):
        """Can pin with an explicit scope parameter."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            # Create a child scope and store entity there
            async with FxtrScope(name="child") as child_scope:
                entity = SampleEntity(value="from_child")
                ref = entity.store()

                # Pin from the child scope
                await ns.pin("mykey", ref, scope=child_scope)

            pins = await ns.list_pins()
            assert "mykey" in pins

    async def test_pin_untracked_entity_raises(self, tmp_db_url: str):
        """Pinning an entity not tracked in scope raises ValueError."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            # Create a fake ID that isn't tracked
            fake_id = FxtrID.builtin("nonexistent_entity")
            with pytest.raises(ValueError, match="not tracked"):
                await ns.pin("mykey", fake_id)


# =============================================================================
# Unpin Tests
# =============================================================================


class TestSQLiteFxtrNamespaceUnpin:
    """Tests for unpin operations."""

    async def test_unpin_removes_pin(self, tmp_db_url: str):
        """Unpin removes an existing pin."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            entity = SampleEntity(value="test")
            await ns.pin("mykey", entity)

            await ns.unpin("mykey")

            pins = await ns.list_pins()
            assert "mykey" not in pins

    async def test_unpin_nonexistent_raises(self, tmp_db_url: str):
        """Unpin of nonexistent key raises KeyError."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            with pytest.raises(KeyError):
                await ns.unpin("nonexistent")


# =============================================================================
# Retrieve Tests
# =============================================================================


class TestSQLiteFxtrNamespaceRetrieve:
    """Tests for retrieve operations."""

    async def test_retrieve_returns_fxtr_id(self, tmp_db_url: str):
        """Retrieve without type returns FxtrID."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            entity = SampleEntity(value="test")
            ref = entity.store()
            await ns.pin("mykey", ref)

            result = await ns.retrieve("mykey")
            assert isinstance(result, FxtrID)
            assert result == ref.ref

    async def test_retrieve_with_type_returns_entity_ref(self, tmp_db_url: str):
        """Retrieve with type returns EntityRef[T]."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            entity = SampleEntity(value="test")
            ref = entity.store()
            await ns.pin("mykey", ref)

            result = await ns.retrieve("mykey", SampleEntity)
            assert isinstance(result, entity_serdes.EntityRef)
            loaded = await result.load()
            assert loaded.value == "test"

    async def test_retrieve_nonexistent_raises(self, tmp_db_url: str):
        """Retrieve of nonexistent key raises KeyError."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            with pytest.raises(KeyError):
                await ns.retrieve("nonexistent")

    async def test_retrieve_into_namespace_scope(self, tmp_db_url: str):
        """Retrieve with into_scope=namespace.scope works (original behavior)."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            entity = SampleEntity(value="test")
            ref = entity.store()
            await ns.pin("mykey", ref)

            # Retrieve into the namespace's own scope explicitly
            result = await ns.retrieve("mykey", SampleEntity, into_scope=ns.scope)
            assert ns.scope.is_tracked(result.ref)

    async def test_retrieve_into_child_scope_with_lift(self, tmp_db_url: str):
        """Retrieve into child scope with copy_into_scope=False (lift)."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            entity = SampleEntity(value="test")
            ref = entity.store()
            await ns.pin("mykey", ref)

            # Create a child scope of the namespace scope
            async with FxtrScope(name="child") as child_scope:
                # Retrieve into child scope via lift (default)
                result = await ns.retrieve("mykey", SampleEntity, into_scope=child_scope)
                assert child_scope.is_tracked(result.ref)

                # Should be loadable from child scope
                loaded = await result.load()
                assert loaded.value == "test"

    async def test_retrieve_into_non_child_scope_without_copy_raises(self, tmp_db_url: str):
        """Retrieve into non-child scope with copy_into_scope=False raises error."""
        # Create an isolated scope that is NOT a child of the namespace scope
        async with FxtrScope(name="isolated", isolated=True) as isolated_scope:
            ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
            async with ns:
                entity = SampleEntity(value="test")
                ref = entity.store()
                await ns.pin("mykey", ref)

                # Try to retrieve into isolated scope without copying - should fail
                with pytest.raises(ValueError, match="not a child of scope"):
                    await ns.retrieve(
                        "mykey", SampleEntity, into_scope=isolated_scope, copy_into_scope=False
                    )

    async def test_retrieve_into_non_child_scope_with_copy(self, tmp_db_url: str):
        """Retrieve into non-child scope with copy_into_scope=True works."""
        # Create an isolated scope that is NOT a child of the namespace scope
        async with FxtrScope(name="isolated", isolated=True) as isolated_scope:
            ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
            async with ns:
                entity = SampleEntity(value="test")
                ref = entity.store()
                await ns.pin("mykey", ref)

                # Retrieve into isolated scope with copying - should work
                result = await ns.retrieve(
                    "mykey", SampleEntity, into_scope=isolated_scope, copy_into_scope=True
                )
                assert isolated_scope.is_tracked(result.ref)

                # Should be loadable from isolated scope
                loaded = await result.load(isolated_scope)
                assert loaded.value == "test"

    async def test_retrieve_active_scope_not_child_without_copy_raises(
        self, tmp_db_url: str, local_fxtr_scope: FxtrScope
    ):
        """Retrieve when active scope is not child of namespace scope raises without copy."""
        # Use isolated namespace scope so active scope (local_fxtr_scope) is NOT its child
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns", isolate_scope=True)
        async with ns:
            # Create entity in local_fxtr_scope and pin it into the namespace
            entity = SampleEntity(value="test")
            ref = entity.store()
            await ns.pin("mykey", ref)

            # Active scope (local_fxtr_scope) is NOT a child of namespace scope (isolated)
            # Retrieve without copy_into_scope should fail
            with pytest.raises(ValueError, match="not.+child"):
                await ns.retrieve("mykey", SampleEntity)

    async def test_retrieve_active_scope_not_child_with_copy_works(
        self, tmp_db_url: str, local_fxtr_scope: FxtrScope
    ):
        """Retrieve when active scope is not child of namespace scope works with copy."""
        # Use isolated namespace scope so active scope (local_fxtr_scope) is NOT its child
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns", isolate_scope=True)
        async with ns:
            # Create entity in local_fxtr_scope and pin it into the namespace
            entity = SampleEntity(value="test")
            ref = entity.store()
            await ns.pin("mykey", ref)

            # Active scope (local_fxtr_scope) is NOT a child of namespace scope (isolated)
            # Retrieve with copy_into_scope=True should work
            result = await ns.retrieve("mykey", SampleEntity, copy_into_scope=True)
            assert ACTIVE_SCOPE.get().is_tracked(result.ref)

            loaded = await result.load()
            assert loaded.value == "test"


# =============================================================================
# try_retrieve Tests
# =============================================================================


class TestSQLiteFxtrNamespaceTryRetrieve:
    """Tests for try_retrieve operations."""

    async def test_try_retrieve_found_returns_fxtr_id(self, tmp_db_url: str):
        """try_retrieve returns FxtrID when found (no type)."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            entity = SampleEntity(value="test")
            ref = entity.store()
            await ns.pin("mykey", ref)

            result = await ns.try_retrieve("mykey")
            assert isinstance(result, FxtrID)
            assert result == ref.ref

    async def test_try_retrieve_found_returns_entity_ref(self, tmp_db_url: str):
        """try_retrieve returns EntityRef when found (with type)."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            entity = SampleEntity(value="test")
            ref = entity.store()
            await ns.pin("mykey", ref)

            result = await ns.try_retrieve("mykey", SampleEntity)
            assert result is not None
            assert isinstance(result, entity_serdes.EntityRef)
            loaded = await result.load()
            assert loaded.value == "test"

    async def test_try_retrieve_not_found_returns_none(self, tmp_db_url: str):
        """try_retrieve returns None when key not found."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            result = await ns.try_retrieve("nonexistent")
            assert result is None

    async def test_try_retrieve_not_found_with_type_returns_none(self, tmp_db_url: str):
        """try_retrieve returns None when key not found (with type)."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            result = await ns.try_retrieve("nonexistent", SampleEntity)
            assert result is None

    async def test_try_retrieve_into_child_scope_with_lift(self, tmp_db_url: str):
        """try_retrieve into child scope with lift works."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            entity = SampleEntity(value="test")
            ref = entity.store()
            await ns.pin("mykey", ref)

            async with FxtrScope(name="child") as child_scope:
                result = await ns.try_retrieve("mykey", SampleEntity, into_scope=child_scope)
                assert result is not None
                assert child_scope.is_tracked(result.ref)

    async def test_try_retrieve_into_non_child_scope_with_copy(self, tmp_db_url: str):
        """try_retrieve into non-child scope with copy works."""
        async with FxtrScope(name="isolated", isolated=True) as isolated_scope:
            ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
            async with ns:
                entity = SampleEntity(value="test")
                ref = entity.store()
                await ns.pin("mykey", ref)

                result = await ns.try_retrieve(
                    "mykey", SampleEntity, into_scope=isolated_scope, copy_into_scope=True
                )
                assert result is not None
                assert isolated_scope.is_tracked(result.ref)


# =============================================================================
# list_pins Tests
# =============================================================================


class TestSQLiteFxtrNamespaceListPins:
    """Tests for list_pins operations."""

    async def test_list_pins_empty(self, tmp_db_url: str):
        """list_pins returns empty dict when no pins."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            pins = await ns.list_pins()
            assert pins == {}

    async def test_list_pins_returns_all(self, tmp_db_url: str):
        """list_pins returns all pinned entities."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            entity1 = SampleEntity(value="first")
            entity2 = SampleEntity(value="second")
            ref1 = entity1.store()
            ref2 = entity2.store()

            await ns.pin("key1", ref1)
            await ns.pin("key2", ref2)

            pins = await ns.list_pins()
            assert len(pins) == 2
            assert pins["key1"] == ref1.ref
            assert pins["key2"] == ref2.ref

    async def test_list_pins_with_prefix(self, tmp_db_url: str):
        """list_pins with prefix filters results."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            entity1 = SampleEntity(value="first")
            entity2 = SampleEntity(value="second")
            entity3 = SampleEntity(value="third")
            ref1 = entity1.store()
            ref2 = entity2.store()
            ref3 = entity3.store()

            await ns.pin("models:encoder", ref1)
            await ns.pin("models:decoder", ref2)
            await ns.pin("data:train", ref3)

            pins = await ns.list_pins("models:")
            assert len(pins) == 2
            assert "models:encoder" in pins
            assert "models:decoder" in pins
            assert "data:train" not in pins


# =============================================================================
# Persistence Tests
# =============================================================================


class TestSQLiteFxtrNamespacePersistence:
    """Tests for data persistence across sessions."""

    async def test_data_persists_across_sessions(self, tmp_db_url: str):
        """Data pinned in one session is available in another."""
        # First session: pin an entity
        ns1 = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns1:
            entity = SampleEntity(value="persistent_value")
            await ns1.pin("persistent_key", entity)

        # Second session: retrieve the entity
        ns2 = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns2:
            result = await ns2.retrieve("persistent_key", SampleEntity)
            loaded = await result.load()
            assert loaded.value == "persistent_value"

    async def test_different_namespaces_are_isolated(self, tmp_db_url: str):
        """Different namespace names are isolated from each other."""
        # Pin in namespace A
        ns_a = SQLiteFxtrNamespace(tmp_db_url, "namespace_a")
        async with ns_a:
            entity = SampleEntity(value="from_a")
            await ns_a.pin("mykey", entity)

        # Namespace B should not see it
        ns_b = SQLiteFxtrNamespace(tmp_db_url, "namespace_b")
        async with ns_b:
            result = await ns_b.try_retrieve("mykey", SampleEntity)
            assert result is None


# =============================================================================
# Entity with References Tests
# =============================================================================


class TestSQLiteFxtrNamespaceWithReferences:
    """Tests for pinning and retrieving entities that contain references."""

    async def test_pin_and_retrieve_entity_with_ref(self, tmp_db_url: str):
        """Can pin and retrieve entity containing a reference to another entity."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            child = SampleEntity(value="child_value")
            child_ref = child.store()
            parent = SampleEntityWithRef(name="parent", child=child_ref)
            await ns.pin("parent_key", parent)

            # Retrieve and verify
            result = await ns.retrieve("parent_key", SampleEntityWithRef)
            loaded = await result.load()
            assert loaded.name == "parent"

            # Load the child reference
            loaded_child = await loaded.child.load()
            assert loaded_child.value == "child_value"

    async def test_retrieve_with_ref_into_child_scope(self, tmp_db_url: str):
        """Retrieving entity with refs into child scope loads correctly."""
        ns = SQLiteFxtrNamespace(tmp_db_url, "test_ns")
        async with ns:
            child = SampleEntity(value="child_value")
            child_ref = child.store()
            parent = SampleEntityWithRef(name="parent", child=child_ref)
            await ns.pin("parent_key", parent)

            async with FxtrScope(name="child_scope") as child_scope:
                result = await ns.retrieve(
                    "parent_key", SampleEntityWithRef, into_scope=child_scope
                )
                assert child_scope.is_tracked(result.ref)

                # Load and verify
                loaded = await result.load()
                assert loaded.name == "parent"

                loaded_child = await loaded.child.load()
                assert loaded_child.value == "child_value"

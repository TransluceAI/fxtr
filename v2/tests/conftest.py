"""Pytest configuration and fixtures for fxtr2 tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.fxtr_scope import GLOBAL_SCOPE, FxtrScope
from fxtr2.data.memory_arena import MemoryArena, MemoryStore


@pytest.fixture
def memory_store() -> Iterator[MemoryStore]:
    """Create a fresh MemoryStore for testing."""
    store = MemoryStore()
    try:
        yield store
    finally:
        store.close(close_arenas=True)


@pytest.fixture
def memory_arena(memory_store: MemoryStore) -> Iterator[MemoryArena]:
    """Create a fresh MemoryArena backed by a MemoryStore."""
    arena = memory_store.new_empty_arena()
    try:
        yield arena
    finally:
        arena.close()


@pytest.fixture
async def local_fxtr_scope() -> AsyncIterator[FxtrScope]:
    """Provide an isolated child scope for the test to run in.

    Tests that use this fixture automatically run inside a child scope of the
    global scope. Any entities stored during the test are cleaned up when the
    test completes, preventing leaks to the global scope.
    """
    async with FxtrScope(name="test_scope") as scope:
        yield scope


@pytest.fixture
async def local_fxtr_arena(local_fxtr_scope: FxtrScope) -> AsyncIterator[MemoryArena]:
    """Provide the main arena from the local scope (for lower-level tests)."""
    yield local_fxtr_scope._main_arena


@pytest.fixture
def ground_context() -> FxtrID:
    """Return the ground context node (\".\")."""
    return FxtrID.from_tag_and_payload(".", b"")


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Create a temporary database path for SQLite tests."""
    return tmp_path / "test.sqlite"


@pytest.fixture
def tmp_db_path_str(tmp_db_path: Path) -> str:
    """Create a temporary database path as string (for sqlite_store tests)."""
    return str(tmp_db_path)


@pytest.fixture
def tmp_db_url(tmp_db_path: Path) -> str:
    """Create a SQLite URL for the temporary database (for legacy sqlite_arena tests)."""
    return f"sqlite+aiosqlite:///{tmp_db_path}"


@pytest.fixture(autouse=True)
def global_scope() -> Iterator[FxtrScope]:
    """Provide the global scope and check for leaked entities.

    This fixture automatically runs for every test. It checks that tests
    don't leak state into the global scope. After the test completes, it verifies
    the global scope's tracked entities haven't changed. If they have, the test
    fails with a descriptive error.
    """
    scope = GLOBAL_SCOPE

    # Record the initial state of tracked entities
    arena_state = scope._store._arenas[scope._main_arena._key]
    initial_liveset = arena_state.liveset.copy()

    yield scope

    # Check for state leakage after test completes
    final_liveset = arena_state.liveset
    new_entities = final_liveset - initial_liveset

    if new_entities:
        # Format the leaked entities for the error message
        leaked_list = ", ".join(str(e) for e in list(new_entities)[:5])
        if len(new_entities) > 5:
            leaked_list += f", ... ({len(new_entities) - 5} more)"
        pytest.fail(
            f"Test leaked {len(new_entities)} entity(ies) into global scope: {leaked_list}. "
            "Tests should not modify the global scope. Use the `local_fxtr_scope` fixture "
            "or create a child scope manually with `async with FxtrScope():`."
        )

"""Pytest configuration and fixtures for fxtr2 tests."""

from __future__ import annotations

import gc
import signal
from collections.abc import AsyncIterator, Collection, Iterator
from pathlib import Path
from typing import Any

import pytest

from fxtr2.data.abstract_arena import FxtrEntityFlatData
from fxtr2.data.entity_core import FxtrCbor, FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope
from fxtr2.data.memory_arena import MemoryArena, MemoryStore
from fxtr2.leases import local_lease
from fxtr2.leases.lease_types import Lease, LifetimeToken
from fxtr2.leases.local_lease import UnconditionalLocalLifetime

_TEST_TIMEOUT = 30


@pytest.fixture(autouse=True)
def _assert_no_lease_leaks(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail the test if any lease is GC'd without explicit discard during its run.

    Uses the process-wide ``_LEASE_LEAK_COUNT`` bumped inside
    ``fxtr2.leases.local_lease._on_lease_leaked``. We ``gc.collect()`` before the
    test to flush any stragglers from previous tests, capture the counter, then
    compare after another collect on teardown.

    Tests that intentionally leak leases (e.g. to verify the leak-warning path
    itself) opt out via ``@pytest.mark.allow_lease_leak``.
    """
    gc.collect()
    before = local_lease.get_lease_leak_count()
    yield
    gc.collect()
    delta = local_lease.get_lease_leak_count() - before
    if delta and "allow_lease_leak" not in request.keywords:
        pytest.fail(f"Test leaked {delta} lease(s) without discard; see warnings in log.")


@pytest.fixture(autouse=True)
def _per_test_timeout() -> Iterator[None]:
    """Fail any single test that runs longer than 10 seconds.

    Uses ``signal.alarm`` (POSIX only) so it works for both sync and async
    tests, including deadlocks that wouldn't respond to anyio cancellation.
    On timeout, SIGALRM fires and raises ``TimeoutError`` in the main thread,
    which pytest reports as a test failure.
    """

    def _handler(signum, frame):  # noqa: ARG001
        # for task in asyncio.all_tasks():
        #     print(f"Straggler task at timeout: {task}", file=sys.stderr)
        #     task.print_stack()
        raise TimeoutError(
            f"Test timed out after {_TEST_TIMEOUT} seconds. If this test is an async test, consider"
            " wrapping it in anyio.fail_after to get a more informative traceback."
        )

    old_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(_TEST_TIMEOUT)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


class _LeasedArena:
    """Convenience wrapper that auto-clones its held lease on every arena call.

    Used in tests where the focus is on entity-storage semantics rather than
    lease mechanics — keeps the call-sites tight without forcing every test to
    thread ``lease.clone()`` through each method. Tests that specifically
    exercise lease semantics (grant validation, breakage, bridging) should
    construct their own ULL directly instead.
    """

    def __init__(self, arena: MemoryArena[Any], lease: Lease[Any]):
        self._arena: MemoryArena[Any] = arena
        self._lease: Lease[Any] = lease

    @property
    def token(self) -> LifetimeToken[Any]:
        return self._arena.token

    def is_tracked(self, node_id: FxtrID) -> bool:
        return self._arena.is_tracked(self._lease.clone(), node_id)

    def track_builtin(self, node_id: FxtrID) -> FxtrID:
        return self._arena.track_builtin(self._lease.clone(), node_id)

    def store(self, entity: FxtrCbor) -> FxtrID:
        return self._arena.store(self._lease.clone(), entity)

    async def load(self, node_id: FxtrID) -> FxtrCbor:
        return await self._arena.load(self._lease.clone(), node_id)

    async def load_flat(self, node_id: FxtrID) -> FxtrEntityFlatData:
        return await self._arena.load_flat(self._lease.clone(), node_id)

    def unwrap_provenance(self, node_id: FxtrID) -> FxtrID | None:
        return self._arena.unwrap_provenance(self._lease.clone(), node_id)

    def create_c_node(self, context_data: FxtrCbor) -> FxtrID:
        return self._arena.create_c_node(self._lease.clone(), context_data)

    def c_node_to_context_data(self, c_node_id: FxtrID) -> FxtrID:
        return self._arena.c_node_to_context_data(self._lease.clone(), c_node_id)

    def create_p_node(self, pathroot_data: FxtrCbor) -> FxtrID:
        return self._arena.create_p_node(self._lease.clone(), pathroot_data)

    def p_node_to_pathroot(self, p_node_id: FxtrID) -> FxtrID:
        return self._arena.p_node_to_pathroot(self._lease.clone(), p_node_id)

    def step_back_p_ctx(self, p_node_id: FxtrID) -> FxtrID | None:
        return self._arena.step_back_p_ctx(self._lease.clone(), p_node_id)

    def create_u_node(self, wrapped_id: FxtrID) -> FxtrID:
        return self._arena.create_u_node(self._lease.clone(), wrapped_id)

    def abandon(self, node_id: FxtrID) -> None:
        return self._arena.abandon(self._lease.clone(), node_id)

    def copy_to_new_arena(self, node_ids: Collection[FxtrID]) -> MemoryArena[Any]:
        return self._arena.copy_to_new_arena(self._lease.clone(), node_ids)

    def track_from_memory_arena(self, node_id: FxtrID, source: _LeasedArena) -> None:
        """Delegate to the arena-level ``track_from_memory_arena`` (checks source
        liveset as well as doing the store-level DAG walk)."""
        self._arena.track_from_memory_arena(
            self._lease.clone(),
            source._arena,
            source._lease.clone(),
            node_id,
        )

    async def copy_from_arena(self, node_id: FxtrID, source: _LeasedArena) -> None:
        """Delegate to the store-backed cross-arena copy with both leases cloned."""
        await self._arena.copy_from_arena(
            self._lease.clone(),
            source._arena,
            source._lease.clone(),
            node_id,
        )

    def close(self) -> None:
        self._arena.close()
        self._lease.discard()


@pytest.fixture
async def local_fxtr_scope() -> AsyncIterator[FxtrScope]:
    """A fresh ``FxtrScope`` used as the active scope for the duration of a test.

    Restored after the Chunk 1 scope rewrite. The scope inherits from the current
    active scope (usually ``GLOBAL_SCOPE``); tests that need an isolated scope should
    construct one directly via ``FxtrScope(isolated=True)``.
    """
    async with FxtrScope(name="local_fxtr_scope_fixture") as scope:
        yield scope


@pytest.fixture
def memory_store() -> Iterator[MemoryStore]:
    """Create a fresh MemoryStore for testing."""
    store = MemoryStore()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def memory_arena(
    memory_store: MemoryStore,
) -> Iterator[tuple[MemoryArena[Any], Lease[Any]]]:
    """Create a fresh MemoryArena backed by a MemoryStore, plus the driving lease.

    The fixture mints a new ``UnconditionalLocalLifetime``, registers its token with
    the store, takes the root lease, and returns ``(arena, lease)``. Tests clone
    ``lease`` as needed to drive arena methods; on teardown the arena is closed and
    the lease is discarded, which ends the ULL and fires the store's end-callback.
    """
    ull = UnconditionalLocalLifetime("test_arena")
    memory_store.register_token(ull.token, name="test_arena")
    lease = ull.root_lease.take()
    arena = memory_store.new_arena(lease.clone())
    arena.track_builtin(lease.clone(), FxtrID.from_tag_and_payload(".", b""))
    try:
        yield arena, lease
    finally:
        arena.close()
        lease.discard()


@pytest.fixture
def _legacy_arena(
    memory_arena: tuple[MemoryArena[Any], Lease[Any]],
) -> _LeasedArena:
    """Auto-cloning arena wrapper for tests that originally used ``local_fxtr_arena``.

    Equivalent semantics for the storage/provenance tests; avoids forcing every
    call site to thread ``lease.clone()`` explicitly.
    """
    arena, lease = memory_arena
    return _LeasedArena(arena, lease)


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
    arena_state = scope._global_store._arenas[scope._main_arena._key]
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

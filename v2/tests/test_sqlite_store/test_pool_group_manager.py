"""Tests for ``_SharedSqlitePoolGroupManager`` (sqlite_namespace.py):
the per-(path, mode) pool-group registry's teardown consistency."""

from __future__ import annotations

from pathlib import Path

import pytest

from fxtr2.data.sqlite_store.sqlite_namespace import _SharedSqlitePoolGroupManager


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_registry_survives_aclose_failure(tmp_path: Path) -> None:
    """A failing ``grp.aclose()`` on last-holder exit must not poison the
    registry: the event/refcount cleanup is unconditional, so the same
    (path, mode) key can be opened again in the same process.

    Regression: the cleanup used to run *after* the ``await
    grp.aclose()`` in the same ``finally`` — any exception from the
    close (most practically ``CancelledError``, when the context
    manager unwinds under a pending cancellation) skipped it, leaving
    the "a pool exists" event behind with the group already popped;
    every later opener then took the re-use branch and died with
    "Connection pool ... not found, likely failed during setup in a
    different task", permanently for the process. Found by the
    experiment demo's crash/revive flow (the first thing to reopen a
    db path after a cancelled session unwind); see the teardown-
    hardening followup in projects/fxtr_experiment/plans/
    phase4_monitoring_ui/chunk3_experiment_demo.md for the wider
    family (this test covers only the registry-consistency half).
    """
    manager = _SharedSqlitePoolGroupManager()
    db_path = str(tmp_path / "test.sqlite")

    first_grp = None
    real_aclose = None

    def boom() -> None:
        raise RuntimeError("injected aclose failure")

    async def failing_aclose() -> None:
        boom()

    with pytest.raises(RuntimeError, match="injected aclose failure"):
        async with manager.get_pool_group(db_path) as grp:
            first_grp = grp
            real_aclose = grp.aclose
            grp.aclose = failing_aclose  # type: ignore[method-assign]

    # The key must be fully cleaned despite the failed close: a fresh
    # open constructs a NEW pool group rather than taking the re-use
    # branch into a missing entry.
    async with manager.get_pool_group(db_path) as grp2:
        assert grp2 is not first_grp

    # Hygiene: actually close the first group's worker threads (its
    # close was the one we sabotaged).
    assert real_aclose is not None
    await real_aclose()

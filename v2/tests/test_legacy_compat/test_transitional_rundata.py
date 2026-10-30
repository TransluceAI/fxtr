"""Tests for fxtr2/legacy_compat/transitional_rundata.py - transitional entity types.

Verifies that transitional entities (TaskWithPredecessors, ImplProvenanceSet,
RunWithPredecessors) can be instantiated, stored, and loaded. This was broken by
the stage 4 entity registry refactor which left unresolved abstract methods from
SelfLoadingLegacyEntity on these classes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from frozendict import frozendict

from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.legacy_compat.data import LegacyCompat
from fxtr2.legacy_compat.transitional_rundata import (
    ImplProvenanceSet,
    RunWithPredecessors,
    TaskToRunMap,
    TaskWithPredecessors,
)
from fxtr.object_model import core as v1_core

# =============================================================================
# V1 Test Entity Classes
# =============================================================================


class SimpleV1Entity(
    v1_core.DataEntityBase, type_id="SimpleTransV1^1", namespace_id="test.transitional"
):
    """A simple v1 test entity for wrapping in LegacyCompat."""

    name: str
    value: int


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def scope() -> AsyncIterator[FxtrScope]:
    """Child scope for transitional rundata tests."""
    async with FxtrScope(name="test_transitional") as scope:
        yield scope


# =============================================================================
# TaskWithPredecessors Tests
# =============================================================================


class TestTaskWithPredecessors:
    """Tests for TaskWithPredecessors instantiation and round-trip."""

    @pytest.mark.anyio
    async def test_no_abstract_methods(self, scope: FxtrScope) -> None:
        """ABC bug is fixed — no unresolved abstract methods."""
        assert not getattr(TaskWithPredecessors, "__abstractmethods__", set())

    @pytest.mark.anyio
    async def test_instantiation(self, scope: FxtrScope) -> None:
        """TaskWithPredecessors can be instantiated."""
        v1_entity = SimpleV1Entity(name="task", value=1)
        task = TaskWithPredecessors(
            legacy_task=LegacyCompat(v1_entity),
            predecessors=frozenset(),
        )
        assert task._get_legacy_cid() == v1_core.entity_cid(v1_entity)

    @pytest.mark.anyio
    async def test_fxtr_type_id(self, scope: FxtrScope) -> None:
        """_fxtr_type_id works (needed by UpgradeCache)."""
        type_id = TaskWithPredecessors._fxtr_type_id()
        assert type_id is not None
        assert type_id == TaskWithPredecessors._fxtr_known_type_id

    @pytest.mark.anyio
    async def test_store_and_load(self, scope: FxtrScope) -> None:
        """TaskWithPredecessors can be stored and loaded."""
        v1_entity = SimpleV1Entity(name="task", value=42)
        task = TaskWithPredecessors(
            legacy_task=LegacyCompat(v1_entity),
            predecessors=frozenset(),
        )
        ref = task.store()
        loaded = await ref.load()

        assert isinstance(loaded, TaskWithPredecessors)
        assert loaded.legacy_task.value.name == "task"
        assert loaded.legacy_task.value.value == 42
        assert loaded.predecessors == frozenset()

    @pytest.mark.anyio
    async def test_with_predecessors(self, scope: FxtrScope) -> None:
        """TaskWithPredecessors with predecessor refs round-trips correctly."""
        v1_a = SimpleV1Entity(name="task_a", value=1)
        task_a = TaskWithPredecessors(
            legacy_task=LegacyCompat(v1_a),
            predecessors=frozenset(),
        )
        task_a_ref = task_a.store()

        v1_b = SimpleV1Entity(name="task_b", value=2)
        task_b = TaskWithPredecessors(
            legacy_task=LegacyCompat(v1_b),
            predecessors=frozenset([task_a_ref]),
        )
        ref = task_b.store()
        loaded = await ref.load()

        assert len(loaded.predecessors) == 1
        pred_ref = next(iter(loaded.predecessors))
        pred = await pred_ref.load()
        assert pred.legacy_task.value.name == "task_a"


# =============================================================================
# ImplProvenanceSet Tests
# =============================================================================


class TestImplProvenanceSet:
    """Tests for ImplProvenanceSet instantiation and round-trip."""

    @pytest.mark.anyio
    async def test_no_abstract_methods(self, scope: FxtrScope) -> None:
        assert not getattr(ImplProvenanceSet, "__abstractmethods__", set())

    @pytest.mark.anyio
    async def test_instantiation_and_roundtrip(self, scope: FxtrScope) -> None:
        """ImplProvenanceSet can be instantiated, stored, and loaded."""
        v1_entity = SimpleV1Entity(name="prov", value=1)
        prov = ImplProvenanceSet(
            provenances=frozendict({}),
            legacy_provenance_set=LegacyCompat(v1_entity),
        )
        assert prov._get_legacy_cid() == v1_core.entity_cid(v1_entity)

        ref = prov.store()
        loaded = await ref.load()

        assert isinstance(loaded, ImplProvenanceSet)
        assert loaded.provenances == frozendict({})
        assert loaded.legacy_provenance_set.value.name == "prov"

    @pytest.mark.anyio
    async def test_fxtr_type_id(self, scope: FxtrScope) -> None:
        type_id = ImplProvenanceSet._fxtr_type_id()
        assert type_id is not None
        assert type_id == ImplProvenanceSet._fxtr_known_type_id


# =============================================================================
# RunWithPredecessors Tests
# =============================================================================


class TestRunWithPredecessors:
    """Tests for RunWithPredecessors instantiation and round-trip."""

    @pytest.mark.anyio
    async def test_no_abstract_methods(self, scope: FxtrScope) -> None:
        assert not getattr(RunWithPredecessors, "__abstractmethods__", set())

    @pytest.mark.anyio
    async def test_instantiation_and_roundtrip(self, scope: FxtrScope) -> None:
        """RunWithPredecessors can be created with all its fields and round-trips."""
        v1_task = SimpleV1Entity(name="task", value=1)
        v1_prov = SimpleV1Entity(name="prov", value=2)
        v1_data = SimpleV1Entity(name="data", value=3)
        v1_info = SimpleV1Entity(name="info", value=4)
        v1_run = SimpleV1Entity(name="run", value=5)

        task = TaskWithPredecessors(
            legacy_task=LegacyCompat(v1_task),
            predecessors=frozenset(),
        )
        prov = ImplProvenanceSet(
            provenances=frozendict({}),
            legacy_provenance_set=LegacyCompat(v1_prov),
        )

        run = RunWithPredecessors(
            task=task.store(),
            provenance=prov.store(),
            run_data=LegacyCompat(v1_data).store(),
            run_info=LegacyCompat(v1_info),
            previous_runs=frozenset(),
            legacy_run_entity=LegacyCompat(v1_run),
        )
        assert run._get_legacy_cid() == v1_core.entity_cid(v1_run)

        ref = run.store()
        loaded = await ref.load()

        assert isinstance(loaded, RunWithPredecessors)
        assert loaded.run_info.value.name == "info"
        assert loaded.legacy_run_entity.value.name == "run"
        assert loaded.previous_runs == frozenset()

        # Verify we can follow entity refs
        loaded_task = await loaded.task.load()
        assert loaded_task.legacy_task.value.name == "task"

        loaded_prov = await loaded.provenance.load()
        assert loaded_prov.legacy_provenance_set.value.name == "prov"

        loaded_data = await loaded.run_data.load()
        assert loaded_data.value.name == "data"

    @pytest.mark.anyio
    async def test_with_previous_runs(self, scope: FxtrScope) -> None:
        """RunWithPredecessors with previous_runs refs round-trips correctly."""
        # Create first run (no predecessors)
        task_a = TaskWithPredecessors(
            legacy_task=LegacyCompat(SimpleV1Entity(name="task_a", value=1)),
            predecessors=frozenset(),
        )
        prov_a = ImplProvenanceSet(
            provenances=frozendict({}),
            legacy_provenance_set=LegacyCompat(SimpleV1Entity(name="prov_a", value=1)),
        )
        run_a = RunWithPredecessors(
            task=task_a.store(),
            provenance=prov_a.store(),
            run_data=LegacyCompat(SimpleV1Entity(name="data_a", value=1)).store(),
            run_info=LegacyCompat(SimpleV1Entity(name="info_a", value=1)),
            previous_runs=frozenset(),
            legacy_run_entity=LegacyCompat(SimpleV1Entity(name="run_a", value=1)),
        )
        run_a_ref = run_a.store()

        # Create second run that depends on first
        task_b = TaskWithPredecessors(
            legacy_task=LegacyCompat(SimpleV1Entity(name="task_b", value=2)),
            predecessors=frozenset([task_a.store()]),
        )
        run_b = RunWithPredecessors(
            task=task_b.store(),
            provenance=prov_a.store(),
            run_data=LegacyCompat(SimpleV1Entity(name="data_b", value=2)).store(),
            run_info=LegacyCompat(SimpleV1Entity(name="info_b", value=2)),
            previous_runs=frozenset([run_a_ref]),
            legacy_run_entity=LegacyCompat(SimpleV1Entity(name="run_b", value=2)),
        )
        ref = run_b.store()
        loaded = await ref.load()

        assert len(loaded.previous_runs) == 1
        prev_ref = next(iter(loaded.previous_runs))
        prev = await prev_ref.load()
        assert prev.legacy_run_entity.value.name == "run_a"


# =============================================================================
# TaskToRunMap Tests
# =============================================================================


class TestTaskToRunMap:
    @pytest.mark.anyio
    async def test_roundtrip(self, scope: FxtrScope) -> None:
        """TaskToRunMap stores and loads a mapping from tasks to runs."""
        task = TaskWithPredecessors(
            legacy_task=LegacyCompat(SimpleV1Entity(name="task", value=1)),
            predecessors=frozenset(),
        )
        prov = ImplProvenanceSet(
            provenances=frozendict({}),
            legacy_provenance_set=LegacyCompat(SimpleV1Entity(name="prov", value=1)),
        )
        run = RunWithPredecessors(
            task=task.store(),
            provenance=prov.store(),
            run_data=LegacyCompat(SimpleV1Entity(name="data", value=1)).store(),
            run_info=LegacyCompat(SimpleV1Entity(name="info", value=1)),
            previous_runs=frozenset(),
            legacy_run_entity=LegacyCompat(SimpleV1Entity(name="run", value=1)),
        )
        task_to_run_map = TaskToRunMap(
            runs=frozendict({task.store(): run.store()}),
        )
        ref = task_to_run_map.store()
        loaded = await ref.load()

        assert isinstance(loaded, TaskToRunMap)
        assert len(loaded.runs) == 1

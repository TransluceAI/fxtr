"""Transitional run data types.

This module provides a specific set of data types for interacting with tasks and runs in the
fxtr1 task system, but built on top of the v2 data types.

These types are "transitional" in the sense that they are primarily designed for compatibility with
the v1 task system, and will eventually be replaced by v2-native types.
"""

from __future__ import annotations

import dataclasses
import typing
import uuid
from typing import Any, Self

import cbrrr
from frozendict import frozendict

from fxtr2.conversion.converters.mashumaro_entities import Child, ChildRef, DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_ref_scope import EntityRef
from fxtr2.legacy_compat.data import LegacyCompat, TransitionalUpgradedEntity, UpgradeCache
from fxtr.impl_plans import plan_dep_util
from fxtr.object_model import core as v1_core
from fxtr.providers import externals_provider as v1_externals_provider

TRANSITIONAL_RUNDATA_NAMESPACE_UUID = uuid.UUID("293797b1-50f2-4a73-8fac-0f8a0a1332d6")


@dataclasses.dataclass(frozen=True)
class TaskWithPredecessors[T: v1_core.EntityBase](
    DataclassEntity,
    TransitionalUpgradedEntity,
    fxtr_type=system_entities.define_type(
        TRANSITIONAL_RUNDATA_NAMESPACE_UUID, "TaskWithPredecessors^1"
    ),
):
    """V2 task with explicit predecessors (kept alive via references).

    Attributes:
        task: The task.
        predecessors: The predecessors of the task.
    """

    legacy_task: Child[LegacyCompat[v1_core.Task[T]]]
    predecessors: frozenset[EntityRef[TaskWithPredecessors]]

    def _get_legacy_cid(self) -> cbrrr.CID:
        """Get the legacy CID of the task."""
        return self.legacy_task.legacy_cid

    @classmethod
    async def upgrade_from(cls, legacy_task: v1_core.Task[T], upgrade_cache: UpgradeCache) -> Self:
        """Upgrade a legacy task from the cache."""
        all_deps = plan_dep_util.plan_deps(legacy_task.function, recursive=True)
        predecessors: dict[cbrrr.CID, EntityRef[TaskWithPredecessors]] = {}
        for dep in all_deps:
            # Find predecessor tasks and look them up in the upgrade cache
            match dep:
                case v1_core.TaskResultHandle(task_ref=ref):
                    if ref not in predecessors:
                        try:
                            predecessors[ref] = await upgrade_cache.lookup_upgraded(ref, cls)
                        except KeyError as exc:
                            raise RuntimeError(
                                f"Predecessor task {ref} not found in cache! Make sure to upgrade"
                                " all predecessor tasks in dependency order."
                            ) from exc
                case v1_core.WorkflowStepResultHandle(
                    workflow_ref=ref
                ) | v1_core.WorkflowResultHandle(workflow_ref=ref):
                    raise ValueError(f"Workflow dependency {dep} not supported yet!")

            # Also save LegacyCompats for every dep plan into the cache; these will later be used to
            # upgrade ImplProvenanceSet, which needs to reference the deps as keys.
            await upgrade_cache.save_upgraded(LegacyCompat(dep))

        wrapped = LegacyCompat(legacy_task)
        result = cls(legacy_task=wrapped, predecessors=frozenset(predecessors.values()))
        await upgrade_cache.save_upgraded(result)
        return result


# need this type alias as a workaround for a mashumaro issue with looking up the module for an
# imported type alias
if typing.TYPE_CHECKING:
    type V1_Plan[T: v1_core.FxtrCallTarget] = v1_core.Plan[T]
else:
    # can be any type at runtime, doesn't matter, but it needs to be generic and mashumaro needs to
    # be able to retrieve it
    class V1_Plan[T: v1_core.FxtrCallTarget](typing.Protocol):
        pass


@dataclasses.dataclass(frozen=True)
class ImplProvenanceSet(
    DataclassEntity,
    TransitionalUpgradedEntity,
    fxtr_type=system_entities.define_type(
        TRANSITIONAL_RUNDATA_NAMESPACE_UUID, "ImplProvenanceSet^1"
    ),
):
    """V2 adapted version of a v1 core.ImplProvenanceSet.

    Attributes:
        provenances: A dictionary of provenance records, mapping from implementation plan to its
            provenance record. Both are wrapped in LegacyCompat to lift old-style entities into the
            v2 system.
    """

    provenances: frozendict[EntityRef[LegacyCompat[V1_Plan[Any]]], ChildRef[LegacyCompat[Any]]]
    legacy_provenance_set: Child[LegacyCompat[v1_core.ImplProvenanceSet]]

    def _get_legacy_cid(self) -> cbrrr.CID:
        """Get the legacy CID of the provenance set."""
        return self.legacy_provenance_set.legacy_cid

    @classmethod
    async def upgrade_from(
        cls, legacy_provenance_set: v1_core.ImplProvenanceSet, upgrade_cache: UpgradeCache
    ) -> Self:
        """Upgrade a legacy provenance set for a given task from the cache."""
        new_provenances: dict[
            EntityRef[LegacyCompat[v1_core.Plan[Any]]], ChildRef[LegacyCompat[Any]]
        ] = {}
        for plan, provenance in legacy_provenance_set.provenances.items():
            # Look up the LegacyCompat for the plan in the cache. Note that these will usually be
            # already present in the cache due to the save_upgraded calls in
            # TaskWithPredecessors.upgrade_from.
            try:
                new_key = await upgrade_cache.lookup_upgraded(plan, LegacyCompat)
            except KeyError as exc:
                raise RuntimeError(
                    f"Plan {plan} not found in cache! Plans should be cached when the task is"
                    " upgraded; did you upgrade the task that this provenance set is for?"
                ) from exc
            new_value = LegacyCompat(provenance).store()
            new_provenances[new_key] = new_value
        wrapped = LegacyCompat(legacy_provenance_set)
        result = cls(provenances=frozendict(new_provenances), legacy_provenance_set=wrapped)
        await upgrade_cache.save_upgraded(result)
        return result


@dataclasses.dataclass(frozen=True)
class RunWithPredecessors[T: v1_core.EntityBase](
    DataclassEntity,
    TransitionalUpgradedEntity,
    fxtr_type=system_entities.define_type(TRANSITIONAL_RUNDATA_NAMESPACE_UUID, "Run^1"),
):
    """V2 adapted version of a v1 core.Run.

    Attributes:
        task: The task that was run.
        provenance: The provenance of the run.
        run_data: The data of the run.
        run_info: The information about the run.
        previous_runs: The previous runs that this run used data from.
        legacy_run_entity: The legacy run entity itself; needed in order to preserve legacy run CID
            references.
    """

    task: EntityRef[TaskWithPredecessors[T]]
    provenance: ChildRef[ImplProvenanceSet]
    run_data: ChildRef[LegacyCompat[T]]
    run_info: Child[LegacyCompat[v1_core.BasicRunInfo]]
    previous_runs: frozenset[EntityRef[RunWithPredecessors]]
    legacy_run_entity: Child[LegacyCompat[v1_core.Run]]

    def _get_legacy_cid(self) -> cbrrr.CID:
        """Get the legacy CID of the run."""
        return self.legacy_run_entity.legacy_cid

    @classmethod
    async def upgrade_from(cls, legacy_run: v1_core.Run, upgrade_cache: UpgradeCache) -> Self:
        """Upgrade a legacy run from the cache."""
        try:
            task = await upgrade_cache.lookup_upgraded(legacy_run.task, TaskWithPredecessors)
        except KeyError as exc:
            raise RuntimeError(
                f"Task {legacy_run.task} not found in cache! Tasks must be cached before a run can"
                " be upgraded."
            ) from exc

        try:
            provenance = await upgrade_cache.lookup_upgraded(
                legacy_run.provenance, ImplProvenanceSet
            )
        except KeyError as exc:
            raise RuntimeError(
                f"Provenance {legacy_run.provenance} not found in cache! Provenances must be cached"
                " before a run can be upgraded."
            ) from exc

        try:
            run_data = await upgrade_cache.lookup_upgraded(legacy_run.run_data, LegacyCompat)
        except KeyError as exc:
            raise RuntimeError(
                f"Run data {legacy_run.run_data} not found in cache! Run data must be cached before"
                " a run can be upgraded."
            ) from exc

        run_info = LegacyCompat(legacy_run.run_info)
        legacy_run_entity = LegacyCompat(legacy_run)

        task_value = await task.load()
        provenance_value = await provenance.load()

        previous_runs = set()
        for dep in plan_dep_util.plan_deps(task_value.legacy_task.value.function, recursive=True):
            match dep:
                case v1_core.TaskResultHandle(task_ref=ref):
                    prev_provenance = provenance_value.legacy_provenance_set.value.provenances.get(
                        v1_core.entity_cid(dep)
                    )
                    if not isinstance(prev_provenance, v1_externals_provider.RunProvenance):
                        raise ValueError(
                            f"Provenance record {prev_provenance} has an invalid provenance for"
                            f" task {task_value.legacy_task.legacy_cid}: {prev_provenance}"
                        )
                    try:
                        prev_run = await upgrade_cache.lookup_upgraded(
                            prev_provenance.run_cid, RunWithPredecessors
                        )
                    except KeyError as exc:
                        raise RuntimeError(
                            f"Previous run {prev_provenance.run_cid} not found in cache! Runs must"
                            " be upgraded in dependency order."
                        ) from exc
                    previous_runs.add(prev_run)
                case v1_core.WorkflowStepResultHandle() | v1_core.WorkflowResultHandle():
                    raise ValueError(f"Workflow dependency {dep} not supported yet!")

        result = cls(
            task=task,
            provenance=provenance,
            run_data=run_data,
            run_info=run_info,
            previous_runs=frozenset(previous_runs),
            legacy_run_entity=legacy_run_entity,
        )
        await upgrade_cache.save_upgraded(result)
        return result


@dataclasses.dataclass(frozen=True)
class TaskToRunMap(
    DataclassEntity,
    fxtr_type=system_entities.define_type(TRANSITIONAL_RUNDATA_NAMESPACE_UUID, "RunSet^1"),
):
    """V2 map from tasks to their runs.

    Attributes:
        runs: The runs in the set.
    """

    runs: frozendict[
        EntityRef[TaskWithPredecessors[Any]],
        EntityRef[RunWithPredecessors[Any]],
    ]

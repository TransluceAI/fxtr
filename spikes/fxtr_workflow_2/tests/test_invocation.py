"""Tests for the realization-map / op-log entities + cache-key canonicalization
(stage 15 part 2, chunk 2a).

Covers: canonicalize() over primitives / refs / nested structures, ServiceRef
rejection, provenance-blindness; build_invocation() cache-key determinism,
provenance-blindness, distinctness, and arg-closure travel; and round-trips of
the realization-map / op-log entities.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from frozendict import frozendict

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.services.untyped_service import UntypedServiceImpl, service_method
from fxtr_workflow_2.cache_key import build_invocation, canonicalize, strip_provenance
from fxtr_workflow_2.entities import (
    RunCompleted,
    RunGroup,
    RunGroupOpened,
    RunStarted,
    WorkflowTaskComplete,
    WorkflowTaskInvocation,
)

pytestmark = pytest.mark.anyio

_TEST_NS = uuid.UUID("6c2d9b04-3a71-4e58-8f0a-1d7e4b9c2a60")


@dataclasses.dataclass(frozen=True)
class _Leaf(DataclassEntity, fxtr_type=system_entities.define_type(_TEST_NS, "leaf")):
    n: int


class _NoopSvc(UntypedServiceImpl):
    @service_method
    async def ping(self) -> str:
        return "pong"


def _bare_ref(n: int) -> EntityRef[UnknownEntity]:
    rid = _Leaf(n=n).store().entity_id
    return EntityRef(entity_id=rid, target_type=UnknownEntity)


# --- canonicalize() ---------------------------------------------------------


async def test_canonicalize_primitives_and_nesting() -> None:
    async with FxtrScope(name="canon_prim"):
        value = {"a": 1, "b": ["x", 2, True], "c": {"d": b"bytes", "e": None}}
        out = canonicalize(value)
        assert out == frozendict(
            {"a": 1, "b": ("x", 2, True), "c": frozendict({"d": b"bytes", "e": None})}
        )


async def test_canonicalize_entity_ref_to_bare_weak_leaf() -> None:
    async with FxtrScope(name="canon_ref"):
        ref = _bare_ref(7)
        out = canonicalize(ref)
        assert out == RawWeakFxtrID(inner_id=strip_provenance(ref.entity_id))


async def test_canonicalize_rejects_service_ref() -> None:
    async with FxtrScope(name="canon_svc") as scope:
        svc_ref = await scope.mount(_NoopSvc())
        with pytest.raises(TypeError, match="ServiceRef"):
            canonicalize(svc_ref)


async def test_canonicalize_rejects_non_str_mapping_key() -> None:
    async with FxtrScope(name="canon_badkey"):
        with pytest.raises(TypeError, match="keys must be str"):
            canonicalize({1: "x"})  # type: ignore[dict-item]


async def test_canonicalize_is_provenance_blind() -> None:
    async with FxtrScope(name="canon_prov"):
        bare = _bare_ref(11).entity_id
        layered = FxtrID(raw_data_id=bytes(bare), provenance_parent=_bare_ref(99).entity_id)
        assert canonicalize(
            EntityRef(entity_id=layered, target_type=UnknownEntity)
        ) == canonicalize(EntityRef(entity_id=bare, target_type=UnknownEntity))


# --- build_invocation() / cache key -----------------------------------------


async def test_invocation_key_is_deterministic() -> None:
    async with FxtrScope(name="inv_det"):
        step = _bare_ref(0)
        arg = _bare_ref(1)
        id1 = build_invocation(step, (arg, 5), {"k": "v"}).store().entity_id
        id2 = build_invocation(step, (arg, 5), {"k": "v"}).store().entity_id
        assert id1 == id2


async def test_invocation_key_is_provenance_blind() -> None:
    async with FxtrScope(name="inv_prov"):
        step_bare = _bare_ref(0)
        arg_id = _bare_ref(1).entity_id
        layered_arg = EntityRef(
            entity_id=FxtrID(raw_data_id=bytes(arg_id), provenance_parent=_bare_ref(2).entity_id),
            target_type=UnknownEntity,
        )
        bare_arg = EntityRef(entity_id=arg_id, target_type=UnknownEntity)
        id_layered = build_invocation(step_bare, (layered_arg,), {}).store().entity_id
        id_bare = build_invocation(step_bare, (bare_arg,), {}).store().entity_id
        assert id_layered == id_bare


async def test_invocation_key_distinguishes_args() -> None:
    async with FxtrScope(name="inv_distinct"):
        step = _bare_ref(0)
        base = build_invocation(step, (1,), {}).store().entity_id
        assert build_invocation(step, (2,), {}).store().entity_id != base
        assert build_invocation(step, (1, 1), {}).store().entity_id != base
        assert build_invocation(step, (1,), {"k": 1}).store().entity_id != base
        # different step fn -> different key
        assert build_invocation(_bare_ref(42), (1,), {}).store().entity_id != base


async def test_invocation_arg_closure_travels_and_roundtrips() -> None:
    async with FxtrScope(name="inv_closure"):
        leaf_id = _Leaf(n=123).store().entity_id
        arg = EntityRef(entity_id=leaf_id, target_type=UnknownEntity)
        inv_ref = build_invocation(_bare_ref(0), (arg,), {}).store()

        # Load the invocation back; its arg leaf is reachable from the same scope.
        loaded = await inv_ref.load()
        assert isinstance(loaded, WorkflowTaskInvocation)
        leaf = await EntityRef(entity_id=leaf_id, target_type=_Leaf).load()
        assert leaf.n == 123

        # store -> load -> store is stable.
        assert loaded.store().entity_id == inv_ref.entity_id


# --- entity round-trips -----------------------------------------------------


async def test_realization_and_oplog_entities_roundtrip() -> None:
    async with FxtrScope(name="entities_rt"):
        rg = RunGroup(name="nightly", description="the suite", created_at=1.0)
        rg_ref = rg.store()
        assert (await rg_ref.load()).name == "nightly"

        inv_ref = build_invocation(_bare_ref(0), (1,), {}).store()
        result_id = _Leaf(n=9).store().entity_id
        complete = WorkflowTaskComplete(
            invocation=inv_ref, result=result_id, depends_on=frozendict()
        )
        complete_ref = complete.store()
        loaded_complete = await complete_ref.load()
        assert loaded_complete.result == result_id
        assert loaded_complete.invocation.entity_id == inv_ref.entity_id

        opened = RunGroupOpened(run_group=rg_ref, owner="test-owner", timestamp=2.0)
        assert (await opened.store().load()).timestamp == 2.0

        started = RunStarted(
            run_group=rg_ref,
            slot_path="evals/run1",
            invocation=inv_ref,
            launched_job=True,
            owner="test-owner",
            timestamp=3.0,
        )
        loaded_started = await started.store().load()
        assert loaded_started.slot_path == "evals/run1"
        assert loaded_started.launched_job is True

        completed = RunCompleted(
            run_group=rg_ref,
            slot_path="evals/run1",
            invocation=inv_ref,
            outcome="success",
            result=result_id,
            reason=None,
            owner="test-owner",
            timestamp=4.0,
        )
        loaded_completed = await completed.store().load()
        assert loaded_completed.outcome == "success"
        assert loaded_completed.result == result_id


async def test_complete_depends_on_roundtrips() -> None:
    async with FxtrScope(name="complete_deps"):
        inv_ref = build_invocation(_bare_ref(0), (1,), {}).store()
        dep_complete = WorkflowTaskComplete(
            invocation=inv_ref, result=_Leaf(n=1).store().entity_id, depends_on=frozendict()
        )
        dep_ref: EntityRef[WorkflowTaskComplete] = dep_complete.store()
        parent = WorkflowTaskComplete(
            invocation=build_invocation(_bare_ref(1), (), {}).store(),
            result=_Leaf(n=2).store().entity_id,
            depends_on=frozendict({"evals/dep": dep_ref}),
        )
        loaded = await parent.store().load()
        assert loaded.depends_on["evals/dep"].entity_id == dep_ref.entity_id

"""Tests for first-class FxtrID support inside step functions (step 5a).

Covers: FxtrID as step args and results, cache key behavior, sub-step
entity passing, config entities, checkpoint support, provenance rejection,
and scope handling.
"""

import dataclasses
import uuid
from pathlib import Path

import pytest

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr_workflow.entrypoint import TopLevelEntrypoint
from fxtr_workflow.step_context import StepContext

from .conftest import StepKeyEntity

_TEST_NS = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Test entity types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class LabelEntity(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_TEST_NS, "test_label"),
):
    label: str


@dataclasses.dataclass(frozen=True)
class ConfigEntity(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_TEST_NS, "test_config"),
):
    setting: str


@dataclasses.dataclass(frozen=True)
class DerivedEntity(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_TEST_NS, "test_derived"),
):
    source_label: str
    suffix: str


# ---------------------------------------------------------------------------
# Basic FxtrID as step arg
# ---------------------------------------------------------------------------


async def test_step_receives_fxtrid_arg(tmp_path: Path):
    """A step receives a FxtrID arg and loads the entity."""

    async def read_label(ctx: StepContext, entity_id: FxtrID) -> str:
        ref = await EntityRef(entity_id, LabelEntity).load()
        return ref.label

    db = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            fid = LabelEntity(label="hello").store().entity_id
            key_id = StepKeyEntity(label="read_label").store().entity_id
            step = ep.bind_external_step(read_label, key_id)
            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(step, fid)
            assert result == "hello"


# ---------------------------------------------------------------------------
# Basic FxtrID as step result
# ---------------------------------------------------------------------------


async def test_step_returns_fxtrid(tmp_path: Path):
    """A step creates an entity and returns its FxtrID."""

    async def create_entity(ctx: StepContext, label: str) -> FxtrID:
        return LabelEntity(label=label).store().entity_id

    db = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="create").store().entity_id
            step = ep.bind_external_step(create_entity, key_id)
            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(step, "world")
            assert isinstance(result, FxtrID)


# ---------------------------------------------------------------------------
# Cache reuse with FxtrID args
# ---------------------------------------------------------------------------


async def test_cache_reuse_same_fxtrid_arg(tmp_path: Path):
    """Same FxtrID arg produces same cache key — step is not re-executed."""
    call_count = 0

    async def counting_step(ctx: StepContext, entity_id: FxtrID) -> str:
        nonlocal call_count
        call_count += 1
        return "done"

    db = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    fid = LabelEntity(label="same").store().entity_id

    # First run
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="counting").store().entity_id
            step = ep.bind_external_step(counting_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            assert await slot.run(step, fid) == "done"
    assert call_count == 1

    # Second run — same FxtrID arg, should be cached
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="counting").store().entity_id
            step = ep.bind_external_step(counting_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            assert await slot.run(step, fid) == "done"
    assert call_count == 1  # not re-executed


async def test_different_fxtrid_arg_different_cache_key(tmp_path: Path):
    """Different FxtrID args produce different cache keys."""
    db = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    async def echo_label(ctx: StepContext, entity_id: FxtrID) -> str:
        ref = await EntityRef(entity_id, LabelEntity).load()
        return ref.label

    fid_a = LabelEntity(label="alpha").store().entity_id
    fid_b = LabelEntity(label="beta").store().entity_id

    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="echo").store().entity_id
            step = ep.bind_external_step(echo_label, key_id)
            slot_a = ep.open_cache_slot(ns, "a")
            slot_b = ep.open_cache_slot(ns, "b")
            assert await slot_a.run(step, fid_a) == "alpha"
            assert await slot_b.run(step, fid_b) == "beta"


# ---------------------------------------------------------------------------
# FxtrID passing between steps
# ---------------------------------------------------------------------------


async def test_step_passes_fxtrid_to_substep(tmp_path: Path):
    """Step A creates an entity and passes its FxtrID to step B."""

    async def producer(ctx: StepContext) -> FxtrID:
        return LabelEntity(label="from_producer").store().entity_id

    async def consumer(ctx: StepContext, entity_id: FxtrID) -> str:
        ref = await EntityRef(entity_id, LabelEntity).load()
        return ref.label

    db = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_prod = StepKeyEntity(label="producer").store().entity_id
            key_cons = StepKeyEntity(label="consumer").store().entity_id
            step_prod = ep.bind_external_step(producer, key_prod)
            step_cons = ep.bind_external_step(consumer, key_cons)

            slot_a = ep.open_cache_slot(ns, "a")
            slot_b = ep.open_cache_slot(ns, "b")

            pending_a = slot_a.run(step_prod)
            # Forward FxtrID result as arg to consumer
            pending_b = slot_b.run(step_cons, pending_a)

            assert await pending_b == "from_producer"


async def test_substep_creates_and_returns_derived_entity(tmp_path: Path):
    """Step A passes entity to step B, B creates a derived entity and returns it."""

    async def outer(ctx: StepContext, handles: dict) -> FxtrID:
        fid = LabelEntity(label="source").store().entity_id
        inner_slot = ctx.cache_slot("inner")
        derived_fid = await inner_slot.run(handles["derive"], fid)
        return derived_fid

    async def derive(ctx: StepContext, source_id: FxtrID) -> FxtrID:
        source = await EntityRef(source_id, LabelEntity).load()
        return DerivedEntity(source_label=source.label, suffix="_derived").store().entity_id

    db = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_outer = StepKeyEntity(label="outer").store().entity_id
            key_derive = StepKeyEntity(label="derive").store().entity_id
            handles = {"derive": ep.bind_external_step(derive, key_derive)}
            step_outer = ep.bind_external_step(outer, key_outer)

            slot = ep.open_cache_slot(ns, "root")
            result_fid = await slot.run(step_outer, handles)
            assert isinstance(result_fid, FxtrID)


# ---------------------------------------------------------------------------
# Config entity support
# ---------------------------------------------------------------------------


async def test_config_entity_accessible_in_step(tmp_path: Path):
    """Step can access its config entity via get_step_config_id()."""

    async def configurable_step(ctx: StepContext, x: int) -> str:
        config_id = ctx.get_step_config_id()
        assert config_id is not None
        config = await EntityRef(config_id, ConfigEntity).load()
        return f"{config.setting}:{x}"

    db = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            config_fid = ConfigEntity(setting="mode_a").store().entity_id
            key_id = StepKeyEntity(label="configurable").store().entity_id
            step = ep.bind_external_step(configurable_step, key_id, config_id=config_fid)
            slot = ep.open_cache_slot(ns, "root")
            assert await slot.run(step, 42) == "mode_a:42"


async def test_config_entity_none_when_not_set(tmp_path: Path):
    """get_step_config_id() returns None when no config was bound."""

    async def no_config_step(ctx: StepContext) -> str:
        assert ctx.get_step_config_id() is None
        return "ok"

    db = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="no_config").store().entity_id
            step = ep.bind_external_step(no_config_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            assert await slot.run(step) == "ok"


# ---------------------------------------------------------------------------
# FxtrID in checkpoint save/load
# ---------------------------------------------------------------------------


async def test_fxtrid_in_checkpoint(tmp_path: Path):
    """FxtrID values survive checkpoint save/load across executions."""
    call_count = 0

    async def checkpointing_step(ctx: StepContext) -> str:
        nonlocal call_count
        call_count += 1
        ckpt = await ctx.load_checkpoint()
        if ckpt is not None:
            # Resumed — verify the FxtrID is loadable
            fid = ckpt["entity_id"]
            entity = await EntityRef(fid, LabelEntity).load()
            return entity.label

        fid = LabelEntity(label="checkpointed").store().entity_id
        await ctx.save_checkpoint({"entity_id": fid})
        raise ValueError("simulated crash")

    db = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    # First run — crashes after checkpoint
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="ckpt").store().entity_id
            step = ep.bind_external_step(checkpointing_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            with pytest.raises(BaseException):
                await slot.run(step)

    # Second run — resumes from checkpoint
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="ckpt").store().entity_id
            step = ep.bind_external_step(checkpointing_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(step)
            assert result == "checkpointed"
    assert call_count == 2


# ---------------------------------------------------------------------------
# Provenance parent rejection
# ---------------------------------------------------------------------------


async def test_provenance_parent_rejected_in_result(tmp_path: Path):
    """Step results cannot contain FxtrIDs with provenance parents."""

    async def bad_result_step(ctx: StepContext, fid_with_prov: FxtrID) -> FxtrID:
        return fid_with_prov

    db = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            # Create a FxtrID with a provenance parent
            base = LabelEntity(label="base").store().entity_id
            prov_parent = LabelEntity(label="provenance").store().entity_id
            fid_with_prov = FxtrID(raw_data_id=base._raw_data_id, provenance_parent=prov_parent)
            key_id = StepKeyEntity(label="bad").store().entity_id
            step = ep.bind_external_step(bad_result_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            with pytest.raises(Exception, match="provenance parent"):
                await slot.run(step, fid_with_prov)


# ---------------------------------------------------------------------------
# FxtrID result from cache hit
# ---------------------------------------------------------------------------


async def test_fxtrid_result_available_after_cache_hit(tmp_path: Path):
    """FxtrID results are loadable by the caller after a cache hit."""

    async def create_step(ctx: StepContext) -> FxtrID:
        return LabelEntity(label="cached_entity").store().entity_id

    db = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    # First run — creates and caches
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="create").store().entity_id
            step = ep.bind_external_step(create_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            fid = await slot.run(step)
            assert isinstance(fid, FxtrID)

    # Second run — cache hit, FxtrID should still be usable
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="create").store().entity_id
            step = ep.bind_external_step(create_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            fid = await slot.run(step)
            assert isinstance(fid, FxtrID)


# ---------------------------------------------------------------------------
# FxtrID serialization round-trip in StepOutput
# ---------------------------------------------------------------------------


async def test_fxtrid_in_list_result(tmp_path: Path):
    """FxtrIDs nested in lists survive serialization."""

    async def list_step(ctx: StepContext) -> list:
        a = LabelEntity(label="a").store().entity_id
        b = LabelEntity(label="b").store().entity_id
        return [a, 42, b]

    db = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="list").store().entity_id
            step = ep.bind_external_step(list_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(step)
            assert len(result) == 3
            assert isinstance(result[0], FxtrID)
            assert result[1] == 42
            assert isinstance(result[2], FxtrID)


async def test_fxtrid_in_dict_result(tmp_path: Path):
    """FxtrIDs nested in dicts survive serialization."""

    async def dict_step(ctx: StepContext) -> dict:
        fid = LabelEntity(label="val").store().entity_id
        return {"entity": fid, "count": 5}

    db = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="dict").store().entity_id
            step = ep.bind_external_step(dict_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(step)
            assert isinstance(result["entity"], FxtrID)
            assert result["count"] == 5

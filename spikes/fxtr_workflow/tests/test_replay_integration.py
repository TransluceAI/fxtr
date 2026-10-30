"""Complex multi-level replay check integration tests.

These tests exercise the replay check with deep nesting, many slots, and non-trivial
threading patterns to catch ordering/indexing bugs that only surface at scale.
"""

from typing import Any

import pytest

from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr_workflow.entrypoint import TopLevelEntrypoint
from fxtr_workflow.errors import ReplayCheckError
from fxtr_workflow.step_context import StepContext

from .conftest import StepKeyEntity

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Test #23: Large nested replay with many foreign and internal slots
# ---------------------------------------------------------------------------


async def test_large_nested_replay(db_path: str):
    """Build a scenario with 3 foreign slots (non-alphabetical order), multiple
    internal slots (non-sorted), inner steps that forward foreign subsets, and
    at least one inner step that receives but never dispatches into a foreign slot.

    Sub-assertions:
    (a) Replay with exact same data → cache hit everywhere
    (b) Change data in a used foreign slot → replay fails
    (c) Change data in an unused foreign slot → replay passes
    (d) Change data in an unaccessed subslot → replay passes
    """
    handles: dict[str, Any] = {}

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def inner_uses_z_and_a(ctx: StepContext, slot_z: Any, slot_a: Any) -> int:
        """Dispatches into slot_z.subslot("v") and slot_a.subslot("v")."""
        a = await slot_z.subslot("v").run(handles["produce"], 1)
        b = await slot_a.subslot("v").run(handles["produce"], 2)
        return a + b

    async def inner_uses_only_a(ctx: StepContext, slot_a: Any, slot_m_unused: Any) -> int:
        """Dispatches into slot_a.subslot("v") only. Receives slot_m but never uses it."""
        return await slot_a.subslot("v").run(handles["produce"], 2)

    async def top_step(ctx: StepContext, slot_z: Any, slot_a: Any, slot_m: Any) -> int:
        """Creates internal slots in non-sorted order and dispatches inner steps."""
        # Internal slots in non-sorted order
        w1 = ctx.cache_slot("w")
        w2 = ctx.cache_slot("b")

        r1 = await w1.run(handles["inner_za"], slot_z, slot_a)
        r2 = await w2.run(handles["inner_a_only"], slot_a, slot_m)
        return r1 + r2

    db = db_path

    # --- Setup: fill foreign slots ---
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_integ").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)

            key_za = StepKeyEntity(label="inner_za").store().entity_id
            key_a_only = StepKeyEntity(label="inner_a_only").store().entity_id
            key_top = StepKeyEntity(label="top_step_23").store().entity_id
            handles["inner_za"] = ep.bind_external_step(inner_uses_z_and_a, key_za)
            handles["inner_a_only"] = ep.bind_external_step(inner_uses_only_a, key_a_only)
            handle_top = ep.bind_external_step(top_step, key_top)

            # Create foreign slots in non-alphabetical order
            slot_z = ep.open_cache_slot(ns, "Z")
            slot_a = ep.open_cache_slot(ns, "A_foreign")
            slot_m = ep.open_cache_slot(ns, "M")

            # Fill them: z and a have used data, m has data but is never dispatched into
            await slot_z.subslot("v").run(handles["produce"], 1)
            await slot_a.subslot("v").run(handles["produce"], 2)
            await slot_m.subslot("v").run(handles["produce"], 3)
            # Also add an unaccessed subslot to slot_z
            await slot_z.subslot("other").run(handles["produce"], 99)

            main_slot = ep.open_cache_slot(ns, "MAIN")
            result = await main_slot.run(handle_top, slot_z, slot_a, slot_m)
            # inner_za: 1*10 + 2*10 = 30
            # inner_a_only: 2*10 = 20
            assert result == 50

    # --- (a) Replay with exact same data → cache hit ---
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_integ").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            key_za = StepKeyEntity(label="inner_za").store().entity_id
            key_a_only = StepKeyEntity(label="inner_a_only").store().entity_id
            key_top = StepKeyEntity(label="top_step_23").store().entity_id
            handles["inner_za"] = ep.bind_external_step(inner_uses_z_and_a, key_za)
            handles["inner_a_only"] = ep.bind_external_step(inner_uses_only_a, key_a_only)
            handle_top = ep.bind_external_step(top_step, key_top)

            slot_z = ep.open_cache_slot(ns, "Z")
            slot_a = ep.open_cache_slot(ns, "A_foreign")
            slot_m = ep.open_cache_slot(ns, "M")
            main_slot = ep.open_cache_slot(ns, "MAIN")
            result = await main_slot.run(handle_top, slot_z, slot_a, slot_m)
            assert result == 50  # cached

    # --- (b) Change data in slot_a (used at deepest level) → fails ---
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_integ").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            key_za = StepKeyEntity(label="inner_za").store().entity_id
            key_a_only = StepKeyEntity(label="inner_a_only").store().entity_id
            key_top = StepKeyEntity(label="top_step_23").store().entity_id
            handles["inner_za"] = ep.bind_external_step(inner_uses_z_and_a, key_za)
            handles["inner_a_only"] = ep.bind_external_step(inner_uses_only_a, key_a_only)
            handle_top = ep.bind_external_step(top_step, key_top)

            slot_z = ep.open_cache_slot(ns, "Z")
            # Different data in A_foreign
            slot_a_diff = ep.open_cache_slot(ns, "A_foreign_diff")
            await slot_a_diff.subslot("v").run(handles["produce"], 99)
            slot_m = ep.open_cache_slot(ns, "M")

            main_slot = ep.open_cache_slot(ns, "MAIN")
            pending = main_slot.run(handle_top, slot_z, slot_a_diff, slot_m)
            with pytest.raises(ReplayCheckError):
                await pending

    # --- (c) Change data in slot_m (passed as arg but never dispatched into) → passes ---
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_integ").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            key_za = StepKeyEntity(label="inner_za").store().entity_id
            key_a_only = StepKeyEntity(label="inner_a_only").store().entity_id
            key_top = StepKeyEntity(label="top_step_23").store().entity_id
            handles["inner_za"] = ep.bind_external_step(inner_uses_z_and_a, key_za)
            handles["inner_a_only"] = ep.bind_external_step(inner_uses_only_a, key_a_only)
            handle_top = ep.bind_external_step(top_step, key_top)

            slot_z = ep.open_cache_slot(ns, "Z")
            slot_a = ep.open_cache_slot(ns, "A_foreign")
            # Different M data
            slot_m_diff = ep.open_cache_slot(ns, "M_diff")
            await slot_m_diff.subslot("v").run(handles["produce"], 999)

            main_slot = ep.open_cache_slot(ns, "MAIN")
            result = await main_slot.run(handle_top, slot_z, slot_a, slot_m_diff)
            assert result == 50  # still cached — M was never observed

    # --- (d) Change data in slot_z.subslot("other") (unaccessed) → passes ---
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_integ").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            key_za = StepKeyEntity(label="inner_za").store().entity_id
            key_a_only = StepKeyEntity(label="inner_a_only").store().entity_id
            key_top = StepKeyEntity(label="top_step_23").store().entity_id
            handles["inner_za"] = ep.bind_external_step(inner_uses_z_and_a, key_za)
            handles["inner_a_only"] = ep.bind_external_step(inner_uses_only_a, key_a_only)
            handle_top = ep.bind_external_step(top_step, key_top)

            # Z with same "v" data but different "other" data
            slot_z_diff_other = ep.open_cache_slot(ns, "Z_diff_other")
            await slot_z_diff_other.subslot("v").run(handles["produce"], 1)
            await slot_z_diff_other.subslot("other").run(handles["produce"], 777)

            slot_a = ep.open_cache_slot(ns, "A_foreign")
            slot_m = ep.open_cache_slot(ns, "M")

            main_slot = ep.open_cache_slot(ns, "MAIN")
            result = await main_slot.run(handle_top, slot_z_diff_other, slot_a, slot_m)
            assert result == 50  # still cached — "other" was never accessed


# ---------------------------------------------------------------------------
# Test #24: Selective replay invalidation across nesting levels
# ---------------------------------------------------------------------------


async def test_selective_replay_invalidation(db_path: str):
    """3-level call structure:
    - Top receives slot_x, slot_y, slot_z
    - Dispatches mid_step_1(slot_x, slot_y) into internal "m1"
    - Dispatches mid_step_2(slot_y, slot_z) into internal "m2"
    - mid_step_1 dispatches leaf into slot_x.subslot("v") and slot_y.subslot("v")
    - mid_step_2 dispatches leaf into slot_y.subslot("v") and slot_z.subslot("v")

    Sub-assertions:
    - Same data → passes
    - Change slot_x.subslot("v") → fails (used by mid_1)
    - Change slot_y.subslot("v") → fails (used by both mid_1 and mid_2)
    - Change slot_z.subslot("v") → fails (used by mid_2)
    - Change slot_x.subslot("other") (unaccessed) → passes
    """
    handles: dict[str, Any] = {}

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def mid_step_1(ctx: StepContext, sx: Any, sy: Any) -> int:
        a = await sx.subslot("v").run(handles["produce"], 1)
        b = await sy.subslot("v").run(handles["produce"], 2)
        return a + b

    async def mid_step_2(ctx: StepContext, sy: Any, sz: Any) -> int:
        a = await sy.subslot("v").run(handles["produce"], 2)
        b = await sz.subslot("v").run(handles["produce"], 3)
        return a + b

    async def top_step(ctx: StepContext, sx: Any, sy: Any, sz: Any) -> int:
        r1 = await ctx.cache_slot("m1").run(handles["mid1"], sx, sy)
        r2 = await ctx.cache_slot("m2").run(handles["mid2"], sy, sz)
        return r1 + r2

    def _register(ep):
        key_produce = StepKeyEntity(label="produce_sel").store().entity_id
        handles["produce"] = ep.bind_external_step(produce, key_produce)
        key_mid1 = StepKeyEntity(label="mid1_sel").store().entity_id
        key_mid2 = StepKeyEntity(label="mid2_sel").store().entity_id
        key_top = StepKeyEntity(label="top_sel").store().entity_id
        handles["mid1"] = ep.bind_external_step(mid_step_1, key_mid1)
        handles["mid2"] = ep.bind_external_step(mid_step_2, key_mid2)
        return ep.bind_external_step(top_step, key_top)

    db = db_path

    # --- Initial run: fill slots and cache ---
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            handle_top = _register(ep)

            slot_x = ep.open_cache_slot(ns, "X")
            slot_y = ep.open_cache_slot(ns, "Y")
            slot_z = ep.open_cache_slot(ns, "Z")

            await slot_x.subslot("v").run(handles["produce"], 1)
            await slot_x.subslot("other").run(handles["produce"], 99)  # unaccessed
            await slot_y.subslot("v").run(handles["produce"], 2)
            await slot_z.subslot("v").run(handles["produce"], 3)

            main = ep.open_cache_slot(ns, "MAIN")
            result = await main.run(handle_top, slot_x, slot_y, slot_z)
            # mid1: 1*10 + 2*10 = 30, mid2: 2*10 + 3*10 = 50
            assert result == 80

    # --- Same data → passes ---
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            handle_top = _register(ep)
            slot_x = ep.open_cache_slot(ns, "X")
            slot_y = ep.open_cache_slot(ns, "Y")
            slot_z = ep.open_cache_slot(ns, "Z")
            main = ep.open_cache_slot(ns, "MAIN")
            result = await main.run(handle_top, slot_x, slot_y, slot_z)
            assert result == 80

    # --- Change slot_x.subslot("v") → fails ---
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            handle_top = _register(ep)
            slot_x_diff = ep.open_cache_slot(ns, "X_diff")
            await slot_x_diff.subslot("v").run(handles["produce"], 999)
            slot_y = ep.open_cache_slot(ns, "Y")
            slot_z = ep.open_cache_slot(ns, "Z")
            main = ep.open_cache_slot(ns, "MAIN")
            pending = main.run(handle_top, slot_x_diff, slot_y, slot_z)
            with pytest.raises(ReplayCheckError):
                await pending

    # --- Change slot_y.subslot("v") → fails (used by both mid steps) ---
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            handle_top = _register(ep)
            slot_x = ep.open_cache_slot(ns, "X")
            slot_y_diff = ep.open_cache_slot(ns, "Y_diff")
            await slot_y_diff.subslot("v").run(handles["produce"], 888)
            slot_z = ep.open_cache_slot(ns, "Z")
            main = ep.open_cache_slot(ns, "MAIN")
            pending = main.run(handle_top, slot_x, slot_y_diff, slot_z)
            with pytest.raises(ReplayCheckError):
                await pending

    # --- Change slot_z.subslot("v") → fails ---
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            handle_top = _register(ep)
            slot_x = ep.open_cache_slot(ns, "X")
            slot_y = ep.open_cache_slot(ns, "Y")
            slot_z_diff = ep.open_cache_slot(ns, "Z_diff")
            await slot_z_diff.subslot("v").run(handles["produce"], 777)
            main = ep.open_cache_slot(ns, "MAIN")
            pending = main.run(handle_top, slot_x, slot_y, slot_z_diff)
            with pytest.raises(ReplayCheckError):
                await pending

    # --- Change slot_x.subslot("other") (unaccessed) → passes ---
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            handle_top = _register(ep)
            slot_x_other = ep.open_cache_slot(ns, "X_other_diff")
            await slot_x_other.subslot("v").run(handles["produce"], 1)  # same "v"
            await slot_x_other.subslot("other").run(handles["produce"], 555)  # diff "other"
            slot_y = ep.open_cache_slot(ns, "Y")
            slot_z = ep.open_cache_slot(ns, "Z")
            main = ep.open_cache_slot(ns, "MAIN")
            result = await main.run(handle_top, slot_x_other, slot_y, slot_z)
            assert result == 80  # still cached

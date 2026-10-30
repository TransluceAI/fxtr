"""``validate_on_close`` toggles the closure-validity pass at writer
exit. With ``True`` (default), inconsistent layers fail at exit before
``layer_format.json`` is written; with ``False``, the same layer
finalizes and is left to fail lazily at read time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store import layer_format as fmt
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from tests.test_layered_file_store.conftest import LfsLeaf


@pytest.mark.anyio
async def test_validate_on_close_rejects_dangling_dep(tmp_path: Path):
    """If a data file references a dep that isn't anywhere in this
    layer / parents, the validation pass fails before finalize."""
    layer = tmp_path / "L"

    async with FxtrScope(name="src") as scope:
        ref = scope.store(LfsLeaf(name="x", value=1))
        flat = await scope._main_arena.load_flat(scope.clone_authorization(), ref.entity_id)

        # Manually craft a layer where the data file's deps list points
        # at a fake id that no parent / this layer covers.
        with pytest.raises(ValueError, match="Layer validation failed"):
            async with LayeredFileStoreIsolatedWriter(layer, parents=[]) as w:
                # Write a data file directly, simulating a buggy walk.
                from fxtr2.data.abstract_arena import DepKind, FxtrEntityFlatData

                fake_dep = FxtrID(raw_data_id=b"d" + b"\xee" * 32)
                bogus_flat = FxtrEntityFlatData(
                    entity_id=ref.entity_id,
                    entity_bytes=flat.entity_bytes,
                    deps=[(fake_dep, DepKind.STRONG_REF)],
                )
                fmt.write_data_file(layer, bogus_flat)
                w._pins["x"] = ref.entity_id

    # Validation failed -> layer not finalized.
    assert not (layer / fmt.LAYER_FORMAT_FILE).exists()


@pytest.mark.anyio
async def test_validate_on_close_disabled_lets_it_through(tmp_path: Path):
    """With ``validate_on_close=False``, the same broken layer
    finalizes (the reader-side will surface the missing dep lazily
    when someone tries to load it)."""
    layer = tmp_path / "L"

    async with FxtrScope(name="src") as scope:
        ref = scope.store(LfsLeaf(name="x", value=1))
        flat = await scope._main_arena.load_flat(scope.clone_authorization(), ref.entity_id)

        async with LayeredFileStoreIsolatedWriter(layer, parents=[], validate_on_close=False) as w:
            from fxtr2.data.abstract_arena import DepKind, FxtrEntityFlatData

            fake_dep = FxtrID(raw_data_id=b"d" + b"\xee" * 32)
            bogus_flat = FxtrEntityFlatData(
                entity_id=ref.entity_id,
                entity_bytes=flat.entity_bytes,
                deps=[(fake_dep, DepKind.STRONG_REF)],
            )
            fmt.write_data_file(layer, bogus_flat)
            w._pins["x"] = ref.entity_id

    # Validation skipped -> layer was finalized.
    assert (layer / fmt.LAYER_FORMAT_FILE).exists()


@pytest.mark.anyio
async def test_validate_on_close_accepts_consistent_layer(tmp_path: Path):
    """A normally-built layer passes validation."""
    layer = tmp_path / "L"
    async with FxtrScope(name="src") as scope:
        ref = scope.store(LfsLeaf(name="x", value=1))
        async with LayeredFileStoreIsolatedWriter(layer, parents=[]) as w:
            await w.pin_to_this_layer("x", ref.entity_id, scope)

    assert (layer / fmt.LAYER_FORMAT_FILE).exists()

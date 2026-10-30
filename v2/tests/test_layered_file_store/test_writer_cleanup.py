"""Writer exception during body skips finalization, leaving the output
directory without ``layer_format.json``. A follow-up reader fails
cleanly with ``LayerMalformedError`` (since the version file is
missing).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store import layer_format as fmt
from fxtr2.data.layered_file_store.errors import LayerMalformedError
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from tests.test_layered_file_store.conftest import LfsLeaf


class _BoomError(Exception):
    pass


@pytest.mark.anyio
async def test_exception_in_body_skips_finalize(tmp_path: Path):
    layer = tmp_path / "L"

    with pytest.raises(_BoomError):
        async with FxtrScope(name="src") as scope:
            ref = scope.store(LfsLeaf(name="x", value=1))
            async with LayeredFileStoreIsolatedWriter(layer, parents=[]) as w:
                await w.pin_to_this_layer("x", ref.entity_id, scope)
                raise _BoomError("intentional")

    # `layer_format.json` was never written.
    assert not (layer / fmt.LAYER_FORMAT_FILE).exists()
    # Reader rejects the partial layer cleanly.
    with pytest.raises(LayerMalformedError):
        async with LayeredFileStoreReader(layer):
            pass


@pytest.mark.anyio
async def test_clean_exit_finalizes(tmp_path: Path):
    """Conversely, a clean exit writes pins/parents/format."""
    layer = tmp_path / "L"
    async with FxtrScope(name="src") as scope:
        ref = scope.store(LfsLeaf(name="x", value=2))
        async with LayeredFileStoreIsolatedWriter(layer, parents=[]) as w:
            await w.pin_to_this_layer("x", ref.entity_id, scope)

    assert (layer / fmt.LAYER_FORMAT_FILE).exists()
    assert (layer / fmt.PINS_FILE).exists()
    assert (layer / fmt.PARENTS_FILE).exists()
    assert fmt.read_format_version(layer) == fmt.LAYER_FORMAT_VERSION

"""mkEntityFromPython round-trip: nix-build the layer that writes
py_testing's `sample_type_id` entity, then read it back as a FxtrBasicType
and confirm it matches the locally-computed type id.

Uses the `nix_build` fixture (conftest.py), so run from the colocated
jj↔git worktree.
"""

from __future__ import annotations

from collections.abc import Callable

import anyio

from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr2.data.system_entities import FxtrBasicType
from py_testing.sample_type import PY_TESTING_NS, sample_type_id


def test_entity_from_python_round_trip(nix_build: Callable[[str], str]) -> None:
    layer = nix_build("packages.x86_64-linux.sample-type-entity")

    async def _read() -> None:
        async with LayeredFileStoreReader(layer) as reader:
            ref = await reader.retrieve("entity", FxtrBasicType)
            # The id pinned by the nix build reproduces the one define_type
            # mints locally — content addressing across the boundary.
            assert ref.entity_id == sample_type_id
            entity = await ref.load()
            assert entity.type_name == "py_testing_sample"
            assert entity.namespace_uuid == PY_TESTING_NS

    anyio.run(_read)

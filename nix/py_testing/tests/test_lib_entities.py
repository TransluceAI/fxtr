"""lib.fxtr.entities round-trip: nix-build the typeEntity layer for a couple
of the library type ids and confirm the pinned id_string equals the baked
literal. This proves the `fxtr_lib_type_ids` stub package's
`define_type(namespace, label)` re-declaration is content-identical to the
canonical type — so the light fxtr2-only venv produces the same FxtrID the
heavy `fxtr_workstation_mcp` / `fxtr_report_builder` definitions would.

Uses the `nix_build` fixture (conftest.py), so run from the colocated jj↔git
worktree.
"""

from __future__ import annotations

from collections.abc import Callable

import anyio
import pytest

from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader

# (lib.fxtr.entities.<name>, layerKey, expected id_string literal). One
# fxtr_workstation_mcp-sourced type + one fxtr_report_builder-sourced type.
_CASES = [
    (
        "workstationServicePlaceholder",
        "workstation_service_placeholder",
        "duzfdis2ybod5jav4d3ktwvnuwr2qvkc7o7lddqhe5c72qcwniqca",
    ),
    (
        "rootReport",
        "root_report",
        "du7aezbzspqg4fsl3x5txk6q2vl3zkow6xc3yqofygkz2h5u4zwua",
    ),
]


@pytest.mark.parametrize("name,layer_key,expected", _CASES)
def test_lib_entity_id_matches_literal(
    nix_build: Callable[[str], str], name: str, layer_key: str, expected: str
) -> None:
    layer = nix_build(f"lib.fxtr.entities.{name}.entity.layer")

    async def _read() -> None:
        async with LayeredFileStoreReader(layer) as reader:
            fid = await reader.retrieve(layer_key, into_scope=GLOBAL_SCOPE, copy_into_scope=True)
            assert fid.id_string() == expected

    anyio.run(_read)

"""The sandbox-bridge service-defn dups must stay wire-identical to the
canonical `fxtr_sandbox_types` ones.

The agent constructs `NixWasmServiceDefn` / `NixContainerServiceDefn`
in-sandbox (against the bridge dup), `.store()`s it, and passes it to
`spawn`; the workstation/spawner decodes it against the canonical type.
That only works if both sides agree on the wire form: the same `fxtr_type`
FxtrID **and** the same fields.

`define_type(ns, label)` keys the FxtrID on namespace + label only — *not*
on the fields — so id-string equality alone wouldn't catch a field drift
(matching id, mismatched schema → broken decode). Hence the field-list
assertion too.
"""

from __future__ import annotations

import dataclasses

import pytest

from fxtr_sandbox_types.entities import (
    NixContainerServiceDefn as CanonicalContainer,
)
from fxtr_sandbox_types.entities import (
    NixWasmServiceDefn as CanonicalWasm,
)
from fxtr_ws_mcp_sandbox_bridge.protocols import (
    NixContainerServiceDefn as BridgeContainer,
)
from fxtr_ws_mcp_sandbox_bridge.protocols import (
    NixWasmServiceDefn as BridgeWasm,
)

pytestmark = pytest.mark.anyio

_PAIRS = [
    pytest.param(CanonicalWasm, BridgeWasm, id="wasm"),
    pytest.param(CanonicalContainer, BridgeContainer, id="container"),
]


@pytest.mark.parametrize(("canonical", "bridge_dup"), _PAIRS)
async def test_same_fxtr_type_id(canonical, bridge_dup) -> None:
    assert bridge_dup.fxtr_type.id_string() == canonical.fxtr_type.id_string()


@pytest.mark.parametrize(("canonical", "bridge_dup"), _PAIRS)
async def test_same_fields(canonical, bridge_dup) -> None:
    assert [f.name for f in dataclasses.fields(bridge_dup)] == [
        f.name for f in dataclasses.fields(canonical)
    ]


@pytest.mark.parametrize(("canonical", "bridge_dup"), _PAIRS)
async def test_flake_path_optional_both(canonical, bridge_dup) -> None:
    assert canonical(git_commit="a", flake_attr="b").flake_path is None
    assert bridge_dup(git_commit="a", flake_attr="b").flake_path is None


async def test_wasm_and_container_ids_distinct() -> None:
    """The two defn kinds are different wire types (different labels)."""
    assert CanonicalWasm.fxtr_type.id_string() != CanonicalContainer.fxtr_type.id_string()

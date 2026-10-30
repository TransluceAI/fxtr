"""Unit tests for the kind-agnostic `NixServiceDefn` dispatch mapping.

The nix-free half of the auto arm: `_concrete_defn_for_kind` maps a
declared `kind` to the concrete defn the runner arms dispatch on, and
refuses unknown kinds. The end-to-end auto spawn (kind eval + cache +
arm dispatch) is `test_nix_spawner.py::test_spawn_auto_kind_service`.
"""

from __future__ import annotations

import pytest
from fxtr_sandbox_types import (
    NixContainerServiceDefn,
    NixPyodideServiceDefn,
    NixServiceDefn,
    NixWasmServiceDefn,
)
from fxtr_sandboxing.spawner import _concrete_defn_for_kind

_DEFN = NixServiceDefn(git_commit="0" * 40, flake_attr="fxtr.services.thing", flake_path="svc")


@pytest.mark.parametrize(
    ("kind", "defn_cls"),
    [
        ("wasm", NixWasmServiceDefn),
        ("container", NixContainerServiceDefn),
        ("pyodide", NixPyodideServiceDefn),
    ],
)
def test_concrete_defn_for_kind_maps_triple(kind: str, defn_cls: type) -> None:
    concrete = _concrete_defn_for_kind(_DEFN, kind)
    assert type(concrete) is defn_cls
    assert (concrete.git_commit, concrete.flake_attr, concrete.flake_path) == (
        _DEFN.git_commit,
        _DEFN.flake_attr,
        _DEFN.flake_path,
    )


def test_concrete_defn_for_kind_unknown_kind_raises() -> None:
    with pytest.raises(RuntimeError, match=r"unknown kind 'oci'.*container, pyodide, wasm"):
        _concrete_defn_for_kind(_DEFN, "oci")

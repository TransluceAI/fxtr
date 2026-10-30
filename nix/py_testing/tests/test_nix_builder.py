"""Round-trip tests over the ``mkFxtrFnBuilder`` / ``mkFxtrScriptBuilder``
builder targets: nix-build a layer whose builder reads a parent layer's
``InputNote`` and pins a derived ``WrappedNote``, then read it back with a fxtr2
reader and confirm the wrapped note + its inner reference (resolved through the
parent layer) survived.

Covers both faces of the builder primitive (fn + script form) over one shared
venv; both are pure (no remote). Plus the provenanced (input-addressed) builder:
a default build-step provenance_context minted on the builder scope, read back as
a layered id whose c-node anchors on the layer's ``$out`` + carries the declared
``provenanceInfo``; and the two eval-time throw paths (provenanceInfo without
provenanced; a provenanced fixed-output build with no provenanceInfo). The
`nix_build` fixture (conftest.py) builds via the harness-resolved git flake ref.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import anyio
import pytest

from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.entity_core import RawFxtrEntityMarkerID
from fxtr2.data.layered_file_store.nix_build_provenance import NixInputAddressedBuildProvenance
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from py_testing.builder_fixture import WrappedNote


async def _read_wrapped(layer: str, expected_label: str) -> None:
    async with LayeredFileStoreReader(layer) as reader:
        wrapped = await (await reader.retrieve("wrapped", WrappedNote)).load()
        assert wrapped.label == expected_label
        # The inner InputNote lives in the parent layer (recorded in the output
        # layer's parents.json); loading it walks there.
        inner = await wrapped.inner.load()
        assert inner.text == "from-parent"


@pytest.fixture(scope="module")
def fn_builder_layer(nix_build: Callable[[str], str]) -> str:
    return nix_build("packages.x86_64-linux.fxtr-fn-builder-check")


@pytest.fixture(scope="module")
def script_builder_layer(nix_build: Callable[[str], str]) -> str:
    return nix_build("packages.x86_64-linux.fxtr-script-builder-check")


def test_fn_builder_reads_parent_and_pins_derived(fn_builder_layer: str) -> None:
    anyio.run(_read_wrapped, fn_builder_layer, "fn")


def test_script_builder_reads_parent_and_pins_derived(script_builder_layer: str) -> None:
    anyio.run(_read_wrapped, script_builder_layer, "script")


async def _read_provenanced(layer: str) -> None:
    """Read the provenanced builder's ``wrapped`` pin back as a layered id and
    confirm the build c-node is a ``NixInputAddressedBuildProvenance`` anchored on
    this layer's ``$out`` (== ``layer``) and carrying the declared info. The auto
    path wraps the data in an n-node whose ``$fxtr.ctx`` marker is the build
    c-node; hop through it to the c-node's d-node payload."""
    async with LayeredFileStoreReader(layer) as reader:
        ref = await reader.retrieve("wrapped", WrappedNote)
        n_node = ref.entity_id._provenance_parent
        assert n_node is not None and n_node.get_tag() == "n"
        n_cbor = await reader.scope._main_arena.load(reader.scope.clone_authorization(), n_node)
        assert isinstance(n_cbor, dict)
        marker = n_cbor["$fxtr.ctx"]
        assert isinstance(marker, RawFxtrEntityMarkerID)
        c_node = marker.inner_id
        assert c_node.get_tag() == "c"
        d_id = reader.scope.c_node_to_context_data(c_node)
        prov = await reader.scope.load(EntityRef(d_id, NixInputAddressedBuildProvenance))
        # $out is exactly the realized layer store path.
        assert prov.output_store_path == layer
        assert prov.provenance_info == {"desc": "py_testing provenanced builder check"}
        # The underlying wrapped note (and its parent-layer inner ref) still resolve.
        wrapped = await ref.load()
        assert wrapped.label == "provenanced"
        assert (await wrapped.inner.load()).text == "from-parent"


@pytest.fixture(scope="module")
def provenanced_builder_layer(nix_build: Callable[[str], str]) -> str:
    return nix_build("packages.x86_64-linux.fxtr-provenanced-fn-builder-check")


def test_provenanced_builder_anchors_on_out_and_carries_info(
    provenanced_builder_layer: str,
) -> None:
    anyio.run(_read_provenanced, provenanced_builder_layer)


def test_provenance_info_requires_provenanced_throws(flake_ref: Callable[[str], str]) -> None:
    # Build directly (not via nix_build) so we can read stderr.
    result = subprocess.run(
        [
            "nix",
            "build",
            "--no-link",
            flake_ref("packages.x86_64-linux.provenance-info-requires-provenanced-check"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "expected provenanceInfo-without-provenanced to fail the build"
    assert "provenanceInfo is only allowed when provenanced" in result.stderr


def test_provenanced_fod_requires_info_throws(flake_ref: Callable[[str], str]) -> None:
    result = subprocess.run(
        [
            "nix",
            "build",
            "--no-link",
            flake_ref("packages.x86_64-linux.provenanced-fod-requires-info-check"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "expected a provenanced FOD with no provenanceInfo to fail"
    assert "fixed-output (FOD) builder has no safe $out anchor" in result.stderr

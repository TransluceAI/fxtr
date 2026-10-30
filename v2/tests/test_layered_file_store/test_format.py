"""Tests for the on-disk format helpers and writer's "non-empty output
directory" guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store import layer_format as fmt
from fxtr2.data.layered_file_store.errors import (
    LayerFormatVersionError,
    LayerMalformedError,
    LayerOutputDirNotEmptyError,
)
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from tests.test_layered_file_store.conftest import LfsLeaf

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _write_simple_layer(layer_dir: Path) -> dict[str, FxtrID]:
    """Build a one-entity layer at ``layer_dir`` and return the pins dict."""
    async with FxtrScope(name="src") as scope:
        ref = scope.store(LfsLeaf(name="hello", value=42))

        async with LayeredFileStoreIsolatedWriter(layer_dir, parents=[]) as writer:
            await writer.pin_to_this_layer("hello", ref.entity_id, scope)
        return {"hello": ref.entity_id}


# ---------------------------------------------------------------------------
# Format constants + round-trip
# ---------------------------------------------------------------------------


def test_format_version_constant():
    assert fmt.LAYER_FORMAT_VERSION == 1


def test_pins_roundtrip(tmp_path: Path):
    fxtr_id = FxtrID(raw_data_id=b"d" + (b"\x00" * 32))
    fmt.write_pins(tmp_path, {"a": fxtr_id, "ns/b": fxtr_id})
    loaded = fmt.read_pins(tmp_path)
    assert loaded == {"a": fxtr_id, "ns/b": fxtr_id}


def test_pins_layered_roundtrip(tmp_path: Path):
    """A layered pin id serializes to a ``[data, prov]`` array and reads back
    layered; a bare pin stays a plain string. (FxtrID equality is
    provenance-blind, so check both halves explicitly.)"""
    data = FxtrID(raw_data_id=b"d" + (b"\x11" * 32))
    # A valid u-node id: a `u` tag wrapping a bare d-id (its payload is that id).
    prov = FxtrID(raw_data_id=b"u" + b"d" + (b"\x22" * 32))
    layered = FxtrID(raw_data_id=data._raw_data_id, provenance_parent=prov)
    bare = FxtrID(raw_data_id=b"d" + (b"\x33" * 32))

    fmt.write_pins(tmp_path, {"layered": layered, "bare": bare})
    # On disk: the layered pin is a 2-element array, the bare pin a string.
    body = json.loads((tmp_path / fmt.PINS_FILE).read_text())
    assert body["layered"] == [data.id_string(), prov.id_string()]
    assert body["bare"] == bare.id_string()

    loaded = fmt.read_pins(tmp_path)
    assert loaded["layered"]._raw_data_id == data._raw_data_id
    assert loaded["layered"]._provenance_parent == prov
    assert loaded["bare"]._provenance_parent is None


def test_read_pins_bad_pair_is_malformed(tmp_path: Path):
    (tmp_path / fmt.PINS_FILE).write_text(json.dumps({"x": ["only-one-element"]}))
    with pytest.raises(LayerMalformedError):
        fmt.read_pins(tmp_path)


def test_parents_roundtrip(tmp_path: Path):
    parents = [tmp_path / "p1", tmp_path / "p2"]
    fmt.write_parents(tmp_path, parents)
    loaded = fmt.read_parents(tmp_path)
    assert loaded == parents


def test_format_version_roundtrip(tmp_path: Path):
    fmt.write_layer_format(tmp_path)
    assert fmt.read_format_version(tmp_path) == fmt.LAYER_FORMAT_VERSION


def test_format_provenanced_flag_roundtrip(tmp_path: Path):
    # Default (bare): flag absent on disk, reads back False.
    fmt.write_layer_format(tmp_path)
    body = json.loads((tmp_path / fmt.LAYER_FORMAT_FILE).read_text())
    assert "provenanced" not in body
    assert fmt.read_layer_format(tmp_path).provenanced is False

    # Provenanced: flag written, reads back True.
    fmt.write_layer_format(tmp_path, provenanced=True)
    fmt_info = fmt.read_layer_format(tmp_path)
    assert fmt_info.version == fmt.LAYER_FORMAT_VERSION
    assert fmt_info.provenanced is True


def test_read_format_bad_provenanced_is_malformed(tmp_path: Path):
    (tmp_path / fmt.LAYER_FORMAT_FILE).write_text(
        json.dumps({"version": fmt.LAYER_FORMAT_VERSION, "provenanced": "yes"})
    )
    with pytest.raises(LayerMalformedError):
        fmt.read_layer_format(tmp_path)


# ---------------------------------------------------------------------------
# Strict-on-missing
# ---------------------------------------------------------------------------


def test_read_pins_missing_is_malformed(tmp_path: Path):
    with pytest.raises(LayerMalformedError):
        fmt.read_pins(tmp_path)


def test_read_parents_missing_is_malformed(tmp_path: Path):
    with pytest.raises(LayerMalformedError):
        fmt.read_parents(tmp_path)


def test_read_format_missing_is_malformed(tmp_path: Path):
    with pytest.raises(LayerMalformedError):
        fmt.read_format_version(tmp_path)


def test_read_format_wrong_version_raises(tmp_path: Path):
    (tmp_path / fmt.LAYER_FORMAT_FILE).write_text(json.dumps({"version": 999}))
    with pytest.raises(LayerFormatVersionError) as exc_info:
        fmt.read_format_version(tmp_path)
    assert exc_info.value.version == 999


# ---------------------------------------------------------------------------
# Data + precomp roundtrips (via a real scope)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_data_file_roundtrip(tmp_path: Path):
    """Write a layer via the writer, then read its data file back via
    the format helpers."""
    layer = tmp_path / "L"
    pins = await _write_simple_layer(layer)
    hello_id = pins["hello"]

    flat = fmt.read_data_file_flat(layer, hello_id)
    assert flat is not None
    assert flat.entity_id == hello_id
    assert flat.entity_bytes is not None and len(flat.entity_bytes) > 0


@pytest.mark.anyio
async def test_data_file_missing_returns_none(tmp_path: Path):
    layer = tmp_path / "L"
    await _write_simple_layer(layer)
    # An ID we never wrote.
    fake = FxtrID(raw_data_id=b"d" + b"\xff" * 32)
    assert fmt.read_data_file_flat(layer, fake) is None
    assert not fmt.data_file_exists(layer, fake)


def test_precomp_file_roundtrip(tmp_path: Path):
    fid = FxtrID(raw_data_id=b"c" + b"\x00" * 32)
    target = FxtrID(raw_data_id=b"d" + b"\x01" * 32)
    (tmp_path / fmt.PRECOMP_DIR).mkdir()
    entry: fmt.PrecompEntry = {"unwraps_to": target.id_string()}
    fmt.write_precomp_file(tmp_path, fid, entry)
    assert fmt.precomp_file_exists(tmp_path, fid)
    loaded = fmt.read_precomp_file(tmp_path, fid)
    assert loaded == entry


def test_precomp_file_missing_returns_none(tmp_path: Path):
    fake = FxtrID(raw_data_id=b"c" + b"\xff" * 32)
    assert fmt.read_precomp_file(tmp_path, fake) is None
    assert not fmt.precomp_file_exists(tmp_path, fake)


# ---------------------------------------------------------------------------
# Writer enforces empty output dir
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_writer_rejects_nonempty_output(tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "leftover.txt").write_text("hi")
    with pytest.raises(LayerOutputDirNotEmptyError):
        async with LayeredFileStoreIsolatedWriter(out, parents=[]):
            pass

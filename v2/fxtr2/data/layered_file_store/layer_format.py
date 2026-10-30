"""On-disk format constants and codec for the layered file entity store.

Each layer directory has the following layout::

    <layer_dir>/
    ├── layer_format.json     # {"version": 1[, "provenanced": true]}
    ├── pins.json             # {"<pin_name>": "<id_string>" | ["<data_id>", "<prov_id>"], ...}
    ├── parents.json          # ["<absolute_path>", ...]
    ├── data/
    │   └── <id_string>.cbor  # FxtrEntityFlatData payload
    └── precomp/
        └── <id_string>.json  # {"unwraps_to": ..., "ctx_node": ..., "backstep": ...}

The pins.json / parents.json files are **required** (with ``{}`` / ``[]``
for an empty layer) — a missing file is a malformed layer.

A pin value is either a plain ``id_string`` (a **bare** pin → a bare data id)
or a 2-element ``[data_id, prov_id]`` array (a **provenanced** pin → a layered
``FxtrID(data, _provenance_parent=prov)``). The ``[data, prov]`` form lets a
reader return the layered id directly — e.g. ``list_pins`` as a pure read,
without re-deriving the data half. ``layer_format.json`` additionally carries an
optional ``provenanced`` flag (writer-side mode + self-description), written
only when ``True`` (absent ⇒ bare) so pre-flag layers stay readable unchanged.

This module provides:

- File-name and directory-name constants
- ``LAYER_FORMAT_VERSION`` (the version this codebase produces)
- Sync read/write helpers for each metadata file, with atomic
  tmp+rename semantics on writes

The CBOR-encoded entity flat data uses the same wire format as
``fxtr2.data.remote.transfer_format`` (a 1-element entity stream).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fxtr2.data.abstract_arena import FxtrEntityFlatData
from fxtr2.data.arena_util import PrecompEntry
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.layered_file_store.errors import LayerFormatVersionError, LayerMalformedError
from fxtr2.data.remote.transfer_format import decode_entity_stream, encode_entity_stream

# ``PrecompEntry`` is defined in ``fxtr2.data.arena_util`` (alongside the
# closure walker that produces it). Re-exported here for convenience.
__all__ = ["PrecompEntry"]

# ------------------------------------------------------------
# Constants
# ------------------------------------------------------------

LAYER_FORMAT_VERSION = 1

LAYER_FORMAT_FILE = "layer_format.json"
PINS_FILE = "pins.json"
PARENTS_FILE = "parents.json"
DATA_DIR = "data"
PRECOMP_DIR = "precomp"

DATA_FILE_SUFFIX = ".cbor"
PRECOMP_FILE_SUFFIX = ".json"


# ------------------------------------------------------------
# Path helpers
# ------------------------------------------------------------


def data_file_path(layer_dir: Path, fxtr_id: FxtrID) -> Path:
    return layer_dir / DATA_DIR / f"{fxtr_id.id_string()}{DATA_FILE_SUFFIX}"


def precomp_file_path(layer_dir: Path, fxtr_id: FxtrID) -> Path:
    return layer_dir / PRECOMP_DIR / f"{fxtr_id.id_string()}{PRECOMP_FILE_SUFFIX}"


# ------------------------------------------------------------
# Atomic write helper
# ------------------------------------------------------------


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write ``data`` to ``target`` atomically via tmp + rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _atomic_write_text(target: Path, text: str) -> None:
    _atomic_write_bytes(target, text.encode("utf-8"))


# ------------------------------------------------------------
# layer_format.json
# ------------------------------------------------------------


@dataclass(frozen=True)
class LayerFormat:
    """Parsed ``layer_format.json``: the format version plus whether the
    layer is **provenanced** (its ``pins.json`` values are ``[data, prov]``
    pairs rather than plain bare-data id strings). ``provenanced`` defaults to
    ``False`` for layers written before the flag existed (absent key ⇒ bare),
    so old layers stay readable unchanged."""

    version: int
    provenanced: bool


def read_layer_format(layer_dir: Path) -> LayerFormat:
    """Read and validate ``layer_format.json``. Raises
    ``LayerFormatVersionError`` if the version isn't supported, or
    ``LayerMalformedError`` if the file is missing / malformed. A missing
    ``provenanced`` key reads as ``False`` (back-compat)."""
    path = layer_dir / LAYER_FORMAT_FILE
    if not path.is_file():
        raise LayerMalformedError(f"{path} missing")
    try:
        body = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise LayerMalformedError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(body, dict) or "version" not in body:
        raise LayerMalformedError(f"{path} missing 'version' key")
    version = body["version"]
    if not isinstance(version, int):
        raise LayerMalformedError(f"{path} 'version' is not an integer")
    if version != LAYER_FORMAT_VERSION:
        raise LayerFormatVersionError(version)
    provenanced = body.get("provenanced", False)
    if not isinstance(provenanced, bool):
        raise LayerMalformedError(f"{path} 'provenanced' is not a boolean")
    return LayerFormat(version=version, provenanced=provenanced)


def read_format_version(layer_dir: Path) -> int:
    """Read and validate the format version. Thin wrapper over
    :func:`read_layer_format` for callers that only need the version."""
    return read_layer_format(layer_dir).version


def write_layer_format(layer_dir: Path, *, provenanced: bool = False) -> None:
    """Atomically write ``layer_format.json`` (the current version + the
    ``provenanced`` flag). Pairs with :func:`read_layer_format`.

    The ``provenanced`` flag is written only when ``True``, so a bare
    layer's format file stays byte-identical to a pre-flag layer.
    """
    body: dict[str, object] = {"version": LAYER_FORMAT_VERSION}
    if provenanced:
        body["provenanced"] = True
    _atomic_write_text(
        layer_dir / LAYER_FORMAT_FILE,
        json.dumps(body, indent=2) + "\n",
    )


# ------------------------------------------------------------
# pins.json
# ------------------------------------------------------------


def _parse_pin_id(path: Path, name: str, value: object) -> FxtrID:
    """Parse one ``pins.json`` value into a (possibly layered) ``FxtrID``.

    A **bare** pin is a plain ``id_string`` (a bare data id). A **provenanced**
    pin is a 2-element ``[data_id, prov_id]`` array → a layered ``FxtrID(data,
    _provenance_parent=prov)``, so a reader can return the layered id without
    re-deriving the data half (e.g. ``list_pins`` as a pure read)."""

    def _one(raw: object, what: str) -> FxtrID:
        if not isinstance(raw, str):
            raise LayerMalformedError(f"{path} pin {name!r} {what} is not a string")
        try:
            return FxtrID.from_id_string(raw)
        except (ValueError, KeyError) as exc:
            raise LayerMalformedError(
                f"{path} pin {name!r} {what} {raw!r} is not a valid FxtrID: {exc}"
            ) from exc

    if isinstance(value, str):
        return _one(value, "value")
    if isinstance(value, list):
        if len(value) != 2:
            raise LayerMalformedError(
                f"{path} pin {name!r} array value must be [data_id, prov_id], got {value!r}"
            )
        data = _one(value[0], "data id")
        prov = _one(value[1], "prov id")
        return FxtrID(raw_data_id=data._raw_data_id, provenance_parent=prov)
    raise LayerMalformedError(f"{path} pin {name!r} value is not a string or [data, prov] array")


def read_pins(layer_dir: Path) -> dict[str, FxtrID]:
    """Read ``pins.json``. Required — missing is malformed.

    A bare pin reads back as a bare ``FxtrID``; a provenanced pin (a ``[data,
    prov]`` array) reads back as a layered ``FxtrID``."""
    path = layer_dir / PINS_FILE
    if not path.is_file():
        raise LayerMalformedError(f"{path} missing (required)")
    try:
        body = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise LayerMalformedError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise LayerMalformedError(f"{path} is not a JSON object")
    pins: dict[str, FxtrID] = {}
    for name, value in body.items():
        if not isinstance(name, str):
            raise LayerMalformedError(f"{path} has non-string pin name {name!r}")
        pins[name] = _parse_pin_id(path, name, value)
    return pins


def write_pins(layer_dir: Path, pins: dict[str, FxtrID]) -> None:
    """Atomically write ``pins.json``.

    A bare pin id serializes to a plain ``id_string``; a layered pin id (one
    carrying a ``_provenance_parent``) serializes to a ``[data_id, prov_id]``
    array (both bare strings), so the layered form round-trips."""

    def _encode(fid: FxtrID) -> str | list[str]:
        if fid._provenance_parent is None:
            return fid.id_string()
        return [
            FxtrID(raw_data_id=fid._raw_data_id).id_string(),
            fid._provenance_parent.id_string(),
        ]

    body = {name: _encode(fid) for name, fid in pins.items()}
    _atomic_write_text(
        layer_dir / PINS_FILE,
        json.dumps(body, indent=2, sort_keys=True) + "\n",
    )


# ------------------------------------------------------------
# parents.json
# ------------------------------------------------------------


def read_parents(layer_dir: Path) -> list[Path]:
    """Read ``parents.json``. Required — missing is malformed. Returns
    absolute paths (caller is responsible for canonicalization if
    needed)."""
    path = layer_dir / PARENTS_FILE
    if not path.is_file():
        raise LayerMalformedError(f"{path} missing (required)")
    try:
        body = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise LayerMalformedError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(body, list):
        raise LayerMalformedError(f"{path} is not a JSON array")
    parents: list[Path] = []
    for entry in body:
        if not isinstance(entry, str):
            raise LayerMalformedError(f"{path} contains non-string entry {entry!r}")
        parents.append(Path(entry))
    return parents


def write_parents(layer_dir: Path, parents: list[Path]) -> None:
    """Atomically write ``parents.json``."""
    _atomic_write_text(
        layer_dir / PARENTS_FILE,
        json.dumps([str(p) for p in parents], indent=2) + "\n",
    )


# ------------------------------------------------------------
# data/<id_string>.cbor
# ------------------------------------------------------------


def read_data_file_flat(layer_dir: Path, fxtr_id: FxtrID) -> FxtrEntityFlatData | None:
    """Read ``data/<id>.cbor``. Returns ``None`` if the file doesn't exist."""
    path = data_file_path(layer_dir, fxtr_id)
    if not path.is_file():
        return None
    raw = path.read_bytes()
    entities = decode_entity_stream(raw)
    if len(entities) != 1:
        raise LayerMalformedError(f"{path} should encode exactly one entity, got {len(entities)}")
    flat = entities[0]
    if flat.entity_id != fxtr_id:
        raise LayerMalformedError(
            f"{path} encodes entity {flat.entity_id} but filename claims {fxtr_id}"
        )
    return flat


def write_data_file(layer_dir: Path, flat: FxtrEntityFlatData) -> None:
    """Atomically write ``data/<id>.cbor``."""
    path = data_file_path(layer_dir, flat.entity_id)
    payload = encode_entity_stream([flat])
    _atomic_write_bytes(path, payload)


def data_file_exists(layer_dir: Path, fxtr_id: FxtrID) -> bool:
    return data_file_path(layer_dir, fxtr_id).is_file()


# ------------------------------------------------------------
# precomp/<id_string>.json
# ------------------------------------------------------------


def read_precomp_file(layer_dir: Path, fxtr_id: FxtrID) -> PrecompEntry | None:
    """Read ``precomp/<id>.json``. Returns ``None`` if the file doesn't exist."""
    path = precomp_file_path(layer_dir, fxtr_id)
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise LayerMalformedError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise LayerMalformedError(f"{path} is not a JSON object")
    entry: PrecompEntry = {}
    for key in ("unwraps_to", "ctx_node", "backstep"):
        if key in body:
            value = body[key]
            if not isinstance(value, str):
                raise LayerMalformedError(f"{path} key {key!r} is not a string")
            entry[key] = value  # type: ignore[literal-required]
    return entry


def write_precomp_file(layer_dir: Path, fxtr_id: FxtrID, entry: PrecompEntry) -> None:
    """Atomically write ``precomp/<id>.json``."""
    path = precomp_file_path(layer_dir, fxtr_id)
    _atomic_write_text(path, json.dumps(entry, indent=2, sort_keys=True) + "\n")


def precomp_file_exists(layer_dir: Path, fxtr_id: FxtrID) -> bool:
    return precomp_file_path(layer_dir, fxtr_id).is_file()


def delete_precomp_file(layer_dir: Path, fxtr_id: FxtrID) -> None:
    """Delete ``precomp/<id>.json`` if it exists. No-op otherwise."""
    path = precomp_file_path(layer_dir, fxtr_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ------------------------------------------------------------
# data/ directory listing
# ------------------------------------------------------------


def iter_data_file_ids(layer_dir: Path):
    """Yield every FxtrID present as a file under ``data/``."""
    data_dir = layer_dir / DATA_DIR
    if not data_dir.is_dir():
        return
    for entry in data_dir.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if not name.endswith(DATA_FILE_SUFFIX):
            continue
        id_string = name[: -len(DATA_FILE_SUFFIX)]
        try:
            yield FxtrID.from_id_string(id_string)
        except (ValueError, KeyError):
            continue

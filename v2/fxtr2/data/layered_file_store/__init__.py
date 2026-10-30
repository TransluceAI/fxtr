"""Layered file entity store.

A filesystem convention for representing "a layer of new fxtr entities on
top of a stack of parent layers, with anything else assumed to live in a
runtime-configured remote entity store."

Main API:

- ``LayeredFileStoreReader``: read-only ``FxtrReadNamespace`` over a layer
  directory and its declared parent stack.
- ``LayeredFileStoreIsolatedWriter``: write-only async CM that emits a
  fresh layer directory on top of the given parent stack.

See ``plans/phase3/stage11_nix_layers/part1_layered_file_store.md`` for
the full design.
"""

from fxtr2.data.layered_file_store.errors import (
    LayerEntityMissingError,
    LayerFormatVersionError,
    LayerMalformedError,
    LayerOutputDirNotEmptyError,
    LayerRemoteNotConfiguredError,
)
from fxtr2.data.layered_file_store.layer_format import LAYER_FORMAT_VERSION, PrecompEntry
from fxtr2.data.layered_file_store.pin_arena import (
    LayeredStoreDeepFetchable,
    arena_over_search_set,
    open_seed_arena,
    resolve_pin,
)
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr2.data.layered_file_store.search import ParentPathRemap
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter

__all__ = [
    # Main API
    "LayeredFileStoreReader",
    "LayeredFileStoreIsolatedWriter",
    "ParentPathRemap",
    # Pin-arena seam (return-an-arena, caller mounts + owns lifetime)
    "arena_over_search_set",
    "resolve_pin",
    "open_seed_arena",
    "LayeredStoreDeepFetchable",
    # Format
    "LAYER_FORMAT_VERSION",
    "PrecompEntry",
    # Errors
    "LayerEntityMissingError",
    "LayerFormatVersionError",
    "LayerMalformedError",
    "LayerOutputDirNotEmptyError",
    "LayerRemoteNotConfiguredError",
]

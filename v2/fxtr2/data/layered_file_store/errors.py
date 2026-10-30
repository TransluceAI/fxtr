"""Error types for the layered file entity store."""

from __future__ import annotations

from pathlib import Path

from fxtr2.data.entity_core import FxtrID


class LayerFormatVersionError(Exception):
    """Reader hit a layer_format.json with a version it doesn't understand."""

    def __init__(self, version: int):
        super().__init__(f"Unsupported layered_file_store layer format version: {version}")
        self.version = version


class LayerRemoteNotConfiguredError(Exception):
    """A load through ``LayeredFileArena`` needed the remote DeepFetchable but
    no ``(DeepFetchable, Lease)`` pair was configured."""

    def __init__(self, fxtr_id: FxtrID):
        super().__init__(
            f"Entity {fxtr_id} is marked remote in this layered store, but no"
            " remote DeepFetchable was configured."
        )
        self.fxtr_id = fxtr_id


class LayerEntityMissingError(Exception):
    """An entity referenced by this layer (or a parent) is genuinely missing:
    not in any local data folder, no precomp file, or precomp exists but the
    configured remote returned ``None`` from ``try_lookup``."""

    def __init__(self, fxtr_id: FxtrID):
        super().__init__(
            f"Entity {fxtr_id} is not present in this layered store or any" " configured remote."
        )
        self.fxtr_id = fxtr_id


class LayerOutputDirNotEmptyError(Exception):
    """``LayeredFileStoreIsolatedWriter`` found a non-empty output directory."""

    def __init__(self, path: Path):
        super().__init__(
            f"Output directory {path} is not empty; layered store writers"
            " require an empty (or non-existent) output directory."
        )
        self.path = path


class LayerMalformedError(Exception):
    """A layer directory is missing required metadata files or contains a
    malformed JSON body."""

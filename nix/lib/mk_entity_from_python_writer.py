"""Standalone writer for lib.fxtr.mkEntityFromPython.

Runs in the *caller-supplied* venv (which provides fxtr2 and the module
named by ``fromPython``) — deliberately separate from the fxtr_nix_writer
console script, which uses the lib-internal venv. Reads ``fromPython`` and
``layerKey`` from the structured-attrs JSON (``$NIX_ATTRS_JSON_FILE``) and
``$out`` for the output directory, resolves the named object to an entity
id, and pins it into a fresh single-layer store.
"""

from __future__ import annotations

import importlib
import json
import os
from typing import Any

import anyio

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, Entity, EntityRef
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter


def _resolve(spec: str) -> Any:
    """Resolve a ``module:attr`` / ``module:Class.attr`` spec to the object."""
    module_name, sep, attr_path = spec.partition(":")
    if not sep or not attr_path:
        raise SystemExit(f"fromPython must be 'module:attr', got {spec!r}")
    obj: Any = importlib.import_module(module_name)
    for part in attr_path.split("."):
        obj = getattr(obj, part)
    return obj


def _entity_id(obj: Any) -> FxtrID:
    if isinstance(obj, FxtrID):
        return obj
    if isinstance(obj, EntityRef):
        return obj.entity_id
    if isinstance(obj, Entity):
        return obj.store().entity_id
    raise SystemExit(f"fromPython must resolve to a FxtrID / EntityRef / Entity, got {type(obj)!r}")


async def _main(out_dir: str, layer_key: str, from_python: str) -> None:
    # Resolving the spec imports its module, which runs define_type / stores
    # the entity into GLOBAL_SCOPE; pin walks GLOBAL_SCOPE to find it.
    entity_id = _entity_id(_resolve(from_python))
    async with LayeredFileStoreIsolatedWriter(out_dir, parents=[]) as writer:
        await writer.pin_to_this_layer(layer_key, entity_id, GLOBAL_SCOPE)


def main() -> None:
    with open(os.environ["NIX_ATTRS_JSON_FILE"]) as f:
        attrs = json.load(f)
    anyio.run(_main, os.environ["out"], attrs["layerKey"], attrs["fromPython"])


if __name__ == "__main__":
    main()

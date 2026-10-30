"""``fxtr-nix-writer`` console-script entry point.

Invoked inside a nix derivation by ``nix/lib/runFxtrWriter.nix``,
which hands over the writer spec via ``__structuredAttrs``. Reads the spec
(the ``writerSpec`` attr) from the structured-attrs JSON at
``$NIX_ATTRS_JSON_FILE`` and the output directory from ``$out`` (set by
nix), then dispatches on the spec's ``kind`` to the matching writer in
``writers``."""

from __future__ import annotations

import json
import os

import anyio

from fxtr_nix_writer import writers

# Spec `kind` -> async writer(out_dir, spec). New writer kinds register here.
_WRITERS = {
    "hello_world": writers.write_hello_world,
    "namespace_export": writers.write_namespace_export,
    "entity_from_tsv_pin": writers.write_entity_from_tsv_pin,
}


def _load_writer_spec() -> dict:
    with open(os.environ["NIX_ATTRS_JSON_FILE"]) as f:
        attrs = json.load(f)
    return attrs["writerSpec"]


def main() -> None:
    spec = _load_writer_spec()
    out_dir = os.environ["out"]
    kind = spec["kind"]
    writer = _WRITERS.get(kind)
    if writer is None:
        raise SystemExit(f"fxtr-nix-writer: unknown spec kind {kind!r}")
    anyio.run(writer, out_dir, spec)

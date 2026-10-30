"""Writers: each takes an output directory + a spec dict and emits an
artifact there (the ``$out`` of a nix derivation; see
``nix/lib/runFxtrWriter.nix``).

Most writers emit a **layered file store**; ``write_namespace_export`` is the
exception — it emits a pure JSON publication manifest (no layer, no network).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fxtr2.data.abstract_arena import DeepFetchable
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from fxtr2.data.tsv_namespace.config import load_namespace_marker
from fxtr2.data.tsv_namespace.key_utils import make_full_key, parse_pin_key
from fxtr2.data.tsv_namespace.tsv_file import read_tsv
from fxtr2.experimental.http.client import http_deep_fetchable
from fxtr2.leases.lease_types import Lease

from fxtr_nix_writer.entities import HelloWorldEntity

# Bump when the manifest layout changes (read by the manager's namespace view).
NAMESPACE_EXPORT_FORMAT_VERSION = 1

# Manifest file name written into ``$out`` by ``write_namespace_export``.
NAMESPACE_EXPORT_FILENAME = "fxtr_namespace_export.json"

# The build-time rehost env contract (the FXTR_REMOTE_* impure-env vars the
# runner's build span delivers; see fxtr_nix_runner.rehost). A FOD writer that
# fetches from the rehost reads these — that is what makes it a "preload".
REMOTE_BASE_URL_ENV = "FXTR_REMOTE_BASE_URL"
REMOTE_BEARER_TOKEN_ENV = "FXTR_REMOTE_BEARER_TOKEN"


async def write_hello_world(out_dir: str, spec: dict[str, Any]) -> None:
    """Write a fresh single-layer store pinning one ``HelloWorldEntity``
    under the name ``"hello"``. The walk in ``pin_to_this_layer`` pulls the
    entity's transitive closure (its type entity, the ctx marker) into the
    layer, so the store is self-contained with no parents or remote."""
    message = spec.get("message", "hello world")

    async with FxtrScope(name="hello_world_writer") as scope:
        ref = scope.store(HelloWorldEntity(message=message))
        async with LayeredFileStoreIsolatedWriter(out_dir, parents=[]) as writer:
            await writer.pin_to_this_layer("hello", ref.entity_id, scope)


async def write_namespace_export(out_dir: str, spec: dict[str, Any]) -> None:
    """Emit a *pure* publication manifest for a committed tsv namespace.

    Spec fields:
    - ``namespaceRoot``: store path of the dir holding ``fxtr_namespace.toml``
      + the committed ``*.fxtr.tsv`` files (reaches the build as flake source).
    - ``namespaceName``: the published name recorded in the manifest.

    Reads only the committed marker + TSVs (no ``.fxtr/``, no network, no
    layer): parses ``reserved_prefix`` from the marker, walks every
    ``*.fxtr.tsv`` under the root, validates each (sorted keys, no dup/colon
    violations, ids parse — all enforced by ``read_tsv``), and writes
    ``$out/fxtr_namespace_export.json``:

        {"format_version": 1, "name": "<namespaceName>",
         "pins": {"<full key>": "<id_string>", ...}}

    Full keys keep their ``prefix:`` part so the manager's namespace read view
    can serve ``list_pins(prefix)`` directly off the manifest. Output is
    byte-stable for a fixed input (keys sorted) so the derivation caches well.
    """
    namespace_root = Path(spec["namespaceRoot"])
    namespace_name = spec["namespaceName"]

    marker = load_namespace_marker(namespace_root)

    pins: dict[str, str] = {}
    for tsv_path in sorted(namespace_root.rglob("*.fxtr.tsv")):
        rel_tsv_path = tsv_path.relative_to(namespace_root).as_posix()
        for tsv_key, fxtr_id in read_tsv(tsv_path).items():
            full_key = make_full_key(rel_tsv_path, tsv_key, marker.reserved_prefix)
            pins[full_key] = fxtr_id.id_string()

    manifest = {
        "format_version": NAMESPACE_EXPORT_FORMAT_VERSION,
        "name": namespace_name,
        "pins": dict(sorted(pins.items())),
    }

    os.makedirs(out_dir, exist_ok=True)
    out_path = Path(out_dir) / NAMESPACE_EXPORT_FILENAME
    out_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


async def write_preload_layer(
    out_dir: str,
    key: str,
    entity_id: FxtrID,
    remote: tuple[DeepFetchable[Any], Lease[Any]],
) -> None:
    """Core of ``entity_from_tsv_pin`` (no env / no name resolution): fetch one
    entity's bytes from ``remote`` into a single-pin layer at ``out_dir``.

    ``mark_as_remote`` makes the id known to the layer (validates it against the
    remote, writes a precomp marker), ``preload`` then fetches its bytes into
    ``data/``, and ``pin_to_this_layer`` records the pin (the id is now covered
    locally, so no ``from_scope`` walk is needed). Content deps of the entity are
    left as precomp markers — they resolve through whatever remote the *reader*
    is opened with (the manager's repo store). Output is byte-stable for a fixed
    id (canonical CBOR + sorted pins/parents), so the FOD hash is reproducible.

    Split from :func:`write_entity_from_tsv_pin` so it is testable in-process
    with a direct ``(DeepFetchable, Lease)`` remote (no HTTP server / nix).
    """
    async with LayeredFileStoreIsolatedWriter(
        out_dir, parents=[], remote=remote, validate_on_close=True
    ) as writer:
        await writer.mark_as_remote(entity_id)
        await writer.preload(entity_id)
        await writer.pin_to_this_layer(key, entity_id)


async def write_entity_from_tsv_pin(out_dir: str, spec: dict[str, Any]) -> None:
    """FOD writer: resolve a single TSV pin key → id, fetch its bytes via the
    build-time rehost, and write a one-pin layer (the
    ``fxtr.entities.<name>``-from-a-TSV-pin form).

    Spec fields:
    - ``namespaceRoot``: store path of the dir holding ``fxtr_namespace.toml`` +
      the committed ``*.fxtr.tsv`` files.
    - ``key``: the full pin key (e.g. ``"greeting"`` or ``"data/models:enc"``).

    Name→id resolution is **pure** (reads only the committed marker + TSVs, like
    the namespace export); only the byte-fetch is impure, via the
    ``FXTR_REMOTE_*`` rehost env (so this derivation declares those as
    ``impureEnvVars`` + a pinned ``outputHash``).
    """
    namespace_root = Path(spec["namespaceRoot"])
    key = spec["key"]

    marker = load_namespace_marker(namespace_root)
    rel_tsv_path, tsv_key = parse_pin_key(key, marker.reserved_prefix)
    entries = read_tsv(namespace_root / rel_tsv_path)
    if tsv_key not in entries:
        raise SystemExit(
            f"fxtr-nix-writer: pin key {key!r} not found in {rel_tsv_path} "
            f"(under {namespace_root})"
        )
    entity_id = entries[tsv_key]

    try:
        base_url = os.environ[REMOTE_BASE_URL_ENV]
    except KeyError:
        raise SystemExit(
            f"fxtr-nix-writer: {REMOTE_BASE_URL_ENV} is unset — a preload FOD must "
            f"run under the runner's build span (it delivers the rehost env)."
        )
    token = os.environ.get(REMOTE_BEARER_TOKEN_ENV)

    async with http_deep_fetchable(base_url, token=token) as remote:
        await write_preload_layer(out_dir, key, entity_id, remote)

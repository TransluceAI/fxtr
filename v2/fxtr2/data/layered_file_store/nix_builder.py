"""Run user Python that writes into a layered file store, from inside a nix build.

"nix" is in the name because the nix-coupled surface here parses the nix build
contract: the ``__structuredAttrs`` JSON at ``$NIX_ATTRS_JSON_FILE`` (a
``builderSpec`` attr), ``$out`` (the output layer dir), and the build-time rehost
env (``FXTR_REMOTE_*``) that turns a build into an FOD fetch. The interesting
logic lives in the **nix-agnostic** :func:`_run`, which is unit-testable with a
tmpdir + an in-process remote.

Two entry shapes, both ending in :func:`_run`:

- **fn form** (``mkFxtrFnBuilder``): nix runs ``python -m
  fxtr2.data.layered_file_store.nix_builder``; the ``__main__`` block reads
  ``builderSpec.entry`` (``"mod:fn"``) and calls :func:`run_entry`.
- **script form** (``mkFxtrScriptBuilder``): a loose script owns ``main`` and
  calls ``nix_builder.main(entrypoint)`` itself.

The ``entrypoint`` is ``async def entrypoint(ctx: BuilderContext) -> None``: it
does its own reads (from ``ctx.readers`` / the remote) and pins outputs via
``ctx.writer``. **Purity is whether the remote is configured**: with no
``FXTR_REMOTE_*`` env the writer/readers open with no remote (an undeclared read
fails naturally); with the rehost env present (the FOD case) the remote is built
and ``ctx.remote`` / ``ctx.writer.preload`` are available.

Scope wiring: each parent layer is opened as an **isolated** reader, and
``ctx.scope`` is opened **non-isolated with those reader scopes as parents**
(``include=``) and becomes the active scope for the entrypoint. So
``await ctx.readers["x"].retrieve(key)`` (which resolves ``into_scope=None`` to
the active scope) lands the entity accessible in ``ctx.scope`` — and entity
construction / pin walks default to it with no explicit scope passing.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio

from fxtr2.data.abstract_arena import DeepFetchable
from fxtr2.data.entity_core import FrozenFxtrCbor, FxtrID, freeze_fxtr_cbor
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope
from fxtr2.data.layered_file_store.nix_build_provenance import mint_build_provenance_c_node
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from fxtr2.leases.lease_types import Lease

# The build-time rehost env contract (the FXTR_REMOTE_* impure-env vars the
# runner's build span delivers; see fxtr_nix_runner.rehost). Present iff this is
# a FOD that fetches from the rehost — which is exactly what makes the builder
# "impure". Mirrors the same contract in nix/fxtr_nix_writer (separate package).
REMOTE_BASE_URL_ENV = "FXTR_REMOTE_BASE_URL"
REMOTE_BEARER_TOKEN_ENV = "FXTR_REMOTE_BEARER_TOKEN"

# The structured-attrs key the nix scaffold (`_runFxtrBuilder.nix`) hands over.
_BUILDER_SPEC_ATTR = "builderSpec"

type Remote = tuple[DeepFetchable[Any], Lease[Any]]
type Entrypoint = Callable[["BuilderContext"], Awaitable[None]]


@dataclasses.dataclass
class BuilderContext:
    """What the entrypoint is handed: an open writer + pre-opened readers, with
    the builder scope wired active.

    - ``spec``: passthrough JSON from the flake (the user's arbitrary args).
    - ``scope``: the builder's ``FxtrScope``, **set as the active scope** for the
      entrypoint's duration and including every parent reader's scope — so
      ``reader.retrieve(key)`` / entity construction / the writer pin walk all
      default to it.
    - ``writer``: an **open** ``LayeredFileStoreIsolatedWriter`` over ``$out``
      (parents + the FOD remote). Pin outputs with
      ``pin_to_this_layer(name, id, ctx.scope)`` (``from_scope`` passed
      explicitly — the writer is not a namespace). FOD-only: ``mark_as_remote``
      / ``preload`` pull a known-remote entity's bytes into the output layer.
    - ``readers``: one **open** ``LayeredFileStoreReader`` per named parent. Read
      an input with ``await ctx.readers["<name>"].retrieve("<pin>")``.
    - ``parents`` / ``remote``: raw, for power users opening their own reader.
      ``remote`` is present iff this is a FOD; ``.clone()`` its lease per extra
      reader (it is discarded when the build finishes).
    """

    spec: dict[str, Any]
    scope: FxtrScope
    writer: LayeredFileStoreIsolatedWriter
    readers: dict[str, LayeredFileStoreReader]
    parents: dict[str, Path]
    remote: Remote | None


async def _run(
    entrypoint: Entrypoint,
    *,
    out_dir: str | Path,
    parents: dict[str, Path],
    remote: Remote | None,
    spec: dict[str, Any],
    provenanced: bool = False,
    provenance_info: FrozenFxtrCbor | None = None,
) -> None:
    """Nix-agnostic core: wire the writer + readers + builder scope, run the
    entrypoint, finalize.

    ``remote`` ownership: the passed lease is the master — ``_run`` hands a fresh
    ``.clone()`` to the writer and to each reader, and discards the master once
    the build finishes. (Mirrors the writer/reader "take the lease handed in"
    convention; callers transfer ownership to ``_run``.)

    ``provenanced`` opens the output ``$out`` writer in provenanced mode (its
    pins round-trip provenance — see ``LayeredFileStoreIsolatedWriter``) **and**
    mints a build-step c-node, opening the builder ``ctx.scope`` with it as the
    ``provenance_context`` — so every entity the entrypoint stores carries this
    build as its provenance with no per-script work. The build kind selects the
    c-node payload (see ``nix_build_provenance``): an **input-addressed** build
    (``remote is None``) anchors on ``out_dir`` (``$out``) plus the optional
    ``provenance_info``; a **fixed-output** build (``remote is not None``) cannot
    anchor on ``$out`` and so **requires** ``provenance_info`` (a ``ValueError``
    otherwise). The entrypoint is mode-agnostic: it pins whatever id it has and
    the writer projects to the provenance form. Parent readers are unaffected
    (each reads its own layer's flag). When ``provenanced`` is false the builder
    scope carries no provenance_context (and ``provenance_info`` is ignored).
    """
    out_dir = Path(out_dir)
    async with contextlib.AsyncExitStack() as stack:
        # Own the master remote lease via the stack (take() = transfer ownership
        # into a fresh copy, per Lease's recommended pattern), so it's discarded
        # last — LIFO: entered first → exits after every consumer clone's CM — on
        # a clean exit AND on any error during setup/entrypoint (e.g. a
        # fixed-output build missing its required provenance_info). A raise must
        # not leak the master, which would otherwise block a remote's teardown.
        if remote is not None:
            remote = (remote[0], stack.enter_context(remote[1].take()))

        # One reader per named parent, each isolated so opening it doesn't
        # disturb the active scope; each gets its own remote-lease clone (FOD).
        readers: dict[str, LayeredFileStoreReader] = {}
        for name, path in parents.items():
            reader_remote: Remote | None = (
                (remote[0], remote[1].clone()) if remote is not None else None
            )
            readers[name] = await stack.enter_async_context(
                LayeredFileStoreReader(
                    path,
                    remote=reader_remote,
                    isolate_scope=True,
                    name=f"nix_builder_parent_{name}",
                )
            )

        # When provenanced, mint the build-step c-node in a small bare minting
        # scope (ground marker, globals reachable for the type entities) so the
        # builder scope layers every stored entity with it. Kept in the stack so
        # it outlives the builder scope (its parent); the c-node is copied into
        # the builder scope at entry, so no lasting reference back is needed.
        include_scopes = [r.scope for r in readers.values()]
        provenance_context: FxtrID | None = None
        provenance_context_from_scope: FxtrScope | None = None
        if provenanced:
            prov_scope = await stack.enter_async_context(
                FxtrScope(name="nix_builder_provenance", isolated=True, include=[GLOBAL_SCOPE])
            )
            provenance_context = mint_build_provenance_c_node(
                prov_scope,
                # input-addressed (pure) anchors on $out; fixed-output (FOD) has
                # no safe anchor and the helper requires provenance_info instead.
                output_store_path=str(out_dir) if remote is None else None,
                provenance_info=provenance_info,
            )
            provenance_context_from_scope = prov_scope
            include_scopes.append(prov_scope)

        # Builder scope: non-isolated (becomes active + inherits the outer scope)
        # and includes every parent reader's scope, so reads through the readers
        # are loadable here.
        scope = await stack.enter_async_context(
            FxtrScope(
                name="nix_builder",
                include=include_scopes,
                provenance_context=provenance_context,
                provenance_context_from_scope=provenance_context_from_scope,
            )
        )

        # The output-layer writer over $out, with the declared parents recorded
        # in parents.json (+ the remote clone for the FOD case).
        writer_remote: Remote | None = (
            (remote[0], remote[1].clone()) if remote is not None else None
        )
        writer = await stack.enter_async_context(
            LayeredFileStoreIsolatedWriter(
                out_dir,
                parents=list(parents.values()),
                remote=writer_remote,
                provenanced=provenanced,
            )
        )

        ctx = BuilderContext(
            spec=spec,
            scope=scope,
            writer=writer,
            readers=readers,
            parents=parents,
            remote=remote,
        )
        await entrypoint(ctx)
        # Stack unwinds here: writer finalizes first (validates + writes
        # pins/parents/format), then the builder scope closes (child), then the
        # parent readers close, and finally the master remote lease is discarded
        # (the first-registered callback above).


# ----------------------------------------------------------------------
# Nix-coupled surface (reads the build contract; thin on purpose)
# ----------------------------------------------------------------------


def _read_builder_spec() -> dict[str, Any]:
    """Read the ``builderSpec`` structured attr nix handed over."""
    with open(os.environ["NIX_ATTRS_JSON_FILE"]) as f:
        return json.load(f)[_BUILDER_SPEC_ATTR]


def _resolve_entrypoint(spec_entry: str) -> Entrypoint:
    """Resolve a ``"module:fn"`` / ``"module:Class.fn"`` spec to the callable."""
    module_name, sep, attr_path = spec_entry.partition(":")
    if not sep or not attr_path:
        raise SystemExit(f"nix_builder entry must be 'module:fn', got {spec_entry!r}")
    obj: Any = importlib.import_module(module_name)
    for part in attr_path.split("."):
        obj = getattr(obj, part)
    return obj


async def amain(entrypoint: Entrypoint) -> None:
    """Parse the nix build contract (structured attrs + ``$out`` + the
    ``FXTR_REMOTE_*`` rehost env) and delegate to :func:`_run`.

    The remote is built **iff** ``FXTR_REMOTE_BASE_URL`` is present (the FOD
    case), via part 2's ``http_deep_fetchable``; pure builders leave it unset.
    """
    builder_spec = _read_builder_spec()
    out_dir = os.environ["out"]
    parents = {name: Path(p) for name, p in builder_spec.get("parents", {}).items()}
    spec = builder_spec.get("spec", {})
    provenanced = builder_spec.get("provenanced", False)
    # The nix `provenanceInfo` attr is plain JSON; freeze it into the immutable
    # FrozenFxtrCbor shape the build-provenance entity field stores. Absent
    # (the helper only emits the key when set) → None.
    raw_provenance_info = builder_spec.get("provenanceInfo")
    provenance_info = None if raw_provenance_info is None else freeze_fxtr_cbor(raw_provenance_info)

    base_url = os.environ.get(REMOTE_BASE_URL_ENV)
    async with contextlib.AsyncExitStack() as stack:
        remote: Remote | None = None
        if base_url:
            # Lazy import: keeps the data layer's hard deps off experimental.http
            # (and keeps _run importable/testable without it).
            from fxtr2.experimental.http.client import http_deep_fetchable

            token = os.environ.get(REMOTE_BEARER_TOKEN_ENV)
            remote = await stack.enter_async_context(http_deep_fetchable(base_url, token=token))
        await _run(
            entrypoint,
            out_dir=out_dir,
            parents=parents,
            remote=remote,
            spec=spec,
            provenanced=provenanced,
            provenance_info=provenance_info,
        )


def main(entrypoint: Entrypoint) -> None:
    """Sync entry the script form calls: ``nix_builder.main(entrypoint)``."""
    anyio.run(amain, entrypoint)


def run_entry(spec_entry: str) -> None:
    """Resolve ``"module:fn"`` and run it (the fn form's ``-m`` launcher path)."""
    main(_resolve_entrypoint(spec_entry))


if __name__ == "__main__":
    _entry = _read_builder_spec().get("entry")
    if not _entry:
        raise SystemExit(
            "nix_builder run as `-m` requires builderSpec.entry (a 'module:fn' string); "
            "use mkFxtrScriptBuilder + nix_builder.main(entrypoint) for the script form."
        )
    run_entry(_entry)

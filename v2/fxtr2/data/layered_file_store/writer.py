"""``LayeredFileStoreIsolatedWriter`` — write-only async CM that emits a
fresh layer directory on top of a stack of parent layers.

Pin / data / precomp files are written incrementally; ``pins.json``,
``parents.json``, and ``layer_format.json`` are written atomically on
clean exit only. On exception, finalization is skipped — the output
directory ends up without ``layer_format.json``, so a follow-up reader
fails cleanly rather than mistaking a partial directory for a finished
layer.

The ``provenanced`` flag is the **preserve-vs-strip** toggle:

- ``provenanced=True`` (preserve): each pin is recorded in its *natural* form —
  a layered ``(data, prov)`` id as a ``[data, prov]`` pair, a bare id as a plain
  id (a provenanced layer can thus hold mixed pins; a bare entity is stored
  bare, not ``u``-wrapped). The walk seeds on the prov node for a layered id
  (its ``UNWRAPS_TO`` pulls the data node along). ``mark_as_remote`` /
  ``preload`` cover **both** levels (prov node + data node) of a layered id so a
  later reader can serve either form.
- ``provenanced=False`` (strip, the default): every pin is stripped to its bare
  data id; a bare writer is byte-for-byte unchanged from a pre-provenance layer.

Branching is on the layering (``_provenance_parent``), never on an id's tag.

When ``remote`` is configured, the writer also owns an isolated
internal ``FxtrScope`` and a private ``_WiretapArena`` attached to it.
The wiretap arena is the writer's load surface for ``preload(ref)``:
``preload`` delegates to ``await self._scope.load(ref)``, which routes
the load through the wiretap. The wiretap's ``load_flat`` is where
"fetch from remote + write data file + write precomp for new-exposed
deps + expand known-remote set" happens, as a side effect of a normal
scope load. Subsequent ``preload`` calls on deps work transparently —
the memory store's inheritance machinery tracks deps under the same
wiretap source automatically.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, Self, overload

import anyio

from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.abstract_arena import (
    DeepFetchable,
    FxtrEntityFlatData,
    ReadableArena,
    validate_lease_grant,
)
from fxtr2.data.arena_util import (
    PrecompEntry,
    compute_precomp_closure,
    derive_precomp_entry_from_deps,
)
from fxtr2.data.entity_core import FxtrCbor, FxtrID
from fxtr2.data.entity_ref_scope import Entity, FxtrScope
from fxtr2.data.layered_file_store import layer_format as fmt
from fxtr2.data.layered_file_store.errors import (
    LayerEntityMissingError,
    LayerOutputDirNotEmptyError,
    LayerRemoteNotConfiguredError,
)
from fxtr2.data.layered_file_store.search import _LayerSearchSet
from fxtr2.leases.lease_bridges import IndependentLocalBridge
from fxtr2.leases.lease_types import Lease, LifetimeToken
from fxtr2.leases.local_lease import LocalLifetimeBrand, UnconditionalLocalLifetime


# Walk items used by ``_walk_and_write``. Sum type via dataclasses so the
# match arms can destructure cleanly without runtime casts.
@dataclass(frozen=True)
class _Discover:
    fid: FxtrID


@dataclass(frozen=True)
class _WriteData:
    flat: FxtrEntityFlatData


# ----------------------------------------------------------------------
# Wiretap arena
# ----------------------------------------------------------------------


def _wiretap_validate_lease(fn):
    """Mirror of ``LayeredFileArena._with_validated_arena_lease`` —
    validates ``lease`` against the wiretap's backing lifetime."""

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _async_wrapper(self: _WiretapArena, lease: Lease[Any], *args, **kwargs):
            validate_lease_grant(self._lifetime.root_lease.grant, lease)
            with lease.take() as held:
                return await fn(self, held, *args, **kwargs)

        return _async_wrapper

    @functools.wraps(fn)
    def _sync_wrapper(self: _WiretapArena, lease: Lease[Any], *args, **kwargs):
        validate_lease_grant(self._lifetime.root_lease.grant, lease)
        with lease.take() as held:
            return fn(self, held, *args, **kwargs)

    return _sync_wrapper


class _WiretapArena[B: LocalLifetimeBrand](ReadableArena[B]):
    """Private ``ReadableArena`` that the writer attaches to its internal
    scope. ``load_flat`` fetches from the writer's configured remote and
    writes the fetched data to disk as a side effect.

    The "this id is in the remote" signal is the on-disk precomp file
    (written by ``mark_as_remote`` for explicit marks and by
    ``load_flat`` itself for uncovered deps it discovers). No
    additional in-memory bookkeeping is needed.
    """

    def __init__(
        self,
        writer: LayeredFileStoreIsolatedWriter,
        lifetime: UnconditionalLocalLifetime[B],
    ):
        self._writer = writer
        self._lifetime = lifetime
        self._tracked: set[FxtrID] = set()
        self._precomp_unwraps_to: dict[FxtrID, FxtrID] = {}
        self._precomp_ctx_node: dict[FxtrID, FxtrID] = {}
        self._precomp_step_back_p_ctx: dict[FxtrID, FxtrID] = {}

    @property
    def token(self) -> LifetimeToken[B]:
        return self._lifetime.token

    def _ingest_precomp_entry(self, fxtr_id: FxtrID, entry: PrecompEntry) -> None:
        if "unwraps_to" in entry:
            target = FxtrID.from_id_string(entry["unwraps_to"])
            self._precomp_unwraps_to[fxtr_id] = target
            self._tracked.add(target)
        if "ctx_node" in entry:
            target = FxtrID.from_id_string(entry["ctx_node"])
            self._precomp_ctx_node[fxtr_id] = target
            self._tracked.add(target)
        if "backstep" in entry:
            target = FxtrID.from_id_string(entry["backstep"])
            self._precomp_step_back_p_ctx[fxtr_id] = target
            self._tracked.add(target)

    @_wiretap_validate_lease
    def is_tracked(self, lease: Lease[B], node_id: FxtrID) -> bool:
        return node_id in self._tracked

    @_wiretap_validate_lease
    async def load(self, lease: Lease[B], node_id: FxtrID) -> FxtrCbor:
        from fxtr2.data.entity_core import fxtr_cbor_loads

        flat = await self._load_flat_impl(node_id)
        if flat.entity_bytes is None:
            raise ValueError(f"Wiretap loaded {node_id} with no entity_bytes")
        decoded = fxtr_cbor_loads(flat.entity_bytes)
        return decoded

    @_wiretap_validate_lease
    async def load_flat(self, lease: Lease[B], node_id: FxtrID) -> FxtrEntityFlatData:
        return await self._load_flat_impl(node_id)

    async def _load_flat_impl(self, node_id: FxtrID) -> FxtrEntityFlatData:
        writer = self._writer
        assert writer._remote_dfetch is not None and writer._remote_lease is not None
        # The memory store routes loads here only for ids it considers
        # reachable through this wiretap (via the inheritance bridge).
        # We trust that — if `try_lookup` returns None, the remote
        # doesn't have it and we surface `LayerEntityMissingError`.

        remote_arena = await writer._remote_dfetch.try_lookup(node_id)
        if remote_arena is None:
            raise LayerEntityMissingError(node_id)
        try:
            flat = await remote_arena.load_flat(writer._remote_lease.clone(), node_id)
            # Side effects: write data file, delete this-layer's precomp
            # if present (the data file supersedes the remote promise),
            # write precomp for any newly-exposed uncovered deps.
            fmt.write_data_file(writer._output_dir, flat)
            fmt.delete_precomp_file(writer._output_dir, node_id)

            uncovered_deps = [
                dep_id
                for dep_id, _kind in flat.deps
                if not writer._has_locally_or_in_parents(dep_id)
            ]
            if uncovered_deps:
                closure = compute_precomp_closure(
                    remote_arena,
                    writer._remote_lease.clone(),
                    uncovered_deps,
                )
                for dep_id in uncovered_deps:
                    entry = closure.get(dep_id.id_string(), PrecompEntry())
                    fmt.write_precomp_file(writer._output_dir, dep_id, entry)

            # Expand tracked-set with all deps. The memory store's
            # inheritance machinery will also track deps under this
            # same wiretap as the load propagates through the scope.
            # The "known-remote" status of each dep is now persisted
            # on disk (precomp/<dep>.json was written above for
            # uncovered ones; covered deps already have signal in this
            # layer or a parent).
            for dep_id, _kind in flat.deps:
                self._tracked.add(dep_id)
        finally:
            remote_arena.close()
        return flat

    def _lazy_populate_precomp(self, fxtr_id: FxtrID) -> None:
        """Populate this wiretap's precomp dicts for ``fxtr_id`` from on-disk
        state if not already known. Mirrors ``LayeredFileArena``: a precomp
        accessor must reflect the deterministic on-disk truth, so ``None``
        from ``unwrap_provenance`` means "no unwrap", never "not loaded yet".

        Reads the layer being written first (``writer._output_dir``), then the
        parent layers (``writer._search_set``). Prefers deriving from a data
        file's deps (which preserve the `UNWRAPS_TO` / `CTX_NODE` / `BACKSTEP`
        kinds); falls back to a ``precomp/<id>.json`` file (the form
        ``mark_as_remote`` writes for remote-only ids).
        """
        if (
            fxtr_id in self._precomp_unwraps_to
            or fxtr_id in self._precomp_ctx_node
            or fxtr_id in self._precomp_step_back_p_ctx
        ):
            return
        writer = self._writer
        # Candidate layer dirs: this layer first, then parents (transitively).
        layer_dirs = [writer._output_dir]
        if writer._search_set is not None:
            data_layer = writer._search_set.find_data(fxtr_id)
            if data_layer is not None:
                layer_dirs.append(data_layer)
            precomp_layer = writer._search_set.find_precomp(fxtr_id)
            if precomp_layer is not None:
                layer_dirs.append(precomp_layer)
        for layer_dir in layer_dirs:
            flat = fmt.read_data_file_flat(layer_dir, fxtr_id)
            if flat is not None:
                entry = derive_precomp_entry_from_deps(flat)
                if entry:
                    self._ingest_precomp_entry(fxtr_id, entry)
                return
            entry_file = fmt.read_precomp_file(layer_dir, fxtr_id)
            if entry_file is not None:
                self._ingest_precomp_entry(fxtr_id, entry_file)
                return

    @_wiretap_validate_lease
    def unwrap_provenance(self, lease: Lease[B], node_id: FxtrID) -> FxtrID | None:
        self._lazy_populate_precomp(node_id)
        result = self._precomp_unwraps_to.get(node_id)
        if result is not None:
            self._tracked.add(result)
        return result

    @_wiretap_validate_lease
    def c_node_to_context_data(self, lease: Lease[B], c_node_id: FxtrID) -> FxtrID:
        self._lazy_populate_precomp(c_node_id)
        result = self._precomp_ctx_node.get(c_node_id)
        assert result is not None, f"No ctx_node precomp for {c_node_id}"
        self._tracked.add(result)
        return result

    @_wiretap_validate_lease
    def p_node_to_pathroot(self, lease: Lease[B], p_node_id: FxtrID) -> FxtrID:
        self._lazy_populate_precomp(p_node_id)
        result = self._precomp_ctx_node.get(p_node_id)
        assert result is not None, f"No ctx_node precomp for {p_node_id}"
        self._tracked.add(result)
        return result

    @_wiretap_validate_lease
    def step_back_p_ctx(self, lease: Lease[B], p_node_id: FxtrID) -> FxtrID | None:
        self._lazy_populate_precomp(p_node_id)
        result = self._precomp_step_back_p_ctx.get(p_node_id)
        if result is not None:
            self._tracked.add(result)
        return result

    @_wiretap_validate_lease
    def abandon(self, lease: Lease[B], node_id: FxtrID) -> None:
        self._tracked.discard(node_id)
        self._precomp_unwraps_to.pop(node_id, None)
        self._precomp_ctx_node.pop(node_id, None)
        self._precomp_step_back_p_ctx.pop(node_id, None)

    @_wiretap_validate_lease
    def copy_to_new_arena(self, lease: Lease[B], node_ids) -> _WiretapArena[B]:
        """Fork a fresh wiretap sharing the same writer + lifetime.

        Used by ``lazily_attach_from_arena`` to give the memory store
        its own steward-copy. The new arena shares the writer reference
        (so it writes to the same output dir and uses the same remote)
        but maintains its own tracked-set, known-remote set, and
        precomp dicts. Same-lifetime brand, so callers can use clones
        of the original lease for the new arena too.
        """
        for nid in node_ids:
            if nid not in self._tracked:
                raise ValueError(f"Node {nid} is not tracked in this wiretap")

        new_arena: _WiretapArena[B] = _WiretapArena(self._writer, self._lifetime)
        to_copy: set[FxtrID] = set()
        frontier: deque[FxtrID] = deque(node_ids)
        while frontier:
            nid = frontier.popleft()
            if nid in to_copy:
                continue
            to_copy.add(nid)
            for precomp_dict in (
                self._precomp_unwraps_to,
                self._precomp_ctx_node,
                self._precomp_step_back_p_ctx,
            ):
                target = precomp_dict.get(nid)
                if target is not None and target not in to_copy:
                    frontier.append(target)

        for nid in to_copy:
            new_arena._tracked.add(nid)
            if nid in self._precomp_unwraps_to:
                new_arena._precomp_unwraps_to[nid] = self._precomp_unwraps_to[nid]
            if nid in self._precomp_ctx_node:
                new_arena._precomp_ctx_node[nid] = self._precomp_ctx_node[nid]
            if nid in self._precomp_step_back_p_ctx:
                new_arena._precomp_step_back_p_ctx[nid] = self._precomp_step_back_p_ctx[nid]
        return new_arena

    def close(self) -> None:
        self._tracked.clear()
        self._precomp_unwraps_to.clear()
        self._precomp_ctx_node.clear()
        self._precomp_step_back_p_ctx.clear()


# ----------------------------------------------------------------------
# Writer
# ----------------------------------------------------------------------


class LayeredFileStoreIsolatedWriter(anyio.AsyncContextManagerMixin):
    """Write-only layered file store writer.

    Construction is sync; usage is as an async context manager.

    Lease ownership convention: the constructor ``take()``s
    ``remote_lease`` (transferring ownership from the caller). The
    caller must pass ``.clone()`` if they want to keep their own copy.
    The writer discards its owned lease on CM exit.
    """

    def __init__(
        self,
        output_dir: str | Path,
        *,
        parents: list[str | Path],
        remote: tuple[DeepFetchable[Any], Lease[Any]] | None = None,
        name: str = "layered_file_store_writer",
        validate_on_close: bool = True,
        provenanced: bool = False,
    ):
        self._output_dir = Path(output_dir).resolve()
        self._parents: list[Path] = [Path(p).resolve() for p in parents]
        # Split the (DeepFetchable, Lease) pair into two fields up front
        # — no internal tuple-handling. The lease is taken (ownership
        # transferred from the caller) and discarded on CM exit; the
        # caller passes ``.clone()`` if they want to retain a copy.
        if remote is not None:
            remote_dfetch, remote_lease = remote
            self._remote_dfetch: DeepFetchable[Any] | None = remote_dfetch
            self._remote_lease: Lease[Any] | None = remote_lease.take()
        else:
            self._remote_dfetch = None
            self._remote_lease = None
        self._name = name
        self._validate_on_close = validate_on_close
        # Provenanced mode: each pin is recorded as the layered (data, prov) id
        # (serialized to a [data, prov] pair in pins.json), so the layer
        # round-trips provenance; mark_as_remote / preload split a layered id
        # into its prov + data ids and cover both. Recorded in
        # layer_format.json as the writer-side mode marker (the reader keys off
        # the pin-value shape, not this flag).
        self._provenanced = provenanced

        # State populated on enter
        self._search_set: _LayerSearchSet | None = None
        # Pins are collected in memory; flushed atomically on clean exit.
        self._pins: dict[str, FxtrID] = {}
        # Internal scope + wiretap (only set when remote is configured).
        self._scope: FxtrScope | None = None
        self._wiretap: _WiretapArena[Any] | None = None
        self._wiretap_lifetime: UnconditionalLocalLifetime[Any] | None = None
        # Ids we've already attached to the wiretap via
        # ``lazily_attach_from_arena``. Used to skip re-attaching the
        # same id on repeat `preload` calls (each attach internally
        # forks the wiretap via `copy_to_new_arena`, so deduping is
        # a real save).
        self._wiretap_attached: set[FxtrID] = set()

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        # 1. Output directory: empty (or non-existent — we create it).
        if self._output_dir.exists():
            if not self._output_dir.is_dir() or any(self._output_dir.iterdir()):
                raise LayerOutputDirNotEmptyError(self._output_dir)
        else:
            self._output_dir.mkdir(parents=True)

        # 2. data/ and precomp/ subdirs.
        (self._output_dir / fmt.DATA_DIR).mkdir(exist_ok=True)
        (self._output_dir / fmt.PRECOMP_DIR).mkdir(exist_ok=True)

        # 3. Parent-search-set for "is this id already covered upstream?" queries.
        search_set = _LayerSearchSet(list(self._parents))
        # Eagerly expand the directly-declared parents so their
        # parents.json files are loaded; deeper layers are lazy.
        for p in self._parents:
            search_set.expand_parents_of(p)
        self._search_set = search_set

        if self._remote_dfetch is not None:
            # 4. Internal scope + wiretap. The scope is the load surface
            # `preload` delegates to; the wiretap is the only thing
            # reachable from it.
            async with UnconditionalLocalLifetime(f"{self._name}_wiretap") as wiretap_lifetime:
                self._wiretap_lifetime = wiretap_lifetime
                self._wiretap = _WiretapArena(self, wiretap_lifetime)
                self._scope = FxtrScope(
                    name=f"{self._name}_scope",
                    isolated=True,
                )
                self._scope._mark_not_deeply_fetchable()
                try:
                    async with self._scope:
                        try:
                            yield self
                            await self._finalize_on_clean_exit()
                        finally:
                            self._scope = None
                finally:
                    if self._wiretap is not None:
                        self._wiretap.close()
                        self._wiretap = None
                    self._wiretap_lifetime = None
        else:
            try:
                yield self
                await self._finalize_on_clean_exit()
            finally:
                pass

        # Discard remote lease + drop search set, regardless of which
        # branch above ran.
        if self._remote_lease is not None:
            self._remote_lease.discard()
            self._remote_lease = None
        self._search_set = None
        self._wiretap_attached.clear()

    async def _finalize_on_clean_exit(self) -> None:
        """Clean-exit finalization: validation pass (optional), then
        atomically write pins/parents/format. Order matters: the
        format file is last so an interrupted finalize leaves the
        layer "unreadable" (no version stamp)."""
        if self._validate_on_close:
            self._validate_layer_closure()
        fmt.write_pins(self._output_dir, self._pins)
        fmt.write_parents(self._output_dir, self._parents)
        fmt.write_layer_format(self._output_dir, provenanced=self._provenanced)

    # ----------------------------------------------------------------
    # Public methods
    # ----------------------------------------------------------------

    async def mark_as_remote(self, entity_id: FxtrID) -> None:
        """Mark ``entity_id`` as living remotely by writing a precomp
        file recording its provenance closure.

        Validates the id exists in the configured remote (via
        ``try_lookup``) when this layer needs to record it; if the id
        is already covered upstream (precomp / data in a parent
        layer), the remote round-trip is skipped — the parent's
        record is enough.

        Idempotent: if a precomp file already exists in this layer,
        no-op. Errors if a *data* file for the same id already exists
        in this layer (mark-as-remote contradicts having local data).

        This method does **not** attach the id to the writer's
        internal wiretap — that happens lazily inside ``preload``.
        So a child layer can ``preload`` a parent-marked id without
        re-marking, since the wiretap attach is preload-driven, not
        mark-driven.

        In a **provenanced** layer, a *layered* ``entity_id`` is split
        into its two existing ids — the bare provenance-node id and the
        bare data id — and **both** are marked, so a later reader can
        serve either the layered or the bare form from the remote. A
        *bare* id (any tag) is marked as its own single id, as in a bare
        layer.
        """
        if self._remote_dfetch is None or self._remote_lease is None:
            raise LayerRemoteNotConfiguredError(entity_id)
        if self._provenanced and entity_id._provenance_parent is not None:
            await self._mark_one_as_remote(
                FxtrID(raw_data_id=entity_id._provenance_parent._raw_data_id)
            )
            await self._mark_one_as_remote(FxtrID(raw_data_id=entity_id._raw_data_id))
            return
        await self._mark_one_as_remote(entity_id)

    async def _mark_one_as_remote(self, entity_id: FxtrID) -> None:
        """Mark a single (bare) id as remote: validate + write its precomp
        closure. Idempotent; no-op if already covered locally / in a parent."""
        assert self._remote_dfetch is not None and self._remote_lease is not None
        if fmt.data_file_exists(self._output_dir, entity_id):
            raise ValueError(
                f"Cannot mark {entity_id} as remote: a local data file"
                f" already exists in this layer."
            )
        # Skip the remote round-trip + precomp write if the entity is
        # already covered in this layer or a parent.
        if self._has_locally_or_in_parents(entity_id):
            return

        remote_arena = await self._remote_dfetch.try_lookup(entity_id)
        if remote_arena is None:
            raise LayerEntityMissingError(entity_id)
        try:
            closure = compute_precomp_closure(
                remote_arena,
                self._remote_lease.clone(),
                [entity_id],
            )
        finally:
            remote_arena.close()
        entry = closure.get(entity_id.id_string(), PrecompEntry())
        fmt.write_precomp_file(self._output_dir, entity_id, entry)

    @overload
    async def preload(self, ref: FxtrID) -> FxtrCbor: ...

    @overload
    async def preload[T: Entity](self, ref: EntityRef[T]) -> T: ...

    async def preload[T: Entity](self, ref: FxtrID | EntityRef[T]) -> FxtrCbor | T:
        """Fetch a known-remote entity via the configured remote, write
        its data to this layer, and return the loaded entity.

        Mirrors ``FxtrScope.load`` / ``FxtrScope.load_cbor`` shapes:
        ``preload(EntityRef[T]) -> T`` decodes the entity;
        ``preload(FxtrID) -> FxtrCbor`` returns the raw CBOR.

        The id must already be in the writer's known-remote set —
        either via an explicit ``mark_as_remote`` call, or transitively
        as a dep of a previously-preloaded entity (each ``preload``
        expands the known-remote set with the loaded entity's deps,
        and the memory store's inheritance machinery tracks those deps
        under the wiretap automatically).

        In a **provenanced** layer, a *layered* ``ref`` fetches **both**
        levels' closures (the bare provenance-node id and the bare data
        id), so a later reader can load either the layered or the bare
        form locally without hitting the remote. The returned entity is
        decoded from the bare data form. A *bare* ref fetches its single
        id, as in a bare layer.
        """
        if self._scope is None or self._wiretap is None or self._wiretap_lifetime is None:
            entity_id = ref.entity_id if isinstance(ref, EntityRef) else ref
            raise LayerRemoteNotConfiguredError(entity_id)

        entity_id = ref.entity_id if isinstance(ref, EntityRef) else ref

        if self._provenanced and entity_id._provenance_parent is not None:
            # Cover both levels so a later reader can serve either form
            # offline — the prov node's closure alone leaves the data node a
            # remote precomp marker (see `_load_flat_impl`). Both go through
            # the scope; the wiretap answers `unwrap_provenance` from on-disk
            # precomp, so the prov node and its data node are adopted with a
            # consistent unwrap view.
            data_id = FxtrID(raw_data_id=entity_id._raw_data_id)
            prov_id = FxtrID(raw_data_id=entity_id._provenance_parent._raw_data_id)
            await self._fetch_to_layer(data_id)
            await self._fetch_to_layer(prov_id)
            if isinstance(ref, EntityRef):
                return await self._scope.load(EntityRef(data_id, ref.target_type))
            return await self._scope.load_cbor(data_id)

        await self._fetch_to_layer(entity_id)
        if isinstance(ref, EntityRef):
            return await self._scope.load(ref)
        return await self._scope.load_cbor(ref)

    async def _fetch_to_layer(self, entity_id: FxtrID) -> None:
        """Attach ``entity_id`` to the wiretap and force its bytes (+ closure)
        to this layer's ``data/`` via a scope load. Idempotent.

        Precondition: the id must already be known to this layer
        (data/precomp here, or covered by a parent layer). For ids
        marked-as-remote we always have precomp here; for
        transitively-discovered deps the wiretap's load_flat wrote
        precomp files for uncovered deps; for parent-marked ids the
        parent's precomp file is what we see. Disk state IS the
        known-remote signal.
        """
        assert self._scope is not None and self._wiretap is not None
        assert self._wiretap_lifetime is not None
        if not self._has_locally_or_in_parents(entity_id):
            raise LayerEntityMissingError(entity_id)
        # Attach to the wiretap on demand (idempotent — the dedup set
        # avoids re-attaching for repeat preloads of the same id).
        # The attach is the only way the memory store knows to route
        # loads through this wiretap; we do it here (rather than in
        # mark_as_remote) so a child layer can preload a parent-marked
        # id without having to re-mark it.
        if entity_id not in self._wiretap_attached:
            self._wiretap._tracked.add(entity_id)
            self._scope._main_arena.lazily_attach_from_arena(
                self._scope._current_auth_lease().clone(),
                entity_id,
                self._wiretap,
                IndependentLocalBridge(lease=self._wiretap_lifetime.root_lease.clone()),
            )
            self._wiretap_attached.add(entity_id)
        await self._scope.load_cbor(entity_id)

    async def pin_to_this_layer(
        self,
        name: str,
        entity_id: FxtrID,
        from_scope: FxtrScope | None = None,
    ) -> None:
        """Record a pin named ``name`` → ``entity_id`` in this layer.

        If the (projected) pin id is already known — covered locally
        (data or precomp), covered in a parent layer (data or precomp,
        transitively), or registered as known-remote via the wiretap —
        just record the pin without walking.

        Otherwise walks ``from_scope`` to bring the entity + transitive deps
        into this layer's ``data/`` (against a forked copy of its main arena,
        so partial failure doesn't leak tracked state into the caller's scope).

        The pin is recorded in the form determined by the layer's mode and the
        id's layering (branch on layering, never on tag):

        - **provenanced (preserve) layer:** the id is recorded in its *natural*
          form — a layered ``(data, prov)`` id as a ``[data, prov]`` pair, a
          bare id as a plain id. The walk seeds on the prov node for a layered
          id (its ``UNWRAPS_TO`` dep pulls the data node + child prov forms
          along), else on the id itself. So a provenanced layer can hold mixed
          pins (genuinely-provenanced → pair, bare → string); a bare entity is
          stored bare, **not** wrapped in a trivial ``u`` node.
        - **bare (strip) layer:** the id is stripped to its bare data form
          (recorded + walked as a plain id), dropping any provenance the caller
          didn't ask to preserve.
        """
        if name in self._pins:
            raise ValueError(f"Pin {name!r} already declared in this layer (no duplicates)")
        assert self._search_set is not None

        if self._provenanced:
            # Preserve: record the id in its natural form; seed the walk on the
            # prov node (layered) or the id itself (bare).
            prov = entity_id._provenance_parent
            pin_value = entity_id
            walk_id = prov if prov is not None else entity_id
        else:
            # Strip: record + walk the bare data id, dropping any provenance.
            pin_value = walk_id = FxtrID(raw_data_id=entity_id._raw_data_id)

        # Fast path: the walk id (the prov node, or the bare id) is already
        # known (data/precomp here or in any parent layer). After
        # mark_as_remote / preload writes, this covers everything the writer
        # "knows about" (so a provenanced FOD pin needs no `from_scope`).
        if self._has_locally_or_in_parents(walk_id):
            self._pins[name] = pin_value
            return

        # Walk path: requires a source scope.
        if from_scope is None:
            raise ValueError(
                f"pin_to_this_layer({name!r}, {entity_id}): id is not known"
                " in this layer / parents / wiretap; supply `from_scope=` to"
                " walk and bring in the data."
            )

        source = from_scope._main_arena
        src_lease = from_scope._current_auth_lease().clone()
        with src_lease.take() as src_lease:
            # Fork an isolated arena off the source scope so the walk
            # doesn't mutate the caller's tracked-set.
            source_copy: ReadableArena[Any] = source.copy_to_new_arena(src_lease.clone(), [walk_id])
            try:
                await self._walk_and_write(walk_id, source_copy, src_lease)
            finally:
                source_copy.close()

        self._pins[name] = pin_value

    # ----------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------

    def _has_locally(self, fxtr_id: FxtrID) -> bool:
        """True iff this layer's own ``data/`` or ``precomp/`` covers
        ``fxtr_id``."""
        return fmt.data_file_exists(self._output_dir, fxtr_id) or fmt.precomp_file_exists(
            self._output_dir, fxtr_id
        )

    def _has_in_parents(self, fxtr_id: FxtrID) -> bool:
        """True iff some parent layer (transitively) has data or precomp
        for ``fxtr_id``."""
        assert self._search_set is not None
        return self._search_set.has_data_or_precomp(fxtr_id)

    def _has_locally_or_in_parents(self, fxtr_id: FxtrID) -> bool:
        return self._has_locally(fxtr_id) or self._has_in_parents(fxtr_id)

    async def _walk_and_write(
        self,
        root: FxtrID,
        source: ReadableArena[Any],
        src_lease: Lease[Any],
    ) -> None:
        """DFS post-order walk: write deps before dependents.

        For each visited id:
          - If already covered (this layer, a parent, or wiretap
            known-remote), stop recursing.
          - Otherwise, load flat from source, schedule the data-file
            write, and recurse on deps.

        We push the ``_WriteData`` item first, then ``_Discover`` items
        for each dep — LIFO pop order means deps get processed (and
        written) before the parent's write, giving topological order.
        """
        worklist: deque[_Discover | _WriteData] = deque([_Discover(root)])
        already_visited: set[FxtrID] = set()

        while worklist:
            match worklist.pop():
                case _WriteData(flat):
                    fmt.write_data_file(self._output_dir, flat)
                case _Discover(fid):
                    if fid in already_visited:
                        continue
                    already_visited.add(fid)
                    if self._has_locally_or_in_parents(fid):
                        # Already covered upstream; stop walking here.
                        # We do *not* re-mark as remote locally — the
                        # existing parent precomp / data / wiretap
                        # registration is what future readers see.
                        continue
                    flat = await source.load_flat(src_lease.clone(), fid)
                    worklist.append(_WriteData(flat))
                    for dep_id, _dep_kind in flat.deps:
                        worklist.append(_Discover(dep_id))

    def _validate_layer_closure(self) -> None:
        """Walk every ``data/<id>.cbor`` file we wrote and assert each
        dep is resolvable in this layer (data or precomp) or some
        parent layer (data or precomp, transitively).

        Raises ``ValueError`` on the first unresolvable dep, naming the
        offending entity and its missing dep. Called before finalization
        on clean exit when ``validate_on_close=True``.
        """
        assert self._search_set is not None
        for entity_id in fmt.iter_data_file_ids(self._output_dir):
            flat = fmt.read_data_file_flat(self._output_dir, entity_id)
            assert flat is not None
            for dep_id, _kind in flat.deps:
                if self._has_locally_or_in_parents(dep_id):
                    continue
                raise ValueError(
                    f"Layer validation failed: entity {entity_id} references"
                    f" dep {dep_id} which is not in this layer or any parent."
                )

    # ----------------------------------------------------------------
    # Misc
    # ----------------------------------------------------------------

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def pins(self) -> dict[str, FxtrID]:
        """The in-memory pin dict (not yet flushed). Returns a copy."""
        return dict(self._pins)

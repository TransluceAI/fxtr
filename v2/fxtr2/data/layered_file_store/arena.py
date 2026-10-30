"""``LayeredFileArena`` — a ``ReadableArena`` over a layered file store.

Closely modeled on ``HttpReadableArena`` (precomp dict shape, tracked-set
discipline, lazy fill) but with disk reads instead of HTTP requests, plus
an internal "fall through to the remote" path for known-remote entries.

When ``load_flat`` is called for an ID:

1. Walk the search set looking for ``data/<id>.cbor``. On hit, decode
   and return; opportunistically ingest any sibling
   ``precomp/<id>.json`` and populate precomp from the loaded deps.

2. On total miss for ``data/``, check whether the ID is in the
   **known-remote** set. An id is known-remote iff either:
   (a) some layer in the search set has a ``precomp/<id>.json``
       (the layer-declared "this id lives remote" signal), or
   (b) it was a dep of an entity we previously loaded from the remote
       — by fxtr2's complete-subgraph invariant, the remote that
       supplied the parent must also supply the children.
   If known-remote *and* a ``source`` is configured, call
   ``source.try_lookup(id)`` and ``remote_arena.load_flat(...)``,
   return the flat data as if it came from a local data file. Close
   the temporary remote arena immediately (the ``DeepFetchable``
   contract guarantees later dep loads can be served by a fresh
   ``try_lookup``).

3. If known-remote but no ``source`` is configured:
   ``LayerRemoteNotConfiguredError``.
4. If neither local data nor known-remote: ``LayerEntityMissingError``
   — the id is unreachable from this arena, which is an internal
   invariant violation (no layer recorded it as data or as remote).

**Lifetime.** The arena holds **no leases** — only a ``Grant[B]`` for its
brand (used to validate the leases callers pass in, and to expose ``token``
via ``grant.subject``). The optional ``source`` rides on the **same brand
``B``**, so remote fall-through threads the *incoming* ``Lease[B]`` straight
through to ``source.try_lookup`` / ``remote_arena.load_flat``. Because nothing
is held, the arena never pins its own lifetime — a clone-and-mount caller is
free to govern its lifetime through a bridge (see the ``pin_arena`` seam).
"""

from __future__ import annotations

import functools
import inspect
from collections import deque
from typing import TYPE_CHECKING, Any, Collection, override

from fxtr2.data import arena_util
from fxtr2.data.abstract_arena import (
    DeepFetchable,
    FxtrEntityFlatData,
    ReadableArena,
    validate_lease_grant,
)
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
    fxtr_cbor_loads,
)
from fxtr2.data.layered_file_store import layer_format as fmt
from fxtr2.data.layered_file_store.errors import (
    LayerEntityMissingError,
    LayerRemoteNotConfiguredError,
)
from fxtr2.leases.lease_types import Grant, Lease, LifetimeToken
from fxtr2.leases.local_lease import LocalLifetimeBrand

if TYPE_CHECKING:
    from fxtr2.data.layered_file_store.search import _LayerSearchSet


def _with_validated_arena_lease(fn):
    """Mirror of ``HttpReadableArena._with_validated_arena_lease`` —
    validates ``lease`` against the arena's backing lifetime, takes a
    hold for the duration of the call, and re-clones it inward."""

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _async_wrapper(self: LayeredFileArena, lease: Lease[Any], *args, **kwargs):
            validate_lease_grant(self._grant, lease)
            with lease.take() as held:
                return await fn(self, held, *args, **kwargs)

        return _async_wrapper

    @functools.wraps(fn)
    def _sync_wrapper(self: LayeredFileArena, lease: Lease[Any], *args, **kwargs):
        validate_lease_grant(self._grant, lease)
        with lease.take() as held:
            return fn(self, held, *args, **kwargs)

    return _sync_wrapper


class LayeredFileArena[B: LocalLifetimeBrand](ReadableArena[B]):
    """``ReadableArena`` over a search-set of layered store directories.

    Generic on its lifetime brand ``B``: callers' leases are type-checked
    against ``B``, and the optional ``source`` rides on the **same** brand so
    remote fall-through can thread the caller's incoming lease through to it.

    See module docstring for the load resolution order and the lifetime model
    (the arena holds no leases — only a ``Grant[B]``).

    Construction is purely sync; the arena tracks IDs in its own tracked-set +
    precomp dicts.
    """

    def __init__(
        self,
        grant: Grant[B],
        search_set: "_LayerSearchSet",
        source: DeepFetchable[B] | None,
    ):
        """Construct a new arena branded on ``grant``.

        ``grant`` is identity only (no lease is held, so the arena pins
        nothing). ``source``, if given, must share the arena's brand ``B`` —
        remote fall-through threads the *incoming* ``Lease[B]`` (the one passed
        to ``load`` / ``load_flat``) through to ``source``.
        """
        self._grant = grant
        self._search_set = search_set
        self._source: DeepFetchable[B] | None = source
        self._tracked: set[FxtrID] = set()
        # Ids we know are remote-resolvable. Populated from two sources:
        # (a) precomp files in the search set (the layer-declared
        # remote signal), and (b) deps of entities we loaded from the
        # remote (since the remote is obligated to also supply them).
        # Loading an id that isn't in local data and isn't here is an
        # invariant violation — the id "fell off the map."
        self._known_remote: set[FxtrID] = set()
        self._precomp_unwraps_to: dict[FxtrID, FxtrID] = {}
        self._precomp_ctx_node: dict[FxtrID, FxtrID] = {}
        self._precomp_step_back_p_ctx: dict[FxtrID, FxtrID] = {}

    @property
    @override
    def token(self) -> LifetimeToken[B]:
        return self._grant.subject

    # ------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------

    def _ingest_precomp_entry(self, fxtr_id: FxtrID, entry: fmt.PrecompEntry) -> None:
        """Populate precomp dicts from a single entry (file or derived)."""
        self._tracked.add(fxtr_id)
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

    def _lazy_populate_precomp(self, fxtr_id: FxtrID) -> bool:
        """Populate precomp dicts for ``fxtr_id`` from disk if we can.

        Prefers deriving from the data file's deps list (since data
        files preserve ``UNWRAPS_TO`` / ``CTX_NODE`` / ``BACKSTEP``
        kinds); falls back to a sibling ``precomp/<id>.json`` file for
        ids known only as remote-references.

        Returns ``True`` iff anything was ingested. Used by both the
        eager seed walk (``_track_with_precomp``) and the lazy paths
        inside the precomp accessors (``unwrap_provenance`` etc.).
        """
        # Skip if we already have any precomp entry for this id.
        if (
            fxtr_id in self._precomp_unwraps_to
            or fxtr_id in self._precomp_ctx_node
            or fxtr_id in self._precomp_step_back_p_ctx
        ):
            return False

        data_layer = self._search_set.find_data(fxtr_id)
        if data_layer is not None:
            flat = fmt.read_data_file_flat(data_layer, fxtr_id)
            if flat is not None:
                entry = arena_util.derive_precomp_entry_from_deps(flat)
                if entry:
                    self._ingest_precomp_entry(fxtr_id, entry)
                    return True
                return False  # data file but no precomp-relevant deps

        precomp_layer = self._search_set.find_precomp(fxtr_id)
        if precomp_layer is not None:
            entry_file = fmt.read_precomp_file(precomp_layer, fxtr_id)
            if entry_file is not None:
                self._ingest_precomp_entry(fxtr_id, entry_file)
                # A precomp file is the layer-declared "this id lives
                # remote" signal; register it in the known-remote set
                # so `_load_flat_impl` can fall through to remote.
                self._known_remote.add(fxtr_id)
                return True

        return False

    def _track_with_precomp(self, fxtr_id: FxtrID) -> None:
        """Add ``fxtr_id`` to the tracked set and populate the full
        precomp closure rooted at it from disk.

        This is the layered-store equivalent of what the HTTP server
        ships in ``/data`` / ``/pin`` responses: a transitively-closed
        precomp map so sync queries on ``fxtr_id`` and any
        provenance-reachable id succeed without further disk work.

        Safe to call on ids that have no precomp relationships — the
        walk terminates immediately at non-provenance leaves.
        """
        frontier: deque[FxtrID] = deque([fxtr_id])
        visited: set[FxtrID] = set()
        while frontier:
            nid = frontier.popleft()
            if nid in visited:
                continue
            visited.add(nid)
            self._tracked.add(nid)
            if not self._lazy_populate_precomp(nid):
                continue
            for target_dict in (
                self._precomp_unwraps_to,
                self._precomp_ctx_node,
                self._precomp_step_back_p_ctx,
            ):
                target = target_dict.get(nid)
                if target is not None and target not in visited:
                    frontier.append(target)

    async def _load_flat_via_remote(
        self,
        lease: Lease[B],
        node_id: FxtrID,
    ) -> FxtrEntityFlatData:
        """Resolve a precomp-only entity through the configured ``source``.

        ``source`` shares the arena's brand ``B``, so we thread the caller's
        incoming ``lease`` straight through — the arena holds no lease of its
        own. See module docstring for why it's safe to close the temporary
        remote arena immediately after a single ``load_flat`` call.
        """
        if self._source is None:
            raise LayerRemoteNotConfiguredError(node_id)
        remote_arena = await self._source.try_lookup(node_id)
        if remote_arena is None:
            raise LayerEntityMissingError(node_id)
        try:
            return await remote_arena.load_flat(lease.clone(), node_id)
        finally:
            remote_arena.close()

    # ------------------------------------------------------------
    # ReadableArena surface
    # ------------------------------------------------------------

    @override
    @_with_validated_arena_lease
    def is_tracked(self, lease: Lease[B], node_id: FxtrID) -> bool:
        return node_id in self._tracked

    @override
    @_with_validated_arena_lease
    async def load(self, lease: Lease[B], node_id: FxtrID) -> FxtrCbor:
        if node_id not in self._tracked:
            raise ValueError(f"Node {node_id} is not tracked in this arena")
        flat = await self._load_flat_impl(lease, node_id)
        if flat.entity_bytes is None:
            raise ValueError(f"Node {node_id} has no entity bytes; cannot decode as CBOR")
        decoded = fxtr_cbor_loads(flat.entity_bytes)

        def _track_child(
            raw_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
        ) -> None:
            self._tracked.add(raw_id.inner_id)

        arena_util.visit_ids(decoded, _track_child)
        return decoded

    @override
    @_with_validated_arena_lease
    async def load_flat(self, lease: Lease[B], node_id: FxtrID) -> FxtrEntityFlatData:
        if node_id not in self._tracked:
            raise ValueError(f"Node {node_id} is not tracked in this arena")
        return await self._load_flat_impl(lease, node_id)

    async def _load_flat_impl(self, lease: Lease[B], node_id: FxtrID) -> FxtrEntityFlatData:
        # 1. Try local data files first.
        data_layer = self._search_set.find_data(node_id)
        if data_layer is not None:
            flat = fmt.read_data_file_flat(data_layer, node_id)
            assert flat is not None, "find_data found it, but read returned None"
            # Track + populate precomp closure for the loaded id, then
            # track its deps (deps' precomp populates lazily on demand).
            self._track_with_precomp(node_id)
            for dep_id, _kind in flat.deps:
                self._tracked.add(dep_id)
            return flat

        # 2. No local data. We can only go to the remote if the id is
        # in our known-remote set — i.e., either:
        #   (a) some layer in the search set has a precomp file for it
        #       (the layer-declared remote signal), or
        #   (b) it was a dep of an entity we previously loaded from the
        #       remote (the remote is obligated to also supply it).
        if node_id not in self._known_remote:
            # (a) may not have been discovered yet — check for a
            # precomp file lazily here. If found, register and proceed.
            if self._search_set.find_precomp(node_id) is not None:
                self._lazy_populate_precomp(node_id)
            else:
                # Not local, not declared remote anywhere, not a
                # transitive remote dep. Internal invariant violation:
                # the id is unreachable from this arena.
                raise LayerEntityMissingError(node_id)

        flat = await self._load_flat_via_remote(lease, node_id)
        # Expand the known-remote set: deps of a remote entity are
        # implicitly remote-resolvable too (the remote answered for the
        # parent; by the complete-subgraph invariant it must also
        # answer for the children).
        for dep_id, _kind in flat.deps:
            self._known_remote.add(dep_id)
            self._tracked.add(dep_id)
        # Track + populate precomp closure (idempotent re-walk).
        self._track_with_precomp(node_id)
        return flat

    @override
    @_with_validated_arena_lease
    def unwrap_provenance(self, lease: Lease[B], node_id: FxtrID) -> FxtrID | None:
        if node_id not in self._tracked:
            raise ValueError(f"Node {node_id} is not tracked in this arena")
        result = self._precomp_unwraps_to.get(node_id)
        if result is None:
            # Fall back to disk precomp if we haven't ingested it yet.
            self._lazy_populate_precomp(node_id)
            result = self._precomp_unwraps_to.get(node_id)
        if result is not None:
            self._tracked.add(result)
        return result

    @override
    @_with_validated_arena_lease
    def c_node_to_context_data(self, lease: Lease[B], c_node_id: FxtrID) -> FxtrID:
        if c_node_id not in self._tracked:
            raise ValueError(f"Node {c_node_id} is not tracked in this arena")
        result = self._precomp_ctx_node.get(c_node_id)
        if result is None:
            self._lazy_populate_precomp(c_node_id)
            result = self._precomp_ctx_node.get(c_node_id)
        assert result is not None, f"No ctx_node precomp for {c_node_id}"
        self._tracked.add(result)
        return result

    @override
    @_with_validated_arena_lease
    def p_node_to_pathroot(self, lease: Lease[B], p_node_id: FxtrID) -> FxtrID:
        if p_node_id not in self._tracked:
            raise ValueError(f"Node {p_node_id} is not tracked in this arena")
        result = self._precomp_ctx_node.get(p_node_id)
        if result is None:
            self._lazy_populate_precomp(p_node_id)
            result = self._precomp_ctx_node.get(p_node_id)
        assert result is not None, f"No ctx_node precomp for {p_node_id}"
        self._tracked.add(result)
        return result

    @override
    @_with_validated_arena_lease
    def step_back_p_ctx(self, lease: Lease[B], p_node_id: FxtrID) -> FxtrID | None:
        if p_node_id not in self._tracked:
            raise ValueError(f"Node {p_node_id} is not tracked in this arena")
        result = self._precomp_step_back_p_ctx.get(p_node_id)
        if result is None:
            self._lazy_populate_precomp(p_node_id)
            result = self._precomp_step_back_p_ctx.get(p_node_id)
        if result is not None:
            self._tracked.add(result)
        return result

    @override
    @_with_validated_arena_lease
    def abandon(self, lease: Lease[B], node_id: FxtrID) -> None:
        if node_id not in self._tracked:
            raise ValueError(f"Node {node_id} is not tracked in this arena")
        self._tracked.discard(node_id)
        self._precomp_unwraps_to.pop(node_id, None)
        self._precomp_ctx_node.pop(node_id, None)
        self._precomp_step_back_p_ctx.pop(node_id, None)

    @override
    @_with_validated_arena_lease
    def copy_to_new_arena(
        self, lease: Lease[B], node_ids: Collection[FxtrID]
    ) -> "LayeredFileArena[B]":
        for node_id in node_ids:
            if node_id not in self._tracked:
                raise ValueError(f"Node {node_id} is not tracked in this arena")

        # Grant + source are identity/reference (no leases held), so the fork
        # just shares them — it brands on the same grant and threads incoming
        # leases through to the same source, exactly like us.
        new_arena = LayeredFileArena(
            self._grant,
            self._search_set,
            self._source,
        )

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
            if nid in self._known_remote:
                new_arena._known_remote.add(nid)
            if nid in self._precomp_unwraps_to:
                new_arena._precomp_unwraps_to[nid] = self._precomp_unwraps_to[nid]
            if nid in self._precomp_ctx_node:
                new_arena._precomp_ctx_node[nid] = self._precomp_ctx_node[nid]
            if nid in self._precomp_step_back_p_ctx:
                new_arena._precomp_step_back_p_ctx[nid] = self._precomp_step_back_p_ctx[nid]

        return new_arena

    @override
    def close(self) -> None:
        # The arena holds no leases (only a grant + source reference), so close
        # just drops the tracked-set / precomp bookkeeping.
        self._tracked.clear()
        self._known_remote.clear()
        self._precomp_unwraps_to.clear()
        self._precomp_ctx_node.clear()
        self._precomp_step_back_p_ctx.clear()

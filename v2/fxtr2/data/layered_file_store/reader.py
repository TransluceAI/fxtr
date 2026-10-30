"""``LayeredFileStoreReader`` — read-only namespace over a layered store.

Mirrors ``HttpFxtrReadNamespace`` in shape, but uses
``anyio.AsyncContextManagerMixin`` for clean nested lifetime management,
and the underlying arena (``LayeredFileArena``) reads from disk.

Pin retrieval delegates to the ``pin_arena`` seam (``arena_over_search_set``):
look up the pin in ``pins.json``, build a fresh per-retrieve ``LayeredFileArena``
over the reader's search set, attach it into the reader's scope via
``lazily_attach_from_arena``, then close the transient arena.

The reader brands its arena on the **store**'s grant when a remote is configured
(the layer is then an overlay on the store — remote fall-through threads the
incoming lease through to it), else on its own disk lifetime. Either way the
arena holds no lease; the reader holds the store lease (when present) for its CM
and feeds the deep-fetch bridge from it. The reader's scope is marked **not
deeply fetchable** — remote resolution stays internal to the arena.

Pins are *layer-local* — ``retrieve`` and ``list_pins`` only see entries
in this reader's own layer, even though the data walk may reach into
parent layers.

In a **provenanced** layer each ``pins.json`` value is a ``[data, prov]`` pair,
so a pin reads back as a layered ``(data, prov)`` ``FxtrID`` (bare layers store
a plain id and read back bare). ``retrieve`` attaches the arena on the bare prov
node and reconstructs the layered id via ``FxtrScope.embed_as_provenance_parent``
(the validated form — tied to what the prov node actually unwraps to);
``list_pins`` returns the stored layered ids directly as a **pure read** (no
attach/embed), so it yields the same id ``retrieve`` does. The by-id path
(``LayeredStoreDeepFetchable``) is unaffected — it resolves bare data ids and
never embeds; provenance reconstruction lives only at the pin boundary.

**Assumption (shared by the whole layered file store):** the layer dirs are
neither deleted nor modified while a reader / arena / deep-fetch source built
over them is in use.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, AsyncGenerator, Self, override

import anyio

from fxtr2.data import entity_ref_scope
from fxtr2.data.abstract_arena import DeepFetchable
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.layered_file_store import layer_format as fmt
from fxtr2.data.layered_file_store.arena import LayeredFileArena
from fxtr2.data.layered_file_store.pin_arena import arena_over_search_set
from fxtr2.data.layered_file_store.search import ParentPathRemap, _LayerSearchSet
from fxtr2.experimental.read_namespace import FxtrReadNamespace
from fxtr2.leases.lease_bridges import IndependentLocalBridge
from fxtr2.leases.lease_types import Grant, Lease
from fxtr2.leases.local_lease import UnconditionalLocalLifetime


class LayeredFileStoreReader(FxtrReadNamespace, anyio.AsyncContextManagerMixin):
    """Read-only ``FxtrReadNamespace`` over a layered file entity store.

    Construction is sync; usage is as an async context manager.

    The remote, if supplied, is a ``(DeepFetchable, Lease)`` pair the
    caller minted. The reader clones the lease for its own use (per
    retrieve) and discards its clone on exit; the caller retains
    ownership of the original lease.
    """

    def __init__(
        self,
        layer_dir: str | Path,
        *,
        remote: tuple[DeepFetchable[Any], Lease[Any]] | None = None,
        isolate_scope: bool = False,
        name: str = "layered_file_store",
        parent_path_remap: ParentPathRemap | None = None,
    ):
        self._layer_dir = Path(layer_dir).resolve()
        # Optional rewrite of parent-layer paths read from parents.json
        # (e.g. the podman nix backend's materialized store; see
        # ``ParentPathRemap``). Applies only to discovered parents, never
        # to ``layer_dir`` itself (which is already a real read-site path).
        self._parent_path_remap = parent_path_remap
        # Split the (DeepFetchable, Lease) pair into two fields up
        # front — no internal tuple-handling. The lease is taken
        # (ownership transferred from the caller) and discarded on CM
        # exit; the caller passes ``.clone()`` if they want a copy.
        # Clones for per-retrieve arenas are minted from
        # ``self._remote_lease``.
        if remote is not None:
            remote_dfetch, remote_lease = remote
            self._remote_dfetch: DeepFetchable[Any] | None = remote_dfetch
            self._remote_lease: Lease[Any] | None = remote_lease.take()
        else:
            self._remote_dfetch = None
            self._remote_lease = None
        self._isolate_scope = isolate_scope
        self._name = name

        # State populated on enter. Pin values read back layered for a
        # provenanced layer and bare otherwise (the ``[data, prov]`` pin shape
        # is self-describing), so the reader keys off the per-pin shape rather
        # than the layer's ``provenanced`` flag.
        self._pins: dict[str, FxtrID] | None = None
        self._search_set: _LayerSearchSet | None = None
        self._backend_lifetime: UnconditionalLocalLifetime[Any] | None = None
        # The arena's brand grant: the store's grant when a remote is configured
        # (the layer is then an overlay on the store, branded on it), else the
        # disk-backed `_backend_lifetime`'s grant. Identity only — no lease held.
        self._brand_grant: Grant[Any] | None = None
        self._scope: entity_ref_scope.FxtrScope | None = None

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        try:
            # 1. Validate + read the root layer's metadata (read_layer_format
            #    validates the version). The search set starts with the root
            #    layer; its parents are unioned in lazily on the first data walk.
            fmt.read_layer_format(self._layer_dir)
            self._pins = fmt.read_pins(self._layer_dir)
            # Force the root layer's parents.json to be read up front so
            # missing-file errors surface during enter, not at first
            # lookup. ``_LayerSearchSet`` continues lazy parent expansion
            # as deeper layers are touched.
            search_set = _LayerSearchSet([self._layer_dir], parent_remap=self._parent_path_remap)
            search_set.expand_parents_of(self._layer_dir)
            self._search_set = search_set

            async with UnconditionalLocalLifetime(self._name) as backend_lifetime:
                self._backend_lifetime = backend_lifetime
                # Brand on the store when we have one (overlay), else on our own
                # disk lifetime. Grant is identity — holding it pins nothing.
                if self._remote_dfetch is not None:
                    assert self._remote_lease is not None
                    self._brand_grant = self._remote_lease.grant
                else:
                    self._brand_grant = backend_lifetime.root_lease.grant

                # Build the scope. Construction is inert, so there is a window
                # between construction and ``async with`` entry to call
                # ``_mark_not_deeply_fetchable`` (the flag is committed at entry).
                self._scope = entity_ref_scope.FxtrScope(
                    name=self._name,
                    isolated=self._isolate_scope,
                )
                self._scope._mark_not_deeply_fetchable()

                try:
                    async with self._scope:
                        yield self
                finally:
                    self._scope = None
                    self._backend_lifetime = None
                    self._brand_grant = None
                    self._search_set = None
                    self._pins = None
        finally:
            # Discard the remote lease we took at construction. Placed
            # in the outermost ``finally`` so it fires even if setup
            # above raises before the inner CMs are entered.
            if self._remote_lease is not None:
                self._remote_lease.discard()
                self._remote_lease = None
                self._remote_dfetch = None

    @property
    @override
    def scope(self) -> entity_ref_scope.FxtrScope:
        assert self._scope is not None, "LayeredFileStoreReader not entered"
        return self._scope

    # ------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------

    def _make_arena_for_retrieve(self, seed_id: FxtrID) -> LayeredFileArena[Any]:
        """Build a fresh per-retrieve arena over our search set, pre-tracking
        ``seed_id`` (so the scope's sync precomp methods work before ``load``).

        Branded on ``self._brand_grant``; the optional ``self._remote_dfetch``
        is the same-branded fall-through source (the arena threads the caller's
        incoming lease through to it — it holds no lease of its own).
        """
        assert self._search_set is not None
        assert self._brand_grant is not None
        return arena_over_search_set(
            self._search_set,
            seed_id,
            grant=self._brand_grant,
            source=self._remote_dfetch,
        )

    def _bridge_lease(self) -> Lease[Any]:
        """A fresh lease of the arena's brand for a deep-fetch bridge (store lease
        when we have a remote, else our disk lifetime's)."""
        if self._remote_dfetch is not None:
            assert self._remote_lease is not None
            return self._remote_lease.clone()
        assert self._backend_lifetime is not None
        return self._backend_lifetime.root_lease.clone()

    # ------------------------------------------------------------
    # FxtrReadNamespace API
    # ------------------------------------------------------------

    @override
    async def _retrieve_impl[
        T: entity_ref_scope.Entity
    ](
        self,
        key: str,
        type: type[T] | None,
        into_scope: entity_ref_scope.FxtrScope | None,
        copy_into_scope: bool,
        allow_not_found: bool,
    ) -> (FxtrID | entity_ref_scope.EntityRef[T] | None):
        assert self._pins is not None, "LayeredFileStoreReader not entered"
        scope = self.scope

        # Resolve the target scope up front (honors the FxtrReadNamespace
        # contract: into_scope=None → active scope, with the lift-path
        # inclusion guard). Raises the helpful copy_into_scope / include hint
        # before any work when the target can't safely lift.
        method_name = "try_retrieve" if allow_not_found else "retrieve"
        resolved_scope = self._resolve_into_scope(into_scope, copy_into_scope, method_name)

        fxtr_id = self._pins.get(key)
        if fxtr_id is None:
            if allow_not_found:
                return None
            raise KeyError(f"Pin {key!r} not found in this layered store")

        # A provenanced pin reads back layered ``(data, prov)``; the arena seeds
        # / attaches on the bare prov node (its ``UNWRAPS_TO`` pulls the data
        # node along), then ``embed`` reconstructs the layered id from what the
        # prov node actually unwraps to (the validated form). A bare pin seeds
        # on itself and is delivered unchanged.
        prov = fxtr_id._provenance_parent
        seed = prov if prov is not None else fxtr_id
        arena = self._make_arena_for_retrieve(seed)
        scope._main_arena.lazily_attach_from_arena(
            scope._current_auth_lease().clone(),
            seed,
            arena,
            IndependentLocalBridge(lease=self._bridge_lease()),
        )
        arena.close()

        delivered = scope.embed_as_provenance_parent(seed) if prov is not None else fxtr_id

        # Deliver into the resolved target (matching FxtrNamespace._retrieve_impl).
        if copy_into_scope:
            await resolved_scope.copy_from_scope(delivered, source_scope=scope)
        else:
            resolved_scope.lift(delivered)

        if type is None:
            return delivered
        return entity_ref_scope.EntityRef(delivered, type)

    @override
    async def list_pins(self, prefix: str = "") -> dict[str, FxtrID]:
        assert self._pins is not None, "LayeredFileStoreReader not entered"
        if prefix:
            return {k: v for k, v in self._pins.items() if k.startswith(prefix)}
        return dict(self._pins)

"""FlatEntitySource backed by an in-memory dict of FxtrEntityFlatData.

Used to feed entities received from a remote (e.g. the hub) into local entity-import
paths. Implements only the narrow ``FlatEntitySource`` interface — not the full
``ReadableArena`` — since the receiving code only needs ``is_tracked`` / ``load_flat`` /
``copy_to_new_arena`` / ``close``. The name reflects that: it's a *source* of entity
data, not a full arena.

Lifetime semantics
------------------

``TransferEntitySource`` has no formal lifetime of its own: the entity data lives in a
plain Python dict and is gated entirely by the Python object's reachability. It is
therefore branded under :data:`fxtr2.leases.local_lease.STATIC_LIFETIME` — every method
accepts (and immediately discards) a ``Lease[StaticBrand]`` from
``STATIC_LIFETIME.lease()`` (cheap to obtain; clones share the static grant). Closing a
``TransferEntitySource`` is the
caller's explicit choice: after ``close()`` the source raises on further reads, even
though the static lease itself never breaks (the memory store's break-callback would
not fire in that case — see ``STATIC_LIFETIME`` docstring for the caveat if you wire
one into ``lazily_attach_from_arena``).

Two-set model
-------------

Internally the source holds two pieces of state:

- ``_entities``: the dict of available data (``FxtrID -> FxtrEntityFlatData``). This is
    shared across ``copy_to_new_arena``-derived siblings; closing any one source drops
    its reference but leaves other sharers' views intact.
- ``_tracked``: the per-source set of IDs this view currently commits to keep
    accessible. Starts at a caller-chosen seed (default: all keys of the data dict).
    ``load_flat`` adds the loaded id and its deps to ``_tracked`` — matching the
    ``ReadableArena.load_flat`` contract that returned dep ids become tracked as a
    side effect.

Builtin tags (``.``/``!``) are not special-cased: they are ordinary tracked IDs. Data
for them is not stored (the dict never holds entries for builtin tags), so
``load_flat`` synthesizes an empty derived-node record on demand as long as the id is
in ``_tracked``.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import override

from fxtr2.data.abstract_arena import FlatEntitySource, FxtrEntityFlatData
from fxtr2.data.entity_core import FxtrID
from fxtr2.leases.lease_types import Lease, LifetimeToken
from fxtr2.leases.local_lease import STATIC_LIFETIME, StaticBrand


def _is_builtin_tag(tag: str) -> bool:
    """Builtin tags represent virtual nodes that have no stored content."""
    return tag in (".", "!")


class TransferEntitySource(FlatEntitySource[StaticBrand]):
    """FlatEntitySource backed by an in-memory dict of entity data.

    Uses :data:`STATIC_LIFETIME` as its lifetime token; see module docstring.
    """

    def __init__(
        self,
        entities: dict[FxtrID, FxtrEntityFlatData],
        tracked: Collection[FxtrID] | None = None,
    ) -> None:
        """Construct a TransferEntitySource.

        Args:
            entities: Dict of available entity data. This dict is held by reference —
                ``copy_to_new_arena`` produces sibling sources sharing the same dict.
            tracked: Initial set of tracked IDs. Defaults to all keys of ``entities``,
                which matches the common "pinned from the full payload" use case.
                Pass a narrower set (e.g. just the roots of a batch) if you want to
                stage tracking incrementally via ``load_flat``.
        """
        self._entities: dict[FxtrID, FxtrEntityFlatData] | None = entities
        self._tracked: set[FxtrID] = set(tracked) if tracked is not None else set(entities.keys())

    @property
    @override
    def token(self) -> LifetimeToken[StaticBrand]:
        return STATIC_LIFETIME.token

    def _ensure_open(self) -> dict[FxtrID, FxtrEntityFlatData]:
        if self._entities is None:
            raise RuntimeError("TransferEntitySource is closed")
        return self._entities

    @override
    def is_tracked(self, lease: Lease[StaticBrand], node_id: FxtrID) -> bool:
        lease.discard()
        return node_id in self._tracked

    @override
    async def load_flat(self, lease: Lease[StaticBrand], node_id: FxtrID) -> FxtrEntityFlatData:
        lease.discard()
        entities = self._ensure_open()
        if node_id not in self._tracked:
            raise ValueError(f"Entity {node_id} not tracked in TransferEntitySource")
        tag = node_id.get_tag()
        if _is_builtin_tag(tag):
            # Builtins have no stored content; synthesize an empty derived-node record.
            return FxtrEntityFlatData(entity_id=node_id, entity_bytes=None, deps=[])
        if node_id not in entities:
            raise ValueError(f"Entity {node_id} is tracked but has no data in TransferEntitySource")
        data = entities[node_id]
        # Loading a node implicitly tracks its deps (matching ReadableArena.load_flat).
        self._tracked.add(node_id)
        for dep_id, _kind in data.deps:
            self._tracked.add(dep_id)
        return data

    @override
    def copy_to_new_arena(
        self, lease: Lease[StaticBrand], node_ids: Collection[FxtrID]
    ) -> TransferEntitySource:
        """Create a sibling source sharing the same entity data, with a narrowed
        tracked set.

        The new source shares ``self._entities`` by reference, so the copy is
        O(|node_ids|). Both sources see the same underlying data; closing one does not
        affect the other beyond dropping its own reference.
        """
        lease.discard()
        entities = self._ensure_open()
        return TransferEntitySource(entities, tracked=set(node_ids))

    @override
    def close(self) -> None:
        """Release this source's reference to the shared entity dict.

        Subsequent ``load_flat`` calls raise. The shared dict is GC'd once all sharing
        sources have released their references.
        """
        self._entities = None
        self._tracked.clear()

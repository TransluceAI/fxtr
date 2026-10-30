"""Pin/seed-oriented arena construction over a layered store + optional backing.

Lower-level than :class:`~fxtr2.data.layered_file_store.reader.LayeredFileStoreReader`:
these helpers **return a** :class:`~fxtr2.data.layered_file_store.arena.LayeredFileArena`
and leave mounting + lifetime to the caller — the caller is the one that can
guarantee the entity stays loadable for the right length of time (mount it on a
scope via ``lazily_attach_from_arena`` with a bridge it owns, then ``close()``
the returned arena).

The arena is branded on a caller-supplied ``Grant[B]`` (identity only — no lease
held; see :class:`LayeredFileArena`'s lifetime model) and resolves remote-pointer
entities through the optional same-branded ``source`` (a ``DeepFetchable[B]``,
typically a repo entity store) by threading the caller's incoming lease.

Three layers, smallest first:

- :func:`arena_over_search_set` — the core: an arena over an existing search set.
- :func:`open_seed_arena` / :func:`resolve_pin` — single-layer convenience
  (build a fresh search set for one layer dir). A caller resolves the pin, then
  opens a seed arena on whichever id it needs loadable — for a layered pin that
  is the bare prov node, not the layered id itself (see :func:`resolve_pin`).
- :class:`LayeredStoreDeepFetchable` — a by-id ``DeepFetchable`` over a *whole*
  (multi-layer) set, for registering as a scope-level deep-fetch source (serving).

**Assumption.** Like the rest of the layered file store, these assume the layer
dirs are neither deleted nor modified while any arena / deep-fetch source built
over them is in use.
"""

from __future__ import annotations

from pathlib import Path
from typing import override

from fxtr2.data.abstract_arena import DeepFetchable, ReadableArena
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.layered_file_store import layer_format as fmt
from fxtr2.data.layered_file_store.arena import LayeredFileArena
from fxtr2.data.layered_file_store.search import ParentPathRemap, _LayerSearchSet
from fxtr2.leases.lease_types import Grant
from fxtr2.leases.local_lease import LocalLifetimeBrand


def arena_over_search_set[
    B: LocalLifetimeBrand
](
    search_set: _LayerSearchSet,
    seed_id: FxtrID,
    *,
    grant: Grant[B],
    source: DeepFetchable[B] | None,
) -> LayeredFileArena[B]:
    """Build a :class:`LayeredFileArena` over ``search_set`` seeded at ``seed_id``.

    Branded on ``grant``; ``source`` (same brand ``B``) supplies remote-pointer
    fall-through. Pre-tracks the seed's precomp closure. The caller owns the
    arena (mount it, then ``close()``).

    ``seed_id`` must be a **bare** id (no ``_provenance_parent``): the arena
    tracks nodes by their bare id and finds data files by bare ``id_string``, so
    a layered seed would silently track its *data* node rather than the prov
    node — to make a layered pin loadable the caller seeds on the bare prov node
    (``id._provenance_parent``), whose ``UNWRAPS_TO`` pulls the data node along.
    """
    assert seed_id._provenance_parent is None, (
        f"arena seed {seed_id} must be bare (no _provenance_parent); seed on the"
        " bare prov node for a layered pin"
    )
    arena: LayeredFileArena[B] = LayeredFileArena(grant, search_set, source)
    arena._track_with_precomp(seed_id)
    return arena


def resolve_pin(layer_dir: Path, pin: str) -> FxtrID:
    """Resolve a layer-local pin name to its ``FxtrID`` via the layer's pins.json.

    Returns the bare id for a bare pin, or the layered ``(data, prov)`` id for a
    provenanced pin (a ``[data, prov]`` pin value). Raises ``KeyError`` if
    ``pin`` is absent in this layer.

    The caller opens an arena via :func:`open_seed_arena` on whichever id it
    actually needs loadable — for a layered pin that is the bare prov node
    (``id._provenance_parent``, whose ``UNWRAPS_TO`` pulls the data node along),
    which is also what the caller attaches + ``embed``s. (There is deliberately
    no "open an arena for a pin" helper that hides that choice: what the arena
    is seeded on must be explicit, since the caller handles it specially.)
    """
    fxtr_id = fmt.read_pins(layer_dir).get(pin)
    if fxtr_id is None:
        raise KeyError(f"pin {pin!r} not found in layer {layer_dir}")
    return fxtr_id


def open_seed_arena[
    B: LocalLifetimeBrand
](
    layer_dir: Path,
    seed_id: FxtrID,
    *,
    grant: Grant[B],
    source: DeepFetchable[B] | None,
    remap: ParentPathRemap | None = None,
) -> LayeredFileArena[B]:
    """:func:`arena_over_search_set` over a fresh single-layer search set.

    Builds a ``_LayerSearchSet`` for ``layer_dir`` (eagerly expanding its direct
    parents, mirroring the reader's enter-time validation) and seeds it.
    """
    search_set = _LayerSearchSet([Path(layer_dir)], parent_remap=remap)
    search_set.expand_parents_of(Path(layer_dir))
    return arena_over_search_set(search_set, seed_id, grant=grant, source=source)


class LayeredStoreDeepFetchable[B: LocalLifetimeBrand](DeepFetchable[B]):
    """By-id :class:`DeepFetchable` over a whole (multi-layer) layered store.

    Wraps a long-lived :class:`_LayerSearchSet` + the arena core so a serving
    scope can ``register_deep_fetch_source`` it and resolve any id the layer set
    holds (layer-local ids from disk; remote-pointer closures fall through to
    ``source``).

    A ``DeepFetchable`` must answer correctly and stably from its first request,
    so the search set is **fully expanded at construction** — after that it is
    effectively immutable, ``try_lookup`` is a pure read, and concurrent lookups
    don't race.
    """

    def __init__(
        self,
        layer_dirs: list[Path],
        *,
        grant: Grant[B],
        source: DeepFetchable[B] | None = None,
        remap: ParentPathRemap | None = None,
    ) -> None:
        self._search_set = _LayerSearchSet([Path(d) for d in layer_dirs], parent_remap=remap)
        self._search_set.expand_fully()
        self._grant = grant
        self._source = source

    @override
    async def try_lookup(self, node_id: FxtrID) -> ReadableArena[B] | None:
        # Pure read: the set is fully expanded, so has_data_or_precomp neither
        # mutates it nor misses a reachable layer.
        if not self._search_set.has_data_or_precomp(node_id):
            return None
        return arena_over_search_set(
            self._search_set, node_id, grant=self._grant, source=self._source
        )

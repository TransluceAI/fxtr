"""Layer search-set machinery, shared by reader and writer.

A search set is a collection of layer directories that we consult, in
order, when looking for entity bytes or precomp metadata. It starts with
the root layer's own parents (plus, for the reader, the root layer
itself) and grows *dynamically*: when we successfully find data in a
layer we haven't seen before, we read that layer's ``parents.json`` and
union them into the search set before recursing further.

Cycle handling is implicit — entries are keyed by canonicalized absolute
path, so a parents cycle just stops adding new entries the second time
around. We don't expect cycles in practice (layers are write-once and not
modified after creation) but the dedup is cheap insurance.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.layered_file_store import layer_format as fmt


@dataclass(frozen=True)
class ParentPathRemap:
    """Rewrite absolute parent-layer paths read from ``parents.json``.

    Layer dirs are not self-contained: ``parents.json`` holds *absolute*
    paths to parent layer dirs. When a layer closure is built somewhere
    other than where it's read — the motivating case is the podman nix
    backend, where layers are built at in-container ``/nix/store/...``
    paths and then *materialized* (closure-copied) into a host bind-mount
    store at ``<root>/nix/store/...`` — the recorded parent paths don't
    exist at the read site. Rather than rewrite ``parents.json`` during
    the copy (which would duplicate layer-format knowledge in the copier),
    the reader rewrites parent paths on the fly via a **prefix swap**.

    This is **read-side only**: the writer records real local parent paths
    and needs no remap. The hook is scoped to parent resolution because
    ``parents.json`` is the only place the format carries paths
    (``pins.json`` holds FxtrIDs).

    (A prefix-pair is all we need today; ``apply`` is the single seam, so a
    more general callable form could slot in later without touching call
    sites.)
    """

    old_prefix: Path
    new_prefix: Path

    def apply(self, path: Path) -> Path:
        """Swap ``old_prefix`` for ``new_prefix`` if ``path`` is under it;
        otherwise return ``path`` unchanged."""
        if path.is_relative_to(self.old_prefix):
            return self.new_prefix / path.relative_to(self.old_prefix)
        return path


@dataclass
class _LayerDirEntry:
    """Per-layer state held by ``_LayerSearchSet``.

    ``path`` is the canonicalized absolute path to the layer dir.
    ``parents_expanded`` tracks whether we've already unioned this layer's
    declared parents into the enclosing search set.
    """

    path: Path
    parents_expanded: bool = False


class _LayerSearchSet:
    """Dynamic search set of layer directories.

    Lookups (`find_data` / `find_precomp`) walk every known layer in
    insertion order. When a layer with `parents_expanded=False` reports a
    hit, the caller is responsible for triggering
    ``expand_parents_of(layer_path)`` so subsequent walks can reach into
    that layer's own parents.

    The search set validates each layer's ``layer_format.json`` (and
    requires ``pins.json`` / ``parents.json`` to exist via the format
    helpers) the first time it touches the layer.

    ``parent_remap`` (optional) rewrites the absolute paths read from each
    layer's ``parents.json`` before they're added to the set — see
    ``ParentPathRemap``. It is *not* applied to ``initial_layers`` (those
    are already real read-site paths); only to discovered parents.
    """

    def __init__(
        self,
        initial_layers: list[Path],
        *,
        parent_remap: ParentPathRemap | None = None,
    ):
        self._entries: dict[Path, _LayerDirEntry] = {}
        self._parent_remap = parent_remap
        for path in initial_layers:
            self._add_layer(path)

    # ------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------

    def _add_layer(self, path: Path) -> _LayerDirEntry:
        canonical = Path(path).resolve()
        existing = self._entries.get(canonical)
        if existing is not None:
            return existing
        # Validate the layer eagerly: format version + required files
        # presence will surface here. `read_format_version` raises
        # `LayerFormatVersionError` / `LayerMalformedError` on issues.
        fmt.read_format_version(canonical)
        # Touching pins.json and parents.json now would be eager — defer
        # to the first lookup so missing-file errors surface during use.
        entry = _LayerDirEntry(path=canonical)
        self._entries[canonical] = entry
        return entry

    def layers(self) -> list[Path]:
        """Return all known layer paths in insertion order."""
        return list(self._entries.keys())

    # ------------------------------------------------------------
    # Parent expansion
    # ------------------------------------------------------------

    def expand_parents_of(self, layer_path: Path) -> None:
        """If ``layer_path`` has unexpanded parents, read its
        ``parents.json`` and add each parent to the search set.

        Idempotent: subsequent calls for the same layer are no-ops.
        """
        canonical = Path(layer_path).resolve()
        entry = self._entries.get(canonical)
        if entry is None or entry.parents_expanded:
            return
        entry.parents_expanded = True
        for parent in fmt.read_parents(canonical):
            if self._parent_remap is not None:
                parent = self._parent_remap.apply(parent)
            self._add_layer(parent)

    def expand_fully(self) -> None:
        """Expand every known layer's parents transitively, to fixpoint.

        After this returns the set is **complete**: every transitively-reachable
        layer is present and ``expand_parents_of`` is a no-op for all of them, so
        the set is effectively immutable under subsequent ``find_data`` /
        ``find_precomp`` reads. Callers that share one search set across
        concurrent reads (e.g. a deep-fetch source) should call this at
        construction so no read mutates the set mid-walk.
        """
        while True:
            before = len(self._entries)
            for path in list(self._entries.keys()):
                self.expand_parents_of(path)
            if len(self._entries) == before:
                return

    # ------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------

    def find_data(self, fxtr_id: FxtrID) -> Path | None:
        """Return the layer directory whose ``data/`` holds an entry for
        ``fxtr_id``, or ``None`` if no layer has it.

        Side effect: each layer whose data folder we touch has its
        parents expanded into the search set, so subsequent lookups
        consider transitively-reachable layers.
        """
        # We snapshot `layers()` at the start of each iteration in case
        # `expand_parents_of` mutates the dict mid-walk.
        i = 0
        while True:
            paths = list(self._entries.keys())
            if i >= len(paths):
                return None
            path = paths[i]
            if fmt.data_file_exists(path, fxtr_id):
                self.expand_parents_of(path)
                return path
            # No hit here — still expand this layer's parents so the
            # walk reaches deeper, then continue.
            self.expand_parents_of(path)
            i += 1

    def find_precomp(self, fxtr_id: FxtrID) -> Path | None:
        """Return the layer directory whose ``precomp/`` holds an entry
        for ``fxtr_id``, or ``None`` if no layer has it.

        Same side-effect contract as ``find_data``.
        """
        i = 0
        while True:
            paths = list(self._entries.keys())
            if i >= len(paths):
                return None
            path = paths[i]
            if fmt.precomp_file_exists(path, fxtr_id):
                self.expand_parents_of(path)
                return path
            self.expand_parents_of(path)
            i += 1

    def has_data_or_precomp(self, fxtr_id: FxtrID) -> bool:
        """True iff some known layer (transitively, after expansion)
        has either a data or precomp entry for ``fxtr_id``."""
        return self.find_data(fxtr_id) is not None or self.find_precomp(fxtr_id) is not None

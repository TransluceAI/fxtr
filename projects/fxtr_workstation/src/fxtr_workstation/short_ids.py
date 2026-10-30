"""Workstation-internal entity key + short-ID allocator.

**Entity key.** A workstation-tracked entity is identified by a
:class:`ProvenancedKey` — the ``(data_id, prov_parent)`` pair derived
from a (possibly layered) :class:`FxtrID`. Bare :class:`FxtrID`
equality only looks at ``raw_data_id``, so layered IDs that differ
only in their provenance half collide on equality and can't be used
as record keys directly. :class:`ProvenancedKey` is the canonical
key type; use :func:`provenanced_key_for` to derive one.

**Services.** Short IDs are ``s{N}`` autoincrement strings, never
recycled for the lifetime of the server. The ``ServiceKey -> short_id``
mapping persists across deactivation (so a register-unregister-
register cycle returns the same short ID), but is dropped entirely
once the underlying ``IRHandle.token`` ends — past that point the
specific handle can never be reused, so retaining the mapping would
just leak.

**Entities.** Short IDs encode the (data_id, prov_parent) pair using
two schemes depending on the data ID's tag:

- **Prefixable** (``d``/``c``/``n``-tagged data): the bucket gets a
  hex-style prefix (``entity_short_id_prefix_len`` chars by default;
  extended on collision against another data ID's bucket). The
  *first* ``(data_id, *)`` pair seen claims the bare prefix
  (e.g. ``da1b2c``). Subsequent distinct prov parents claim
  ``da1b2c@2``, ``da1b2c@3``, …. A late-arriving ``(data_id, None)``
  claims ``da1b2c@0``. The "untracked-provenance" form
  ``(data_id, u(data_id))``, when not first-seen, claims the
  special ``da1b2c@u`` slot rather than ``@N`` (the u-node's
  content is redundant with the data ID itself, so the suffix
  doesn't need to enumerate). ``@1`` is never allocated — the
  bare prefix is implicitly the @1 form.
- **Non-prefixable** (``.``/``!``/``p``/``u``-tagged data): the
  bucket prefix is the full ``data.id_string()``. ``(data, None)``
  renders bare; ``(data, u(data))`` renders as ``prefix@u`` (same
  redundancy elision as the prefixable case); ``(data, prov)``
  renders as ``f"{prefix}@{shortid_of((prov, None))}"`` — the prov
  half's short ID is recursively allocated. No ``@N``
  enumeration here — the composite shape is unambiguous.

Either way, the (key → short_id) mapping persists for the server's
run lifetime: an entity's content-addressed identity never goes
stale, so re-registration of the same key always returns the same
short ID.

Lookups go through :meth:`ShortIDAllocator.lookup_active_service` /
:meth:`ShortIDAllocator.lookup_active_entity` which only resolve
short IDs whose key is currently active — i.e. the workstation has
at least one live registration tracking it.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from dataclasses import dataclass, field

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceKey

#: Tags whose ``local_id_string`` is a long, prefix-able string.
_PREFIXABLE_TAGS = frozenset({"d", "c", "n"})


def _is_self_u_prov(key: "ProvenancedKey") -> bool:
    """Whether ``key.prov_parent`` is ``u(key.data_id)`` — i.e. the
    "untracked-provenance" form of the data half itself.

    The compact ``@u`` short-id form (a sibling of the ``@0`` /
    ``@N`` forms for prefixable buckets, and the special-cased
    ``prefix@u`` for non-prefixable) is only meaningful in this
    case: every other layered ``(X, u(Y))`` shape is invalid per
    v2's invariant that a u-node's payload equals what it unwraps
    to, so the data half must equal the u-node's payload.
    """
    if key.prov_parent is None:
        return False
    if key.prov_parent.get_tag() != "u":
        return False
    _, payload = key.prov_parent.split_tag_and_payload()
    return payload == key.data_id._raw_data_id


@dataclass(frozen=True)
class ProvenancedKey:
    """Workstation-internal entity key carrying both halves of a
    layered :class:`FxtrID`.

    Used as the key type in
    :attr:`fxtr_workstation.state.WorkstationState.entity_records`,
    session refcount dicts, the short-ID allocator, etc. Both fields
    are *bare* :class:`FxtrID`\\ s (no nested ``_provenance_parent``)
    so equality + hashing behave naturally.

    Construct via :func:`provenanced_key_for` from a (possibly
    layered) FxtrID. Reconstruct the layered form via
    :meth:`to_layered_fxtr_id` for handoff to the v2 scope / arena
    APIs.

    Attributes:
        data_id: The data-level FxtrID (always bare).
        prov_parent: The provenance parent (bare), or ``None`` if
            this entity has no attached provenance.
    """

    data_id: FxtrID
    prov_parent: FxtrID | None

    def to_layered_fxtr_id(self) -> FxtrID:
        """Re-assemble the layered :class:`FxtrID` for this key.

        ``prov_parent`` becomes the layered ID's
        ``_provenance_parent`` if set; otherwise the bare data ID is
        returned.
        """
        if self.prov_parent is None:
            return self.data_id
        return FxtrID(
            raw_data_id=self.data_id._raw_data_id,
            provenance_parent=self.prov_parent,
        )


def provenanced_key_for(fxtr_id: FxtrID) -> ProvenancedKey:
    """Derive a :class:`ProvenancedKey` from a (possibly layered)
    :class:`FxtrID`.

    Strips ``_provenance_parent`` from both halves so the resulting
    key carries only bare FxtrIDs. The data half is the input ID
    with its ``_provenance_parent`` cleared; the prov half is the
    input's ``_provenance_parent`` (cleared too, defensively, since
    the v2 invariant is single-level provenance).
    """
    data_bare = FxtrID(raw_data_id=fxtr_id._raw_data_id)
    prov = fxtr_id._provenance_parent
    if prov is None:
        return ProvenancedKey(data_id=data_bare, prov_parent=None)
    prov_bare = FxtrID(raw_data_id=prov._raw_data_id)
    return ProvenancedKey(data_id=data_bare, prov_parent=prov_bare)


@dataclass
class _EntityBucket:
    """Per-data_id bucket for entity-short-id allocation.

    All keys with the same ``data_id`` share a bucket, distinguished
    by their ``prov_parent``. The bucket stores the bucket's base
    ``prefix`` plus the per-prov rendered short IDs.

    For prefixable buckets, ``next_suffix`` tracks the next ``@N``
    enumeration value to allocate (always ≥ 2 — the bare form
    occupies the implicit @1 slot, ``@0`` is reserved for late
    ``(data, None)``, and we never allocate @1 explicitly).

    For non-prefixable buckets, ``next_suffix`` is unused — every
    distinct prov composes its own ``prefix@<prov_short>`` short
    ID.
    """

    prefix: str
    is_prefixable: bool
    short_ids_by_prov: dict[FxtrID | None, str] = field(default_factory=dict)
    next_suffix: int = 2


@dataclass
class ShortIDAllocator:
    """Run-lifetime short-ID allocator.

    Args:
        entity_short_id_prefix_len: Default prefix length for
            ``d``/``c``/``n``-tagged entity IDs. Extended on
            collision against another data ID's bucket.
    """

    entity_short_id_prefix_len: int = 7
    _service_short_ids: dict[ServiceKey, str] = field(default_factory=dict)
    _service_keys_by_short_id: dict[str, ServiceKey] = field(default_factory=dict)
    _service_counter: Iterator[int] = field(default_factory=lambda: itertools.count())
    _active_service_keys: set[ServiceKey] = field(default_factory=set)
    # Per-data_id buckets. Each bucket holds the short IDs for every
    # ``(data_id, prov_parent)`` pair we've encountered.
    _entity_buckets: dict[FxtrID, _EntityBucket] = field(default_factory=dict)
    # Reverse map: short_id → ProvenancedKey, regardless of whether
    # the key is currently active. Always populated alongside the
    # forward bucket entry; never removed for the lifetime of the
    # server (content-addressed IDs never go stale).
    _keys_by_short_id: dict[str, ProvenancedKey] = field(default_factory=dict)
    _active_entity_keys: set[ProvenancedKey] = field(default_factory=set)
    # Which prefixable prefixes are currently claimed (by data_id).
    # Used to detect collisions when minting a new prefixable bucket
    # so we can extend the prefix one char at a time until unique.
    _prefixable_prefix_to_data_id: dict[str, FxtrID] = field(default_factory=dict)

    # ============================================================
    # Services
    # ============================================================

    def get_or_assign_service(self, key: ServiceKey) -> str:
        """Allocate or look up a short ID for ``key``; mark it active.

        Pure allocation — the allocator does not own lifecycle policy.
        Reclaiming the mapping when the underlying handle dies is the
        caller's responsibility: ``state`` registers a guarded
        handle-token-end callback (``_on_service_handle_token_ended``)
        that calls :meth:`forget_service` only once the record is no
        longer tracked/registered, so a *broken-but-still-tracked*
        service stays resolvable here (its short ID keeps mapping)
        rather than vanishing mid-flight.
        """
        existing = self._service_short_ids.get(key)
        if existing is None:
            n = next(self._service_counter)
            existing = f"s{n}"
            self._service_short_ids[key] = existing
            self._service_keys_by_short_id[existing] = key
        self._active_service_keys.add(key)
        return existing

    def lookup_active_service(self, short_id: str) -> ServiceKey | None:
        """Resolve a short ID to its ServiceKey if currently active."""
        key = self._service_keys_by_short_id.get(short_id)
        if key is None or key not in self._active_service_keys:
            return None
        return key

    def short_id_for_service(self, key: ServiceKey) -> str:
        """Look up the short ID for a previously-allocated key.

        Raises ``KeyError`` if the key has never been allocated; does
        NOT check whether the key is currently active. Useful for
        snapshot builders that walk the record tables (where every
        record's key is guaranteed to have a short ID) without
        wanting to repeat ``get_or_assign`` machinery.
        """
        return self._service_short_ids[key]

    def deactivate_service(self, key: ServiceKey) -> None:
        """Mark a key as no longer active.

        The ``ServiceKey -> short_id`` mapping is preserved so a future
        re-registration of the same key returns the same short ID.
        Use :meth:`forget_service` to drop the mapping entirely.
        """
        self._active_service_keys.discard(key)

    def forget_service(self, key: ServiceKey) -> None:
        """Drop all knowledge of ``key``.

        Called from the handle's token end-callback when the underlying
        handle's lifetime ends — past that point the specific handle
        can never be reused, so the short-ID mapping is dead weight.
        Idempotent: safe to call repeatedly or for keys that are
        already forgotten.
        """
        self._active_service_keys.discard(key)
        short_id = self._service_short_ids.pop(key, None)
        if short_id is not None:
            self._service_keys_by_short_id.pop(short_id, None)

    # ============================================================
    # Entities
    # ============================================================

    def get_or_assign_entity(self, fxtr_id: FxtrID) -> str:
        """Allocate or look up a short ID for ``fxtr_id``'s
        :class:`ProvenancedKey`; mark active.

        Idempotent — the same FxtrID (whether layered or bare,
        modulo equal ``data_id`` + ``prov_parent``) always returns
        the same short ID for the lifetime of the server.

        For non-prefixable data IDs with non-None provenance, the
        prov half's own ``(prov, None)`` short ID is recursively
        allocated as needed (but *not* marked active — only the
        explicitly-requested key becomes active). This keeps the
        ``data.id_string()@<prov_short>`` composite parseable via
        the reverse lookup table.
        """
        return self._assign_entity(fxtr_id, mark_active=True)

    def get_or_assign_entity_inactive(self, fxtr_id: FxtrID) -> str:
        """Like :meth:`get_or_assign_entity` but does *not* mark the
        key active (stage 8b).

        Used by the out-of-band direct entity load path: children
        FxtrIDs in the loaded payload need short_ids for display, but
        the workstation isn't minting :class:`TrackedEntityRecord`s
        for them, so leaving them inactive keeps
        :meth:`lookup_active_entity` honest. The mapping is
        run-lifetime stable, so a key first allocated this way that
        later becomes active (e.g. via a session-bound
        :class:`LoadEntityOp`) keeps the same short_id.
        """
        return self._assign_entity(fxtr_id, mark_active=False)

    def _assign_entity(self, fxtr_id: FxtrID, *, mark_active: bool) -> str:
        key = provenanced_key_for(fxtr_id)
        bucket = self._entity_buckets.get(key.data_id)
        if bucket is None:
            bucket = self._allocate_bucket(key.data_id)
        existing = bucket.short_ids_by_prov.get(key.prov_parent)
        if existing is None:
            existing = self._allocate_entity_short_id(bucket, key)
            bucket.short_ids_by_prov[key.prov_parent] = existing
            self._keys_by_short_id[existing] = key
        if mark_active:
            self._active_entity_keys.add(key)
        return existing

    def _allocate_bucket(self, data_id: FxtrID) -> _EntityBucket:
        """Mint a fresh per-data_id bucket. For prefixable data IDs,
        find a prefix that doesn't collide with any other bucket
        (extending one char at a time on collision)."""
        if data_id.get_tag() in _PREFIXABLE_TAGS:
            full = data_id.id_string()
            for n in range(self.entity_short_id_prefix_len, len(full) + 1):
                candidate = full[:n]
                taken_by = self._prefixable_prefix_to_data_id.get(candidate)
                if taken_by is None:
                    self._prefixable_prefix_to_data_id[candidate] = data_id
                    bucket = _EntityBucket(prefix=candidate, is_prefixable=True)
                    self._entity_buckets[data_id] = bucket
                    return bucket
                # Collision: another data_id already uses this prefix.
                # Extend by one char and retry.
            raise AssertionError(
                f"unable to allocate prefixable short id for {data_id!r}: "
                f"full id_string already mapped to a different data_id"
            )
        # Non-prefixable: use the full id_string as the prefix.
        bucket = _EntityBucket(prefix=data_id.id_string(), is_prefixable=False)
        self._entity_buckets[data_id] = bucket
        return bucket

    def _allocate_entity_short_id(self, bucket: _EntityBucket, key: ProvenancedKey) -> str:
        """Compute a fresh short ID for ``key`` in ``bucket``. Caller
        has verified the key isn't already in ``bucket.short_ids_by_prov``.
        """
        is_self_u = _is_self_u_prov(key)
        if bucket.is_prefixable:
            if not bucket.short_ids_by_prov:
                # First entry in this bucket — claim the bare prefix.
                # (Implicit suffix 1; @1 is never explicitly written.)
                return bucket.prefix
            if key.prov_parent is None:
                # Late-arriving ``(data, None)`` — claim @0.
                return f"{bucket.prefix}@0"
            if is_self_u:
                # ``(X, u(X))`` — the "untracked-provenance" form
                # of X. Claim the special @u slot rather than
                # rendering ``@uX.id_string()`` (which is
                # redundant — the u-node's content is X itself).
                return f"{bucket.prefix}@u"
            # New prov-bearing key — allocate the next @N.
            suffix = bucket.next_suffix
            bucket.next_suffix += 1
            return f"{bucket.prefix}@{suffix}"
        # Non-prefixable.
        if key.prov_parent is None:
            return bucket.prefix
        if is_self_u:
            # ``(X, u(X))`` — drop the redundant inner; ``@u`` is
            # uniquely determined by X's bucket prefix.
            return f"{bucket.prefix}@u"
        # Recurse: the prov half's ``(prov, None)`` short ID
        # composes into ours. Don't mark the prov key as active —
        # this is just naming-side bookkeeping.
        prov_short_id = self._assign_entity(key.prov_parent, mark_active=False)
        return f"{bucket.prefix}@{prov_short_id}"

    def lookup_active_entity(self, short_id: str) -> FxtrID | None:
        """Resolve a short ID to its layered :class:`FxtrID` if the
        key is currently active.

        The returned FxtrID is layered: if the key has a non-None
        ``prov_parent``, the result has its ``_provenance_parent``
        set; otherwise it's the bare data ID. Callers can re-derive
        the :class:`ProvenancedKey` via :func:`provenanced_key_for`
        if they need the structured form.
        """
        key = self._keys_by_short_id.get(short_id)
        if key is None or key not in self._active_entity_keys:
            return None
        return key.to_layered_fxtr_id()

    def lookup_known_entity(self, short_id: str) -> ProvenancedKey | None:
        """Resolve a short ID to its :class:`ProvenancedKey` if the
        short ID was ever allocated (regardless of active state).

        Useful for parsing composite non-prefixable shorts that
        embed a recursively-allocated prov-half short, where the
        prov key may not itself be active.
        """
        return self._keys_by_short_id.get(short_id)

    def short_id_for_entity(self, fxtr_id: FxtrID) -> str:
        """Look up the short ID for a previously-allocated entity
        FxtrID's :class:`ProvenancedKey`.

        Raises ``KeyError`` if not allocated; does NOT check active
        state. Symmetric with :meth:`short_id_for_service` — used by
        snapshot builders / encode callbacks.
        """
        key = provenanced_key_for(fxtr_id)
        bucket = self._entity_buckets[key.data_id]
        return bucket.short_ids_by_prov[key.prov_parent]

    def deactivate_entity(self, fxtr_id: FxtrID) -> None:
        """Mark an entity's :class:`ProvenancedKey` as no longer
        active. The (key → short_id) mapping is preserved.
        """
        self._active_entity_keys.discard(provenanced_key_for(fxtr_id))

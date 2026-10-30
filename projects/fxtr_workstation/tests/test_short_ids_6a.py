"""Stage 6a.3 unit tests for the provenance-aware short-ID allocator.

Covers the new bucket-based scheme:

- Prefixable (``d``/``c``/``n``) data IDs:
    - First-seen ``(data, *)`` claims the bare prefix (suffix 1 implicit).
    - Subsequent distinct provenance parents claim ``@2``, ``@3``, ...
    - Late-arriving ``(data, None)`` claims ``@0``.
    - ``@1`` is never allocated.
    - Re-allocation of the same key returns the same short ID
      (run-lifetime stable).
    - Collision-extending the prefix on conflict against another data
      ID with a shared bucket prefix.
- Non-prefixable (``.``/``!``/``p``/``u``) data IDs:
    - ``(data, None)`` renders bare (no ``@``).
    - ``(data, prov)`` renders as ``f"{data.id_string()}@{shortid_of((prov, None))}"``.
    - Multi-level: composing through layered provenance.
- Round-trip lookup: ``lookup_active_entity`` returns the layered
  :class:`FxtrID` (with ``_provenance_parent`` attached when prov is
  not None); ``lookup_known_entity`` resolves even when the key isn't
  marked active.
"""

from __future__ import annotations

import hashlib

from fxtr2.data.entity_core import FxtrID
from fxtr_workstation.short_ids import ProvenancedKey, ShortIDAllocator, provenanced_key_for


def _d_id(seed: bytes) -> FxtrID:
    """Make a fresh ``d``-tagged FxtrID from ``seed``."""
    return FxtrID.from_tag_and_payload("d", hashlib.sha256(seed).digest())


def _n_id(seed: bytes) -> FxtrID:
    return FxtrID.from_tag_and_payload("n", hashlib.sha256(seed).digest())


def _c_id(seed: bytes) -> FxtrID:
    return FxtrID.from_tag_and_payload("c", hashlib.sha256(seed).digest())


def _layered(data: FxtrID, prov: FxtrID) -> FxtrID:
    return FxtrID(raw_data_id=data._raw_data_id, provenance_parent=prov)


# ============================================================
# Prefixable: first-seen claims bare; @0 / @N allocation rules.
# ============================================================


def test_first_seen_data_none_claims_bare_prefix() -> None:
    """First ``(data, None)`` for a fresh data_id gets the bare prefix
    (no ``@`` suffix)."""
    alloc = ShortIDAllocator()
    data = _d_id(b"a")
    sid = alloc.get_or_assign_entity(data)
    # Bare prefix: no ``@``.
    assert "@" not in sid
    # Default prefix length is 7 chars.
    assert len(sid) == 7


def test_first_seen_data_with_prov_claims_bare_prefix() -> None:
    """If the *first* call for a data_id is layered, the layered key
    claims the bare prefix; the unprovenanced key is allocated later
    and gets ``@0``."""
    alloc = ShortIDAllocator()
    data = _d_id(b"b")
    prov = _n_id(b"prov")

    sid_layered = alloc.get_or_assign_entity(_layered(data, prov))
    assert "@" not in sid_layered

    # Now request the bare key — it gets ``@0``.
    sid_bare = alloc.get_or_assign_entity(data)
    assert sid_bare == f"{sid_layered}@0"


def test_subsequent_distinct_provenance_increments_at_two() -> None:
    """Second, third, ... distinct prov parents allocate ``@2``,
    ``@3``, … skipping ``@1`` (the bare form occupies the implicit
    @1 slot)."""
    alloc = ShortIDAllocator()
    data = _d_id(b"c")
    prov_a = _n_id(b"prov_a")
    prov_b = _n_id(b"prov_b")
    prov_c = _n_id(b"prov_c")

    sid_first = alloc.get_or_assign_entity(data)  # bare (suffix 1).
    sid_a = alloc.get_or_assign_entity(_layered(data, prov_a))
    sid_b = alloc.get_or_assign_entity(_layered(data, prov_b))
    sid_c = alloc.get_or_assign_entity(_layered(data, prov_c))

    assert sid_a == f"{sid_first}@2"
    assert sid_b == f"{sid_first}@3"
    assert sid_c == f"{sid_first}@4"


def test_at_zero_form_only_when_data_none_arrives_late() -> None:
    """Order: ``(data, prov_a)``, ``(data, None)``, ``(data, prov_b)``
    yields bare, ``@0``, ``@2``."""
    alloc = ShortIDAllocator()
    data = _d_id(b"d")
    prov_a = _n_id(b"a")
    prov_b = _n_id(b"b")

    sid_first = alloc.get_or_assign_entity(_layered(data, prov_a))
    sid_none = alloc.get_or_assign_entity(data)
    sid_b = alloc.get_or_assign_entity(_layered(data, prov_b))

    assert "@" not in sid_first
    assert sid_none == f"{sid_first}@0"
    assert sid_b == f"{sid_first}@2"


def test_at_one_is_never_allocated() -> None:
    """Sanity check: across many distinct prov parents, no allocation
    ever produces ``prefix@1``."""
    alloc = ShortIDAllocator()
    data = _d_id(b"e")
    seen: set[str] = set()
    seen.add(alloc.get_or_assign_entity(data))  # bare
    seen.add(alloc.get_or_assign_entity(data))  # bare again (idempotent)
    for i in range(10):
        seen.add(alloc.get_or_assign_entity(_layered(data, _n_id(f"p{i}".encode()))))
    # ``@1`` is reserved for the bare form (which renders without ``@``).
    assert not any(s.endswith("@1") for s in seen), seen


def test_self_u_prov_uses_at_u_suffix_for_prefixable() -> None:
    """``(X, u(X))`` for prefixable X claims ``prefix@u`` (when not
    first-seen) rather than ``@N`` — the u-node's content is
    redundant with X itself, so the suffix doesn't enumerate."""
    alloc = ShortIDAllocator()
    data = _d_id(b"u_prefixable")
    u_self = FxtrID.from_tag_and_payload("u", data._raw_data_id)

    sid_first = alloc.get_or_assign_entity(data)  # bare
    sid_uself = alloc.get_or_assign_entity(_layered(data, u_self))
    assert sid_uself == f"{sid_first}@u"

    # Mixing in a later non-self prov still allocates @2 (the @u
    # slot is reserved for self-u and doesn't count against the
    # @N counter).
    prov_other = _n_id(b"other")
    sid_other = alloc.get_or_assign_entity(_layered(data, prov_other))
    assert sid_other == f"{sid_first}@2"


def test_self_u_prov_first_seen_claims_bare_for_prefixable() -> None:
    """If ``(X, u(X))`` is the first form seen, it claims the bare
    prefix (per the existing first-seen rule). A late-arriving
    ``(X, None)`` then gets ``@0``; a third distinct prov gets
    ``@2``."""
    alloc = ShortIDAllocator()
    data = _d_id(b"u_first")
    u_self = FxtrID.from_tag_and_payload("u", data._raw_data_id)
    prov_other = _n_id(b"other")

    sid_uself = alloc.get_or_assign_entity(_layered(data, u_self))
    sid_none = alloc.get_or_assign_entity(data)
    sid_other = alloc.get_or_assign_entity(_layered(data, prov_other))

    assert "@" not in sid_uself  # bare
    assert sid_none == f"{sid_uself}@0"
    assert sid_other == f"{sid_uself}@2"


def test_self_u_prov_uses_at_u_suffix_for_non_prefixable() -> None:
    """For non-prefixable X (e.g. a ``!`` builtin), ``(X, u(X))``
    renders as ``X.id_string()@u`` rather than the verbose
    ``X.id_string()@u<X.id_string()>`` composite."""
    alloc = ShortIDAllocator()
    builtin = FxtrID.from_tag_and_payload("!", b"some_type")
    u_self = FxtrID.from_tag_and_payload("u", builtin._raw_data_id)

    sid = alloc.get_or_assign_entity(_layered(builtin, u_self))
    assert sid == "!some_type@u"


def test_self_u_prov_lookup_round_trips() -> None:
    """``lookup_active_entity`` on a ``@u`` short ID returns the
    layered ``(X, u(X))`` FxtrID."""
    alloc = ShortIDAllocator()
    data = _d_id(b"u_roundtrip")
    u_self = FxtrID.from_tag_and_payload("u", data._raw_data_id)
    alloc.get_or_assign_entity(data)  # claim bare first
    sid = alloc.get_or_assign_entity(_layered(data, u_self))

    got = alloc.lookup_active_entity(sid)
    assert got is not None
    assert got._raw_data_id == data._raw_data_id
    assert got._provenance_parent == u_self


def test_re_allocation_of_same_key_is_idempotent() -> None:
    """Run-lifetime stable: re-registering the same key returns the
    same short ID even after a deactivate cycle."""
    alloc = ShortIDAllocator()
    data = _d_id(b"f")
    prov = _n_id(b"f-prov")

    sid_a1 = alloc.get_or_assign_entity(_layered(data, prov))
    sid_b1 = alloc.get_or_assign_entity(data)
    alloc.deactivate_entity(_layered(data, prov))
    alloc.deactivate_entity(data)
    sid_a2 = alloc.get_or_assign_entity(_layered(data, prov))
    sid_b2 = alloc.get_or_assign_entity(data)

    assert sid_a1 == sid_a2
    assert sid_b1 == sid_b2


def test_prefix_collision_extends_one_char_at_a_time() -> None:
    """Two distinct data IDs whose id_strings share a 7-char prefix get
    different bucket prefixes — the colliding bucket extends the
    prefix until unique."""
    # Use a tiny prefix length so collisions are easy to engineer.
    alloc = ShortIDAllocator(entity_short_id_prefix_len=2)
    # Find two ``d`` IDs whose id_strings agree on the first 2 chars.
    # ``id_string()`` for ``d`` is ``"d" + base32_lower(payload)``, so
    # any two distinct payloads whose first base32 char matches will
    # do. We brute-force a pair to keep the test deterministic.
    candidates: list[FxtrID] = []
    for i in range(256):
        fid = FxtrID.from_tag_and_payload("d", bytes([i]) * 32)
        if fid.id_string()[:2] == "da":
            candidates.append(fid)
        if len(candidates) >= 2:
            break
    assert len(candidates) >= 2, "could not find two colliding payloads"
    a, b = candidates[0], candidates[1]
    assert a.id_string()[:2] == b.id_string()[:2] == "da"

    sid_a = alloc.get_or_assign_entity(a)
    sid_b = alloc.get_or_assign_entity(b)
    # First taker gets the bare 2-char prefix.
    assert sid_a == "da"
    # Second extends.
    assert sid_b.startswith("da")
    assert len(sid_b) > 2
    assert sid_b != sid_a


# ============================================================
# Non-prefixable: composite ``data@<prov-short>`` form.
# ============================================================


def test_non_prefixable_data_none_renders_bare_id_string() -> None:
    """``(., None)`` and ``(!str, None)`` render as their full
    ``id_string()`` with no ``@`` suffix."""
    alloc = ShortIDAllocator()
    dot = FxtrID.from_tag_and_payload(".", b"")
    builtin = FxtrID.from_tag_and_payload("!", b"str")

    assert alloc.get_or_assign_entity(dot) == "."
    assert alloc.get_or_assign_entity(builtin) == "!str"


def test_non_prefixable_with_provenance_composes_through_prov_short_id() -> None:
    """``(., prov)`` renders as ``.@<short_of((prov, None))>``."""
    alloc = ShortIDAllocator()
    dot = FxtrID.from_tag_and_payload(".", b"")
    prov_data = _c_id(b"x")  # c-tag, prefixable
    layered = _layered(dot, prov_data)

    sid = alloc.get_or_assign_entity(layered)
    # The prov-half short ID is independently allocated.
    prov_short = alloc.short_id_for_entity(prov_data)
    assert sid == f".@{prov_short}"


def test_recursive_prov_allocation_does_not_mark_prov_active() -> None:
    """Allocating a non-prefixable composite recursively allocates the
    prov-half short_id for naming purposes only — the prov key is
    *not* marked active. ``lookup_active_entity`` for the prov-half
    short returns None, but ``lookup_known_entity`` still resolves
    it."""
    alloc = ShortIDAllocator()
    dot = FxtrID.from_tag_and_payload(".", b"")
    prov_data = _c_id(b"y")
    alloc.get_or_assign_entity(_layered(dot, prov_data))

    prov_short = alloc.short_id_for_entity(prov_data)
    # The prov-half itself wasn't get-or-assigned by the test; it was
    # only allocated for the composite name. So it's not active.
    assert alloc.lookup_active_entity(prov_short) is None
    # But the mapping is known.
    known = alloc.lookup_known_entity(prov_short)
    assert known == ProvenancedKey(data_id=prov_data, prov_parent=None)


# ============================================================
# Lookup round-trip.
# ============================================================


def test_lookup_round_trip_layered_id() -> None:
    """``lookup_active_entity(short_id)`` returns the layered FxtrID
    with ``_provenance_parent`` reconstructed from the underlying
    :class:`ProvenancedKey`."""
    alloc = ShortIDAllocator()
    data = _d_id(b"r")
    prov = _n_id(b"r-prov")
    layered = _layered(data, prov)
    sid = alloc.get_or_assign_entity(layered)

    got = alloc.lookup_active_entity(sid)
    assert got is not None
    # FxtrID equality ignores prov, so compare both halves explicitly.
    assert got._raw_data_id == data._raw_data_id
    assert got._provenance_parent == prov


def test_lookup_round_trip_bare_id() -> None:
    """``lookup_active_entity`` for a bare key returns the bare
    FxtrID (no ``_provenance_parent``)."""
    alloc = ShortIDAllocator()
    data = _d_id(b"s")
    sid = alloc.get_or_assign_entity(data)

    got = alloc.lookup_active_entity(sid)
    assert got is not None
    assert got._raw_data_id == data._raw_data_id
    assert got._provenance_parent is None


def test_lookup_for_inactive_returns_none() -> None:
    """Once ``deactivate_entity`` runs, ``lookup_active_entity``
    returns None — but ``lookup_known_entity`` still resolves."""
    alloc = ShortIDAllocator()
    data = _d_id(b"t")
    sid = alloc.get_or_assign_entity(data)
    alloc.deactivate_entity(data)
    assert alloc.lookup_active_entity(sid) is None
    assert alloc.lookup_known_entity(sid) == provenanced_key_for(data)

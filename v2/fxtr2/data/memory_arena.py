"""In-memory implementation of fxtr arenas with lease-backed lifetime management.

This module provides the primary in-memory storage backend for fxtr:

- ``MemoryStore``: A global backing store that holds entity data and tracks token-keyed
  ownership / inheritance for each node. Tokens are minted and owned by callers
  (typically via ``UnconditionalLocalLifetime``) and *registered* with the store. The
  store is a subscriber to each token's end-callback; it does not own the lifetime,
  hold a lease on the token, or cache the token's grant — grant uniqueness is the
  lifetime owner's invariant to enforce, not the subscriber's. All scopes share a
  single global ``MemoryStore``.

- ``MemoryArena``: A "view" into the ``MemoryStore`` with a tracked liveset of
  accessible entities (the liveset lives in ``MemoryStore._arenas[key].liveset``).
  Each arena is associated with a lifetime token and validates every public call by
  consuming a ``Lease`` for that token, checking that the lease's subject is this
  arena's token and that it is still active. Grant-level checking is not performed
  at this layer (same subscriber rationale as the store).

- ``LifetimeToken``: Abstract identity objects from ``fxtr2.leases.lease_types``. The
  store treats every token as a fully independent storage layer; it does **not**
  record parent / ancestor relationships, hold parent-lease clones, or perform
  antichain bookkeeping. Lifetime ordering between scopes (parent must outlive
  child) is enforced by ``FxtrScope``'s own ancestor cap-bridges, which already
  hold parent-lease clones independent of this layer.

Each node tracks two independent token-keyed sets:

- ``owned_tokens``: tokens that have direct ownership of this node — its data is in
  memory and the same token also covers (owns or has an inheritance bridge for)
  every dep.
- ``inheritance_bridges``: per-token entries describing how *that* token reaches
  this node when it doesn't own it directly. Each entry pairs a *source descriptor*
  (a foreign ``ReadableArena[Y]`` or a ``_SameStoreSentinel(target_token=Y)`` for the
  same-store case) with a ``(X_reg, Y)`` bridge record stored on
  ``_token_data[X_reg].bridges[Y]``. The bridge is a ``LocalBridge`` (the same
  ``IndependentLocalBridge | DependentLocalBridge`` taxonomy used by ``FxtrScope``);
  each at-rest lease in the bridge is what structurally pins ``Y`` alive while
  ``X_reg`` is alive.

A token T has access to node N **iff** ``T in N.owned_tokens`` *or*
``T in N.inheritance_bridges``. There is no implicit ancestor access: callers that
want T to reach a node go through ``lazily_attach_from_arena`` (or the FxtrScope
``lift`` path that wraps it) to install an inheritance entry.

GC is lease-driven: when a registered token ends (all outstanding leases discarded,
or the caller-owned lifetime revoked), the store's end-callback runs and nodes
whose ``owned_tokens`` *and* ``inheritance_bridges`` become empty are freed. No
explicit invalidation call is needed.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import logging
import os
import traceback
import weakref
from typing import Any, Collection, Mapping, cast, override

import anyio
import cbor2

from fxtr2.data import entity_core
from fxtr2.data.abstract_arena import (
    DepKind,
    FxtrEntityFlatData,
    LeaseGrantMismatchError,
    ReadableArena,
    ReadWriteArena,
)
from fxtr2.data.arena_util import (
    visit_and_project_ids,
    visit_and_project_ids_in_canonical_cbor_order,
    visit_ids,
)
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)
from fxtr2.leases.lease_bridges import DependentLocalBridge, IndependentLocalBridge, LocalBridge
from fxtr2.leases.lease_types import Lease, LifetimeBrand, LifetimeToken

logger = logging.getLogger(__name__)


# =============================================================================
# Unified node types
# =============================================================================


@dataclasses.dataclass
class NodeData:
    """In-memory loaded data for a node.

    Whether a node is "stored" (has bytes) vs "derived" (contentless) is determined by
    whether entity_bytes is None. The d/n vs c/p/u distinction comes from the node ID's tag.
    """

    entity_bytes: bytes | None
    """CBOR bytes for d/n nodes. None for derived nodes (c/p/u/./!)."""

    requires: frozenset[FxtrID]
    """Direct dependency IDs (other nodes referenced by this node's data)."""

    strong_linked_refs: frozenset[FxtrID]
    """Strong links (d/n nodes only). Subset of requires."""

    ctx_node: FxtrID | None
    """Context/pathroot node (c/p nodes only)."""

    backstep_node: FxtrID | None
    """Backstep node (p nodes only). Set only on marker-position p-children
    (i.e. p-nodes projected from a ``RawFxtrEntityMarkerID`` slot in
    ``_bare_load_impl``'s p-projector). Marker-position p-children always
    have an ``unwraps_to`` that's a contentless tag (`.`/`c`/`p`/`u`),
    which can't be loaded — so they never spawn their own children, which
    means no marker-position p-child can itself be the parent of another
    marker-position child. Backstep chains therefore have length exactly 1,
    which bounds the sync-dep cascade depth (see ``MemoryStore`` docstring)."""


@dataclasses.dataclass(frozen=True)
class _SameStoreSentinel:
    """Inheritance source marker: 'load N under ``target_token`` in this same MemoryStore'.

    Used by ``MemoryStore.bare_lazily_attach_from_arena`` when the supplied source is a
    ``MemoryArena`` bound to the same store. Loading through a sentinel entry recurses
    into ``_ensure_own`` under ``target_token`` rather than calling the source arena's
    ``load_flat``, avoiding the unnecessary copy-and-abandon dance.
    """

    target_token: LifetimeToken[Any]


@dataclasses.dataclass
class _InheritanceEntry:
    """How a single token (the entry's key on ``MemoryStoreNode.inheritance_bridges``)
    reaches a node when it doesn't own it directly.

    The ``source`` carries either a real foreign ``ReadableArena[Y]`` or a
    ``_SameStoreSentinel(target_token=Y)``. The bridging shape — i.e. how to mint a
    ``Lease[Y]`` per load — is stored once per ``(X_reg, Y)`` on
    ``_token_data[X_reg].bridges[Y]``; this entry just records *which* source the
    node uses for that ``X_reg``.
    """

    source: ReadableArena[Any] | _SameStoreSentinel

    def source_token(self) -> LifetimeToken[Any]:
        if isinstance(self.source, _SameStoreSentinel):
            return self.source.target_token
        return self.source.token


@dataclasses.dataclass
class MemoryStoreNode:
    """A node in the global memory store.

    A node may have in-memory data, per-token inheritance entries, or both. Two
    independent token-keyed sets describe access:

    - ``owned_tokens``: tokens with direct ownership (data is in memory and this
      token covers every dep). Set membership only — no antichain, no domination.
    - ``inheritance_bridges``: per-token entries (one ``_InheritanceEntry`` per
      ``X_reg``) describing the source ``X_reg`` reaches this node through. Each
      entry's bridge record is shared across nodes via
      ``_token_data[X_reg].bridges[Y]`` where ``Y = entry.source_token()``.

    A token T has access iff it is a key in either map. The two are independent:
    a token may own a node and *also* have an inheritance entry on it (rare, but
    legal — ownership is the fast path for loads, inheritance still pins the
    bridge for any deps it covers).
    """

    my_id: FxtrID

    data: NodeData | None
    """In-memory loaded data. Always populated for derived tags (``./!/c/p/u``)
    once any token has access to the node — the discovery / fresh-store paths
    that grant access fill it in eagerly. May be None for d/n nodes whose
    entity bytes haven't been loaded yet (an async load will populate it)."""

    unwraps_to: FxtrID | None
    """The node this one unwraps to (provenance nodes only). Always available even without data,
    because it is set when the node is first discovered (from inheritance or from storing)."""

    inheritance_bridges: dict[LifetimeToken[Any], _InheritanceEntry]
    """Per-token inheritance entries: ``X_reg`` → entry describing the source X_reg
    reaches this node through. The corresponding bridge record (the ``LocalBridge``,
    its ``claimed_ids`` set, and its break-callback bookkeeping) lives in
    ``_token_data[X_reg].bridges[entry.source_token()]``.
    """

    owned_tokens: set[LifetimeToken[Any]]
    """Tokens that have direct ownership: data in memory + every dep covered."""

    wrapped_by: set[FxtrID]
    """Nodes that unwrap to this one. Used for deep-fetch traversal, not for GC."""


# =============================================================================
# Arena types
# =============================================================================
@dataclasses.dataclass(frozen=True)
class ArenaKey:
    """A key for an arena of entities (unique within a store)."""

    ix: int


@dataclasses.dataclass
class MemoryArenaState:
    """Tracking state for an arena (lives on the ``MemoryStore``)."""

    my_key: ArenaKey
    token: LifetimeToken[Any]
    liveset: set[FxtrID]


# =============================================================================
# Bridge records (per-(X_reg, Y) shared state)
# =============================================================================


@dataclasses.dataclass
class _BridgeRecord:
    """Store-side record for a ``(X_reg, Y)`` inheritance bridge.

    Keyed by ``Y`` in ``_token_data[X_reg].bridges``. Holds the ``LocalBridge``
    (taking ownership of every at-rest lease at registration) plus the set of node
    IDs that currently use this bridge for inheritance under ``X_reg``.

    Lifetime invariant: each at-rest lease inside ``bridge`` keeps its target
    lifetime alive — so as long as ``X_reg`` is alive (and hasn't released this
    bridge via ``_release_bridge_claim`` for the last claimed node), ``Y`` is also
    alive. This is the new model's structural replacement for the old
    parent/ancestor lease tracking that ``LifetimeTokenData`` used to perform.
    """

    bridge: LocalBridge
    """The ``LocalBridge`` (Independent or Dependent) used to mint ``Lease[Y]``
    on each load. ``IndependentLocalBridge.lease`` or every value in
    ``DependentLocalBridge.leases`` is the at-rest lease this record owns."""

    claimed_ids: set[FxtrID]
    """Node IDs whose ``inheritance_bridges[X_reg]`` references this bridge.
    Tracked so we know when to tear the record down (empty set)."""

    deregister_callbacks: list[Any]
    """Deregister handles returned from ``register_broken_callback`` on each at-rest
    lease in ``bridge``. Called on intentional teardown to detach the subscriptions
    before the leases are discarded."""

    break_reason: BaseException | None = None
    """If any at-rest lease breaks, the reason is stored here and re-surfaced on
    any subsequent ``_invoke_bridge`` attempt. Stays ``None`` on intentional
    teardown because we deregister callbacks before discarding."""


@dataclasses.dataclass
class LifetimeTokenData:
    """Per-token bookkeeping on ``MemoryStore``.

    Populated when the token is registered via ``MemoryStore.register_token``. The
    store is a subscriber to the caller-owned lifetime: it holds no lease on this
    token, no cached grant, and no parent / ancestor information.
    """

    node_associations: set[FxtrID]
    """Reverse index: every node ID where this token appears in ``owned_tokens``
    or ``inheritance_bridges``. Used for O(1) teardown when the token ends."""

    bridges: dict[LifetimeToken[Any], _BridgeRecord]
    """Inheritance bridges this token registered as ``X_reg``: maps ``Y`` to its
    shared ``_BridgeRecord``. Conceptually
    ``dict[LifetimeToken[X_reg], dict[LifetimeToken[Y], _BridgeRecord]]``; node
    storage uses ``Any`` brands."""


# =============================================================================
# Errors
# =============================================================================


class InheritanceParentInvariantError(Exception):
    """Error raised when an inheritance parent fails to track a child node it is supposed to have."""


# =============================================================================
# MemoryStore
# =============================================================================

DEBUG_DROPPED_ARENAS = os.environ.get("FXTR_DEBUG_DROPPED_ARENAS", "0") == "1"


class MemoryStore:
    """Global backing store for in-memory entity data.

    All scopes share a single ``MemoryStore`` (via the ``GLOBAL_MEMORY_STORE``
    contextvar). Every token is a fully independent storage layer; the store does
    not record parent/ancestor relationships or perform antichain bookkeeping.

    External arenas (SQLite, Git, RPC) can be attached to nodes as inheritance
    sources via ``bare_lazily_attach_from_arena``, supplying a ``LocalBridge`` that
    mints fresh ``Lease[Y]`` per load. Same-store inheritance is detected
    automatically and stored as a sentinel; callers don't see the distinction.

    All token-level bookkeeping lives in ``_token_data[token]`` — the abstract
    ``LifetimeToken`` object is a pure identity and carries no mutable state of
    its own.

    **Sync-Dep Closure invariant.** For every token T and every node N in the
    store: if T has access to N (owned or inherited), then T has access to
    every *sync-discoverable* dep of N — recursively. Sync-discoverable deps
    are the relations exposed by sync queries on the arena protocol:

    - ``N.unwraps_to`` (read by ``unwrap_provenance``)
    - ``N.data.ctx_node`` for c/p (read by ``c_node_to_context_data`` /
      ``p_node_to_pathroot``)
    - ``N.data.backstep_node`` for p (read by ``step_back_p_ctx``)

    The invariant guarantees that any sync query returning a dep ID hands
    back an ID T can immediately track in its arena. Load-time deps for
    d/n nodes (CBOR children listed in ``data.requires``) are *not* covered
    by this invariant — they are not sync-discoverable, and access to them
    is granted lazily during async loads.

    The invariant is maintained by routing every gain-of-access through
    one of two primitives:

    - ``_add_owned_token`` for direct ownership grants (called when the
      caller already has the data locally — fresh stores, store dedup,
      ownership upgrades). It **strict-asserts** that the token already
      has access to every dep (``unwraps_to`` plus everything in
      ``data.requires``); the proof of T's right to own the node has to
      come from the caller's prior work, never from the function. If the
      assertion fires, an upstream caller violated its precondition.
    - ``_adopt_inheritance_entry`` for source-driven adoptions. It walks
      the sync-dep chain through source queries and installs each node's
      claim: derived nodes (``./!/c/p/u``, where the source can hand
      over everything via sync queries) get **owned** entries directly
      — building the proof bottom-up as the recursion unwinds — while
      d/n nodes (entity bytes still need an async load) get **inherited**
      entries pointing at the source so future loads flow through the
      bridge.

    Callers that grant ownership of a node whose deps aren't yet
    accessible are responsible for establishing that access first
    (typical patterns: bottom-up recursive sub-store of the unwrap
    projection on the n/p store paths — fresh and dedup'd alike —
    deps-first worklists for batch transfers). ``_add_owned_token``'s
    assertion is the safety net that catches contract violations before
    they propagate into silently-broken loads.
    """

    def __init__(self, *, force_async_sleep: bool = False):
        self._force_async_sleep = force_async_sleep

        # Entity states
        self._entity_states: dict[FxtrID, MemoryStoreNode] = {}

        # Bytes storage (inline, replaces InMemoryBytesBackingStore)
        self._bytes_data: dict[bytes, bytes] = {}
        self._bytes_refcounts: dict[bytes, int] = {}

        # Token management: one row per known token (reverse index + bridges).
        self._token_data: dict[LifetimeToken[Any], LifetimeTokenData] = {}

        # Arena management: one row per open arena (liveset + token). ``weakref.finalize``
        # on each ``MemoryArena`` calls ``_finalize_arena_object`` so dropping an arena
        # without ``close()`` still runs cleanup and can log leak warnings.
        self._arena_counter: int = 0
        self._arenas: dict[ArenaKey, MemoryArenaState] = {}
        # Present only when DEBUG_DROPPED_ARENAS is enabled — captures a stack
        # trace at arena-creation time to help pin down leak sites.
        self._debug_dropped_arena_tbs: dict[ArenaKey, traceback.StackSummary] = {}

    # -------------------------------------------------------------------------
    # Bytes storage (internal)
    # -------------------------------------------------------------------------

    def _save_bytes(self, key: bytes, value: bytes) -> None:
        """Save bytes to the inline store, incrementing refcount."""
        self._bytes_data[key] = value
        self._bytes_refcounts[key] = self._bytes_refcounts.get(key, 0) + 1

    def _get_bytes(self, key: bytes) -> bytes:
        """Get bytes from the inline store."""
        return self._bytes_data[key]

    def _free_bytes(self, key: bytes) -> None:
        """Decrement refcount and free bytes if zero."""
        ct = self._bytes_refcounts[key] - 1
        if ct == 0:
            del self._bytes_data[key]
            del self._bytes_refcounts[key]
        else:
            self._bytes_refcounts[key] = ct

    # -------------------------------------------------------------------------
    # Token management
    # -------------------------------------------------------------------------

    def register_token(
        self,
        token: LifetimeToken[Any],
        *,
        name: str = "",
    ) -> None:
        """Register a pre-existing lifetime token with the store.

        The token is minted by the caller — typically as
        ``UnconditionalLocalLifetime(name).token``. The store becomes a subscriber:
        it allocates an empty ``LifetimeTokenData`` row and subscribes
        ``_on_token_end`` to the token's end-callback for GC.

        The store does **not** record parent/ancestor relationships; lifetime
        ordering between scopes is enforced by callers via lease bridges (e.g.
        ``FxtrScope``'s ancestor cap-bridges hold parent-lease clones).

        Args:
            token: The caller-owned lifetime token to register.
            name: Optional label used only in debug log messages.

        Raises:
            RuntimeError: if ``token`` is already registered.
        """
        if token in self._token_data:
            raise RuntimeError(f"Token {token!r} is already registered on this store")

        self._token_data[token] = LifetimeTokenData(
            node_associations=set(),
            bridges={},
        )

        # Register the end-callback for GC. The callback fires when all outstanding
        # leases for this lifetime are discarded (or on revoke). We register after
        # populating ``_token_data`` so a synchronously-fired callback (caller passed
        # an already-ended token — DOA) finds a populated row it can tear down.
        token.register_end_callback(lambda: self._on_token_end(token))

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Token {token!r} registered ({name!r})")

    def _on_token_end(self, token: LifetimeToken[Any]) -> None:
        """End-callback for a token: drop its claims on every node it touches and tear
        down every bridge it registered.

        Fired when all outstanding leases for the token have been discarded (or on
        ``revoke``). Idempotent-ish: the token may have already been scrubbed from the
        store if something ran earlier; guard against missing entries.
        """
        row = self._token_data.pop(token, None)
        if row is None:
            logger.warning(f"Token {token!r} not found in _token_data; already ended?")
            return

        node_associations = row.node_associations

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                f"Token {token!r} ended ({len(node_associations)} nodes, "
                f"{len(row.bridges)} bridges)"
            )

        # Remove token from every node's claim maps; collect arenas to close at
        # end (deduped by id) and nodes that become empty. For inheritance entries
        # we just collect the source arena (if not a sentinel) — per-node
        # ``arena.abandon`` would require a Lease[Y] for each call, and at this
        # point we're about to discard the bridge anyway. ``arena.close()`` is
        # the bulk teardown signal.
        nodes_to_free: list[MemoryStoreNode] = []
        arenas_to_close: dict[int, ReadableArena[Any]] = {}
        for node_id in list(node_associations):
            node = self._entity_states.get(node_id)
            if node is None:
                continue
            node.owned_tokens.discard(token)
            entry = node.inheritance_bridges.pop(token, None)
            if entry is not None and not isinstance(entry.source, _SameStoreSentinel):
                arenas_to_close.setdefault(id(entry.source), entry.source)
            if not node.owned_tokens and not node.inheritance_bridges:
                nodes_to_free.append(node)

        # Tear down every bridge this token registered: deregister break-callbacks
        # then discard the at-rest leases.
        for _y_token, record in row.bridges.items():
            for cb in record.deregister_callbacks:
                try:
                    cb()
                except Exception as e:  # noqa: BLE001 — best-effort detach
                    logger.error(
                        "Error deregistering broken-callback for bridge"
                        f" {token!r}->{_y_token!r}: {e}"
                    )
            record.bridge.discard_at_rest()

        # Close all foreign source arenas owned by entries we popped above.
        for arena in arenas_to_close.values():
            arena.close()

        # Free nodes whose claim maps both became empty.
        for node in nodes_to_free:
            self._free_node(node)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Token {token!r} torn down, freed {len(nodes_to_free)} nodes")

    # -------------------------------------------------------------------------
    # Lease validation (internal)
    # -------------------------------------------------------------------------

    def _take_and_validate(self, lease: Lease[Any]) -> tuple[LifetimeToken[Any], Lease[Any]]:
        """Take the caller's lease, validate it, return ``(token, taken)``.

        The caller is responsible for discarding ``taken`` (typically ``with taken:``).
        ``taken`` may be cloned internally as needed.

        Validates token identity (subject is known to this store) and liveness. Grant
        checking is not performed; the store is a subscriber to caller-owned lifetimes
        and relies on the lifetime owner to enforce grant uniqueness.
        """
        taken = lease.take()
        try:
            token = taken.subject
            if token not in self._token_data:
                raise RuntimeError(
                    f"Lease {taken!r} references token {token!r} which is not known to this store"
                )
            taken.ensure_active()
            return token, taken
        except BaseException:
            taken.discard()
            raise

    # -------------------------------------------------------------------------
    # Bridge registration / invocation (internal)
    # -------------------------------------------------------------------------

    def _register_bridge[
        B_X: LifetimeBrand, B_Y: LifetimeBrand
    ](
        self,
        x_reg: LifetimeToken[B_X],
        y: LifetimeToken[B_Y],
        bridge: LocalBridge,
    ) -> _BridgeRecord:
        """Idempotently register a ``(x_reg, y)`` bridge record.

        If a record already exists for the pair, the caller's ``bridge`` is
        discarded via ``bridge.discard_at_rest()`` and the existing record is
        returned. Otherwise the bridge is taken (this method now owns its at-rest
        leases) and a broken-callback is registered on each at-rest lease so that
        any one breaking marks the whole record broken.
        """
        per_x = self._token_data[x_reg].bridges
        existing = per_x.get(y)
        if existing is not None:
            bridge.discard_at_rest()
            return existing

        record = _BridgeRecord(
            bridge=bridge,
            claimed_ids=set(),
            deregister_callbacks=[],
        )
        per_x[y] = record

        def _on_break(reason: str) -> None:
            if record.break_reason is None:
                record.break_reason = RuntimeError(
                    f"Bridge lease for ({x_reg!r}->{y!r}) broke: {reason}"
                )
            logger.error(
                "Bridge lease for X_reg=%r Y=%r broke; affected IDs: %r — subsequent "
                "loads will raise. Reason: %s",
                x_reg,
                y,
                sorted(record.claimed_ids, key=lambda i: repr(i)),
                reason,
            )

        # Subscribe to every at-rest lease in the bridge. For
        # IndependentLocalBridge there's exactly one (``bridge.lease``); for
        # DependentLocalBridge there's one per stored input lease.
        at_rest_leases: list[Lease[Any]]
        if isinstance(bridge, IndependentLocalBridge):
            at_rest_leases = [bridge.lease]
        else:
            assert isinstance(bridge, DependentLocalBridge)
            at_rest_leases = list(bridge.leases.values())

        for ls in at_rest_leases:
            record.deregister_callbacks.append(ls.register_broken_callback(_on_break))

        return record

    def _invoke_bridge[
        B_X: LifetimeBrand, B_Y: LifetimeBrand
    ](self, x_reg: LifetimeToken[B_X], y: LifetimeToken[B_Y], base: Lease[B_X],) -> Lease[B_Y]:
        """Produce a fresh ``Lease[Y]`` via the stored ``(x_reg, y)`` bridge.

        ``base`` is a fresh ``Lease[X_reg]`` produced by the caller (consumed here).
        Raises the stored ``break_reason`` if any input lease has already broken,
        in which case ``base`` is still consumed (discarded).
        """
        x_row = self._token_data.get(x_reg)
        per_x = x_row.bridges if x_row is not None else {}
        record = per_x.get(y)
        if record is None:
            base.discard()
            raise RuntimeError(f"No bridge entry for (X_reg={x_reg!r}, Y={y!r})")
        if record.break_reason is not None:
            base.discard()
            raise record.break_reason

        bridge = record.bridge
        if isinstance(bridge, IndependentLocalBridge):
            base.discard()
            return cast(Lease[B_Y], bridge.lease.clone())
        else:
            assert isinstance(bridge, DependentLocalBridge)
            cloned_inputs = {tok: ls.clone() for tok, ls in bridge.leases.items()}
            return cast(Lease[B_Y], bridge.converter(cloned_inputs, base))

    def _release_bridge_claim[
        B_X: LifetimeBrand, B_Y: LifetimeBrand
    ](self, x_reg: LifetimeToken[B_X], y: LifetimeToken[B_Y], node_id: FxtrID,) -> None:
        """Drop ``node_id`` from the ``(x_reg, y)`` bridge's claim set; tear the
        bridge down if that was the last claim."""
        x_row = self._token_data.get(x_reg)
        if x_row is None:
            return
        per_x = x_row.bridges
        record = per_x.get(y)
        if record is None:
            return
        record.claimed_ids.discard(node_id)
        if not record.claimed_ids:
            for cb in record.deregister_callbacks:
                try:
                    cb()
                except Exception as e:  # noqa: BLE001 — best-effort detach
                    logger.error(
                        f"Error deregistering broken-callback for bridge {x_reg!r}->{y!r}: {e}"
                    )
            record.bridge.discard_at_rest()
            del per_x[y]

    # -------------------------------------------------------------------------
    # Node management (internal)
    # -------------------------------------------------------------------------

    def _get_or_create_node(self, node_id: FxtrID) -> MemoryStoreNode:
        """Get an existing node or create a new empty one (no data, no claims)."""
        node = self._entity_states.get(node_id)
        if node is not None:
            return node
        node = MemoryStoreNode(
            my_id=node_id,
            data=None,
            unwraps_to=None,
            inheritance_bridges={},
            owned_tokens=set(),
            wrapped_by=set(),
        )
        self._entity_states[node_id] = node
        return node

    def _populate_node_data(
        self, node: MemoryStoreNode, data: NodeData, unwraps_to: FxtrID | None
    ) -> None:
        """Populate a node's data, upgrading from inheritance-only if needed.

        If the node already has data, this is a no-op (dedup).
        If the node was inheritance-only, the data is populated and the unwraps_to
        relationship is verified to be consistent.
        """
        if node.data is not None:
            assert node.unwraps_to == unwraps_to, (
                f"Node {node.my_id}: unwraps_to mismatch: existing={node.unwraps_to},"
                f" new={unwraps_to}"
            )
            return

        node.data = data

        # Set up unwraps_to if not already set
        if node.unwraps_to is None and unwraps_to is not None:
            node.unwraps_to = unwraps_to
            # Set up wrapped_by on the unwrapped node
            unwrapped = self._entity_states.get(unwraps_to)
            if unwrapped is not None:
                unwrapped.wrapped_by.add(node.my_id)
        elif node.unwraps_to is not None:
            assert node.unwraps_to == unwraps_to, (
                f"Node {node.my_id}: unwraps_to mismatch: existing={node.unwraps_to},"
                f" new={unwraps_to}"
            )

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Node {node.my_id}: data populated")

    def _free_node(self, node: MemoryStoreNode) -> None:
        """Free a node from the store (both claim maps must be empty)."""
        assert (
            not node.owned_tokens
        ), f"Cannot free node {node.my_id} with owned_tokens={node.owned_tokens!r}"
        assert not node.inheritance_bridges, (
            f"Cannot free node {node.my_id} with inheritance_bridges="
            f"{list(node.inheritance_bridges.keys())!r}"
        )

        # Free bytes data if stored
        if node.data is not None and node.data.entity_bytes is not None:
            self._free_bytes(node.my_id._raw_data_id)

        # Remove from wrapped_by of unwrapped node
        if node.unwraps_to is not None:
            unwrapped = self._entity_states.get(node.unwraps_to)
            if unwrapped is not None:
                unwrapped.wrapped_by.discard(node.my_id)

        # Remove from entity states
        del self._entity_states[node.my_id]

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Node {node.my_id}: freed from store")

    def _already_has_data_for(self, node_id: FxtrID) -> bool:
        """Check if a node has in-memory data (not just inheritance)."""
        node = self._entity_states.get(node_id)
        return node is not None and node.data is not None

    def _add_owned_token(self, node: MemoryStoreNode, token: LifetimeToken[Any]) -> bool:
        """Add ``token`` to the node's ``owned_tokens`` (idempotent).

        **Strict precondition.** ``token`` must already have access (owned or
        inherited) to every dep of ``node``:

        - ``node.unwraps_to`` (if not None) — the sync-discoverable unwrap.
        - Every ID in ``node.data.requires`` — covers sync-deps for derived
          tags (ctx_node, backstep_node) and load-time deps for d/n
          (CBOR children).

        ``node.data`` must also be populated; an unowned-data node has no
        legitimate path to ownership. The function does *not* establish
        access to deps — it only verifies the precondition and records the
        new claim. Constructing the proof is the caller's job: bottom-up
        recursive sub-stores for store paths (fresh *and* dedup'd — the
        n/p cases store their unwrap projection before granting
        ownership), ``_adopt_inheritance_entry`` for source-driven
        adoptions, etc. If the assertion fires, some upstream caller
        violated the Sync-Dep-Closure / load-time-dep contract; granting
        ownership without dep access would silently break later loads or
        sync queries.

        Returns True if the token was actually added at this node, False if
        it was already present. There is no domination check — every token
        owns independently.
        """
        if token in node.owned_tokens:
            return False
        assert node.data is not None, (
            f"_add_owned_token: cannot grant ownership of {node.my_id} "
            "with unpopulated NodeData (no proof that token covers deps)"
        )
        if node.unwraps_to is not None:
            unwrap_node = self._entity_states[node.unwraps_to]
            assert self._node_has_access(unwrap_node, token), (
                f"_add_owned_token: token {token!r} cannot own {node.my_id} "
                f"— no access to unwraps_to={node.unwraps_to}"
            )
        for dep_id in node.data.requires:
            dep_node = self._entity_states[dep_id]
            assert self._node_has_access(dep_node, token), (
                f"_add_owned_token: token {token!r} cannot own {node.my_id} "
                f"— no access to requires entry {dep_id}"
            )
        node.owned_tokens.add(token)
        self._token_data[token].node_associations.add(node.my_id)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Node {node.my_id}: added token {token!r} to owned_tokens")
        return True

    def _node_has_access(self, node: MemoryStoreNode, token: LifetimeToken[Any]) -> bool:
        """True iff ``token`` owns the node directly or has an inheritance entry on it."""
        return token in node.owned_tokens or token in node.inheritance_bridges

    # -------------------------------------------------------------------------
    # Inheritance entry adoption
    # -------------------------------------------------------------------------

    def _adopt_inheritance_entry[
        B_X: LifetimeBrand
    ](
        self,
        node_id: FxtrID,
        source: ReadableArena[Any] | _SameStoreSentinel,
        x_reg: LifetimeToken[B_X],
        x_reg_lease: Lease[B_X],
    ) -> MemoryStoreNode:
        """Adopt ``node_id`` into ``x_reg``'s claims via the given source.

        This is the inheritance-grant primitive that maintains the Sync-Dep
        Closure invariant (see ``MemoryStore`` class docstring). The flow is
        the same whether the node is new to the store or already present:

        1. Discover every sync-discoverable dep by issuing source queries
           (``unwrap_provenance`` for any provenance node, plus
           ``c_node_to_context_data`` for c-nodes and ``p_node_to_pathroot``
           / ``step_back_p_ctx`` for p-nodes). For foreign sources these
           queries auto-track each result in the source's liveset, which is
           precisely the proof that the source has the dep accessible — we
           never install a claim we don't actually have. (For sentinel
           sources the same info is read from ``_entity_states``.)
        2. Recursively adopt each sync-dep under ``x_reg`` via the same
           source. The recursive query against the now-tracked dep grows
           the source liveset further, so the cascade naturally covers the
           whole closure.
        3. Install ``x_reg``'s claim on the node:

           - **Derived nodes** (``./!/c/p/u``, where ``data`` is fully
             populated by the discovery step): install as **owned**, not
             inherited. The node's data is everything sync-readable; no
             future async load could fetch more, so an inheritance entry
             pointing at the source would be dead weight. For foreign
             sources we also abandon the source's claim on this node.
           - **d/n nodes** (``data`` is None — entity bytes still need an
             async load): install as **inherited** under the source. The
             bridge keeps the source alive for future loads.

        If ``x_reg`` already has access (owned or inherited) on the node,
        this is an idempotent no-op; any redundant fork claim is abandoned.

        ``x_reg_lease`` is a live ``Lease[x_reg]`` (typically a clone) used
        to mint per-call ``Lease[Y]`` values for source queries. The caller
        retains ownership; this method clones as needed.
        """
        y = source.target_token if isinstance(source, _SameStoreSentinel) else source.token
        x_row = self._token_data.get(x_reg)
        per_x = x_row.bridges if x_row is not None else {}
        record = per_x.get(y)
        assert record is not None, (
            "_adopt_inheritance_entry: no bridge record for "
            f"(X_reg={x_reg!r}, Y={y!r}) — should have been registered at attach time"
        )

        existing = self._entity_states.get(node_id)
        if existing is not None and self._node_has_access(existing, x_reg):
            # x_reg already has access (owned or inherited). The caller may
            # have handed us a freshly-forked source arena; if so, we owe an
            # abandon on it. Skip the abandon if x_reg's existing inheritance
            # entry happens to point at this exact source (legal under the
            # arena-X_reg-binding invariant for nodes reached via shared
            # forks during recursion).
            if not isinstance(source, _SameStoreSentinel):
                existing_entry = existing.inheritance_bridges.get(x_reg)
                if existing_entry is None or existing_entry.source is not source:
                    y_lease = self._invoke_bridge(x_reg, y, x_reg_lease.clone())
                    source.abandon(y_lease, node_id)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"Node {node_id}: x_reg already has access, idempotent skip")
            return existing

        # Discover sync-deps from the source. For foreign sources this issues
        # arena queries that auto-track each result in the source's liveset,
        # so by the time we install the entry the source genuinely has the
        # deps tracked. For sentinel sources this reads from ``_entity_states``.
        unwraps_to, data = self._discover_sync_deps_from_source(node_id, source, x_reg, x_reg_lease)

        # Sanity-check consistency with existing in-store data, if any. The
        # source's view of the node must match what we already have.
        if existing is not None:
            assert existing.unwraps_to == unwraps_to, (
                f"_adopt_inheritance_entry: source-reported unwraps_to {unwraps_to} "
                f"for {node_id} doesn't match in-store {existing.unwraps_to}"
            )
            if existing.data is not None and data is not None:
                assert (
                    existing.data.ctx_node == data.ctx_node
                ), f"_adopt_inheritance_entry: source-reported ctx_node mismatch for {node_id}"
                assert (
                    existing.data.backstep_node == data.backstep_node
                ), f"_adopt_inheritance_entry: source-reported backstep_node mismatch for {node_id}"

        # Cascade through sync-discoverable deps. For foreign sources each
        # recursive call issues another round of source queries, growing the
        # source's liveset to cover the dep chain.
        if unwraps_to is not None:
            unwrapped_node = self._adopt_inheritance_entry(unwraps_to, source, x_reg, x_reg_lease)
            if existing is None:
                unwrapped_node.wrapped_by.add(node_id)
        if data is not None:
            if data.ctx_node is not None:
                self._adopt_inheritance_entry(data.ctx_node, source, x_reg, x_reg_lease)
            if data.backstep_node is not None:
                self._adopt_inheritance_entry(data.backstep_node, source, x_reg, x_reg_lease)

        # Install x_reg's claim. Derived nodes (data populated) get
        # ownership directly — there's nothing left for the source to serve.
        # d/n nodes get an inheritance entry so future async loads can flow
        # through the bridge.
        if data is not None:
            # Derived: install as owned.
            if existing is None:
                node = MemoryStoreNode(
                    my_id=node_id,
                    data=data,
                    unwraps_to=unwraps_to,
                    inheritance_bridges={},
                    owned_tokens={x_reg},
                    wrapped_by=set(),
                )
                self._entity_states[node_id] = node
            else:
                existing.owned_tokens.add(x_reg)
                node = existing
            self._token_data[x_reg].node_associations.add(node_id)
            # Foreign source: release the source's claim on this node since
            # we won't be loading from it.
            if not isinstance(source, _SameStoreSentinel):
                y_lease = self._invoke_bridge(x_reg, y, x_reg_lease.clone())
                source.abandon(y_lease, node_id)
        else:
            # d/n: install inheritance entry; bridge serves future loads.
            if existing is None:
                node = MemoryStoreNode(
                    my_id=node_id,
                    data=None,
                    unwraps_to=unwraps_to,
                    inheritance_bridges={x_reg: _InheritanceEntry(source=source)},
                    owned_tokens=set(),
                    wrapped_by=set(),
                )
                self._entity_states[node_id] = node
            else:
                existing.inheritance_bridges[x_reg] = _InheritanceEntry(source=source)
                node = existing
            self._token_data[x_reg].node_associations.add(node_id)
            record.claimed_ids.add(node_id)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                f"Node {node_id}: claim installed (X_reg={x_reg!r}, "
                f"new={existing is None}, "
                f"as={'owned' if data is not None else 'inherited'})"
            )
        return node

    def _discover_sync_deps_from_source[
        B_X: LifetimeBrand
    ](
        self,
        node_id: FxtrID,
        source: ReadableArena[Any] | _SameStoreSentinel,
        x_reg: LifetimeToken[B_X],
        x_reg_lease: Lease[B_X],
    ) -> tuple[FxtrID | None, NodeData | None]:
        """Discover ``node_id``'s sync-discoverable deps from ``source``.

        Returns ``(unwraps_to, data)``:

        - ``unwraps_to``: the dep returned by ``source.unwrap_provenance``,
          or None for nodes without an unwrap (d, ., !).
        - ``data``: a fully-populated ``NodeData`` for derived tags
          (./!/c/p/u), or None for d/n (whose bytes still need an async
          load to reach).

        For foreign sources this issues live sync queries
        (``unwrap_provenance`` plus ``c_node_to_context_data`` /
        ``p_node_to_pathroot`` / ``step_back_p_ctx`` for c/p), each of
        which has the side effect of auto-tracking its result in the
        source's liveset — which is what makes those deps reachable
        through the bridge for any later inheritance entries the caller
        installs. For ``_SameStoreSentinel`` sources the same info is
        read directly from the in-store node under the sentinel's target
        token (which the Sync-Dep Closure invariant guarantees has
        fully-populated data for derived tags).

        ``data is not None`` for the toplevel result is the caller's
        signal that the node is fully knowable from the source and can
        be installed as owned rather than inherited.
        """
        y = source.target_token if isinstance(source, _SameStoreSentinel) else source.token
        tag = node_id.get_tag()

        if isinstance(source, _SameStoreSentinel):
            # Read deps directly from the in-store node under target_token.
            # By the sync-dep closure invariant (and the precondition that
            # target_token has access to node_id), data is already populated
            # for derived tags.
            existing_under_y = self._entity_states.get(node_id)
            assert existing_under_y is not None, (
                f"_adopt_inheritance_entry: same-store sentinel for X_reg={x_reg!r} "
                f"target={y!r} requires node {node_id} to already exist in the store; "
                "caller must ensure-own under the target_token before adopting."
            )
            unwraps_to = existing_under_y.unwraps_to
            if tag in (".", "!", "u", "c", "p"):
                src_data = existing_under_y.data
                assert src_data is not None, (
                    f"_adopt_inheritance_entry: derived node {node_id} under "
                    f"target_token {y!r} should have populated data per the "
                    "sync-dep closure invariant."
                )
                ctx_node = src_data.ctx_node
                backstep_node = src_data.backstep_node
            else:
                # d/n: data may legitimately be None (bytes need async load).
                ctx_node = None
                backstep_node = None
        else:
            # Foreign source: issue sync queries.
            with self._invoke_bridge(x_reg, y, x_reg_lease.clone()) as y_lease:
                unwraps_to = source.unwrap_provenance(y_lease.clone(), node_id)
                ctx_node = None
                backstep_node = None
                if tag == "c":
                    ctx_node = source.c_node_to_context_data(y_lease.clone(), node_id)
                elif tag == "p":
                    ctx_node = source.p_node_to_pathroot(y_lease.clone(), node_id)
                    backstep_node = source.step_back_p_ctx(y_lease.clone(), node_id)

        # Build NodeData for derived tags. For d/n leave data=None.
        data: NodeData | None
        if tag in (".", "!"):
            data = NodeData(
                entity_bytes=None,
                requires=frozenset(),
                strong_linked_refs=frozenset(),
                ctx_node=None,
                backstep_node=None,
            )
        elif tag == "u":
            data = NodeData(
                entity_bytes=None,
                requires=frozenset(),
                strong_linked_refs=frozenset(),
                ctx_node=None,
                backstep_node=None,
            )
        elif tag == "c":
            assert ctx_node is not None
            data = NodeData(
                entity_bytes=None,
                requires=frozenset({ctx_node}),
                strong_linked_refs=frozenset(),
                ctx_node=ctx_node,
                backstep_node=None,
            )
        elif tag == "p":
            assert ctx_node is not None
            requires = (
                frozenset({ctx_node, backstep_node})
                if backstep_node is not None
                else frozenset({ctx_node})
            )
            data = NodeData(
                entity_bytes=None,
                requires=requires,
                strong_linked_refs=frozenset(),
                ctx_node=ctx_node,
                backstep_node=backstep_node,
            )
        else:
            # d / n — bytes are not sync-discoverable.
            data = None

        return unwraps_to, data

    def _adopt_inheritance_entry_or_none[
        B_X: LifetimeBrand
    ](
        self,
        node_id: FxtrID | None,
        source: ReadableArena[Any] | _SameStoreSentinel,
        x_reg: LifetimeToken[B_X],
        x_reg_lease: Lease[B_X],
    ) -> (MemoryStoreNode | None):
        if node_id is None:
            return None
        return self._adopt_inheritance_entry(node_id, source, x_reg, x_reg_lease)

    # -------------------------------------------------------------------------
    # Loading from inheritance entries
    # -------------------------------------------------------------------------

    async def _ensure_own[
        B: LifetimeBrand
    ](self, node_id: FxtrID, token: LifetimeToken[B], caller_lease: Lease[B],) -> MemoryStoreNode:
        """Ensure a node is owned by the given token, loading through its
        inheritance entry if needed.

        For d/n nodes, either recurses under the sentinel target token
        (sync, same store) or loads from the foreign source arena (async).
        For derived nodes, the Sync-Dep Closure invariant guarantees data
        is already populated and the sync-dep chain is reachable to the
        token, so the upgrade is a single ``_add_owned_token`` call.
        """
        node = self._entity_states[node_id]
        if token in node.owned_tokens:
            return node

        match node_id.get_tag():
            case "d" | "n":
                entry = node.inheritance_bridges.get(token)
                if entry is None:
                    raise RuntimeError(
                        f"_ensure_own: token {token!r} has no access to node {node_id}"
                    )
                source = entry.source
                y = entry.source_token()
                if isinstance(source, _SameStoreSentinel):
                    # Same-store sentinel: recurse under Y, then propagate the
                    # newly-discovered deps as inheritance entries under token.
                    # Wrap minted y_lease (recursive call doesn't take it).
                    with self._invoke_bridge(token, y, caller_lease.clone()) as y_lease:
                        target_node = await self._ensure_own(node_id, y, y_lease)
                    # Adopt deps under our token (cloning the bridge once per dep
                    # via _adopt_inheritance_entry's internal claim tracking).
                    assert target_node.data is not None
                    for dep_id in target_node.data.requires:
                        self._adopt_inheritance_entry(dep_id, source, token, caller_lease)
                    if target_node.unwraps_to is not None and node.unwraps_to is None:
                        node.unwraps_to = target_node.unwraps_to
                        unwrapped = self._entity_states.get(target_node.unwraps_to)
                        if unwrapped is not None:
                            unwrapped.wrapped_by.add(node_id)
                    self._add_owned_token(node, token)
                    return self._entity_states[node_id]
                else:
                    y_lease = self._invoke_bridge(token, y, caller_lease.clone())
                    flat_data = await source.load_flat(y_lease, node_id)
                    all_dep_ids = set(dep_id for dep_id, _ in flat_data.deps)
                    try:
                        for dep_id in all_dep_ids:
                            self._adopt_inheritance_entry(dep_id, source, token, caller_lease)
                    except InheritanceParentInvariantError as exc:
                        exc.add_note(f"While loading {node_id} from {source}")
                        raise
                    self._bare_store_flat_impl(flat_data, token)
                    return self._entity_states[node_id]
            case "." | "!" | "c" | "p" | "u":
                # Derived: data is already populated and sync-deps reachable
                # (Sync-Dep Closure invariant). Just upgrade to ownership.
                if token not in node.inheritance_bridges:
                    raise RuntimeError(
                        f"_ensure_own: token {token!r} has no access to node {node_id}"
                    )
                assert node.data is not None
                self._add_owned_token(node, token)
                return node
            case tag:
                raise ValueError(f"Unsupported node tag: {node_id} -> {tag}")

    # -------------------------------------------------------------------------
    # Inheritance arena attachment
    # -------------------------------------------------------------------------

    def bare_lazily_attach_from_arena[
        B_X: LifetimeBrand, B_Y: LifetimeBrand
    ](
        self,
        lease: Lease[B_X],
        node_ids: FxtrID | Collection[FxtrID],
        source: ReadableArena[B_Y],
        bridge: LocalBridge,
    ) -> None:
        """Lazily attach one or more nodes from a source arena under ``lease``'s token.

        ``bridge`` is a ``LocalBridge`` (the ``IndependentLocalBridge |
        DependentLocalBridge`` union from ``fxtr2.leases.lease_bridges``) describing
        how to mint ``Lease[Y]`` per load. Its at-rest leases are taken on first
        attach for a given ``(X_reg, Y)``; subsequent attaches for the same pair
        discard the new bridge and reuse the existing record.

        Same-store detection: if ``source`` is a ``MemoryArena`` bound to this same
        ``MemoryStore``, the attach is performed in *sentinel mode* — the source
        arena is **not** forked via ``copy_to_new_arena`` (we don't need its
        liveset; sync queries read straight from ``_entity_states`` under
        ``source.token``). For foreign sources we fork once over ``node_ids``
        and hand the fork to ``_adopt_inheritance_entry``, which grows the
        fork's liveset via source queries as the cascade walks the sync-dep
        closure. Per-node claim shape — owned for derived (``./!/c/p/u``)
        nodes, inherited for d/n — is decided inside the cascade; callers
        don't see the distinction.

        Empty ``node_ids``: registers the bridge but skips the fork-and-adopt phase.

        For ``IndependentLocalBridge`` we cheap-validate
        ``bridge.lease.subject is source.token`` at attach time. Mismatches in the
        ``DependentLocalBridge`` case (where the converter output is a runtime
        decision) surface as a grant-mismatch on first dispatch.
        """
        node_id_list: list[FxtrID]
        if isinstance(node_ids, FxtrID):
            node_id_list = [node_ids]
        else:
            node_id_list = list(node_ids)

        # Cheap pre-check: IndependentLocalBridge's lease must mint Lease[Y].
        if isinstance(bridge, IndependentLocalBridge):
            if bridge.lease.subject is not source.token:
                lease.discard()
                bridge.discard_at_rest()
                raise ValueError(
                    f"IndependentLocalBridge lease subject {bridge.lease.subject!r} "
                    f"does not match source.token {source.token!r}"
                )

        x_reg, taken_x = self._take_and_validate(lease)
        with taken_x:
            # Decide sentinel vs foreign mode.
            sentinel_mode = isinstance(source, MemoryArena) and source._store is self
            stored_source: ReadableArena[Any] | _SameStoreSentinel
            if sentinel_mode:
                stored_source = _SameStoreSentinel(target_token=source.token)
            else:
                stored_source = None  # type: ignore[assignment] — set below

            # Register / dedup the (X_reg, Y) bridge record.
            self._register_bridge(x_reg, source.token, bridge)
            if not node_id_list:
                return

            if sentinel_mode:
                for nid in node_id_list:
                    self._adopt_inheritance_entry(nid, stored_source, x_reg, taken_x)
                return

            # Foreign mode: fork the source so we have a private liveset, then adopt
            # each node against the fork. The fork is structurally pinned by
            # whichever inheritance entries the cascade installs pointing at it
            # (for d/n deps that need future async loads). If no entries get
            # installed (e.g., everything adopted is derived and owned outright),
            # the fork drops out of scope here and ``weakref.finalize`` cleans it
            # up; we don't need an explicit close.
            base_for_source = taken_x.clone()
            y_lease = self._invoke_bridge(x_reg, source.token, base_for_source)
            owned_source_arena = source.copy_to_new_arena(y_lease, node_id_list)
            for nid in node_id_list:
                self._adopt_inheritance_entry(nid, owned_source_arena, x_reg, taken_x)

    # -------------------------------------------------------------------------
    # Store operations
    # -------------------------------------------------------------------------

    def bare_store(self, lease: Lease[Any], entity: FxtrCbor) -> FxtrID:
        """Store an entity in the store, assigning the caller's token to its ``owned_tokens``.

        ``lease`` is validated against the store's known tokens and consumed. All
        children of the entity must already be in ``_entity_states``.
        """
        token, taken = self._take_and_validate(lease)
        with taken:
            return self._bare_store_impl(entity, token, taken)

    def _bare_store_impl[
        B: LifetimeBrand
    ](self, entity: FxtrCbor, token: LifetimeToken[B], caller_lease: Lease[B]) -> FxtrID:
        """Internal entry point for storing an entity under ``token``."""
        assert isinstance(entity, dict)
        assert "$fxtr.ctx" in entity
        ctx_id = entity["$fxtr.ctx"]
        assert isinstance(ctx_id, entity_core.RawFxtrEntityMarkerID)

        ctx_tag, ctx_payload = ctx_id.inner_id.split_tag_and_payload()
        match ctx_tag:
            case ".":
                # Ground "d" node.
                entity_bytes = entity_core.fxtr_cbor_dumps_with_marker_check(entity)
                new_id = entity_core.FxtrID.from_tag_and_payload(
                    "d", hashlib.sha256(entity_bytes).digest()
                )

                if self._already_has_data_for(new_id):
                    # Dedup: the bytes are already saved; just grant
                    # ownership. ``_add_owned_token`` verifies ``token`` has
                    # access to every CBOR child (covered by the arena
                    # layer's ``_ensure_all_accessible_for_storing``);
                    # d-nodes have no unwrap chain to ensure.
                    self._add_owned_token(self._entity_states[new_id], token)
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"Node {new_id} already in store (dedup)")
                    return new_id

                requires: set[FxtrID] = set()
                strong_links: set[FxtrID] = set()

                def _d_visitor(
                    child_typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
                ) -> None:
                    assert child_typed_id.inner_id in self._entity_states
                    requires.add(child_typed_id.inner_id)
                    if isinstance(child_typed_id, RawStrongFxtrID):
                        strong_links.add(child_typed_id.inner_id)

                visit_ids(entity, _d_visitor)

                self._save_bytes(new_id._raw_data_id, entity_bytes)
                node = self._get_or_create_node(new_id)
                data = NodeData(
                    entity_bytes=entity_bytes,
                    requires=frozenset(requires),
                    strong_linked_refs=frozenset(strong_links),
                    ctx_node=None,
                    backstep_node=None,
                )
                self._populate_node_data(node, data, unwraps_to=None)
                self._add_owned_token(node, token)
                return new_id

            case "c":
                # Stored provenance "n" node.
                entity_bytes = entity_core.fxtr_cbor_dumps_with_marker_check(entity)
                new_id = entity_core.FxtrID.from_tag_and_payload(
                    "n", hashlib.sha256(entity_bytes).digest()
                )

                n_requires: set[FxtrID] = set()
                n_strong_links: set[FxtrID] = set()

                def _n_projector(
                    child_typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
                ) -> FxtrID:
                    child_id = child_typed_id.inner_id
                    assert child_id in self._entity_states
                    child_node = self._entity_states[child_id]
                    if child_node.unwraps_to is None:
                        raise ValueError(
                            "Illegal entity: 'n'-type node has non-provenance child:"
                            f" {child_id}. All children of an 'n'-node must be provenance nodes."
                        )
                    n_requires.add(child_id)
                    if isinstance(child_typed_id, RawStrongFxtrID):
                        n_strong_links.add(child_id)
                    return child_node.unwraps_to

                unwrap_data = visit_and_project_ids(entity, _n_projector)

                # Recursively store the unwrapped (d-form) node first — on the
                # dedup path below as well. Invariant upheld: storing an
                # entity leaves ``token`` owning the n *and* its unwrap
                # projection, with the d-form bytes saved locally — the same
                # end state whether or not the n was already present (content
                # dedup must not change outcomes). A populated n does not
                # imply a populated d-form: foreign loads (``_ensure_own`` →
                # ``_bare_store_flat_impl``) populate only the n and adopt its
                # ``unwraps_to`` as a dataless inheritance entry, and
                # ``_add_owned_token`` refuses ownership of dataless nodes.
                # The d-form bytes are derivable from the cbor in hand, so we
                # re-derive them and route them through the normal store path,
                # whose own dedup branches make every level idempotent:
                # already-populated levels reduce to an ownership grant,
                # dataless levels get their bytes saved. Token access to the
                # d-form's CBOR children holds by the Sync-Dep Closure
                # invariant (each is the ``unwraps_to`` of an accessible
                # child of the n-form). This mirrors the "p" case below,
                # which also unconditionally stores its unwrap projection
                # before granting ownership.
                # TODO: a dedup'd n-store could skip re-deriving the d-form
                # (projection + hash) by first checking that every node on
                # the existing ``unwraps_to`` chain already has data.
                unwrap_id = self._bare_store_impl(unwrap_data, token, caller_lease)

                if self._already_has_data_for(new_id):
                    # Dedup: the n's bytes are already saved, and the
                    # recursion above ensured ``token`` owns the populated
                    # unwrap chain. ``_add_owned_token`` verifies the
                    # remaining preconditions (access to ``unwraps_to`` and
                    # to every CBOR child; the latter is covered by the
                    # arena layer's ``_ensure_all_accessible_for_storing``).
                    node = self._entity_states[new_id]
                    assert node.unwraps_to == unwrap_id, (
                        f"Node {new_id}: unwraps_to mismatch on dedup'd store:"
                        f" existing={node.unwraps_to}, recomputed={unwrap_id}"
                    )
                    self._add_owned_token(node, token)
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"Node {new_id} already in store (dedup)")
                    return new_id

                self._save_bytes(new_id._raw_data_id, entity_bytes)
                node = self._get_or_create_node(new_id)
                data = NodeData(
                    entity_bytes=entity_bytes,
                    requires=frozenset(n_requires),
                    strong_linked_refs=frozenset(n_strong_links),
                    ctx_node=None,
                    backstep_node=None,
                )
                self._populate_node_data(node, data, unwraps_to=unwrap_id)
                self._add_owned_token(node, token)
                return new_id

            case "p":
                # Path provenance "p" node.
                payload_data = cbor2.loads(ctx_payload)
                assert isinstance(payload_data, list)
                assert len(payload_data) >= 2, "any valid p node has a nonempty path"
                external_payload = payload_data[:-1]
                new_id = FxtrID.from_tag_and_payload("p", cbor2.dumps(external_payload))

                ctx_node = self._entity_states[ctx_id.inner_id]

                p_child_counter = 0

                def _p_projector(
                    child_typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
                ) -> FxtrID:
                    nonlocal p_child_counter
                    expected_child_id = FxtrID.from_tag_and_payload(
                        "p", cbor2.dumps(external_payload + [p_child_counter])
                    )
                    p_child_counter += 1
                    child_id = child_typed_id.inner_id
                    if child_id != expected_child_id:
                        raise ValueError(
                            f"Illegal p-tag entity: child ID {child_id} does not match"
                            f" expected {expected_child_id}."
                        )
                    assert child_id in self._entity_states
                    child_node = self._entity_states[child_id]
                    assert child_node.unwraps_to is not None
                    return child_node.unwraps_to

                unwrap_data = visit_and_project_ids_in_canonical_cbor_order(entity, _p_projector)

                if new_id in self._entity_states:
                    retrieved_node = self._entity_states[new_id]
                elif token not in ctx_node.owned_tokens:
                    # ctx is reachable to us only via inheritance — step back through
                    # that source to discover the parent p-node.
                    entry = ctx_node.inheritance_bridges.get(token)
                    if entry is None:
                        raise RuntimeError(
                            f"Cannot store p-node: token {token!r} has no access to "
                            f"ctx node {ctx_node.my_id}"
                        )
                    source = entry.source
                    y = entry.source_token()
                    if isinstance(source, _SameStoreSentinel):
                        # Same-store sentinel: by the Sync-Dep Closure invariant,
                        # ctx_node's data was populated at adoption time.
                        assert ctx_node.data is not None
                        backstep_id = ctx_node.data.backstep_node
                    else:
                        y_lease = self._invoke_bridge(token, y, caller_lease.clone())
                        backstep_id = source.step_back_p_ctx(y_lease, ctx_node.my_id)
                    retrieved_node = self._adopt_inheritance_entry_or_none(
                        backstep_id, source, token, caller_lease
                    )
                    assert retrieved_node is not None
                    assert new_id == retrieved_node.my_id
                else:
                    raise RuntimeError(
                        "The entity being stored is a path-provenance node, but it is not"
                        " alive in the store, and its ctx is owned by this token (so the"
                        " p-node should also be in the store)!"
                    )

                actual_id_of_unwrap_data = self._bare_store_impl(unwrap_data, token, caller_lease)
                if actual_id_of_unwrap_data != retrieved_node.unwraps_to:
                    raise ValueError(
                        "Illegal p-tag entity: provided wrapped data is inconsistent"
                        f" (expected {retrieved_node.unwraps_to} got"
                        f" {actual_id_of_unwrap_data})"
                    )

                if token in retrieved_node.owned_tokens:
                    return new_id

                # Per Sync-Dep Closure: any token with access to a derived node
                # has access to the full sync-dep chain (ctx_node, etc.) and
                # the node's data is populated. Upgrading token from inheritance
                # to ownership is just _add_owned_token (which itself cascades).
                if token not in retrieved_node.inheritance_bridges:
                    raise RuntimeError(
                        f"Cannot establish ownership for p-node {new_id}: token {token!r} "
                        "has no inheritance entry and no direct ownership path."
                    )
                self._add_owned_token(retrieved_node, token)
                return new_id

            case "u":
                # "u"-type provenance node.
                def _u_projector(
                    child_typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
                ) -> FxtrID:
                    child_id = child_typed_id.inner_id
                    assert child_id in self._entity_states
                    child_node = self._entity_states[child_id]
                    child_tag, child_payload = child_id.split_tag_and_payload()
                    if child_tag != "u":
                        raise ValueError(f"Illegal 'u'-type node has non-'u' child: {child_id}")
                    unwraps_to = FxtrID(raw_data_id=child_payload)
                    assert child_node.unwraps_to == unwraps_to
                    return unwraps_to

                unwrap_data = visit_and_project_ids(entity, _u_projector)
                unwrap_id = self._bare_store_impl(unwrap_data, token, caller_lease)
                new_id = entity_core.FxtrID.from_tag_and_payload("u", unwrap_id._raw_data_id)

                node = self._get_or_create_node(new_id)
                data = NodeData(
                    entity_bytes=None,
                    requires=frozenset(),
                    strong_linked_refs=frozenset(),
                    ctx_node=None,
                    backstep_node=None,
                )
                self._populate_node_data(node, data, unwraps_to=unwrap_id)
                self._add_owned_token(node, token)
                return new_id

            case _:
                raise ValueError(
                    f'Invalid entity: "$fxtr.ctx" has tag {ctx_tag}!'
                    ' Expected ".", "c", "p", or "u".'
                )

    def bare_store_flat(self, lease: Lease[Any], flat_data: FxtrEntityFlatData) -> FxtrID:
        """Store flat entity data under the caller's token.

        All dep IDs must already be in ``_entity_states``. ``lease`` is validated
        against the store's known tokens and consumed.
        """
        token, taken = self._take_and_validate(lease)
        with taken:
            return self._bare_store_flat_impl(flat_data, token)

    def _bare_store_flat_impl[
        B: LifetimeBrand
    ](self, flat_data: FxtrEntityFlatData, token: LifetimeToken[B]) -> FxtrID:
        """Internal entry point for storing flat data under ``token``."""
        node_id = flat_data.entity_id
        entity_bytes = flat_data.entity_bytes
        deps = flat_data.deps

        for dep_id, _ in deps:
            if dep_id not in self._entity_states:
                raise RuntimeError(f"Dependency {dep_id} is not in _entity_states.")

        if self._already_has_data_for(node_id):
            # Dedup: grant ownership of the toplevel only.
            # ``_add_owned_token`` verifies ``token`` already has access
            # (owned *or* inherited) to ``unwraps_to`` and every
            # ``requires`` entry. Unlike ``_bare_store_impl``'s n-path,
            # this deliberately does NOT materialize the unwrap chain:
            # flat-store is the load/transfer path, which keeps d/n deps
            # as lazy inheritance entries until something loads them.
            self._add_owned_token(self._entity_states[node_id], token)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"Node {node_id} already in store (dedup)")
            return node_id

        tag = node_id.get_tag()

        if tag in ("d", "n"):
            requires: set[FxtrID] = set()
            strong_linked_refs: set[FxtrID] = set()
            unwraps_to: FxtrID | None = None
            for dep_id, dep_kind in deps:
                match dep_kind:
                    case DepKind.STRONG_REF:
                        requires.add(dep_id)
                        strong_linked_refs.add(dep_id)
                    case DepKind.WEAK_REF:
                        requires.add(dep_id)
                    case DepKind.UNWRAPS_TO:
                        assert unwraps_to is None
                        unwraps_to = dep_id
                    case _:
                        raise ValueError(f"Unexpected dep kind for d/n node: {dep_kind}")
            assert entity_bytes is not None
            self._save_bytes(node_id._raw_data_id, entity_bytes)
            node = self._get_or_create_node(node_id)
            data = NodeData(
                entity_bytes=entity_bytes,
                requires=frozenset(requires),
                strong_linked_refs=frozenset(strong_linked_refs),
                ctx_node=None,
                backstep_node=None,
            )
            self._populate_node_data(node, data, unwraps_to=unwraps_to)
            self._add_owned_token(node, token)

        elif tag in ("c", "p"):
            ctx_node_id: FxtrID | None = None
            backstep_node_id: FxtrID | None = None
            unwraps_to_cp: FxtrID | None = None
            for dep_id, dep_kind in deps:
                match dep_kind:
                    case DepKind.CTX_NODE:
                        assert ctx_node_id is None
                        ctx_node_id = dep_id
                    case DepKind.BACKSTEP:
                        assert backstep_node_id is None
                        backstep_node_id = dep_id
                    case DepKind.UNWRAPS_TO:
                        assert unwraps_to_cp is None
                        unwraps_to_cp = dep_id
                    case _:
                        raise ValueError(f"Unexpected dep kind for {tag} node: {dep_kind}")
            assert ctx_node_id is not None
            assert unwraps_to_cp is not None
            assert entity_bytes is None
            if backstep_node_id is not None:
                requires_set = frozenset({ctx_node_id, backstep_node_id})
            else:
                requires_set = frozenset({ctx_node_id})
            node = self._get_or_create_node(node_id)
            data = NodeData(
                entity_bytes=None,
                requires=requires_set,
                strong_linked_refs=frozenset(),
                ctx_node=ctx_node_id,
                backstep_node=backstep_node_id,
            )
            self._populate_node_data(node, data, unwraps_to=unwraps_to_cp)
            self._add_owned_token(node, token)

        elif tag == "u":
            unwraps_to_u: FxtrID | None = None
            for dep_id, dep_kind in deps:
                match dep_kind:
                    case DepKind.UNWRAPS_TO:
                        assert unwraps_to_u is None
                        unwraps_to_u = dep_id
                    case _:
                        raise ValueError(f"Unexpected dep kind for u node: {dep_kind}")
            assert unwraps_to_u is not None
            assert entity_bytes is None
            node = self._get_or_create_node(node_id)
            data = NodeData(
                entity_bytes=None,
                requires=frozenset(),
                strong_linked_refs=frozenset(),
                ctx_node=None,
                backstep_node=None,
            )
            self._populate_node_data(node, data, unwraps_to=unwraps_to_u)
            self._add_owned_token(node, token)

        elif tag in (".", "!"):
            assert not deps
            assert entity_bytes is None
            node = self._get_or_create_node(node_id)
            data = NodeData(
                entity_bytes=None,
                requires=frozenset(),
                strong_linked_refs=frozenset(),
                ctx_node=None,
                backstep_node=None,
            )
            self._populate_node_data(node, data, unwraps_to=None)
            self._add_owned_token(node, token)

        else:
            raise ValueError(f"Unsupported node tag: {tag}")

        return node_id

    # -------------------------------------------------------------------------
    # Load operations
    # -------------------------------------------------------------------------

    async def bare_load(self, lease: Lease[Any], node_id: FxtrID) -> FxtrCbor:
        """Load an entity from the store under the caller's token.

        ``lease`` is validated against the store's known tokens and consumed.
        """
        token, taken = self._take_and_validate(lease)
        with taken:
            return await self._bare_load_impl(node_id, token, taken)

    async def _bare_load_impl(
        self,
        node_id: FxtrID,
        token: LifetimeToken[Any],
        caller_lease: Lease[Any],
    ) -> FxtrCbor:
        """Internal entry point for loading an entity under ``token``."""
        assert node_id in self._entity_states
        node = await self._ensure_own(node_id, token, caller_lease)
        assert node.data is not None

        if node.data.entity_bytes is not None:
            # d/n node: rehydrate from bytes
            if self._force_async_sleep:
                await anyio.sleep(0.00001)
            cbor_bytes = self._get_bytes(node_id._raw_data_id)
            return entity_core.fxtr_cbor_loads(cbor_bytes)

        # Derived node: reconstruct from structure
        match node_id.get_tag():
            case "." | "!" | "c":
                raise ValueError(f"Cannot load a contentless ID! Got {node_id}")

            case "p":
                # Loading a p-node creates its children lazily.
                _, node_payload = node_id.split_tag_and_payload()
                assert node.unwraps_to is not None
                assert node.data.ctx_node is not None
                payload_data = cbor2.loads(node_payload)
                assert isinstance(payload_data, list)

                ctx_node_id = node.data.ctx_node
                unwrap_contents = await self._bare_load_impl(node.unwraps_to, token, caller_lease)

                p_child_counter = 0

                def _p_load_projector(
                    child_typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
                ) -> FxtrID:
                    nonlocal p_child_counter
                    unwrapped_child_id = child_typed_id.inner_id
                    assert unwrapped_child_id in self._entity_states
                    new_p_child_id = FxtrID.from_tag_and_payload(
                        "p", cbor2.dumps(payload_data + [p_child_counter])
                    )
                    p_child_counter += 1

                    if new_p_child_id in self._entity_states:
                        existing_child = self._entity_states[new_p_child_id]
                        assert existing_child.unwraps_to == unwrapped_child_id
                        self._add_owned_token(existing_child, token)
                    elif isinstance(child_typed_id, RawFxtrEntityMarkerID):
                        child_node = self._get_or_create_node(new_p_child_id)
                        child_data = NodeData(
                            entity_bytes=None,
                            requires=frozenset({ctx_node_id, node_id}),
                            strong_linked_refs=frozenset(),
                            ctx_node=ctx_node_id,
                            backstep_node=node_id,
                        )
                        self._populate_node_data(
                            child_node, child_data, unwraps_to=unwrapped_child_id
                        )
                        self._add_owned_token(child_node, token)
                    else:
                        child_node = self._get_or_create_node(new_p_child_id)
                        child_data = NodeData(
                            entity_bytes=None,
                            requires=frozenset({ctx_node_id}),
                            strong_linked_refs=frozenset(),
                            ctx_node=ctx_node_id,
                            backstep_node=None,
                        )
                        self._populate_node_data(
                            child_node, child_data, unwraps_to=unwrapped_child_id
                        )
                        self._add_owned_token(child_node, token)
                    return new_p_child_id

                return visit_and_project_ids_in_canonical_cbor_order(
                    unwrap_contents, _p_load_projector
                )

            case "u":
                assert node.unwraps_to is not None
                unwrap_contents = await self._bare_load_impl(node.unwraps_to, token, caller_lease)

                def _u_load_projector(
                    child_typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
                ) -> FxtrID:
                    return self._create_u_node_impl(child_typed_id.inner_id, token)

                return visit_and_project_ids(unwrap_contents, _u_load_projector)

            case tag:
                raise ValueError(f"Invalid derived node type {tag} for node {node_id}")

    async def bare_load_flat(self, lease: Lease[Any], node_id: FxtrID) -> FxtrEntityFlatData:
        """Load flat entity data without deserializing CBOR.

        ``lease`` is validated against the store's known tokens and consumed.
        """
        token, taken = self._take_and_validate(lease)
        with taken:
            return await self._bare_load_flat_impl(node_id, token, taken)

    async def _bare_load_flat_impl(
        self,
        node_id: FxtrID,
        token: LifetimeToken[Any],
        caller_lease: Lease[Any],
    ) -> FxtrEntityFlatData:
        """Internal entry point for loading flat entity data under ``token``."""
        assert node_id in self._entity_states
        node = await self._ensure_own(node_id, token, caller_lease)
        assert node.data is not None

        entity_bytes: bytes | None = None
        deps: list[tuple[FxtrID, DepKind]] = []

        if node.data.entity_bytes is not None:
            # d/n node
            entity_bytes = bytes(self._get_bytes(node_id._raw_data_id))
            for req_id in node.data.requires:
                if req_id in node.data.strong_linked_refs:
                    deps.append((req_id, DepKind.STRONG_REF))
                else:
                    deps.append((req_id, DepKind.WEAK_REF))
            if node.unwraps_to is not None:
                deps.append((node.unwraps_to, DepKind.UNWRAPS_TO))
        else:
            # Derived node
            if node.data.ctx_node is not None:
                deps.append((node.data.ctx_node, DepKind.CTX_NODE))
            if node.data.backstep_node is not None:
                deps.append((node.data.backstep_node, DepKind.BACKSTEP))
            if node.unwraps_to is not None:
                deps.append((node.unwraps_to, DepKind.UNWRAPS_TO))

        return FxtrEntityFlatData(entity_id=node_id, entity_bytes=entity_bytes, deps=deps)

    # -------------------------------------------------------------------------
    # Query
    # -------------------------------------------------------------------------

    def unwrap_provenance(self, lease: Lease[Any], node_id: FxtrID) -> FxtrID | None:
        """Unwrap a provenance node; ``lease`` is validated and consumed.

        Asserts the node is known to the store and returns its ``unwraps_to``
        (or ``None``).
        """
        _, taken = self._take_and_validate(lease)
        with taken:
            assert node_id in self._entity_states
            return self._entity_states[node_id].unwraps_to

    # -------------------------------------------------------------------------
    # Special node creation
    # -------------------------------------------------------------------------

    def track_builtin(self, lease: Lease[Any], node_id: FxtrID) -> FxtrID:
        """Track a "." or "!" builtin node under the caller's token."""
        token, taken = self._take_and_validate(lease)
        with taken:
            assert node_id.get_tag() in (".", "!")
            node = self._get_or_create_node(node_id)
            if node.data is None:
                node.data = NodeData(
                    entity_bytes=None,
                    requires=frozenset(),
                    strong_linked_refs=frozenset(),
                    ctx_node=None,
                    backstep_node=None,
                )
            self._add_owned_token(node, token)
            return node_id

    def create_c_node(self, lease: Lease[Any], context_data: FxtrCbor) -> FxtrID:
        """Create a context provenance node from its context data."""
        token, taken = self._take_and_validate(lease)
        with taken:
            if not isinstance(context_data, dict) or "value" not in context_data:
                raise ValueError("Context data must have a 'value' field")
            value = context_data["value"]
            if not isinstance(value, (RawStrongFxtrID, RawWeakFxtrID)):
                raise ValueError("Context data 'value' field must be a node ID")
            data_id = self._bare_store_impl(context_data, token, taken)
            data_tag, data_payload = data_id.split_tag_and_payload()
            if data_tag != "d":
                raise ValueError("Context data must be for a d-node")
            assert data_id in self._entity_states
            assert value.inner_id in self._entity_states
            new_id = FxtrID.from_tag_and_payload("c", data_payload)
            node = self._get_or_create_node(new_id)
            if node.data is None:
                data = NodeData(
                    entity_bytes=None,
                    requires=frozenset({data_id}),
                    strong_linked_refs=frozenset(),
                    ctx_node=data_id,
                    backstep_node=None,
                )
                self._populate_node_data(node, data, unwraps_to=value.inner_id)
            self._add_owned_token(node, token)
            return new_id

    def c_node_to_context_data(self, lease: Lease[Any], c_node_id: FxtrID) -> FxtrID:
        """Destruct a context provenance node."""
        token, taken = self._take_and_validate(lease)
        with taken:
            assert c_node_id.get_tag() == "c"
            node = self._entity_states[c_node_id]
            if not self._node_has_access(node, token):
                raise RuntimeError(f"token {token!r} has no access to node {c_node_id}")
            assert node.data is not None and node.data.ctx_node is not None
            return node.data.ctx_node

    def create_p_node(self, lease: Lease[Any], pathroot_data: FxtrCbor) -> FxtrID:
        """Create a path provenance node from its pathroot data."""
        token, taken = self._take_and_validate(lease)
        with taken:
            if not isinstance(pathroot_data, dict) or "value" not in pathroot_data:
                raise ValueError("Pathroot data must have a 'value' field")
            value = pathroot_data["value"]
            if not isinstance(value, (RawStrongFxtrID, RawWeakFxtrID)):
                raise ValueError("Pathroot data 'value' field must be a node ID")
            data_id = self._bare_store_impl(pathroot_data, token, taken)
            data_tag, _ = data_id.split_tag_and_payload()
            if data_tag != "d":
                raise ValueError("Pathroot data must be for a d-node")
            assert data_id in self._entity_states
            assert value.inner_id in self._entity_states
            new_payload = cbor2.dumps([data_id._raw_data_id])
            new_id = FxtrID.from_tag_and_payload("p", new_payload)
            node = self._get_or_create_node(new_id)
            if node.data is None:
                data = NodeData(
                    entity_bytes=None,
                    requires=frozenset({data_id}),
                    strong_linked_refs=frozenset(),
                    ctx_node=data_id,
                    backstep_node=None,
                )
                self._populate_node_data(node, data, unwraps_to=value.inner_id)
            self._add_owned_token(node, token)
            return new_id

    def p_node_to_pathroot(self, lease: Lease[Any], p_node_id: FxtrID) -> FxtrID:
        """Destruct a path provenance node to get its pathroot."""
        token, taken = self._take_and_validate(lease)
        with taken:
            assert p_node_id.get_tag() == "p"
            node = self._entity_states[p_node_id]
            if not self._node_has_access(node, token):
                raise RuntimeError(f"token {token!r} has no access to node {p_node_id}")
            assert node.data is not None and node.data.ctx_node is not None
            return node.data.ctx_node

    def step_back_p_ctx(self, lease: Lease[Any], p_node_id: FxtrID) -> FxtrID | None:
        """Step back along a path provenance node."""
        token, taken = self._take_and_validate(lease)
        with taken:
            assert p_node_id.get_tag() == "p"
            node = self._entity_states[p_node_id]
            if not self._node_has_access(node, token):
                raise RuntimeError(f"token {token!r} has no access to node {p_node_id}")
            assert node.data is not None
            return node.data.backstep_node

    def create_u_node(self, lease: Lease[Any], wrapped_id: FxtrID) -> FxtrID:
        """Create a u-type provenance node wrapping another node."""
        token, taken = self._take_and_validate(lease)
        with taken:
            return self._create_u_node_impl(wrapped_id, token)

    def _create_u_node_impl(self, wrapped_id: FxtrID, token: LifetimeToken[Any]) -> FxtrID:
        """Internal entry point for creating a u-node under ``token``."""
        assert wrapped_id in self._entity_states
        new_id = FxtrID.from_tag_and_payload("u", wrapped_id._raw_data_id)
        node = self._get_or_create_node(new_id)
        if node.data is None:
            data = NodeData(
                entity_bytes=None,
                requires=frozenset(),
                strong_linked_refs=frozenset(),
                ctx_node=None,
                backstep_node=None,
            )
            self._populate_node_data(node, data, unwraps_to=wrapped_id)
        self._add_owned_token(node, token)
        return new_id

    # -------------------------------------------------------------------------
    # Arena management
    # -------------------------------------------------------------------------

    def new_arena(self, lease: Lease[Any]) -> MemoryArena[Any]:
        """Create a new empty arena tied to the lease's token.

        Validates ``lease`` against the store's known tokens, allocates an
        ``ArenaKey``, records ``MemoryArenaState`` on the store, and returns a
        ``MemoryArena``. The lease is consumed; callers that need to retain a
        copy should pass ``lease.clone()``.

        A ``weakref.finalize`` is attached so that an arena dropped without
        an explicit ``close()`` logs a warning (and, if
        ``DEBUG_DROPPED_ARENAS`` is enabled, includes a stack trace of the
        creation site).
        """
        token, taken = self._take_and_validate(lease)
        with taken:
            key = ArenaKey(ix=self._arena_counter)
            self._arena_counter += 1
            self._arenas[key] = MemoryArenaState(my_key=key, token=token, liveset=set())
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"New arena {key} created (token {token!r})")
            arena: MemoryArena[Any] = MemoryArena(store=self, key=key)
            if DEBUG_DROPPED_ARENAS:
                self._debug_dropped_arena_tbs[key] = traceback.extract_stack()
            weakref.finalize(arena, self._finalize_arena_object, key)
            return arena

    def _new_arena_for_token(self, token: LifetimeToken[Any]) -> MemoryArena[Any]:
        """Allocate a new arena row for an already-registered token (no lease).

        For tests and other internal call sites that need a second arena sharing an
        existing token. Prefer ``new_arena(lease)`` for normal scoped creation.
        """
        row = self._token_data.get(token)
        if row is None:
            raise RuntimeError(f"Token {token!r} is not registered on this MemoryStore")
        key = ArenaKey(ix=self._arena_counter)
        self._arena_counter += 1
        self._arenas[key] = MemoryArenaState(my_key=key, token=token, liveset=set())
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"New arena {key} for existing token {token!r}")
        arena: MemoryArena[Any] = MemoryArena(store=self, key=key)
        if DEBUG_DROPPED_ARENAS:
            self._debug_dropped_arena_tbs[key] = traceback.extract_stack()
        weakref.finalize(arena, self._finalize_arena_object, key)
        return arena

    def _finalize_arena_object(self, key: ArenaKey) -> None:
        if key in self._arenas:
            live_nodes = len(self._arenas[key].liveset)
            if live_nodes:
                if logger.isEnabledFor(logging.INFO):
                    logger.info(f"Arena {key} was finalized while open! ({live_nodes} nodes live)")
                if DEBUG_DROPPED_ARENAS and key in self._debug_dropped_arena_tbs:
                    try:
                        tb = self._debug_dropped_arena_tbs[key]
                        if logger.isEnabledFor(logging.WARNING):
                            logger.warning(
                                f"Arena {key} finalized while open! Contains:"
                                f" {self._arenas[key].liveset}. Created at:\n"
                                f"{''.join(tb.format())}"
                            )
                    except BaseException:
                        if logger.isEnabledFor(logging.WARNING):
                            logger.warning(
                                f"Error formatting traceback for arena {key}",
                                exc_info=True,
                            )
            else:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f"Empty arena {key} finalized while open; closing.")
            self.close_arena(key)

    def track_in_arena(self, node_id: FxtrID, arena_key: ArenaKey) -> None:
        """Track a node in an arena. Validates that the node is reachable by the arena's token."""
        arena_state = self._arenas[arena_key]
        if node_id in arena_state.liveset:
            return  # Already tracked
        node = self._entity_states.get(node_id)
        if node is None:
            raise RuntimeError(
                f"Node {node_id} is not in the store! Cannot track in arena {arena_key}."
            )
        if not self._node_has_access(node, arena_state.token):
            raise RuntimeError(
                f"Node {node_id} is not reachable by token {arena_state.token!r}!"
                f" Cannot track in arena {arena_key}."
            )
        arena_state.liveset.add(node_id)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Node {node_id} tracked in arena {arena_key}")

    def untrack_from_arena(self, lease: Lease[Any], node_id: FxtrID, arena_key: ArenaKey) -> None:
        """Remove a node from an arena's liveset; ``lease`` is validated and consumed.

        The lease's token must match the token recorded for ``arena_key``.
        """
        token, taken = self._take_and_validate(lease)
        with taken:
            arena_state = self._arenas[arena_key]
            if arena_state.token is not token:
                raise RuntimeError(
                    f"Lease token {token!r} does not match arena {arena_key}"
                    f" (token {arena_state.token!r})"
                )
            arena_state.liveset.discard(node_id)

    def close_arena(self, arena_key: ArenaKey) -> None:
        """Close an arena, removing all nodes from its liveset."""
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Closing arena {arena_key}")
        arena_state = self._arenas.pop(arena_key, None)
        if arena_state is not None:
            arena_state.liveset.clear()
        self._debug_dropped_arena_tbs.pop(arena_key, None)

    # -------------------------------------------------------------------------
    # Lookup
    # -------------------------------------------------------------------------

    async def try_lookup_with_lease[
        B: LifetimeBrand
    ](self, lease: Lease[B], node_id: FxtrID) -> ReadableArena[B] | None:
        """Try to look up an entity visible to the caller's token.

        Only returns a result if the token has access (owned or inheritance entry)
        on the node. Returns a fresh arena that the caller can use via clones of
        their own lease; no separate lease is returned.
        """
        token, taken = self._take_and_validate(lease)
        with taken:
            node = self._entity_states.get(node_id)
            if node is None:
                return None
            if not self._node_has_access(node, token):
                return None
            arena = self.new_arena(taken.clone())
            self.track_in_arena(node_id, arena._key)
            return arena

    # -------------------------------------------------------------------------
    # Store lifecycle
    # -------------------------------------------------------------------------

    def close(self, close_arenas: bool = False) -> None:
        """Close the store, freeing all in-memory state.

        Matches the old API: by default, refuses to close if any arenas are
        still open; pass ``close_arenas=True`` to close them all first.
        Under normal operation cleanup flows through token end-callbacks —
        ``close()`` is primarily a teardown escape hatch for tests and
        long-lived process shutdown.
        """
        if self._arenas:
            if close_arenas:
                for arena_key in list(self._arenas.keys()):
                    self.close_arena(arena_key)
            else:
                raise ValueError("Cannot close store with open arenas")
        # Free byte data for every node with entity_bytes.
        for node in list(self._entity_states.values()):
            if node.data is not None and node.data.entity_bytes is not None:
                self._free_bytes(node.my_id._raw_data_id)
        self._entity_states.clear()
        self._token_data.clear()

    # -------------------------------------------------------------------------
    # External copy
    # -------------------------------------------------------------------------

    async def _copy_from_external_arena_impl[
        B_dest: LifetimeBrand, B_src: LifetimeBrand
    ](
        self,
        entity_id: FxtrID,
        dest_token: LifetimeToken[B_dest],
        dest_taken: Lease[B_dest],
        source: ReadableArena[B_src],
        src_taken: Lease[B_src],
    ) -> None:
        """DAG-walk copy of ``entity_id`` (and its deps) from ``source`` into the store.

        Internal helper for ``bare_copy_from_arena``. Expects both leases to already be
        live-and-taken by the caller (typically a ``MemoryArena`` method that did
        ``.take()`` + validation at entry). The leases are held alive for the whole
        worklist and cloned before each call so every consumed lease is fresh; the
        caller retains ownership and is responsible for discarding both on return.

        Does not track the entity in any arena; the caller is responsible for
        calling ``arena._track_from_store(entity_id)`` after.
        """
        del dest_taken  # reserved for future threading; not used in the current walk
        with contextlib.ExitStack() as stack:
            # Fork the source arena so we have a private liveset for the discovery
            # walk. copy_to_new_arena consumes its lease argument, so pass a clone.
            tmp_source_arena = source.copy_to_new_arena(src_taken.clone(), [entity_id])
            stack.callback(tmp_source_arena.close)

            worklist: list[_DiscoverWork | _SaveWork] = [_DiscoverWork(entity_id=entity_id)]
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"  Copying entity {entity_id.summary()} from {source}")

            while worklist:
                item = worklist.pop()
                if isinstance(item, _DiscoverWork):
                    existing = self._entity_states.get(item.entity_id)
                    if existing is not None and dest_token in existing.owned_tokens:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(f"  Discover({item.entity_id.summary()}): already owned")
                    else:
                        flat_data = await tmp_source_arena.load_flat(
                            src_taken.clone(), item.entity_id
                        )
                        worklist.append(_SaveWork(flat_data=flat_data))
                        for dep_id, _ in flat_data.deps:
                            worklist.append(_DiscoverWork(entity_id=dep_id))
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(
                                f"  Discover({item.entity_id.summary()}): loading from source"
                            )
                elif isinstance(item, _SaveWork):
                    # Deps are stored first (LIFO worklist ordering), so the
                    # ownership invariant holds when we grant the caller's token.
                    self._bare_store_flat_impl(item.flat_data, dest_token)
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"  Save({item.flat_data.entity_id.summary()}): saved")

    async def bare_copy_from_arena[
        B_dest: LifetimeBrand, B_src: LifetimeBrand
    ](
        self,
        dest_lease: Lease[B_dest],
        src_lease: Lease[B_src],
        source: ReadableArena[B_src],
        entity_id: FxtrID,
    ) -> None:
        """DAG-walk copy from ``source`` into this store under ``dest_lease``'s token.

        Validates and consumes both leases. Does not track ``entity_id`` in any arena;
        the caller should ``track_in_arena`` afterward if needed.
        """
        dest_token, dest_taken = self._take_and_validate(dest_lease)
        with dest_taken:
            with src_lease.take() as src_taken:
                await self._copy_from_external_arena_impl(
                    entity_id, dest_token, dest_taken, source, src_taken
                )

    def bare_track_from_memory_arena[
        B_dest: LifetimeBrand, B_src: LifetimeBrand
    ](
        self,
        dest_lease: Lease[B_dest],
        src_lease: Lease[B_src],
        entity_id: FxtrID,
        *,
        dest_inheritance_bridges: Mapping[LifetimeToken[Any], LocalBridge],
    ) -> None:
        """Sync DAG walk that extends ``dest_lease``'s token's claims to
        ``entity_id`` and its transitive deps, without async I/O.

        For each node visited, picks the cheapest available path:

        1. Already covered by ``dest_token`` (owned or inheritance) — skip.
        2. **Claim-holder adopt**: if any token in
           ``dest_inheritance_bridges`` itself holds a claim (owned *or*
           inherited) on the node, hand off to ``_dest_adopt_inheritance``,
           which installs a dest-side ``_SameStoreSentinel`` entry targeting
           that holder and cascades through the node's sync-dep closure via
           ``_adopt_inheritance_entry`` — no data, no wire I/O, no source
           fork. The walk stops at this node; ``d``/``n`` deps under it
           stay lazy and materialize through the load-time ``_ensure_own``
           cascade. Lifetime-sound regardless of the holder's claim shape:
           the dest's bridge keeps the holder's token alive for the dest's
           lifetime, and claims only drop at token end.
        3. **Transfer through data**: if the node has in-memory data, plan to
           grant ``dest_token`` ownership and recurse on deps. ``d``/``n``
           nodes without in-memory data fail (would require an async load);
           derived nodes always have populated data per the Sync-Dep Closure
           invariant.

        When the walk's *source token* is itself among
        ``dest_inheritance_bridges`` (e.g. the marker-inherit path, where
        the source is the dest scope's parent), strategy 2 already matches
        at the root and the whole call degenerates to a lift-style lazy
        adopt.

        Mutations are committed bottom-up via a LIFO worklist of discover and
        save items, so each node is fully reachable to ``dest_token`` by the
        time its parent's save runs. Partial state on failure is additive
        (no rollback): the saves that already ran stay in the store.

        Args:
            dest_lease: A live lease on the destination token; consumed.
            src_lease: A live lease on the source token; consumed. The source
                must already have access to ``entity_id``.
            entity_id: Root of the DAG to transfer.
            dest_inheritance_bridges: Mapping from token Y to a
                ``LocalBridge`` that mints ``Lease[Y]`` for ``dest_token``.
                Nodes claim-held by any of these tokens are adopted lazily
                instead of transferred through data. Typically the
                destination scope's ``is_ancestor_or_arena_owner=True``
                cap-bridges.
        """
        dest_token, dest_taken = self._take_and_validate(dest_lease)
        with dest_taken:
            with src_lease.take() as src_taken:
                self._bare_track_from_memory_arena_impl(
                    entity_id,
                    dest_token,
                    dest_taken,
                    src_taken,
                    dest_inheritance_bridges,
                )

    def _bare_track_from_memory_arena_impl(
        self,
        entity_id: FxtrID,
        dest_token: LifetimeToken[Any],
        dest_taken: Lease[Any],
        src_taken: Lease[Any],
        dest_bridges: Mapping[LifetimeToken[Any], LocalBridge],
    ) -> None:
        """Internal driver for ``bare_track_from_memory_arena``."""
        del src_taken  # No longer needed: derived data is always populated
        # (Sync-Dep Closure invariant), so we don't materialize via src.
        visited: set[FxtrID] = set()
        worklist: list[_DiscoverTrackWork | _SaveOwnedTrackWork | _SaveAdoptTrackWork] = [
            _DiscoverTrackWork(entity_id=entity_id)
        ]

        while worklist:
            item = worklist.pop()
            if isinstance(item, _DiscoverTrackWork):
                nid = item.entity_id
                if nid in visited:
                    continue
                visited.add(nid)
                node = self._entity_states.get(nid)
                if node is None:
                    raise RuntimeError(f"track_from_memory_arena: node {nid} is not in the store.")
                # (1) Already covered by dest.
                if dest_token in node.owned_tokens or dest_token in node.inheritance_bridges:
                    continue
                # (2) Dest-adopt: scan ``dest_bridges`` for a token that
                # itself holds a claim (owned or inherited) on the node.
                # Lifetime-sound regardless of the claim's shape: the dest's
                # bridge keeps the claim-holder's token alive for the dest's
                # lifetime, and claims only drop at token end, so a
                # same-store sentinel targeting the holder stays loadable.
                adopt_target: LifetimeToken[Any] | None = None
                for holder in dest_bridges:
                    if holder in node.owned_tokens or holder in node.inheritance_bridges:
                        adopt_target = holder
                        break
                if adopt_target is not None:
                    # _dest_adopt_inheritance handles the sync-dep cascade
                    # internally via _adopt_inheritance_entry; no need to walk
                    # deps from the worklist here.
                    worklist.append(_SaveAdoptTrackWork(entity_id=nid, target_token=adopt_target))
                    continue
                # (3) Data path. By Sync-Dep Closure, derived nodes accessible
                # to any token always have populated data; ``data is None`` only
                # for d/n nodes whose bytes haven't been loaded. Sync transfer
                # can't proceed for those — would require an async load.
                if node.data is None:
                    raise RuntimeError(
                        f"track_from_memory_arena: node {nid} has no in-memory "
                        "data (would require async load)."
                    )
                # Plan to grant ownership; recurse on deps + unwraps_to.
                worklist.append(_SaveOwnedTrackWork(entity_id=nid))
                for dep_id in node.data.requires:
                    if dep_id not in visited:
                        worklist.append(_DiscoverTrackWork(entity_id=dep_id))
                if node.unwraps_to is not None and node.unwraps_to not in visited:
                    worklist.append(_DiscoverTrackWork(entity_id=node.unwraps_to))
            elif isinstance(item, _SaveOwnedTrackWork):
                self._add_owned_token(self._entity_states[item.entity_id], dest_token)
            else:
                assert isinstance(item, _SaveAdoptTrackWork)
                self._dest_adopt_inheritance(
                    item.entity_id,
                    item.target_token,
                    dest_token,
                    dest_taken,
                    dest_bridges,
                )

    def _dest_adopt_inheritance(
        self,
        node_id: FxtrID,
        target_y: LifetimeToken[Any],
        dest_token: LifetimeToken[Any],
        dest_taken: Lease[Any],
        dest_bridges: Mapping[LifetimeToken[Any], LocalBridge],
    ) -> None:
        """Install dest-side inheritance for ``node_id`` (and its full
        sync-dep closure) by routing through ``_adopt_inheritance_entry``.

        ``target_y`` is a dest-bridged token that holds its own claim on the
        node (verified during discover), so the dest-side source descriptor
        is always a fresh ``_SameStoreSentinel(target_token=target_y)`` —
        no data, no wire I/O, no source fork; later loads recurse lazily
        through the holder's claim. ``_adopt_inheritance_entry`` then
        cascades through every sync-discoverable dep (read from
        ``_entity_states`` for sentinel sources). Idempotent on re-adopt.
        """
        node = self._entity_states[node_id]
        if dest_token in node.inheritance_bridges:
            return
        assert target_y in node.owned_tokens or target_y in node.inheritance_bridges, (
            f"_dest_adopt_inheritance: token {target_y!r} holds no claim on node "
            f"{node_id}; should have been verified during discover."
        )
        # Register dest's bridge for (dest_token, target_y).
        self._register_bridge(dest_token, target_y, dest_bridges[target_y].clone())
        # Hand off to _adopt_inheritance_entry, which cascades through the
        # sync-dep closure: derived nodes get owned, d/n nodes get
        # inheritance entries pointing at the holder.
        self._adopt_inheritance_entry(
            node_id, _SameStoreSentinel(target_token=target_y), dest_token, dest_taken
        )


@dataclasses.dataclass(frozen=True)
class _DiscoverWork:
    entity_id: FxtrID


@dataclasses.dataclass(frozen=True)
class _SaveWork:
    flat_data: FxtrEntityFlatData


@dataclasses.dataclass(frozen=True)
class _DiscoverTrackWork:
    """Discover-side work for ``bare_track_from_memory_arena``."""

    entity_id: FxtrID


@dataclasses.dataclass(frozen=True)
class _SaveOwnedTrackWork:
    """Commit-side: grant ``dest_token`` direct ownership of ``entity_id``."""

    entity_id: FxtrID


@dataclasses.dataclass(frozen=True)
class _SaveAdoptTrackWork:
    """Commit-side: install a dest-side ``_SameStoreSentinel`` inheritance
    entry on ``entity_id`` targeting ``target_token``, a dest-bridged token
    that holds its own claim (owned or inherited) on the node."""

    entity_id: FxtrID
    target_token: LifetimeToken[Any]


# =============================================================================
# MemoryArena
# =============================================================================


class MemoryArena[B: LifetimeBrand](ReadWriteArena[B]):
    """An in-memory arena backed by a ``MemoryStore``, branded by a lifetime.

    The arena's liveset lives on the store (``store._arenas[key].liveset``). Every
    public method takes a ``Lease[B]`` that is checked for subject identity (matches
    this arena's token) and liveness, then consumed. The arena does not cache or
    check a ``Grant``: as a subscriber view on a caller-owned token it cannot
    guarantee grant uniqueness, so grant-level checks are left to the lifetime
    owner. The arena does not register any end-callback on its token — if the
    token dies, subsequent method calls simply fail lease validation.
    """

    def __init__(self, *, store: MemoryStore, key: ArenaKey) -> None:
        """Construct a MemoryArena. Callers must already have inserted
        ``MemoryArenaState`` at ``key`` on ``store`` (see ``MemoryStore.new_arena`` /
        ``MemoryStore._new_arena_for_token``).
        """
        self._store = store
        self._key = key
        self._closed = False
        arena_tok = store._arenas[key].token
        self._token_cached: LifetimeToken[B] = cast(LifetimeToken[B], arena_tok)

    @property
    @override
    def token(self) -> LifetimeToken[B]:
        return self._token_cached

    def _liveset(self) -> set[FxtrID]:
        """The arena's live node set (same object as on ``MemoryStore._arenas``)."""
        return self._store._arenas[self._key].liveset

    def __repr__(self) -> str:
        if self._closed:
            return f"<MemoryArena store:{id(self._store):x} key={self._key} (closed)>"
        return (
            f"<MemoryArena store:{id(self._store):x} key={self._key} token={self.token!r} (open)>"
        )

    def _check_open(self) -> None:
        if self._closed:
            raise ValueError(f"Arena {self!r} is closed")

    def _assert_lease_matches_arena(self, lease: Lease[B]) -> None:
        """Subject / liveness check for this arena (does not ``take()`` the lease).

        On a subject mismatch the lease is discarded before raising: callers
        (``load``/``store``/``is_tracked``/…) consume the lease they are handed,
        so the reject path must too — otherwise the (still-active) lease leaks,
        which, for a session lease, pins the session lifetime and hangs teardown.
        """
        lease.ensure_active()
        if lease.subject is not self.token:
            lease.discard()
            raise LeaseGrantMismatchError(
                f"Lease subject {lease.subject!r} does not match this arena's token {self.token!r}"
            )

    def _precheck_lease_for_arena(self, lease: Lease[B]) -> None:
        """Arena-side checks before handing ``lease`` to ``MemoryStore`` (which will ``take()``)."""
        self._check_open()
        self._assert_lease_matches_arena(lease)

    def _track_from_store(self, node_id: FxtrID) -> FxtrID:
        self._store.track_in_arena(node_id, self._key)
        return node_id

    def _track_from_store_or_none(self, node_id: FxtrID | None) -> FxtrID | None:
        if node_id is None:
            return None
        return self._track_from_store(node_id)

    def _ensure_all_accessible_for_storing(self, entity: FxtrCbor) -> None:
        ls = self._liveset()

        def _visitor(child_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID) -> None:
            if child_id.inner_id not in ls:
                raise ValueError(
                    f"The entity being stored references {child_id.inner_id} which is not"
                    " tracked in the arena!"
                )

        visit_ids(entity, _visitor)

    # --- ReadWriteArena interface ---

    @override
    def close(self) -> None:
        if self._closed:
            return
        self._store.close_arena(self._key)
        self._closed = True

    @override
    def is_tracked(self, lease: Lease[B], node_id: FxtrID) -> bool:
        self._assert_lease_matches_arena(lease)
        with lease.take():
            if self._closed:
                return False
            return node_id in self._liveset()

    @override
    def store(self, lease: Lease[B], entity: FxtrCbor) -> FxtrID:
        self._precheck_lease_for_arena(lease)
        try:
            self._ensure_all_accessible_for_storing(entity)
        except BaseException:
            lease.discard()
            raise
        return self._track_from_store(self._store.bare_store(lease, entity))

    @override
    async def load(self, lease: Lease[B], node_id: FxtrID) -> FxtrCbor:
        self._precheck_lease_for_arena(lease)
        if node_id not in self._liveset():
            lease.discard()
            raise ValueError(
                f"Node {node_id} is not tracked in the arena! Only tracked nodes can be loaded."
            )
        loaded = await self._store.bare_load(lease, node_id)

        def _visitor(child_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID) -> None:
            self._track_from_store(child_id.inner_id)

        visit_ids(loaded, _visitor)
        return loaded

    @override
    async def load_flat(self, lease: Lease[B], node_id: FxtrID) -> FxtrEntityFlatData:
        self._precheck_lease_for_arena(lease)
        if node_id not in self._liveset():
            lease.discard()
            raise ValueError(f"Node {node_id} is not tracked in the arena!")
        flat_data = await self._store.bare_load_flat(lease, node_id)
        for dep_id, _ in flat_data.deps:
            self._track_from_store(dep_id)
        return flat_data

    @override
    def unwrap_provenance(self, lease: Lease[B], node_id: FxtrID) -> FxtrID | None:
        self._precheck_lease_for_arena(lease)
        return self._track_from_store_or_none(self._store.unwrap_provenance(lease, node_id))

    @override
    def track_builtin(self, lease: Lease[B], node_id: FxtrID) -> FxtrID:
        self._precheck_lease_for_arena(lease)
        self._store.track_builtin(lease, node_id)
        return self._track_from_store(node_id)

    @override
    def create_c_node(self, lease: Lease[B], context_data: FxtrCbor) -> FxtrID:
        self._precheck_lease_for_arena(lease)
        self._ensure_all_accessible_for_storing(context_data)
        return self._track_from_store(self._store.create_c_node(lease, context_data))

    @override
    def c_node_to_context_data(self, lease: Lease[B], c_node_id: FxtrID) -> FxtrID:
        self._precheck_lease_for_arena(lease)
        return self._track_from_store(self._store.c_node_to_context_data(lease, c_node_id))

    @override
    def create_p_node(self, lease: Lease[B], pathroot_data: FxtrCbor) -> FxtrID:
        self._precheck_lease_for_arena(lease)
        self._ensure_all_accessible_for_storing(pathroot_data)
        return self._track_from_store(self._store.create_p_node(lease, pathroot_data))

    @override
    def p_node_to_pathroot(self, lease: Lease[B], p_node_id: FxtrID) -> FxtrID:
        self._precheck_lease_for_arena(lease)
        return self._track_from_store(self._store.p_node_to_pathroot(lease, p_node_id))

    @override
    def step_back_p_ctx(self, lease: Lease[B], p_node_id: FxtrID) -> FxtrID | None:
        self._precheck_lease_for_arena(lease)
        return self._track_from_store_or_none(self._store.step_back_p_ctx(lease, p_node_id))

    @override
    def create_u_node(self, lease: Lease[B], wrapped_id: FxtrID) -> FxtrID:
        self._precheck_lease_for_arena(lease)
        return self._track_from_store(self._store.create_u_node(lease, wrapped_id))

    @override
    def abandon(self, lease: Lease[B], node_id: FxtrID) -> None:
        self._precheck_lease_for_arena(lease)
        if node_id not in self._liveset():
            lease.discard()
            raise ValueError(f"Node {node_id} is not tracked in the arena!")
        self._store.untrack_from_arena(lease, node_id, self._key)

    @override
    async def copy_from_arena[
        B2: LifetimeBrand
    ](
        self,
        lease: Lease[B],
        source: ReadableArena[B2],
        src_lease: Lease[B2],
        entity_id: FxtrID,
    ) -> None:
        self._precheck_lease_for_arena(lease)
        await self._store.bare_copy_from_arena(lease, src_lease, source, entity_id)
        self._track_from_store(entity_id)

    @override
    def lazily_attach_from_arena[
        B_Y: LifetimeBrand
    ](
        self,
        lease: Lease[B],
        node_ids: FxtrID | Collection[FxtrID],
        source: ReadableArena[B_Y],
        bridge: LocalBridge,
    ) -> None:
        self._precheck_lease_for_arena(lease)
        self._store.bare_lazily_attach_from_arena(lease, node_ids, source, bridge)
        if isinstance(node_ids, FxtrID):
            self._track_from_store(node_ids)
        else:
            for nid in node_ids:
                self._track_from_store(nid)

    def track_from_memory_arena[
        B_src: LifetimeBrand
    ](
        self,
        lease: Lease[B],
        source: MemoryArena[B_src],
        src_lease: Lease[B_src],
        entity_id: FxtrID,
        *,
        dest_inheritance_bridges: Mapping[LifetimeToken[Any], LocalBridge] = {},
    ) -> None:
        """Sync DAG walk that extends this arena's lifetime claim over ``entity_id``.

        Both arenas must share the same backing ``MemoryStore`` and ``source``
        must currently track ``entity_id``. ``dest_inheritance_bridges`` lets
        the underlying walk lazily adopt nodes claim-held by any of the
        bridged tokens on the way down — see
        :meth:`MemoryStore.bare_track_from_memory_arena` for the full
        algorithm. Defaults to empty when the caller knows every node in the
        DAG is sync-transferable through the data path alone.
        """
        self._precheck_lease_for_arena(lease)
        if source._store is not self._store:
            lease.discard()
            src_lease.discard()
            raise ValueError("track_from_memory_arena only supports same-store arenas.")
        if entity_id not in source._liveset():
            lease.discard()
            src_lease.discard()
            raise ValueError(f"Node {entity_id} is not tracked in the source arena!")
        self._store.bare_track_from_memory_arena(
            lease,
            src_lease,
            entity_id,
            dest_inheritance_bridges=dest_inheritance_bridges,
        )
        self._track_from_store(entity_id)

    @override
    def copy_to_new_arena(self, lease: Lease[B], node_ids: Collection[FxtrID]) -> MemoryArena[B]:
        """Copy the given nodes to a new arena under the same token + grant.

        This is a cheap python-side duplication of tracking state — no new lifetime
        is introduced, no new lease is returned. Callers continue to use clones of
        their existing lease to access the returned arena.
        """
        with lease:
            self._precheck_lease_for_arena(lease)
            for node_id in node_ids:
                if node_id not in self._liveset():
                    raise ValueError(f"Node {node_id} is not tracked in this arena!")
            new_arena: MemoryArena[B] = self._store.new_arena(lease)
            for node_id in node_ids:
                new_arena._track_from_store(node_id)
            return new_arena

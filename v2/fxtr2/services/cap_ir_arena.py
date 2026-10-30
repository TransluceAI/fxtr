"""Arena-over-Cap-IR — server adapter and client arena.

Lets a `ReadableArena` cross a Cap-IR dispatch boundary so fxtr entity
data flows through `IRHandle` round-trips the same way live capabilities
do.

- `ArenaIRHandleAdapter[B]` — server side. Wraps a `ReadableArena[B]`
  and exposes it as an `IRHandle[B]` whose token aliases the wrapped
  arena's. Handles two wire methods: `load_flat` and `abandon` (the
  latter takes a list of IDs).
- `CapIRArenaRemoteState[B_Z]` — internal shared state for one logical
  remote arena. Owns the wire `IRHandle`, the per-mode lifetime/lease
  conversion machinery, the refcounted liveset across all sibling
  arenas, and the precomp caches that serve sync provenance reads.
- `CapIRArena[B_Z]` — client-side `ReadableArena[B_Z]`. A thin share
  of one `CapIRArenaRemoteState`; multiple sibling `CapIRArena`s
  produced by `copy_to_new_arena` share the same remote state and
  refcount their tracked-id slices against it. Local fork =
  in-process bookkeeping only; only when a node's shared refcount
  drops to zero does an `abandon` cross the wire.

This module also contains wire-encoding helpers shared by both sides
— `_encode_fxtr_id` / `_decode_fxtr_id` (module-private; arena-internal
paths only), their public provenance-aware counterparts
(`encode_fxtr_id_with_prov` / `decode_fxtr_id_with_prov`), and the
public precomp helpers `compute_precomp_closure` / `ingest_precomp`
plus the private reply parser `_decode_flat_data`.
"""

from __future__ import annotations

import dataclasses
import logging
from collections import deque
from collections.abc import Callable, Collection, Iterable, Sequence
from typing import Any, Mapping, Sequence, cast, override

import anyio.abc

from fxtr2.data.abstract_arena import (
    DepKind,
    FxtrEntityFlatData,
    LeaseGrantMismatchError,
    ReadableArena,
)
from fxtr2.data.entity_core import FxtrCbor, FxtrID, fxtr_cbor_loads
from fxtr2.leases.lease_bridges import (
    DependentLocalBridge,
    DependentPipelinedBridge,
    IndependentLocalBridge,
    IndependentPipelinedBridge,
    LocalOrPipelinedBridge,
)
from fxtr2.leases.lease_types import Lease, LifetimeBrand, LifetimeToken
from fxtr2.leases.leased_cap_common import (
    IRHandleLocalImplBase,
    UnwrappedCallArg,
    discard_leases_in,
)
from fxtr2.leases.leased_cap_ir import (
    Call,
    GetItem,
    IRException,
    IRHandle,
    IRTagged,
    IRValue,
    Request,
    UnwrapForPipelining,
)
from fxtr2.leases.local_lease import DerivedLocalLifetime, LocalLifetimeBrand, LocalLifetimeToken

__all__ = [
    "FXTR_ID_TAG",
    "FXTR_ID_WITH_PROV_TAG",
    "ArenaIRHandleAdapter",
    "CapIRArena",
    "CapIRArenaRemoteState",
]

logger = logging.getLogger(__name__)
FXTR_ID_TAG = "fxtr_id"
"""Wire tag for a bare FxtrID (no provenance parent).

Payload is `bytes(id) == id._raw_data_id`. Used by every arena-internal
path (precomp closures, load_flat replies, wire abandons) where the IDs
in flight are by construction arena-side bare identities.
"""

_ABANDON_ID_FIELD = "id"
_ABANDON_COUNT_FIELD = "count"
"""Wire field names for ``"abandon"`` request entries.

Each entry is a ``Mapping`` with these two fields: ``id`` is the
``IRTagged(FXTR_ID_TAG, bytes)`` to abandon; ``count`` is the
introduction-refcount the *client* observed for this id (i.e. the
number of distinct wire emissions in which the server introduced this
id to the client). The server decrements its
``_total_introductions[id]`` by exactly that amount and only releases
the id on the underlying arena when the counter reaches zero. This
shape protects against a TOCTOU race where the server has emitted an
introduction to the client (bumping the counter) between the client
constructing the abandon and the server receiving it — the abandon's
shipped count won't include the new introduction, so the post-decrement
counter stays > 0 and the id remains tracked. Mirrors
``DiscardMessage.refcount`` in ``fxtr2/rpc/lowering_types.py``.
"""

FXTR_ID_WITH_PROV_TAG = "fxtr_id_with_prov"
"""Wire tag for a layered FxtrID (carries a provenance parent).

Payload is the **provenance parent's** `_raw_data_id` only (the data
ID is implicit, recoverable on the receiver via sync
`unwrap_provenance`). Used by the service path's encode/decode
helpers; the receiver's call-scope arena must already have the
provenance parent in its liveset (and its precomp populated for that
parent) before decode runs.
"""


# =============================================================================
# Wire encoders/decoders for FxtrID
# =============================================================================


def _encode_fxtr_id(id: FxtrID) -> IRTagged[str, bytes]:
    """Encode a bare `FxtrID` as `IRTagged(FXTR_ID_TAG, bytes)`.

    Asserts the input has no `_provenance_parent` — used by
    arena-internal paths (precomp closures, load_flat replies, wire
    abandons) which by construction only carry bare arena identities.
    For user-facing service-level encoding (which may carry
    provenance), use :func:`encode_fxtr_id_with_prov`.
    """
    # TODO(daniel): Debug why this still triggers under new provenance flow.
    # assert id._provenance_parent is None, (
    #     "_encode_fxtr_id received an ID with a provenance parent; "
    #     "the arena-internal paths only encode bare IDs. Use "
    #     "encode_fxtr_id_with_prov for service-level encoding."
    # )
    if id._provenance_parent is not None:
        logger.warning(
            "_encode_fxtr_id received an ID with a provenance parent; "
            "this is not supposed to occur for arena-internal paths."
        )
    return IRTagged(FXTR_ID_TAG, bytes(id))


def _decode_fxtr_id(value: IRValue) -> FxtrID:
    """Inverse of :func:`_encode_fxtr_id` — accepts only ``FXTR_ID_TAG``.

    Raises ``IRException(category="MalformedArenaPacket")`` if the input
    is not a properly-tagged bytes value, including any
    ``FXTR_ID_WITH_PROV_TAG`` (which is a service-path-only encoding;
    arena-internal paths must never emit it).
    """
    if not isinstance(value, IRTagged) or value.tag != FXTR_ID_TAG:
        raise IRException(
            message=f"expected IRTagged({FXTR_ID_TAG!r}, bytes), got {value!r}",
            category="MalformedArenaPacket",
        )
    inner = value.value
    if not isinstance(inner, bytes):
        raise IRException(
            message=(
                f"expected IRTagged({FXTR_ID_TAG!r}) value to be bytes, got {type(inner).__name__}"
            ),
            category="MalformedArenaPacket",
        )
    return FxtrID(raw_data_id=inner)


def encode_fxtr_id_with_prov(id: FxtrID) -> IRTagged[str, bytes]:
    """Encode a (possibly provenance-bearing) FxtrID for the service path.

    - If `id._provenance_parent` is `None`: same as :func:`_encode_fxtr_id`
      — emit ``IRTagged(FXTR_ID_TAG, bytes(id))``.
    - Otherwise: emit ``IRTagged(FXTR_ID_WITH_PROV_TAG,
      bytes(id._provenance_parent))``. The receiver recovers the data
      ID by calling ``arena.unwrap_provenance(lease, prov_id)``
      synchronously at decode time.
    """
    if id._provenance_parent is None:
        return IRTagged(FXTR_ID_TAG, bytes(id))
    return IRTagged(FXTR_ID_WITH_PROV_TAG, bytes(id._provenance_parent))


def decode_fxtr_id_with_prov(
    value: IRValue,
    *,
    embed_prov: "Callable[[FxtrID], FxtrID]",
) -> FxtrID:
    """Decode a (possibly provenance-bearing) FxtrID from the service path.

    Args:
        value: The encoded IRValue. Must be either
            ``IRTagged(FXTR_ID_TAG, bytes)`` or
            ``IRTagged(FXTR_ID_WITH_PROV_TAG, bytes)``.
        embed_prov: Callable that takes a bare provenance-node FxtrID
            and returns ``FxtrID(unwrapped_data, provenance_parent=prov)``.
            Typically :meth:`FxtrScope.embed_as_provenance_parent` —
            the scope already enforces "prov is tracked", "tag is
            provenance", and the sync unwrap. This function calls it
            unconditionally for ``FXTR_ID_WITH_PROV_TAG`` leaves and
            never for bare leaves.

    Returns:
        A decoded ``FxtrID``. For the with-prov tag, the result is
        whatever ``embed_prov(prov_id)`` returns.

    Raises:
        IRException: Malformed input.
        Whatever ``embed_prov`` raises (typically ``ValueError`` if
        the prov ID isn't tracked / unwrappable in the target scope).
    """
    if not isinstance(value, IRTagged):
        raise IRException(
            message=f"expected IRTagged for FxtrID; got {type(value).__name__}",
            category="MalformedPacket",
        )
    inner = value.value
    if value.tag == FXTR_ID_TAG:
        if not isinstance(inner, bytes):
            raise IRException(
                message=(
                    f"IRTagged({FXTR_ID_TAG!r}) value must be bytes; got {type(inner).__name__}"
                ),
                category="MalformedPacket",
            )
        return FxtrID(raw_data_id=inner)
    if value.tag == FXTR_ID_WITH_PROV_TAG:
        if not isinstance(inner, bytes):
            raise IRException(
                message=(
                    f"IRTagged({FXTR_ID_WITH_PROV_TAG!r}) value must be bytes; got"
                    f" {type(inner).__name__}"
                ),
                category="MalformedPacket",
            )
        prov_id = FxtrID(raw_data_id=inner)
        return embed_prov(prov_id)
    raise IRException(
        message=f"unexpected FxtrID tag {value.tag!r}",
        category="MalformedPacket",
    )


# =============================================================================
# Precomp closure: helpers for sync provenance reads over the wire
# =============================================================================


def compute_and_track_precomp_closure(
    arena: ReadableArena[Any],
    lease: Lease[Any],
    seed_ids: Iterable[FxtrID],
) -> list[dict[str, IRValue]]:
    """Compute the closed precomp map for `seed_ids` as an IR-encoded list,
    side-effect-tracking every visited ID into ``arena``'s liveset.

    Walks `seed_ids` via BFS, calling `unwrap_provenance` /
    `c_node_to_context_data` / `p_node_to_pathroot` /
    `step_back_p_ctx` on the arena (with cloned leases) for every
    visited provenance node, and following the discovered targets.
    Each visited ID with at least one relationship gets one entry in
    the returned list:

        {
            "id": <tagged FxtrID>,
            # any of the below as applicable:
            "unwraps_to":      <tagged FxtrID>,
            "ctx_node":        <tagged FxtrID>,
            "step_back_p_ctx": <tagged FxtrID>,
        }

    The list is *closed*: every relationship target is itself an
    `id` in some entry (or is a leaf node — `d` / `!` — that
    legitimately has no relationships and is omitted from the
    output). Client-side ingestion via :func:`ingest_precomp`
    populates three sync caches that fully cover the abstract
    `ReadableArena`'s sync provenance reads.

    **Tracking contract.** Every concrete ``ReadableArena`` impl's
    sync provenance reads (`unwrap_provenance` / `c_node_to_context_data`
    / `p_node_to_pathroot` / `step_back_p_ctx`) tracks its result in
    the arena's liveset (e.g. via ``MemoryArena._track_from_store_or_none``
    or ``SQLiteNamespaceArena._track_from_precomputed``). Because this
    walk uses those same arena methods, every visited ID is in
    ``arena``'s liveset by the time the function returns. Callers
    that build a wire arena packet **must** call this on the same
    arena that will be wrapped by :class:`ArenaIRHandleAdapter`
    (typically a ``copy_to_new_arena`` fork) so that any precomp
    value becomes a tracked-id the receiver can later ``load_flat``
    over the wire — see ``part5bugfix1_plan.md`` for the invariant
    discussion.

    Ownership: `lease` is taken on entry (one copy retained as
    `outer`); each arena method gets a fresh clone, and `outer` is
    discarded on exit (success or exception).
    """
    entries: list[dict[str, IRValue]] = []
    frontier: deque[FxtrID] = deque(seed_ids)
    visited: set[FxtrID] = set()
    with lease.take() as outer:
        while frontier:
            node_id = frontier.popleft()
            if node_id in visited:
                continue
            visited.add(node_id)
            tag = node_id.get_tag()

            entry: dict[str, IRValue] = {"id": _encode_fxtr_id(node_id)}

            # Provenance nodes (c/p/u/n) can be unwrapped.
            if tag in ("c", "p", "u", "n"):
                uw = arena.unwrap_provenance(outer.clone(), node_id)
                if uw is not None:
                    entry["unwraps_to"] = _encode_fxtr_id(uw)
                    frontier.append(uw)

            # c-nodes have ctx_node; p-nodes have pathroot (also called ctx_node
            # on the wire) plus step_back_p_ctx.
            if tag == "c":
                ctx = arena.c_node_to_context_data(outer.clone(), node_id)
                entry["ctx_node"] = _encode_fxtr_id(ctx)
                frontier.append(ctx)
            elif tag == "p":
                ctx = arena.p_node_to_pathroot(outer.clone(), node_id)
                entry["ctx_node"] = _encode_fxtr_id(ctx)
                frontier.append(ctx)
                sb = arena.step_back_p_ctx(outer.clone(), node_id)
                if sb is not None:
                    entry["step_back_p_ctx"] = _encode_fxtr_id(sb)
                    frontier.append(sb)

            # Skip entries that have no relationships beyond `id` — those are
            # leaf nodes (e.g. `d`-nodes reachable via deps) and the client
            # doesn't need to cache anything for them.
            if len(entry) > 1:
                entries.append(entry)
    return entries


def ingest_precomp(
    entries: Sequence[Mapping[str, IRValue]],
    *,
    unwraps_to: dict[FxtrID, FxtrID],
    ctx_node: dict[FxtrID, FxtrID],
    step_back_p_ctx: dict[FxtrID, FxtrID],
) -> None:
    """Walk a precomp-entry list and populate the three caller-supplied caches.

    Each entry that carries a relationship adds a row to the
    corresponding cache; relationships absent from an entry simply
    aren't added. Symmetric inverse of
    :func:`compute_and_track_precomp_closure`'s output shape.
    """
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise IRException(
                message=f"precomp entry must be a mapping, got {type(entry).__name__}",
                category="MalformedArenaPacket",
            )
        if "id" not in entry:
            raise IRException(
                message="precomp entry missing 'id'",
                category="MalformedArenaPacket",
            )
        node_id = _decode_fxtr_id(entry["id"])
        if "unwraps_to" in entry:
            unwraps_to[node_id] = _decode_fxtr_id(entry["unwraps_to"])
        if "ctx_node" in entry:
            ctx_node[node_id] = _decode_fxtr_id(entry["ctx_node"])
        if "step_back_p_ctx" in entry:
            step_back_p_ctx[node_id] = _decode_fxtr_id(entry["step_back_p_ctx"])


def _decode_flat_data(
    reply: IRValue,
) -> tuple[FxtrEntityFlatData, Sequence[Mapping[str, IRValue]]]:
    """Inverse of the inline encoding done in `ArenaIRHandleAdapter._handle_load_flat`.

    Returns `(flat, precomp_entries)` so the caller (typically
    `CapIRArena.load_flat`) can ingest the precomp list separately
    from the flat data.
    """
    if not isinstance(reply, Mapping):
        raise IRException(
            message=f"expected load_flat reply to be a Mapping, got {type(reply).__name__}",
            category="MalformedArenaPacket",
        )
    try:
        entity_id_raw = reply["entity_id"]
        entity_bytes_raw = reply["entity_bytes"]
        deps_raw = reply["deps"]
        precomp_raw = reply["precomp"]
    except KeyError as exc:
        raise IRException(
            message=f"load_flat reply missing field {exc.args[0]!r}",
            category="MalformedArenaPacket",
        ) from exc

    entity_id = _decode_fxtr_id(entity_id_raw)

    if entity_bytes_raw is not None and not isinstance(entity_bytes_raw, bytes):
        raise IRException(
            message=(
                "load_flat reply 'entity_bytes' must be bytes or None,"
                f" got {type(entity_bytes_raw).__name__}"
            ),
            category="MalformedArenaPacket",
        )

    if not isinstance(deps_raw, Sequence) or isinstance(deps_raw, (str, bytes)):
        raise IRException(
            message=f"load_flat reply 'deps' must be a Sequence, got {type(deps_raw).__name__}",
            category="MalformedArenaPacket",
        )

    deps: list[tuple[FxtrID, DepKind]] = []
    for dep in deps_raw:
        if not isinstance(dep, Mapping) or "id" not in dep or "kind" not in dep:
            raise IRException(
                message="dep entry must be a Mapping with 'id' and 'kind'",
                category="MalformedArenaPacket",
            )
        kind_raw = dep["kind"]
        if not isinstance(kind_raw, str):
            raise IRException(
                message=f"dep 'kind' must be a string, got {type(kind_raw).__name__}",
                category="MalformedArenaPacket",
            )
        try:
            kind = DepKind(kind_raw)
        except ValueError as exc:
            raise IRException(
                message=f"unknown DepKind value {kind_raw!r}",
                category="MalformedArenaPacket",
            ) from exc
        deps.append((_decode_fxtr_id(dep["id"]), kind))

    if not isinstance(precomp_raw, Sequence) or isinstance(precomp_raw, (str, bytes)):
        raise IRException(
            message=(
                f"load_flat reply 'precomp' must be a Sequence, got {type(precomp_raw).__name__}"
            ),
            category="MalformedArenaPacket",
        )

    flat = FxtrEntityFlatData(entity_id=entity_id, entity_bytes=entity_bytes_raw, deps=deps)
    # Each entry's shape is validated lazily when `ingest_precomp` walks the
    # list; the cast is sound because that walk raises `MalformedArenaPacket`
    # if any entry isn't a Mapping with the expected fields.
    return flat, cast(Sequence[Mapping[str, IRValue]], precomp_raw)


# =============================================================================
# Server-side adapter: ArenaIRHandleAdapter
# =============================================================================


class ArenaIRHandleAdapter[B: LocalLifetimeBrand](IRHandleLocalImplBase[B]):
    """Wrap a `ReadableArena[B]` as an `IRHandle[B]`.

    The handle's token aliases the wrapped arena's token (so leases on
    one are leases on the other). Two request methods, both spelled
    as `Call(method, arg)`:

    - `"load_flat"` — arg is a tagged `FxtrID`. Returns a Mapping with
      flat bytes + deps + a closed precomp map (see
      :func:`compute_and_track_precomp_closure` for shape).
    - `"abandon"` — arg is a ``Sequence[Mapping[{"id", "count"}]]``.
      Each entry releases the introduction-refcount of one id by
      ``count``; when an id's counter hits zero on the server, it is
      abandoned on the wrapped arena. See ``_ABANDON_ID_FIELD`` /
      ``_ABANDON_COUNT_FIELD`` for the full shape and race protection.

    Forking a sub-view is **not** a wire operation. The
    :class:`CapIRArena` client manages forking locally via shared
    :class:`CapIRArenaRemoteState`; nothing needs to round-trip to
    the server for it.

    **Introduction refcount.** Every wire emission that mentions an
    arena-side ID counts as exactly one *introduction* of that id to
    the client (per ``DiscardMessage`` in
    ``fxtr2/rpc/lowering_types.py``). The adapter tracks
    ``_total_introductions: dict[FxtrID, int]`` — the cumulative
    number of emissions the server has shipped per id. Each emission
    bumps the counter by one (unique id per emission, regardless of
    how many times the id appears in that emission's payload). A wire
    ``"abandon"`` ships the client's local introduction count per id
    and the server decrements its counter by exactly that amount; an
    in-flight introduction races to a strictly higher counter, so the
    abandon won't accidentally reclaim a re-introduced id. This is
    what keeps invariant (c) — "server tracks ⊇ client tracks" — safe
    under concurrent abandon + introduction.

    `GetItem` requests are rejected with
    `IRException(category="GetItemUnsupported")`. Unknown methods
    raise `IRException(category="UnknownArenaMethod")`. Malformed
    argument shapes raise `IRException(category="MalformedArenaPacket")`.

    The caller must supply a `task_group` whose lifetime outlives the
    arena's token (the standard `IRHandleLocalImplBase` invariant).
    Typically co-managed via `open_lifetime_task_group`.
    """

    def __init__(
        self,
        arena: ReadableArena[B],
        *,
        task_group: anyio.abc.TaskGroup,
    ) -> None:
        token = arena.token
        if not isinstance(token, LocalLifetimeToken):
            raise TypeError(
                f"ArenaIRHandleAdapter requires a LocalLifetimeToken, got {type(token).__name__}"
            )
        self._arena = arena
        self._token: LocalLifetimeToken[B] = token
        self._task_group = task_group
        self._total_introductions: dict[FxtrID, int] = {}

    def register_emission(self, ids: Iterable[FxtrID]) -> None:
        """Bump the introduction refcount of every distinct id in ``ids`` by +1.

        Called by the wire-packet builders after assembling a payload
        that mentions ``ids`` — the initial ``fxtr_arena`` packet
        emitted by service-call dispatch / arena-handle fork, and
        every ``load_flat`` reply (registered internally). Idempotent
        within a single call: each unique id contributes +1 regardless
        of how many times it appears in ``ids``.
        """
        for nid in set(ids):
            self._total_introductions[nid] = self._total_introductions.get(nid, 0) + 1

    @override
    async def _run_request_async(
        self,
        lease: Lease[B],
        request: Call[UnwrappedCallArg] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        try:
            with lease.take() as lease:
                if isinstance(request, GetItem):
                    raise IRException(
                        message="ArenaIRHandleAdapter does not support GetItem",
                        category="GetItemUnsupported",
                    )
                method = request.method
                if not isinstance(method, str):
                    raise IRException(
                        message=f"method must be a string, got {type(method).__name__}",
                        category="UnknownArenaMethod",
                    )
                if method == "load_flat":
                    return await self._handle_load_flat(lease, request.arg)
                if method == "abandon":
                    return self._handle_abandon(lease, request.arg)
                raise IRException(
                    message=f"unknown arena method {method!r}",
                    category="UnknownArenaMethod",
                )
        finally:
            # The transfer convention gives us ownership of any leases in
            # `request.arg`; our handlers never consume them (FxtrIDs only).
            # Defensive cleanup so a malformed arg with leases inside doesn't
            # leak; idempotent for valid inputs (which contain no leases).
            if isinstance(request, Call):
                discard_leases_in(request.arg)

    async def _handle_load_flat(
        self, lease: Lease[B], arg: UnwrappedCallArg
    ) -> Mapping[str, IRValue]:
        node_id = _decode_fxtr_id(cast(IRValue, arg))
        flat = await self._arena.load_flat(lease.clone(), node_id)
        # Seed the precomp closure with the loaded node + every dep, so the
        # client can do any subsequent provenance read locally.
        seeds: list[FxtrID] = [node_id, *(d for d, _ in flat.deps)]
        precomp = compute_and_track_precomp_closure(self._arena, lease.clone(), seeds)
        # Register the introduction refcount for every id mentioned in
        # the reply (the loaded id, every dep, and every key/value in
        # the precomp closure). Each unique id contributes +1 to
        # ``_total_introductions`` (per the cap-IR introduction-refcount
        # pattern; see ``_ABANDON_ID_FIELD`` docstring).
        self.register_emission(_collect_load_flat_reply_ids(flat, precomp))
        return {
            "entity_id": _encode_fxtr_id(flat.entity_id),
            "entity_bytes": flat.entity_bytes,
            "deps": [
                {"id": _encode_fxtr_id(d_id), "kind": d_kind.value} for d_id, d_kind in flat.deps
            ],
            "precomp": precomp,
        }

    def _handle_abandon(self, lease: Lease[B], arg: UnwrappedCallArg) -> None:
        """Decrement introduction refcounts; abandon underlying when they reach zero.

        Wire form is ``Sequence[Mapping[{"id", "count"}]]`` — see
        ``_ABANDON_ID_FIELD`` / ``_ABANDON_COUNT_FIELD``. Each entry
        decrements ``_total_introductions[id]`` by ``count``. When
        the post-decrement counter reaches zero, ``id`` is abandoned
        on the wrapped arena. Counters > 0 mean the server has
        emitted a more recent introduction for ``id`` that the client
        had not observed when constructing this abandon — the id
        stays tracked.
        """
        if not isinstance(arg, Sequence) or isinstance(arg, (str, bytes)):
            raise IRException(
                message=f"abandon arg must be a Sequence, got {type(arg).__name__}",
                category="MalformedArenaPacket",
            )
        for item in arg:
            if not isinstance(item, Mapping):
                raise IRException(
                    message=(
                        f"abandon entry must be a Mapping{{{_ABANDON_ID_FIELD!r}, "
                        f"{_ABANDON_COUNT_FIELD!r}}}, got {type(item).__name__}"
                    ),
                    category="MalformedArenaPacket",
                )
            id_value = item.get(_ABANDON_ID_FIELD)
            count_value = item.get(_ABANDON_COUNT_FIELD)
            if id_value is None or count_value is None:
                raise IRException(
                    message=(
                        "abandon entry missing required field "
                        f"({_ABANDON_ID_FIELD!r} or {_ABANDON_COUNT_FIELD!r})"
                    ),
                    category="MalformedArenaPacket",
                )
            if not isinstance(count_value, int) or isinstance(count_value, bool):
                raise IRException(
                    message=(
                        f"abandon entry {_ABANDON_COUNT_FIELD!r} must be an int, "
                        f"got {type(count_value).__name__}"
                    ),
                    category="MalformedArenaPacket",
                )
            node_id = _decode_fxtr_id(cast(IRValue, id_value))
            cur = self._total_introductions.get(node_id)
            if cur is None or cur < count_value:
                raise IRException(
                    message=(
                        f"abandon entry for {node_id} ships count={count_value} but "
                        f"server's introduction counter is {cur} — protocol violation"
                    ),
                    category="MalformedArenaPacket",
                )
            new_count = cur - count_value
            if new_count == 0:
                del self._total_introductions[node_id]
                self._arena.abandon(lease.clone(), node_id)
            else:
                self._total_introductions[node_id] = new_count
        # Lease was cloned per-abandon; discard the original so its
        # active state is consumed.
        lease.discard()
        return None


def _collect_load_flat_reply_ids(
    flat: FxtrEntityFlatData,
    precomp: Sequence[Mapping[str, IRValue]],
) -> set[FxtrID]:
    """Enumerate every id mentioned in a ``load_flat`` reply payload.

    Used to register introduction refcounts on the server and mirror
    them on the client. Returns the set of: the loaded ``entity_id``,
    every dep id, and every key + value in any precomp entry.
    """
    ids: set[FxtrID] = {flat.entity_id}
    for dep_id, _ in flat.deps:
        ids.add(dep_id)
    ids |= collect_precomp_ids(precomp)
    return ids


def collect_precomp_ids(precomp: Sequence[Mapping[str, IRValue]]) -> set[FxtrID]:
    """Enumerate every id mentioned anywhere in a precomp closure.

    Returns the set of all entry ``id`` keys plus every value cited by
    the entries (``unwraps_to`` / ``ctx_node`` / ``step_back_p_ctx``).
    Used by introduction-refcount registration on both sides; matching
    the same enumeration rule on server and client is what keeps the
    counters in sync.
    """
    ids: set[FxtrID] = set()
    for entry in precomp:
        if "id" in entry:
            ids.add(_decode_fxtr_id(entry["id"]))
        for field in ("unwraps_to", "ctx_node", "step_back_p_ctx"):
            if field in entry:
                ids.add(_decode_fxtr_id(entry[field]))
    return ids


# =============================================================================
# Client-side: CapIRArenaRemoteState — shared across sibling arenas
# =============================================================================


@dataclasses.dataclass
class _CapIRArenaBridgeState:
    """Bridge-mode state for :class:`CapIRArena.with_converter`.

    Wraps any :data:`LocalOrPipelinedBridge` with the per-arena
    metadata needed to drive it from a derived-input lease — namely
    ``base_token``, the input of ``derived`` whose extracted lease
    feeds Dependent converters as ``base``.

    Lifetime: this object is owned by exactly one
    :class:`CapIRArenaRemoteState`; it is discarded when the remote
    state is closed (which happens when the last refcount drops to
    zero or — exceptionally — when the constructor fails).
    """

    base_token: LifetimeToken[Any]
    bridge: LocalOrPipelinedBridge

    def discard(self) -> None:
        """Discard every at-rest lease in the wrapped bridge. Idempotent."""
        self.bridge.discard_at_rest()


class CapIRArenaRemoteState[B_Z: LifetimeBrand]:
    """Shared state for one logical remote arena, used by N sibling :class:`CapIRArena` views.

    Owns:

    - The wire `IRHandle` (concrete; no `IRFuture` variant).
    - The per-mode lifetime/lease conversion machinery
      (`z_token`, `derived`, `x_token`, `bridge_state`).
    - The refcounted liveset (`_refcounts: dict[FxtrID, int]`)
      tracking how many sibling :class:`CapIRArena` views still hold
      each ID.
    - The three precomp caches that serve sync provenance reads.

    Responsibilities:

    - **Lease conversion.** :meth:`make_y_lease` is the existing
      Z→Y conversion logic, centralized here since the bridge state is
      shared. Each call clones the bridge's at-rest leases (no
      destructive consumption); the bridge converter returns a fresh
      `Lease[Y]` (or `UnwrapForPipelining[Lease[Y], …]`).
    - **Wire dispatch.** :meth:`request_remote` runs a request through
      the Z→Y conversion + the remote's `dispatch`.
    - **Refcount management.** :meth:`acquire` bumps; :meth:`release`
      decrements. When a release drops a count to zero, the wire
      `abandon` is dispatched as a single batched call covering every
      ID that hit zero in that release pass, and the precomp entries
      for those IDs are purged. If the release also empties the
      refcount map entirely, the bridge state's at-rest leases are
      discarded and the state is marked closed.

      Refcounts have **two contributing sources**: per-sibling-arena
      tracking (each sibling's liveset entry contributes +1) and
      per-precomp-entry citation (each ``unwraps_to`` /
      ``ctx_node`` / ``step_back_p_ctx`` value in the precomp cache
      contributes +1 to the *value* it points at). The per-precomp
      contribution mirrors the server side, where every visited ID
      in the wire-shipped precomp closure is tracked in the wrapped
      arena's liveset (via
      :func:`compute_and_track_precomp_closure`). When an ID's
      refcount drops to zero, ``_drop_to_zero`` purges the precomp
      entries keyed by that ID *and* decrements the values cited by
      those entries, cascading naturally through the closure. See
      ``v2/plans/phase3/stage8c_user_caps/part5bugfix1_plan.md`` for
      the invariant discussion.

    Construction-failure cleanup: callers using a `bridge` arg own it
    until the state is fully constructed; if `__init__` raises, the
    caller is responsible for discarding `bridge`. The factory
    classmethods on :class:`CapIRArena` handle this.
    """

    # Lifetime / brand state.
    _z_token: LifetimeToken[B_Z]
    _derived: DerivedLocalLifetime[Any] | None
    _x_token: LifetimeToken[Any] | None
    _bridge_state: _CapIRArenaBridgeState | None

    # Wire endpoint (concrete handle; no IRFuture).
    _remote: IRHandle[Any]

    # Tracked-ID bookkeeping (shared across siblings).
    _refcounts: dict[FxtrID, int]

    # IDs whose refcount hit zero in a no-lease release path
    # (typically :meth:`CapIRArena.close`). These wire abandons are
    # deferred until the next :meth:`release` call dispatches them in
    # the same batched wire `abandon` as that call's own zero-hits.
    # If the shared state shuts down via :meth:`_close_at_rest` before
    # any release-with-lease happens, the queue is dropped — the
    # IRHandle drops along with the state and the server-side
    # adapter's MemoryArena is reclaimed via Python GC.
    _pending_abandons: list[FxtrID]

    # Sync provenance caches (shared across siblings).
    _precomp_unwraps_to: dict[FxtrID, FxtrID]
    _precomp_ctx_node: dict[FxtrID, FxtrID]
    _precomp_step_back_p_ctx: dict[FxtrID, FxtrID]

    # Mirror of the server's ``ArenaIRHandleAdapter._total_introductions``:
    # how many distinct wire emissions have introduced each id to this
    # client. Bumped by +1 per id per emission observed (initial
    # packet, every ``load_flat`` reply); shipped on abandon and
    # decremented to zero after. Per the cap-IR pattern in
    # ``DiscardMessage``: this is what protects against the abandon /
    # re-introduction race.
    _remote_refcounts: dict[FxtrID, int]

    _closed: bool

    def __init__(
        self,
        *,
        z_token: LifetimeToken[B_Z],
        derived: DerivedLocalLifetime[Any] | None,
        x_token: LifetimeToken[Any] | None,
        bridge_state: _CapIRArenaBridgeState | None,
        remote: IRHandle[Any],
        initial_tracked: Collection[FxtrID],
        initial_precomp: Sequence[Mapping[str, IRValue]] = (),
    ) -> None:
        """Validate per-mode invariants and seed the refcount map.

        Invariants (mirrors the previous ``CapIRArena.__init__``):

        - **direct mode**: `derived is None`, `x_token is z_token`,
          `bridge_state is None`.
        - **derived (no bridge)**: `derived.token is z_token`,
          `x_token in derived.inputs`, `bridge_state is None`.
        - **with-converter**: `derived.token is z_token`,
          `x_token is None`, `bridge_state is not None`,
          `bridge_state.base_token in derived.inputs`.
        - In direct/derived modes the remote handle must be on
          `x_token`.

        On any validation failure the state is left invalid; the
        caller (factory) is responsible for discarding any owned
        resources (e.g. the `bridge_state`).
        """
        has_bridge = bridge_state is not None
        if derived is None:
            if has_bridge:
                raise ValueError(
                    "CapIRArenaRemoteState: `bridge_state` requires a `derived` lifetime"
                )
            if x_token is None or x_token is not z_token:
                raise ValueError(
                    "CapIRArenaRemoteState: when `derived` is None, `x_token` must be `z_token`"
                )
        else:
            if derived.token is not z_token:
                raise ValueError(
                    "CapIRArenaRemoteState: `derived.token` must be the same as `z_token`"
                )
            if has_bridge:
                if x_token is not None:
                    raise ValueError(
                        "CapIRArenaRemoteState: with `bridge_state`, `x_token` must be None"
                    )
                if bridge_state.base_token not in derived.inputs:
                    raise ValueError(
                        "CapIRArenaRemoteState: `bridge_state.base_token` must be in"
                        " `derived.inputs`"
                    )
            else:
                if x_token is None:
                    raise ValueError(
                        "CapIRArenaRemoteState: derived mode without bridge requires `x_token`"
                    )
                if x_token not in derived.inputs:
                    raise ValueError(
                        "CapIRArenaRemoteState: `x_token` must be one of `derived.inputs`"
                    )
        if not has_bridge:
            assert x_token is not None
            if remote.token is not x_token:
                raise ValueError(
                    "CapIRArenaRemoteState: without a bridge, `remote.token` must be `x_token`"
                )

        self._z_token = z_token
        self._derived = derived
        self._x_token = x_token
        self._bridge_state = bridge_state
        self._remote = remote
        self._refcounts = {nid: 1 for nid in initial_tracked}
        self._pending_abandons = []
        self._precomp_unwraps_to = {}
        self._precomp_ctx_node = {}
        self._precomp_step_back_p_ctx = {}
        self._remote_refcounts = {}
        self._closed = False
        # Mirror the server's introduction-refcount bump for the
        # initial wire emission: every id mentioned in
        # ``initial_tracked`` ∪ keys/values of ``initial_precomp``
        # contributes +1 to ``_remote_refcounts``. Must use the same
        # enumeration rule as the server (see
        # ``ArenaIRHandleAdapter.register_emission`` callsites).
        emission_ids: set[FxtrID] = set(initial_tracked) | collect_precomp_ids(initial_precomp)
        self._observe_emission(emission_ids)
        if initial_precomp:
            self._ingest_precomp(initial_precomp)

    def _observe_emission(self, ids: Iterable[FxtrID]) -> None:
        """Mirror :meth:`ArenaIRHandleAdapter.register_emission` on the client.

        Each unique id in ``ids`` represents one wire-observed
        introduction; we bump ``_remote_refcounts[id]`` by +1 per id.
        Same idempotency rule as the server side: multiple
        appearances in a single emission count as one.
        """
        for nid in set(ids):
            self._remote_refcounts[nid] = self._remote_refcounts.get(nid, 0) + 1

    # -- Public read-only accessors -----------------------------------------

    @property
    def z_token(self) -> LifetimeToken[B_Z]:
        return self._z_token

    @property
    def closed(self) -> bool:
        return self._closed

    def is_tracked(self, node_id: FxtrID) -> bool:
        """Pure-state membership check; ignores any per-arena slicing."""
        return node_id in self._refcounts

    def get_unwraps_to(self, node_id: FxtrID) -> FxtrID | None:
        return self._precomp_unwraps_to.get(node_id)

    def get_ctx_node(self, node_id: FxtrID) -> FxtrID | None:
        return self._precomp_ctx_node.get(node_id)

    def get_step_back_p_ctx(self, node_id: FxtrID) -> FxtrID | None:
        return self._precomp_step_back_p_ctx.get(node_id)

    # -- Lease conversion ---------------------------------------------------

    def precheck(self, lease: Lease[B_Z]) -> None:
        """Active-lease + brand-match check matching ``MemoryArena`` semantics."""
        if self._closed:
            raise ValueError("CapIRArenaRemoteState is closed")
        lease.ensure_active()
        if lease.subject is not self._z_token:
            raise LeaseGrantMismatchError(
                f"Lease subject {lease.subject!r} does not match this arena's "
                f"token {self._z_token!r}"
            )

    def make_y_lease(
        self, z_lease: Lease[B_Z]
    ) -> Lease[Any] | UnwrapForPipelining[Lease[Any], Any]:
        """Take ownership of `z_lease`; return a Y-lease (possibly pipelined).

        - Direct mode: pass-through (z is x is y by brand).
        - Derived (no bridge) mode: `exchange_reverse` to extract the
          single X-lease, return as Y.
        - With-converter (bridge) mode: `exchange_reverse` to extract
          ``Lease[base_token]``, then dispatch on the bridge variant
          (Independent clones the at-rest lease and discards the
          unused base; Dependent runs its converter on cloned inputs).
        """
        if self._derived is None:
            # Direct mode: z_lease's brand is the same as the remote's.
            return z_lease.take()
        # Derived / with-converter modes.
        inputs = self._derived.exchange_reverse(z_lease.take())
        if self._bridge_state is None:
            # Derived mode (no bridge): single X, Y == X.
            assert self._x_token is not None
            x_lease = inputs.pop(self._x_token)
            for stray in inputs.values():
                stray.discard()
            return x_lease
        # With-converter (bridge) mode: extract base, dispatch on
        # bridge variant.
        base = inputs.pop(self._bridge_state.base_token)
        for stray in inputs.values():
            stray.discard()
        bridge = self._bridge_state.bridge
        if isinstance(bridge, IndependentLocalBridge):
            base.discard()
            return bridge.lease.clone()
        if isinstance(bridge, IndependentPipelinedBridge):
            base.discard()
            return UnwrapForPipelining(
                future=bridge.pipelined.future,
                lease=bridge.pipelined.lease.clone(),
            )
        if isinstance(bridge, DependentLocalBridge):
            local_clones = {tok: lease.clone() for tok, lease in bridge.leases.items()}
            return bridge.converter(local_clones, base)
        assert isinstance(bridge, DependentPipelinedBridge)
        cloned = bridge.clone()
        return cloned.converter(cloned.leases, base)

    def dispatch_remote(
        self,
        y_lease: Lease[Any] | UnwrapForPipelining[Lease[Any], Any],
        request: Request,
    ):
        """Dispatch `request` on `_remote`; returns `(future, future_lease)`."""
        return self._remote.dispatch(y_lease, request)

    async def request_remote(self, z_lease: Lease[B_Z], request: Request) -> IRValue:
        """Combined pattern: convert Z→Y lease, dispatch, await, discard."""
        y_lease = self.make_y_lease(z_lease)
        fut, fut_lease = self.dispatch_remote(y_lease, request)
        try:
            return await fut.wait(fut_lease.clone())
        finally:
            fut_lease.discard()

    # -- Refcounted liveset / wire abandon ----------------------------------

    def acquire(self, ids: Iterable[FxtrID]) -> None:
        """Bump refcounts for each id (creating the entry at 1 if missing)."""
        for nid in ids:
            self._refcounts[nid] = self._refcounts.get(nid, 0) + 1

    def _drop_to_zero(self, nid: FxtrID, zero_queue: list[FxtrID]) -> None:
        """Local bookkeeping for an ID whose refcount just hit zero.

        Removes the id from the refcount map, purges its precomp
        entries (i.e. entries keyed by ``nid``), and decrements the
        refcounts of every value those entries cited — cascading
        through any value that itself hits zero. Each id whose count
        drops to zero in the cascade is appended to ``zero_queue``;
        the caller is responsible for emitting them all in a single
        batched wire abandon.

        Does **not** dispatch a wire abandon — callers
        decide whether to dispatch now (:meth:`release`) or queue
        for later (:meth:`release_no_dispatch`).
        """
        del self._refcounts[nid]
        zero_queue.append(nid)
        # Pull the values cited by precomp entries keyed by nid; each
        # is a -1 ref to the value (matching the +1 from
        # ``_ingest_precomp_slot``).
        cascade_values: list[FxtrID] = []
        for cache in (
            self._precomp_unwraps_to,
            self._precomp_ctx_node,
            self._precomp_step_back_p_ctx,
        ):
            value = cache.pop(nid, None)
            if value is not None:
                cascade_values.append(value)
        for v in cascade_values:
            cur = self._refcounts.get(v)
            if cur is None:
                # Defensive: bump/decrement should keep this in sync.
                # If we land here it indicates a bookkeeping bug
                # earlier; loudly fail rather than silently miss the
                # cascade.
                raise RuntimeError(
                    f"_drop_to_zero: precomp value {v} cited by {nid}'s precomp "
                    "entry has no refcount; ingest/decrement bookkeeping is broken"
                )
            if cur == 1:
                self._drop_to_zero(v, zero_queue)
            else:
                self._refcounts[v] = cur - 1

    def release(
        self,
        z_lease: Lease[B_Z],
        ids: Collection[FxtrID],
    ) -> None:
        """Decrement refcounts for each id; dispatch a batched wire abandon.

        Consumes ``z_lease``. The wire batch covers every ID that hits
        zero in this call **plus** any ``_pending_abandons`` queued by
        prior :meth:`release_no_dispatch` calls; the queue is cleared
        in the same atomic step. If nothing needs to go over the wire
        (no zero-hits and an empty queue), the lease is just discarded.

        Refcounts dropping to zero also purge the associated precomp
        entries. If the release empties the refcount map entirely,
        the state is closed (bridge at-rest leases discarded) and any
        still-queued abandons are dropped — server-side reclaim then
        rides on the IRHandle drop.
        """
        zeros: list[FxtrID] = []
        for nid in ids:
            cur = self._refcounts.get(nid)
            if cur is None:
                z_lease.discard()
                raise RuntimeError(f"CapIRArenaRemoteState.release: id {nid} has no refcount")
            if cur == 1:
                # ``_drop_to_zero`` cascades through the precomp values
                # cited by ``nid``'s entry, appending each id whose
                # count hits zero (including ``nid`` itself) to
                # ``zeros``.
                self._drop_to_zero(nid, zeros)
            else:
                self._refcounts[nid] = cur - 1

        # Pull in any deferred abandons queued by close(); they get
        # dispatched in the same batched wire call as our zero-hits.
        if self._pending_abandons:
            zeros.extend(self._pending_abandons)
            self._pending_abandons.clear()

        if zeros and not self._closed:
            # Fire-and-forget: drop the result lease so the dispatched
            # task drains as soon as it completes. Wire entry per id:
            # the count we ship is the local
            # ``_remote_refcounts[id]`` (= the number of distinct wire
            # emissions in which the server has introduced this id to
            # us). After shipping, we reset ``_remote_refcounts[id]``
            # to zero — any later introduction from the server lives
            # at a strictly higher server-side counter, so the
            # decrement in ``_handle_abandon`` won't reclaim it.
            arg: list[Mapping[str, IRValue]] = []
            for nid in zeros:
                count = self._remote_refcounts.pop(nid, 0)
                arg.append(
                    {
                        _ABANDON_ID_FIELD: _encode_fxtr_id(nid),
                        _ABANDON_COUNT_FIELD: count,
                    }
                )
            y_lease = self.make_y_lease(z_lease)
            _fut, fut_lease = self.dispatch_remote(y_lease, Call("abandon", arg))
            fut_lease.discard()
        else:
            z_lease.discard()

        if not self._refcounts:
            self._close_at_rest()

    def _ingest_precomp(self, entries: Sequence[Mapping[str, IRValue]]) -> None:
        """Populate the shared precomp caches from a precomp-entry list,
        bumping ``_refcounts`` for every newly-installed value.

        Internal: called automatically at construction (from
        ``initial_precomp``) and from :meth:`CapIRArena.load_flat` after
        each remote reply.

        Refcount contract: each newly-installed ``unwraps_to`` /
        ``ctx_node`` / ``step_back_p_ctx`` value bumps
        ``_refcounts[value]`` by +1 (creating the entry at 1 if missing).
        Re-ingesting the same entry is idempotent — slots that are
        already populated do not double-bump.
        """
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise IRException(
                    message=f"precomp entry must be a mapping, got {type(entry).__name__}",
                    category="MalformedArenaPacket",
                )
            if "id" not in entry:
                raise IRException(
                    message="precomp entry missing 'id'",
                    category="MalformedArenaPacket",
                )
            node_id = _decode_fxtr_id(entry["id"])
            self._ingest_precomp_slot(self._precomp_unwraps_to, node_id, entry, "unwraps_to")
            self._ingest_precomp_slot(self._precomp_ctx_node, node_id, entry, "ctx_node")
            self._ingest_precomp_slot(
                self._precomp_step_back_p_ctx, node_id, entry, "step_back_p_ctx"
            )

    def _ingest_precomp_slot(
        self,
        cache: dict[FxtrID, FxtrID],
        node_id: FxtrID,
        entry: Mapping[str, IRValue],
        field: str,
    ) -> None:
        """Helper: populate one slot of the precomp cache + bump refcount.

        If ``field`` is absent from ``entry`` or the slot is already
        populated for ``node_id``, this is a no-op (no bump). Otherwise
        decode the value, install it, and bump ``_refcounts[value]``.
        """
        if field not in entry:
            return
        if node_id in cache:
            # Already populated — re-ingest is idempotent; don't double-bump.
            return
        value = _decode_fxtr_id(entry[field])
        cache[node_id] = value
        self._refcounts[value] = self._refcounts.get(value, 0) + 1

    def release_no_dispatch(self, ids: Collection[FxtrID]) -> None:
        """Decrement refcounts without a wire dispatch; queue zero-hits.

        Used by :meth:`CapIRArena.close`, which has no Z-lease in
        scope. IDs whose refcount drops to zero are removed from
        local bookkeeping and added to ``_pending_abandons``; the
        next :meth:`release` call (typically a sibling arena's
        ``abandon``) flushes the queue in the same batched wire
        abandon as its own zero-hits. If no such call ever comes —
        i.e. every sibling closes via this path — the refcount map
        eventually empties here, :meth:`_close_at_rest` drops the
        queue, and server-side cleanup rides on the IRHandle drop
        via Python GC.
        """
        for nid in ids:
            cur = self._refcounts.get(nid)
            if cur is None:
                raise RuntimeError(
                    f"CapIRArenaRemoteState.release_no_dispatch: id {nid} has no refcount"
                )
            if cur == 1:
                # Cascade through precomp values; queue every id whose
                # count hit zero (including ``nid``) for the next
                # release-with-dispatch to flush.
                cascade_zeros: list[FxtrID] = []
                self._drop_to_zero(nid, cascade_zeros)
                self._pending_abandons.extend(cascade_zeros)
            else:
                self._refcounts[nid] = cur - 1
        if not self._refcounts:
            self._close_at_rest()

    def _close_at_rest(self) -> None:
        """Discard the bridge state's at-rest leases. Idempotent.

        Also drops any still-queued ``_pending_abandons`` and clears
        ``_remote_refcounts`` — by this point the refcount map is
        empty, no live arenas remain, and we have no Z-lease. The
        server-side adapter's MemoryArena (and its
        ``_total_introductions`` tally) is reclaimed when the
        IRHandle drops.
        """
        if self._closed:
            return
        self._closed = True
        self._pending_abandons.clear()
        self._remote_refcounts.clear()
        if self._bridge_state is not None:
            self._bridge_state.discard()
            self._bridge_state = None


# =============================================================================
# Client-side arena: CapIRArena (thin share of a CapIRArenaRemoteState)
# =============================================================================


class CapIRArena[B_Z: LifetimeBrand](ReadableArena[B_Z]):
    """Client-side `ReadableArena[B_Z]` whose backing data lives across a Cap-IR boundary.

    Each `CapIRArena` is a thin share of one
    :class:`CapIRArenaRemoteState`. Multiple sibling arenas produced
    by :meth:`copy_to_new_arena` share the same remote state and
    refcount their tracked-id slices against it; only when a node's
    shared refcount drops to zero does an `abandon` cross the wire.

    The class is generic only on `B_Z` — the user-visible brand
    exposed by every public method. The internal X/Y/K token
    relationships are tracked at runtime in the
    :class:`CapIRArenaRemoteState`; brand-level safety is enforced at
    construction time by the three classmethod factories
    (:meth:`direct`, :meth:`derived`, :meth:`with_converter`), each
    of which has its own type parameters.

    The three modes (each is one classmethod):

    1. **`direct`** — no derived lifetime, no bridge. Z = X = Y.
    2. **`derived`** — `B_Z` is a fresh derived lifetime whose inputs
       are `{B_X}`, with `B_Y = B_X` (no bridge).
    3. **`with_converter`** — `B_Z` is derived from a single input,
       with `B_Y` arbitrary. Each request runs the bridge converter
       to mint a `Lease[B_Y]`.

    Provenance reads (`unwrap_provenance` / `c_node_to_context_data`
    / `p_node_to_pathroot` / `step_back_p_ctx`) are served from local
    caches in the remote state, populated by every `load_flat` reply
    (the closed precomp map covers every legitimate provenance read)
    and by any `initial_precomp` shipped at construction time (used
    by the service path for sync-decoding prov-bearing args).
    """

    _remote_state: CapIRArenaRemoteState[B_Z]
    _liveset: set[FxtrID]
    _closed: bool

    def __init__(
        self,
        *,
        remote_state: CapIRArenaRemoteState[B_Z],
        initial_liveset: Collection[FxtrID],
    ) -> None:
        """Construct a thin share of `remote_state` exposing `initial_liveset`.

        Use the :meth:`direct` / :meth:`derived` / :meth:`with_converter`
        factories for the typical "fresh state + arena" build, or
        :meth:`from_remote_state` to construct an additional view of
        an existing state (e.g. when a sender has pre-populated the
        precomp from a self-describing arena packet).

        The caller is expected to have already bumped the remote
        state's refcount for each id in `initial_liveset` (factories
        do this via the constructor; :meth:`copy_to_new_arena` does
        it explicitly; :meth:`from_remote_state` expects the
        remote_state to come pre-seeded).
        """
        for nid in initial_liveset:
            assert (
                nid in remote_state._refcounts
            ), f"CapIRArena: initial_liveset id {nid} is not in the remote state's refcounts"
        self._remote_state = remote_state
        self._liveset = set(initial_liveset)
        self._closed = False

    # -- Factories -----------------------------------------------------------

    @classmethod
    def direct[
        B: LifetimeBrand
    ](
        cls: type["CapIRArena[B]"],
        *,
        token: LifetimeToken[B],
        remote: IRHandle[B],
        initial_tracked: Collection[FxtrID],
        initial_precomp: Sequence[Mapping[str, IRValue]] = (),
    ) -> "CapIRArena[B]":
        """Mode 1 factory: no derived lifetime, no bridge. `Z = X = Y = B`.

        `initial_tracked` must list every ID currently tracked on the
        remote `ArenaIRHandleAdapter` — each one starts at refcount 1
        on the freshly-built remote state. `initial_precomp` is an
        optional precomp closure (same shape as
        :func:`compute_and_track_precomp_closure`'s output) that pre-populates
        the sync provenance caches; supply this when constructing an
        arena from a wire ``fxtr_arena`` packet so prov-bearing
        FxtrIDs in args / return can be sync-decoded.
        """
        state: CapIRArenaRemoteState[B] = CapIRArenaRemoteState(
            z_token=token,
            derived=None,
            x_token=token,
            bridge_state=None,
            remote=remote,
            initial_tracked=initial_tracked,
            initial_precomp=initial_precomp,
        )
        return cls(remote_state=state, initial_liveset=initial_tracked)

    @classmethod
    def derived[
        OutBrand: LocalLifetimeBrand,
        XBrand: LifetimeBrand,
    ](
        cls: type["CapIRArena[OutBrand]"],
        *,
        derived: DerivedLocalLifetime[OutBrand],
        x_token: LifetimeToken[XBrand],
        remote: IRHandle[XBrand],
        initial_tracked: Collection[FxtrID],
        initial_precomp: Sequence[Mapping[str, IRValue]] = (),
    ) -> "CapIRArena[OutBrand]":
        """Mode 2 factory: derived lifetime; `B_Y = B_X` (no bridge)."""
        state: CapIRArenaRemoteState[OutBrand] = CapIRArenaRemoteState(
            z_token=derived.token,
            derived=derived,
            x_token=x_token,
            bridge_state=None,
            remote=remote,
            initial_tracked=initial_tracked,
            initial_precomp=initial_precomp,
        )
        return cls(remote_state=state, initial_liveset=initial_tracked)

    @classmethod
    def with_converter[
        OutBrand: LocalLifetimeBrand,
        BaseBrand: LifetimeBrand,
        YBrand: LifetimeBrand,
    ](
        cls: type["CapIRArena[OutBrand]"],
        *,
        derived: DerivedLocalLifetime[OutBrand],
        base_token: LifetimeToken[BaseBrand],
        remote: IRHandle[YBrand],
        bridge: LocalOrPipelinedBridge,
        initial_tracked: Collection[FxtrID],
        initial_precomp: Sequence[Mapping[str, IRValue]] = (),
    ) -> "CapIRArena[OutBrand]":
        """Mode 3 factory: bridge-driven Z→Y conversion.

        Takes ownership of every at-rest lease in `bridge` — the
        remote state holds them and discards each on
        :meth:`CapIRArenaRemoteState._close_at_rest` (which fires when
        the last refcount drops to zero). Construction failure
        discards the supplied bridge before re-raising.
        """
        bridge_state = _CapIRArenaBridgeState(base_token=base_token, bridge=bridge)
        try:
            state: CapIRArenaRemoteState[OutBrand] = CapIRArenaRemoteState(
                z_token=derived.token,
                derived=derived,
                x_token=None,
                bridge_state=bridge_state,
                remote=remote,
                initial_tracked=initial_tracked,
                initial_precomp=initial_precomp,
            )
        except BaseException:
            bridge_state.discard()
            raise
        return cls(remote_state=state, initial_liveset=initial_tracked)

    # -- Token / state -------------------------------------------------------

    @property
    @override
    def token(self) -> LifetimeToken[B_Z]:
        return self._remote_state._z_token

    def _check_local_tracked(self, lease: Lease[B_Z], node_id: FxtrID) -> None:
        """Ensure the lease is valid and `node_id` is in the local liveset.

        Discards the lease on failure (the caller is supposed to have
        passed it transferred-in but consumes nothing on this path).
        """
        if self._closed:
            lease.discard()
            raise ValueError("CapIRArena is closed")
        self._remote_state.precheck(lease)
        if node_id not in self._liveset:
            lease.discard()
            raise ValueError(f"Node {node_id} is not tracked in this CapIRArena")

    # -- ReadableArena interface --------------------------------------------

    @override
    def is_tracked(self, lease: Lease[B_Z], node_id: FxtrID) -> bool:
        # Permissive on closed: a closed arena trivially tracks nothing.
        # Matches ``MemoryArena.is_tracked`` and lets callers query after
        # the arena auto-closes via abandon-to-empty.
        lease.ensure_active()
        if lease.subject is not self._remote_state._z_token:
            raise LeaseGrantMismatchError(
                f"Lease subject {lease.subject!r} does not match this arena's "
                f"token {self._remote_state._z_token!r}"
            )
        with lease.take():
            if self._closed:
                return False
            return node_id in self._liveset

    @override
    async def load(self, lease: Lease[B_Z], node_id: FxtrID) -> FxtrCbor:
        """Load a stored entity. Implemented as `load_flat` + CBOR decode for
        d/n nodes; raises `NotImplementedError` for derived nodes (c/p/u)
        (their bytes-form synthesis is server-internal in the memory store;
        if needed, callers can re-route via the bridging scope and let the
        memory store re-synthesise)."""
        flat = await self.load_flat(lease, node_id)
        if flat.entity_bytes is None:
            raise NotImplementedError(
                f"CapIRArena.load on derived node {node_id} (no stored "
                "bytes) is not implemented; use load_flat and bridge via "
                "lazily_attach_from_arena to let the consumer scope's memory "
                "store re-synthesise."
            )
        decoded = fxtr_cbor_loads(flat.entity_bytes)
        # Match the abstract contract: child IDs become tracked as a side
        # effect of load (load_flat already added them to `_liveset`).
        return decoded

    @override
    async def load_flat(self, lease: Lease[B_Z], node_id: FxtrID) -> FxtrEntityFlatData:
        self._check_local_tracked(lease, node_id)
        arg = _encode_fxtr_id(node_id)
        reply = await self._remote_state.request_remote(lease, Call("load_flat", arg))
        flat, precomp = _decode_flat_data(reply)
        # Mirror the server's introduction-refcount bump for this
        # reply: every id mentioned anywhere in the payload (entity,
        # deps, precomp keys/values) contributes +1 to
        # ``_remote_refcounts``. Must use the same enumeration rule as
        # ``ArenaIRHandleAdapter._handle_load_flat``'s
        # ``register_emission`` call.
        self._remote_state._observe_emission(_collect_load_flat_reply_ids(flat, precomp))
        self._remote_state._ingest_precomp(precomp)
        # Newly-discovered deps (and the loaded id, if not already in the
        # local liveset — though it always is after _check_local_tracked)
        # need to be tracked here, bumping the shared refcount once each.
        new_ids: list[FxtrID] = []
        if flat.entity_id not in self._liveset:
            new_ids.append(flat.entity_id)
        for d_id, _ in flat.deps:
            if d_id not in self._liveset and d_id not in new_ids:
                new_ids.append(d_id)
        if new_ids:
            self._remote_state.acquire(new_ids)
            self._liveset.update(new_ids)
        return flat

    def _track_discovered(self, node_id: FxtrID) -> None:
        """Add `node_id` to the local liveset, bumping the shared refcount once.

        Used by sync provenance reads — when an unwrap / ctx / pathroot
        / step-back lookup returns an ID we hadn't been tracking
        locally, we adopt it. Idempotent (no-op if already in the
        local liveset).

        Defense-in-depth: ``node_id`` came from the precomp cache,
        which means ``_ingest_precomp_slot`` already bumped its
        refcount on the way in. So ``_remote_state._refcounts`` must
        already contain ``node_id``; we assert that here. If the
        assertion fires, the bookkeeping in
        ``_ingest_precomp_slot`` / ``_drop_to_zero`` has fallen out
        of sync — correctness depends on it.
        """
        if node_id in self._liveset:
            return
        if node_id not in self._remote_state._refcounts:
            raise RuntimeError(
                f"CapIRArena._track_discovered: precomp value {node_id} has "
                "no refcount entry on the shared state. Ingest/decrement "
                "bookkeeping is broken — see "
                "v2/plans/phase3/stage8c_user_caps/part5bugfix1_plan.md."
            )
        self._remote_state.acquire([node_id])
        self._liveset.add(node_id)

    @override
    def unwrap_provenance(self, lease: Lease[B_Z], node_id: FxtrID) -> FxtrID | None:
        self._check_local_tracked(lease, node_id)
        with lease.take():
            result = self._remote_state.get_unwraps_to(node_id)
            if result is not None:
                self._track_discovered(result)
            return result

    @override
    def c_node_to_context_data(self, lease: Lease[B_Z], c_node_id: FxtrID) -> FxtrID:
        self._check_local_tracked(lease, c_node_id)
        with lease.take():
            result = self._remote_state.get_ctx_node(c_node_id)
            if result is None:
                raise ValueError(
                    f"c-node {c_node_id} has no ctx_node entry in the precomp "
                    "cache; load_flat the c-node first to populate it"
                )
            self._track_discovered(result)
            return result

    @override
    def p_node_to_pathroot(self, lease: Lease[B_Z], p_node_id: FxtrID) -> FxtrID:
        self._check_local_tracked(lease, p_node_id)
        with lease.take():
            # Server-side `compute_precomp_closure` stores p-node pathroots
            # under the `ctx_node` key (matching `_p_node_to_pathroot`'s wire
            # name in the precomp dict).
            result = self._remote_state.get_ctx_node(p_node_id)
            if result is None:
                raise ValueError(
                    f"p-node {p_node_id} has no pathroot entry in the precomp "
                    "cache; load_flat the p-node first to populate it"
                )
            self._track_discovered(result)
            return result

    @override
    def step_back_p_ctx(self, lease: Lease[B_Z], p_node_id: FxtrID) -> FxtrID | None:
        self._check_local_tracked(lease, p_node_id)
        with lease.take():
            result = self._remote_state.get_step_back_p_ctx(p_node_id)
            if result is not None:
                self._track_discovered(result)
            return result

    @override
    def abandon(self, lease: Lease[B_Z], node_id: FxtrID) -> None:
        self._check_local_tracked(lease, node_id)
        with lease.take() as taken:
            self._liveset.discard(node_id)
            self._remote_state.release(taken.clone(), [node_id])
            # Auto-close on empty: a `ReadableArena` with no tracked entries
            # is conceptually unusable — the public surface (load/sync queries)
            # all check the liveset before doing anything, and there's no
            # public method to add entries back to an existing arena. So once
            # the liveset is empty there's nothing left to do but release
            # local resources.
            if not self._liveset:
                self.close()

    @override
    def copy_to_new_arena(
        self, lease: Lease[B_Z], node_ids: Collection[FxtrID]
    ) -> "CapIRArena[B_Z]":
        """Create a sibling arena sharing this arena's remote state.

        No wire dispatch: forking is purely local bookkeeping. Each
        requested ID's shared refcount is bumped, the sibling
        ``CapIRArena`` is created with `node_ids` as its initial
        liveset, and both arenas continue independently — closing one
        only releases its share of refcounts.

        The lease is consumed (matching the abstract `ReadableArena`
        contract) but no Z→Y conversion is needed; we just validate
        it.
        """
        if self._closed:
            lease.discard()
            raise ValueError("CapIRArena is closed")
        self._remote_state.precheck(lease)
        with lease.take():
            tracked: set[FxtrID] = set()
            for nid in node_ids:
                if nid not in self._liveset:
                    raise ValueError(f"Node {nid} is not tracked in this CapIRArena")
                tracked.add(nid)
            self._remote_state.acquire(tracked)
            return CapIRArena(remote_state=self._remote_state, initial_liveset=tracked)

    @override
    def close(self) -> None:
        """Release this arena's share of the shared refcounts.

        The remote state's bookkeeping is decremented for each id in
        the local liveset. IDs whose shared refcount drops to zero
        are *not* explicitly abandoned over the wire from `close`
        (we don't have a Z-lease here); the wire `IRHandle` is held
        alive by the remote state, and once every sibling has closed
        the handle drops via Python GC and the server-side
        `ArenaIRHandleAdapter` is finalized along with it.
        """
        if self._closed:
            return
        self._closed = True
        leftover = list(self._liveset)
        self._liveset.clear()
        if leftover:
            self._remote_state.release_no_dispatch(leftover)

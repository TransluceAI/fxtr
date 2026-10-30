"""Substitution walker for the agent event log.

Walks a wire-form workstation payload (a ``FxtrServiceData`` tree
with entity / service / callback refs encoded as bracketed
two-element forms — ``["entity", short_id]`` etc.) and produces a
:data:`fxtr2.data.entity_core.FrozenFxtrCbor` tree with typed
:class:`EntityRef` / placeholder-entity leaves.

This is the host-side analogue of
``fxtr_sandbox_rpc_adapter/_provenance.py``'s walker: same fan-out
shape, same dispatch on Mapping / Sequence children. Differences:

- Input is wire-form (bracketed shapes) rather than the in-Python
  ``FxtrServiceData`` shape.
- Output substitutes a service short_id leaf with an
  :class:`EntityRef` to a freshly-stored
  :class:`WorkstationServicePlaceholder` (capturing the service's
  ``$fxtr.type`` + ``$fxtr.ctx``).
- Output substitutes a callback ``["callback", N]`` leaf with an
  :class:`EntityRef` to an :class:`OwnedCallbackPlaceholder`
  (capturing ``slot_n`` + the reverse-looked-up ``slot_key``).
- Output substitutes an entity ``["entity", short_id]`` leaf with a
  :class:`RawWeakFxtrID` after resolving the workstation FxtrID via
  ``workstation_proxy.get_entity``.

Why ``RawWeakFxtrID`` for entity refs instead of an :class:`EntityRef`
wrapper: the field that holds the walked output is typed
:data:`FrozenFxtrCbor`, which the cbor encoder consumes directly. At
the FxtrCbor layer, a raw-id wrapper is the leaf shape; mashumaro
passes it through unchanged. Going through ``EntityRef`` would
double-wrap.

Service and callback placeholders are stored entities in their own
right (each has a ``$fxtr.type``), so they ride as
``RawStrongFxtrID`` leaves — strong link because the placeholder is
logically owned by the log entry whose payload references it.

The walker is async because:

- Entity short_ids need a workstation RPC round-trip
  (``workstation_proxy.get_entity``) to map to a FxtrID reachable in
  the destination ``scope``.
- Service short_ids need *two* workstation RPC round-trips per
  unique short_id (``$fxtr.type`` + ``$fxtr.ctx``). These two run in
  parallel; the placeholder cache (keyed by short_id) deduplicates
  identical service refs across the payload.

Caller contract: the walker takes an explicit ``scope`` argument
where every resolved entity FxtrID must be loadable and where each
stored placeholder (service / callback) is materialized. ``scope``
must be mountable — the walker opens transient child scopes for
each workstation lookup. This follows v2's scope discipline (see
``v2/notes/scope_discipline.md``) — no implicit active-scope reads,
no implicit stores. Callers (the log builder) pass the impl scope
owned by the :class:`AgentMcpRunningSessionImpl`.

The walker takes a ``ServiceRef[WorkstationServiceProto]`` rather
than a pre-resolved proxy. Each entity / service lookup opens a
short-lived ``FxtrScope(isolated=True,
include=[self._scope])``, re-resolves the workstation_ref in that
temp scope, performs the lookup, and copies the resulting FxtrID
(or stored placeholder) back into ``self._scope`` via
``copy_from_scope`` before the temp scope drains. The benefits:

- The workstation proxy and the lifted service / entity
  registrations don't accumulate on the long-lived impl scope —
  their lifetime is bounded to a single lookup.
- The walker doesn't depend on the active scope being a mountable
  parent of its walk scope. Concretely, the log writer task is
  started via ``impl_tg.start_soon`` and runs with active scope =
  :data:`GLOBAL_SCOPE`; the walker's ``svc_ref.resolve(scope=temp)``
  uses the explicit ``temp`` scope rather than the active scope
  default, so the resolve doesn't try to mount against
  ``GLOBAL_SCOPE``.

Entry point: :func:`walk_payload`.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping
from typing import Any

import anyio
from frozendict import frozendict
from fxtr2.data.entity_core import (
    FrozenFxtrCbor,
    FxtrID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr2.services.service_cap_common import UnknownService
from fxtr_workstation.protocols import WorkstationServiceProto

from .agent_log_types import (
    OwnedCallbackPlaceholder,
    WorkstationServicePlaceholder,
)

_log = logging.getLogger(__name__)

#: Wire-form tags this walker recognizes inside a 2-element list.
_TAG_ENTITY = "entity"
_TAG_SERVICE = "service"
_TAG_CALLBACK = "callback"
_TAG_BYTES = "bytes"

#: Sentinel ``slot_key`` for owned callbacks whose slot has already
#: been closed and forgotten by the time we walk a payload referencing
#: them. Pathological — the walker logs a warning when it hits this.
_UNKNOWN_SLOT_KEY = "<unknown>"


class WalkerContext:
    """Per-walk-tree state.

    Holds caches keyed by workstation short_id so that the same entity
    / service appearing multiple times in a payload only triggers one
    RPC round-trip and produces one stored placeholder entity.

    A fresh :class:`WalkerContext` is constructed per top-level
    :func:`walk_payload` call. The caller passes an explicit
    ``scope`` (the destination for stored placeholder entities and
    the loadability domain for resolved entity FxtrIDs) and a
    ``ServiceRef[WorkstationServiceProto]`` (re-resolved into a
    transient child scope for each lookup). See the module
    docstring's *Caller contract* section.

    ``scope`` must be mountable — the walker opens transient
    ``include=[self._scope]`` child scopes for each lookup.

    ``slot_key_for_slot_n`` is read on demand for owned-callback
    placeholders; the typical caller passes
    ``mirror.pending_slot_index.get`` (read function, not the dict
    directly, so the walker doesn't capture a stale snapshot).
    """

    def __init__(
        self,
        *,
        scope: FxtrScope,
        workstation_ref: ServiceRef[WorkstationServiceProto],
        slot_key_for_slot_n: "Any",
    ) -> None:
        self._scope = scope
        self._workstation_ref = workstation_ref
        self._slot_key_for_slot_n = slot_key_for_slot_n
        #: workstation entity short_id → resolved FxtrID (loadable in ``scope``).
        self._entity_cache: dict[str, FxtrID] = {}
        #: in-flight entity short_id lookups. First task creates the
        #: event + does the work + fires; later tasks wait + read the
        #: cache. Stops redundant RPCs under parallel walks.
        self._entity_in_flight: dict[str, anyio.Event] = {}
        #: workstation service short_id → stored placeholder entity id
        #: (the placeholder is stored in ``scope``).
        self._service_cache: dict[str, FxtrID] = {}
        self._service_in_flight: dict[str, anyio.Event] = {}
        #: workstation callback slot_n → stored placeholder entity id
        #: (the placeholder is stored in ``scope``).
        self._callback_cache: dict[int, FxtrID] = {}

    @property
    def scope(self) -> FxtrScope:
        return self._scope

    @property
    def workstation_ref(self) -> ServiceRef[WorkstationServiceProto]:
        return self._workstation_ref

    def prefill_entity_cache(self, mapping: dict[str, FxtrID]) -> None:
        """Pre-populate the entity short_id → FxtrID cache.

        Used when the caller has already resolved some short_ids in
        an earlier window where the workstation still recognized
        them — see
        ``WsConnection._prefill_entity_short_ids`` for the
        ``present_to_user`` lifetime story. Pre-filled entries
        short-circuit :meth:`resolve_entity_short_id`'s RPC.

        Caller is responsible for ensuring the FxtrIDs in
        ``mapping`` are reachable in ``self.scope`` (typically by
        having resolved them through a proxy whose dispatch scope
        is ``self.scope`` or an ancestor of it).
        """
        self._entity_cache.update(mapping)

    async def resolve_entity_short_id(self, short_id: str) -> FxtrID:
        """Resolve an entity ``short_id`` to a FxtrID reachable in
        ``self.scope``. Caches per walk so repeated references don't
        re-round-trip — even under parallel walks (the in-flight
        event coordinates concurrent first-misses).

        Opens a transient ``include=[self._scope]`` mountable child
        scope, re-resolves ``workstation_ref`` into it, performs
        ``get_entity``, then copies the returned FxtrID up into
        ``self._scope`` before draining the temp scope. The temp
        scope's lifetime is bounded to a single lookup so the lifted
        workstation registration doesn't dangle on ``self._scope``.
        """
        cached = self._entity_cache.get(short_id)
        if cached is not None:
            return cached
        in_flight = self._entity_in_flight.get(short_id)
        if in_flight is not None:
            await in_flight.wait()
            return self._entity_cache[short_id]
        # We're the first caller; do the work.
        event = anyio.Event()
        self._entity_in_flight[short_id] = event
        try:
            async with FxtrScope(
                isolated=True,
                include=[self._scope],
                name="log_walker_entity_lookup",
            ) as temp:
                workstation = await self._workstation_ref.resolve(scope=temp)
                fid = await workstation.get_entity(short_id)
                # ``get_entity`` returns a FxtrID chain-bridged into
                # ``temp`` (the proxy's home scope). Copy it into
                # ``self._scope`` so it stays tracked after ``temp``
                # drains.
                await self._scope.copy_from_scope(fid, source_scope=temp)
            self._entity_cache[short_id] = fid
            return fid
        finally:
            event.set()
            # Leave the event in the dict — subsequent waiters
            # short-circuit through the cache check anyway, but a
            # stragglier could still consult it on the slow path.

    async def stored_service_placeholder(self, short_id: str) -> FxtrID:
        """Build (or look up) a stored :class:`WorkstationServicePlaceholder`
        for ``short_id``. Caches per walk so identical service refs in
        the same payload collapse to one RPC pair + one stored entity,
        even under parallel walks.

        Performs two parallel RPC round-trips on the first call:
        ``workstation.get_service(short_id)`` → untyped proxy →
        invoke ``$fxtr.type`` + ``$fxtr.ctx`` simultaneously.

        Like :meth:`resolve_entity_short_id`, the entire RPC sequence
        runs inside a transient ``include=[self._scope]`` mountable
        child scope: ``workstation_ref`` is re-resolved there, the
        service ref is resolved there (so its bridge entry lives on
        the temp scope rather than ``self._scope``), the placeholder
        is stored there, and finally the placeholder's FxtrID is
        copied up into ``self._scope`` via ``copy_from_scope`` (which
        walks the entity DAG so the placeholder's ``service_type_id``
        + ``service_ctx`` FxtrID fields come along).
        """
        cached = self._service_cache.get(short_id)
        if cached is not None:
            return cached
        in_flight = self._service_in_flight.get(short_id)
        if in_flight is not None:
            await in_flight.wait()
            return self._service_cache[short_id]
        event = anyio.Event()
        self._service_in_flight[short_id] = event
        try:
            async with FxtrScope(
                isolated=True,
                include=[self._scope],
                name="log_walker_service_lookup",
            ) as temp:
                workstation = await self._workstation_ref.resolve(scope=temp)
                svc_ref = await workstation.get_service(short_id)
                svc_ref = svc_ref.replace_service_type(UnknownService)
                proxy = await svc_ref.resolve(scope=temp)

                type_id_slot: list[FxtrID | None] = [None]
                ctx_slot: list[FxtrID | None] = [None]

                async def _fetch_type() -> None:
                    type_id_slot[0] = await proxy.service_type_id()

                async def _fetch_ctx() -> None:
                    ctx = await getattr(proxy, "$fxtr.ctx")()
                    assert isinstance(
                        ctx, FxtrID
                    ), f"$fxtr.ctx returned non-FxtrID {ctx!r} on service {short_id!r}"
                    ctx_slot[0] = ctx

                async with anyio.create_task_group() as tg:
                    tg.start_soon(_fetch_type)
                    tg.start_soon(_fetch_ctx)

                type_id = type_id_slot[0]
                service_ctx = ctx_slot[0]
                assert type_id is not None and service_ctx is not None

                placeholder = WorkstationServicePlaceholder(
                    short_id=short_id,
                    service_type_id=type_id,
                    service_ctx=service_ctx,
                )
                # Store in ``temp`` (where ``type_id`` / ``ctx_id``
                # are tracked, since they rode the proxy-return path
                # into ``temp``), then copy the placeholder's
                # FxtrID up into ``self._scope`` — copy_from_scope
                # walks the entity DAG and pulls the type_id +
                # service_ctx FxtrIDs along with the placeholder.
                ref = placeholder.store(temp)
                await self._scope.copy_from_scope(ref.entity_id, source_scope=temp)
            self._service_cache[short_id] = ref.entity_id
            return ref.entity_id
        finally:
            event.set()

    def stored_callback_placeholder(self, slot_n: int) -> FxtrID:
        """Build (or look up) a stored :class:`OwnedCallbackPlaceholder`
        for ``slot_n``. Sync — no RPC needed; the slot_key is reverse-
        looked-up locally via the configured callable.
        """
        cached = self._callback_cache.get(slot_n)
        if cached is not None:
            return cached
        slot_key = self._slot_key_for_slot_n(slot_n)
        if slot_key is None:
            _log.warning(
                "log_walker: callback slot %d not in pending_slot_index; "
                "storing OwnedCallbackPlaceholder with slot_key=%r — "
                "this usually means the slot was closed and forgotten "
                "before the referencing payload was walked.",
                slot_n,
                _UNKNOWN_SLOT_KEY,
            )
            slot_key = _UNKNOWN_SLOT_KEY
        placeholder = OwnedCallbackPlaceholder(slot_n=slot_n, slot_key=slot_key)
        # Store explicitly into ``self._scope`` per scope discipline.
        # The placeholder has no entity-ref fields, so no reachability
        # rationale is needed beyond "we own this fresh entity."
        ref = placeholder.store(self._scope)
        self._callback_cache[slot_n] = ref.entity_id
        return ref.entity_id


async def walk_with_context(value: Any, ctx: WalkerContext) -> FrozenFxtrCbor:
    """Walk one wire-form value using a pre-built :class:`WalkerContext`.

    Useful when multiple top-level walks need to share the same
    cache (e.g. building a :class:`ScriptExecutionEvent` from
    several queued ops — duplicate refs across ops dedupe).
    Callers that don't need cross-walk sharing should use
    :func:`walk_payload` instead, which constructs a fresh context
    each call.
    """
    return await _walk(value, ctx)


async def walk_payload(
    value: Any,
    *,
    scope: FxtrScope,
    workstation_ref: ServiceRef[WorkstationServiceProto],
    slot_key_for_slot_n: "Any",
) -> FrozenFxtrCbor:
    """Walk a wire-form payload tree, substituting tagged leaves.

    Args:
        value: The wire-form value to walk. Must be one of:
            primitive scalar / None / bytes, a tagged 2-element list
            (``["entity", id]`` / ``["service", id]`` /
            ``["callback", N]`` / ``["bytes", b64]``), a 1-element
            literal-list-escape (``[[inner]]``), or a dict whose
            values follow the same shape (string keys).
        scope: Destination scope for stored placeholder entities
            and the loadability domain for resolved entity FxtrIDs.
            Must be mountable. See the module docstring's *Caller
            contract* section.
        workstation_ref: Workstation service ref for entity / service
            resolution. The walker re-resolves this into a transient
            ``include=[scope]`` child scope for each lookup, so the
            ref must be registered in ``scope`` or one of its
            parents.
        slot_key_for_slot_n: Callable ``(slot_n: int) -> str | None``
            returning the agent-facing slot_key for an owned-callback
            slot — typically ``mirror.pending_slot_index.get``.

    Returns:
        A :data:`FrozenFxtrCbor` tree: frozendicts / tuples /
        scalars / bytes / :class:`RawWeakFxtrID` (entity leaves) /
        :class:`RawStrongFxtrID` (service / callback placeholder
        leaves).

    Raises:
        ValueError: A list isn't a recognized 2-tagged or
            1-literal-list shape; a dict key isn't a string; an
            unrecognized wire tag; any structural mismatch with the
            wire-form grammar.
    """
    ctx = WalkerContext(
        scope=scope,
        workstation_ref=workstation_ref,
        slot_key_for_slot_n=slot_key_for_slot_n,
    )
    return await _walk(value, ctx)


async def _walk(value: Any, ctx: WalkerContext) -> FrozenFxtrCbor:
    """Body of :func:`walk_payload`. Recursive."""
    # Primitives / None / bytes — pass through.
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value

    # Mappings — recurse into values, build a frozendict. Children
    # processed in parallel (independent subtrees).
    if isinstance(value, Mapping):
        items = list(value.items())
        for k, _ in items:
            if not isinstance(k, str):
                raise ValueError(f"log_walker: dict keys must be str, got {type(k).__name__}")
        results_map: list[FrozenFxtrCbor] = [None] * len(items)
        async with anyio.create_task_group() as tg:
            for i, (_, v) in enumerate(items):
                tg.start_soon(_walk_into_slot, results_map, i, v, ctx)
        return frozendict({items[i][0]: results_map[i] for i in range(len(items))})

    # Lists — must be either a 2-element tagged form or a
    # 1-element literal-list-escape.
    if isinstance(value, list):
        return await _walk_list(value, ctx)

    raise ValueError(f"log_walker: unexpected leaf type {type(value).__name__}: {value!r}")


async def _walk_into_slot(
    results: list[FrozenFxtrCbor], idx: int, child: Any, ctx: WalkerContext
) -> None:
    """Walk ``child`` and write the result into ``results[idx]``.

    Defined at module level (not nested) so closures don't capture
    loop variables and ``tg.start_soon`` can pass positional args
    directly.
    """
    results[idx] = await _walk(child, ctx)


async def _walk_list(value: list[Any], ctx: WalkerContext) -> FrozenFxtrCbor:
    """Dispatch a wire-form list: 2-tagged form or literal-list-escape."""
    # 2-element tagged form: ["tag", inner]
    if len(value) == 2 and isinstance(value[0], str):
        tag, inner = value[0], value[1]
        if tag == _TAG_ENTITY:
            if not isinstance(inner, str):
                raise ValueError(f"log_walker: 'entity' tag payload must be str, got {inner!r}")
            fid = await ctx.resolve_entity_short_id(inner)
            return RawWeakFxtrID(inner_id=fid)
        if tag == _TAG_SERVICE:
            if not isinstance(inner, str):
                raise ValueError(f"log_walker: 'service' tag payload must be str, got {inner!r}")
            fid = await ctx.stored_service_placeholder(inner)
            return RawStrongFxtrID(inner_id=fid)
        if tag == _TAG_CALLBACK:
            if not isinstance(inner, int):
                raise ValueError(f"log_walker: 'callback' tag payload must be int, got {inner!r}")
            fid = ctx.stored_callback_placeholder(inner)
            return RawStrongFxtrID(inner_id=fid)
        if tag == _TAG_BYTES:
            if not isinstance(inner, str):
                raise ValueError(
                    f"log_walker: 'bytes' tag payload must be base64 str, got {inner!r}"
                )
            return base64.b64decode(inner)
        raise ValueError(f"log_walker: unrecognized wire tag {tag!r}")

    # 1-element literal-list-escape: [[inner_items...]]
    if len(value) == 1 and isinstance(value[0], list):
        inner_items: list[Any] = value[0]
        results: list[FrozenFxtrCbor] = [None] * len(inner_items)
        async with anyio.create_task_group() as tg:
            for i, item in enumerate(inner_items):
                tg.start_soon(_walk_into_slot, results, i, item, ctx)
        return tuple(results)

    raise ValueError(
        f"log_walker: malformed wire-escape list: {value!r} "
        "(must be a 2-element tagged form or a 1-element literal-list-escape)"
    )

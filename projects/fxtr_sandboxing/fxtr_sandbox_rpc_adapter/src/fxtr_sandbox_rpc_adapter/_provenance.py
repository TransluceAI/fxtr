"""Guest-side provenance setup for sandbox connect calls.

When the host invokes ``connect_with_bundle_provenance(config, bundle)``
on the guest's ``_GuestMainImpl``, the guest walks ``config`` to:

1. Replace every :class:`ServiceRef` leaf with a stored
   :class:`ServiceConfigReplacementEntity` capturing the service's
   ``$fxtr.type`` and ``$fxtr.ctx``. The result is a snapshot that
   preserves the structure of the original config but carries no
   live service handles — safe to embed in a content-addressed
   :class:`SandboxProvenanceContext` entity.

2. Reject any user-provided entity reference (FxtrID-shaped leaf in
   the wire-decoded ``FxtrServiceData`` tree) whose loaded type is
   :data:`SERVICE_CONFIG_REPLACEMENT_TYPE_ID`. This prevents users
   from smuggling a forged replacement into the snapshot at the
   top level. Wrapping a SCRE inside an outer DataclassEntity the
   user controls bypasses this — that counts as deliberate opt-in,
   not an escape we need to block.

The walker is async because the introspection calls (``$fxtr.ctx`` /
``$fxtr.type`` on each ServiceRef; ``load()`` on each entity-bearing
FxtrID) cross the RPC wire. Independent leaves fan out via
:func:`anyio.create_task_group` at each ``Mapping`` / ``Sequence``
node, and the two introspection calls inside
:func:`_replace_service_ref` run in parallel — so call-side latency
for a config with N refs scales with the deepest single-chain length
rather than with N. Replacements are stored in-flight and never
re-visited; the safety check applies only to user-provided refs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import anyio
from frozendict import frozendict
from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_core import FrozenFxtrCbor, FxtrID, RawStrongFxtrID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import ServiceRef
from fxtr_sandbox_types import SERVICE_CONFIG_REPLACEMENT_TYPE_ID, ServiceConfigReplacementEntity


class SandboxProvenanceEscapeError(Exception):
    """Raised when a user config contains an EntityRef pointing at a
    :class:`ServiceConfigReplacementEntity`.

    The host wraps this as a wire-level error before it crosses back
    to the caller, so external clients see a clean rejection.
    """


# Tags whose FxtrIDs represent stored entity data (vs. ground / builtin
# anchors). When the walker encounters one of these, it loads the
# entity to perform the SCRE escape-safety check; other tags ride
# through verbatim.
_ENTITY_TAGS = frozenset({"d", "n"})


async def walk_config_for_provenance(value: Any) -> FrozenFxtrCbor:
    """Walk ``value`` (a ``FxtrServiceData``-shaped tree) producing a
    ``FrozenFxtrCbor`` snapshot.

    For each leaf:

    - Primitive (``None | bool | int | float | str | bytes``):
      passthrough.
    - :class:`FxtrID`: emitted as ``RawWeakFxtrID``. If the id's tag
      indicates an entity (``d`` / ``n``), the entity is loaded as
      :class:`UnknownEntity` and the
      :class:`SandboxProvenanceEscapeError` check runs on its
      ``$fxtr.type``.
    - :class:`ServiceRef`: resolved as the untyped proxy
      (``UnknownServiceProxy`` — ``connect`` always arrives with the
      configs as the untyped form ``ServiceRef[UnknownService]``).
      ``$fxtr.ctx`` + ``$fxtr.type`` are fetched, a
      :class:`ServiceConfigReplacementEntity` is built + stored in
      the active scope, and a ``RawStrongFxtrID`` to the replacement
      ref is emitted. Strong link because the replacement is
      logically owned by the snapshot.
    - :class:`Mapping`: recurse into a ``frozendict``.
    - :class:`Sequence` (non-str, non-bytes): recurse into a ``tuple``.

    Returns a ``FrozenFxtrCbor``-typed value suitable for assignment
    to :attr:`SandboxProvenanceContext.config_snapshot`.

    Raises:
        SandboxProvenanceEscapeError: a user-provided FxtrID resolved
            to an entity whose stored ``$fxtr.type`` is
            :data:`SERVICE_CONFIG_REPLACEMENT_TYPE_ID`.
        ValueError: a leaf isn't a recognized ``FxtrServiceData``
            shape. Defensive — the typed dispatch already validated
            the wire form before the impl runs.
    """
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    if isinstance(value, FxtrID):
        if value.get_tag() in _ENTITY_TAGS:
            await _reject_replacement_smuggling(value)
        return RawWeakFxtrID(inner_id=value)
    if isinstance(value, EntityRef):
        # On the untyped FxtrServiceData wire path, EntityRefs decode
        # to bare FxtrIDs (target-type annotation is dropped). This
        # branch covers the defensive case where the caller hands the
        # walker an already-typed EntityRef — same treatment.
        fid = value.entity_id
        if fid.get_tag() in _ENTITY_TAGS:
            await _reject_replacement_smuggling(fid)
        return RawWeakFxtrID(inner_id=fid)
    if isinstance(value, ServiceRef):
        return await _replace_service_ref(value)
    if isinstance(value, Mapping):
        # Validate keys up-front so a malformed mapping fails fast
        # without spawning any work. (Children are processed in
        # parallel — we don't want to discover the bad key only
        # after some children have already raced.)
        items_map = list(value.items())
        for k, _ in items_map:
            if not isinstance(k, str):
                raise ValueError(f"config walk: dict keys must be str, got {type(k).__name__}")
        results_map: list[FrozenFxtrCbor] = [None] * len(items_map)  # type: ignore[list-item]
        async with anyio.create_task_group() as tg:
            for i, (_, v) in enumerate(items_map):
                tg.start_soon(_walk_into_slot, results_map, i, v)
        return frozendict({items_map[i][0]: results_map[i] for i in range(len(items_map))})
    if isinstance(value, Sequence):
        items_seq = list(value)
        results_seq: list[FrozenFxtrCbor] = [None] * len(items_seq)  # type: ignore[list-item]
        async with anyio.create_task_group() as tg:
            for i, item in enumerate(items_seq):
                tg.start_soon(_walk_into_slot, results_seq, i, item)
        return tuple(results_seq)
    raise ValueError(f"config walk: unexpected leaf type {type(value).__name__}: {value!r}")


async def _walk_into_slot(results: list[FrozenFxtrCbor], idx: int, child: Any) -> None:
    """Walk ``child`` and store the result at ``results[idx]``.

    Defined at module level (rather than as a nested function) so the
    closure doesn't capture the loop variables and so ``tg.start_soon``
    can pass positional args directly.
    """
    results[idx] = await walk_config_for_provenance(child)


async def _reject_replacement_smuggling(fid: FxtrID) -> None:
    """Load ``fid`` as :class:`UnknownEntity` and reject if its
    stored ``$fxtr.type`` is :data:`SERVICE_CONFIG_REPLACEMENT_TYPE_ID`.
    """
    ref: EntityRef[UnknownEntity] = EntityRef(entity_id=fid, target_type=UnknownEntity)
    loaded = await ref.load()
    raw = loaded.raw_cbor
    if not isinstance(raw, frozendict):
        # Defensive: every well-formed entity cbor is a mapping at the
        # top level. If it isn't, this isn't an entity we can reason
        # about — bail out without rejecting (we only block confirmed
        # SCRE refs).
        return
    type_marker = raw.get("$fxtr.type")
    if isinstance(type_marker, (RawWeakFxtrID, RawStrongFxtrID)):
        type_id = type_marker.inner_id
    else:
        # Entity has no $fxtr.type marker (unusual; benign for our
        # purposes — definitely not a SCRE).
        return
    if type_id == SERVICE_CONFIG_REPLACEMENT_TYPE_ID:
        raise SandboxProvenanceEscapeError(
            f"User config contains an EntityRef to a"
            f" ServiceConfigReplacementEntity ({fid}); rejecting before"
            " invoking the guest entrypoint. Wrap the entity inside"
            " another DataclassEntity you own if you really need to"
            " pass it through opaquely."
        )


async def _replace_service_ref(ref: ServiceRef[Any]) -> RawStrongFxtrID:
    """Resolve ``ref`` as an untyped proxy, fetch its ``$fxtr.type`` +
    ``$fxtr.ctx`` (in parallel), store a
    :class:`ServiceConfigReplacementEntity`,
    and return a ``RawStrongFxtrID`` to it.
    """
    proxy = await ref.resolve()
    # service_type_id wires through $fxtr.type via the
    # UnknownServiceProxy helper; this is the loadable form
    # bridged into the active scope's main arena. $fxtr.ctx isn't
    # surfaced as a named method on UnknownServiceProxy, so go
    # through the catch-all __getattr__ path. Both are independent
    # wire calls, so run them in parallel.
    type_id_slot: list[FxtrID | None] = [None]
    ctx_slot: list[FxtrID | None] = [None]

    async def _fetch_type_id() -> None:
        type_id_slot[0] = await proxy.service_type_id()

    async def _fetch_ctx() -> None:
        result = await getattr(proxy, "$fxtr.ctx")()
        assert isinstance(
            result, FxtrID
        ), f"$fxtr.ctx returned non-FxtrID {result!r} on service {ref!r}"
        ctx_slot[0] = result

    async with anyio.create_task_group() as tg:
        tg.start_soon(_fetch_type_id)
        tg.start_soon(_fetch_ctx)

    service_type_id = type_id_slot[0]
    service_ctx = ctx_slot[0]
    assert service_type_id is not None and service_ctx is not None

    scre = ServiceConfigReplacementEntity(
        service_type_id=service_type_id,
        service_ctx=service_ctx,
    )
    scre_ref = scre.store()
    return RawStrongFxtrID(inner_id=scre_ref.entity_id)

"""Provenanced-builder entrypoint for the fxtr_nix_runner E2E fixture.

Run by ``mkFxtrFnBuilder`` (``entry = "provenanced_builder:build"``, ``provenanced
= true``): construct a **genuinely-provenanced** entity from ``ctx.spec`` (stored
under a ``provenance_context`` so it carries a c-node) and pin it into the
preserve-mode output layer. No remote — a pure builder. The layer preserves the
entity's provenance (a ``[data, prov]`` pin), so reading it back through the
manager yields a layered ``(data, prov)`` id — the end-to-end provenanced-builder
path: nix scaffold → builderSpec.provenanced → writer → mount_pin embed.
"""

from __future__ import annotations

import dataclasses
import uuid

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.layered_file_store.nix_builder import BuilderContext
from fxtr2.data.system_entities import define_type

_NS = uuid.UUID("e7c4a9b2-1d36-4f80-9a55-2c8b0e7d4f13")
_GROUND = FxtrID.from_tag_and_payload(".", b"")


@dataclasses.dataclass(frozen=True)
class Greeting(DataclassEntity, fxtr_type=define_type(_NS, "prov_e2e_greeting")):
    text: str


def _make_c_node(scope: FxtrScope) -> FxtrID:
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=_GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=_GROUND),
        "ctx_key": "provenanced_builder_e2e",
    }
    return scope._main_arena.create_c_node(scope.clone_authorization(), data)


async def build(ctx: BuilderContext) -> None:
    # Store the entity under a provenance_context so it is genuinely layered
    # (a c-node trace), then pin it — preserve mode keeps it as a [data, prov].
    c_node = _make_c_node(ctx.scope)
    async with FxtrScope(
        provenance_context=c_node,
        provenance_context_from_scope=ctx.scope,
        include=[ctx.scope],
        name="provenanced_builder",
    ) as scope:
        ref = scope.store(Greeting(text=ctx.spec["text"]))
        await ctx.writer.pin_to_this_layer("pinned", ref.entity_id, scope)

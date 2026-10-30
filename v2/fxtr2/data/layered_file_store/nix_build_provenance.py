"""Nix-build provenance c-node entities + the minting helper.

When a layer builder runs in **provenanced** mode (the ``provenanced`` flag on
``mkFxtrFnBuilder`` / ``mkFxtrScriptBuilder``), ``nix_builder._run`` mints a
build-step c-node and opens the builder ``ctx.scope`` with it as the
``provenance_context`` — so every entity the entrypoint stores carries the build
as its provenance, with no per-script work. This module owns the c-node's d-node
payload + the minting helper.

The payload splits by nix build kind, because the *output store path* (``$out``)
is only a safe anchor for **input-addressed** builds:

- **input-addressed (pure)** — ``$out`` is computed from the derivation's inputs
  and known before the build runs, so embedding it in the output content is fine
  and deterministic. A receiver who has that path in their nix store can ask nix
  which derivation built the entity (``nix-store --query --deriver $out`` →
  the ``.drv``). :class:`NixInputAddressedBuildProvenance` carries ``$out`` plus
  an optional author-declared ``provenance_info``.
- **fixed-output (FOD)** — ``$out`` is derived from ``outputHash``, which *is* the
  content hash, so embedding ``$out`` in the content is an unsolvable fixpoint.
  There is no safe machine anchor, so the author **must** declare
  ``provenance_info`` up front. :class:`NixFixedOutputBuildProvenance` carries
  only that.

So ``$out`` is the machine anchor (input-addressed only) and ``provenance_info``
is the declared human supplement (everywhere). Both forms are byte-stable for
fixed inputs, so the minted c-node id is reproducible across rebuilds.

Mirrors how ``fxtr_sandboxing`` declares ``SandboxProvenanceContext`` and mints a
per-``connect`` c-node from it.
"""

from __future__ import annotations

import dataclasses
import uuid

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FrozenFxtrCbor, FxtrID, RawFxtrEntityMarkerID
from fxtr2.data.entity_ref_scope import FxtrScope

# Namespace for the nix-build provenance entity types.
NIX_BUILD_PROVENANCE_NS = uuid.UUID("dc95eed6-4385-4ba3-b0af-7e9b5ac994b3")


def _ground_fxtr_id() -> FxtrID:
    return FxtrID.from_tag_and_payload(".", b"")


def _ground_marker() -> RawFxtrEntityMarkerID:
    """An explicit ground ``$fxtr.ctx`` marker.

    Supplied at construction so ``_fxtr_convert_to_cbor`` doesn't fall back to the
    active scope's marker — the c-node is minted before the builder scope exists,
    so there may be no (or a wrong) active scope. Mirrors the explicit-marker
    trick in ``FxtrScope._get_or_mint_fallback_or_own_marker``.
    """
    return RawFxtrEntityMarkerID(inner_id=_ground_fxtr_id())


@dataclasses.dataclass(frozen=True)
class NixInputAddressedBuildProvenance(
    DataclassEntity,
    fxtr_type=system_entities.define_type(
        NIX_BUILD_PROVENANCE_NS, "nix_input_addressed_build_provenance"
    ),
):
    """Build-step c-node payload for an **input-addressed** (pure) layer build.

    ``output_store_path`` is the build's ``$out`` — the machine anchor a receiver
    walks back to the ``.drv``. ``provenance_info`` is the optional author-declared
    supplement (the nix ``provenanceInfo`` JSON, frozen). ``value`` is the c-node
    unwrap target — always ``"."`` ground, as ``create_c_node`` requires.
    """

    output_store_path: str
    provenance_info: FrozenFxtrCbor | None = None
    value: FxtrID = dataclasses.field(default_factory=_ground_fxtr_id)


@dataclasses.dataclass(frozen=True)
class NixFixedOutputBuildProvenance(
    DataclassEntity,
    fxtr_type=system_entities.define_type(
        NIX_BUILD_PROVENANCE_NS, "nix_fixed_output_build_provenance"
    ),
):
    """Build-step c-node payload for a **fixed-output** (FOD) layer build.

    A FOD has no safe ``$out`` anchor, so ``provenance_info`` (the nix
    ``provenanceInfo`` JSON, frozen) is **required** — the author declares the
    provenance up front. ``value`` is the c-node unwrap target — always ``"."``.
    """

    provenance_info: FrozenFxtrCbor
    value: FxtrID = dataclasses.field(default_factory=_ground_fxtr_id)

    class Config(DataclassEntity.Config):
        # c-node context payload: ``create_c_node`` requires ``value`` physically
        # present, but ``value`` is always the ground default — so opt out of the
        # inherited ``omit_default``.
        omit_default = False


def mint_build_provenance_c_node(
    scope: FxtrScope,
    *,
    output_store_path: str | None,
    provenance_info: FrozenFxtrCbor | None,
) -> FxtrID:
    """Build the right c-node payload for this build kind and mint a c-node in
    ``scope``, returning the bare c-tagged id.

    The build kind is selected by ``output_store_path``: a non-``None`` path means
    an **input-addressed** build (``$out`` is the safe anchor); ``None`` means a
    **fixed-output** build, which requires ``provenance_info`` (no anchor to fall
    back to).

    ``scope`` must be a bare scope (ground ``$fxtr.ctx`` marker) so the minted
    c-node's d-node body validates as a context node; the payload supplies its own
    explicit ground marker so the conversion never depends on the active scope.
    """
    entity: DataclassEntity
    if output_store_path is not None:
        entity = NixInputAddressedBuildProvenance(
            output_store_path=output_store_path,
            provenance_info=provenance_info,
            fxtr_ctx=_ground_marker(),
        )
    else:
        if provenance_info is None:
            raise ValueError(
                "a fixed-output (FOD) provenanced builder has no safe $out anchor; "
                "it must declare provenanceInfo"
            )
        entity = NixFixedOutputBuildProvenance(
            provenance_info=provenance_info,
            fxtr_ctx=_ground_marker(),
        )
    return scope.create_c_node(entity._fxtr_convert_to_cbor())

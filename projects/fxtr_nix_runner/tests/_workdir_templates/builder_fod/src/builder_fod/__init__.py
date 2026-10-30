"""FOD-builder entrypoint for the fxtr_nix_runner E2E fixture.

Run by ``mkFxtrFnBuilder`` (``entry = "builder_fod:build"``) inside a fixed-output
derivation: fetch the entity named by ``ctx.spec["entityId"]`` from the build
span's rehost and pin its bytes into the output layer, leaving any content deps
remote (precomp markers) — so reading the pinned entity back resolves those deps
through the manager's repo store. Exercises the builder's FOD path end-to-end
(``ctx.remote`` built from ``FXTR_REMOTE_*`` → writer ``mark_as_remote`` /
``preload``).
"""

from __future__ import annotations

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.layered_file_store.nix_builder import BuilderContext


async def build(ctx: BuilderContext) -> None:
    entity_id = FxtrID.from_id_string(ctx.spec["entityId"])
    await ctx.writer.mark_as_remote(entity_id)
    await ctx.writer.preload(entity_id)
    await ctx.writer.pin_to_this_layer("pinned", entity_id)
